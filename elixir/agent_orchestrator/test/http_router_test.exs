defmodule AgentOrchestrator.HTTPRouterTest do
  @moduledoc """
  Exercises the control surface in-process via Plug.Test — no Bandit socket, no
  live lease plane. The orchestrator supervision tree (Registry / ResultStore /
  AgentSupervisor) is up because the app starts it; only the HTTP listener is
  off under :test (config_env() != :test).
  """
  use ExUnit.Case, async: false

  import Plug.Test
  import Plug.Conn

  alias AgentOrchestrator.HTTPRouter

  @opts HTTPRouter.init([])
  @token "test-bearer-token"

  setup do
    Application.put_env(:agent_orchestrator, :bearer_token, @token)
    # Null the lease bearer so default-on presence is a deterministic no-network
    # :no_bearer fast-fail (→ presence :unregistered) rather than hitting a plane.
    Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)
    Application.delete_env(:agent_orchestrator, :cmd_allowlist)

    on_exit(fn ->
      Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
      Application.delete_env(:agent_orchestrator, :cmd_allowlist)
    end)

    :ok
  end

  defp call(conn), do: HTTPRouter.call(conn, @opts)

  defp authed(method, path, body \\ nil) do
    conn =
      case body do
        nil -> conn(method, path)
        map -> conn(method, path, Jason.encode!(map)) |> put_req_header("content-type", "application/json")
      end

    put_req_header(conn, "authorization", "Bearer " <> @token)
  end

  defp body_json(conn), do: Jason.decode!(conn.resp_body)

  describe "auth" do
    test "503 when no bearer is configured (fail closed)" do
      Application.delete_env(:agent_orchestrator, :bearer_token)
      conn = call(conn(:get, "/v1/health"))
      assert conn.status == 503
      assert body_json(conn)["error"] == "service_unavailable"
    end

    test "401 when the bearer is missing or wrong" do
      assert call(conn(:get, "/v1/health")).status == 401
      assert call(conn(:get, "/v1/health") |> put_req_header("authorization", "Bearer nope")).status == 401
    end

    test "accepts a case-insensitive scheme" do
      conn = call(conn(:get, "/v1/health") |> put_req_header("authorization", "bearer " <> @token))
      assert conn.status == 200
    end
  end

  describe "GET /v1/health" do
    test "reports ok + a live-agent count + protocol version" do
      conn = call(authed(:get, "/v1/health"))
      assert conn.status == 200
      body = body_json(conn)
      assert body["ok"] == true
      assert is_integer(body["active_agents"])
      assert body["protocol_version"] == HTTPRouter.protocol_version()
    end
  end

  describe "POST /v1/agents" do
    test "spawns an agent and the result is awaitable" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", args: ["hi there"]}))
      assert conn.status == 201
      agent_id = body_json(conn)["agent_id"]
      assert is_binary(agent_id)

      await = call(authed(:post, "/v1/agents/#{agent_id}/await", %{timeout_ms: 5_000}))
      assert await.status == 200
      result = body_json(await)["result"]
      assert result["exit_status"] == 0
      assert result["output"] == ["hi there"]
      assert result["running"] == false
    end

    test "422 when cmd is missing" do
      conn = call(authed(:post, "/v1/agents", %{args: ["x"]}))
      assert conn.status == 422
      assert body_json(conn)["error"] == "schema_invalid"
    end

    test "422 when args is not a list of strings" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", args: [1, 2]}))
      assert conn.status == 422
    end

    test "422 on a malformed JSON body" do
      conn =
        conn(:post, "/v1/agents", "{not json")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer " <> @token)
        |> call()

      assert conn.status == 422
      assert body_json(conn)["error"] == "schema_invalid"
    end

    test "422 when the executable does not exist" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "definitely-not-a-real-binary-xyz"}))
      assert conn.status == 422
      assert body_json(conn)["detail"] =~ "executable not found"
    end

    test "403 when cmd is outside the allowlist" do
      Application.put_env(:agent_orchestrator, :cmd_allowlist, ["echo"])
      conn = call(authed(:post, "/v1/agents", %{cmd: "sh", args: ["-c", "true"]}))
      assert conn.status == 403
      assert body_json(conn)["error"] == "permission_denied"
    end

    test "422 on a malformed lineage parent uuid (runner refuses the spawn)" do
      conn =
        call(authed(:post, "/v1/agents", %{cmd: "echo", lineage: %{parent_agent_uuid: "not-a-uuid"}}))

      assert conn.status == 422
      assert body_json(conn)["detail"] =~ "invalid lineage"
    end
  end

  describe "GET /v1/agents" do
    test "lists live agent ids" do
      conn = call(authed(:get, "/v1/agents"))
      assert conn.status == 200
      body = body_json(conn)
      assert is_list(body["agents"])
      assert body["count"] == length(body["agents"])
    end
  end

  describe "snapshot / stop / unknown" do
    test "404 snapshot for an unknown id" do
      assert call(authed(:get, "/v1/agents/ag-nope")).status == 404
    end

    test "404 stop for an unknown id" do
      assert call(authed(:delete, "/v1/agents/ag-nope")).status == 404
    end

    test "404 on an unknown route" do
      assert call(authed(:get, "/v1/bogus")).status == 404
    end
  end

  describe "POST /v1/agents/:id/await timeout" do
    test "504 when a long-running agent outlives the await deadline" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "sh", args: ["-c", "sleep 5"]}))
      agent_id = body_json(conn)["agent_id"]

      await = call(authed(:post, "/v1/agents/#{agent_id}/await", %{timeout_ms: 50}))
      assert await.status == 504
      assert body_json(await)["error"] == "await_timeout"

      # Clean up the still-running agent.
      assert call(authed(:delete, "/v1/agents/#{agent_id}")).status == 200
    end
  end
end
