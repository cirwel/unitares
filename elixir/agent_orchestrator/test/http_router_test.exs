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
  alias AgentOrchestrator.MemoryIdempotencyLedger
  alias AgentOrchestrator.SpawnGate

  @opts HTTPRouter.init([])
  @token "test-bearer-token"

  setup do
    MemoryIdempotencyLedger.clear()
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
        nil ->
          conn(method, path)

        map ->
          conn(method, path, Jason.encode!(map))
          |> put_req_header("content-type", "application/json")
      end

    put_req_header(conn, "authorization", "Bearer " <> @token)
  end

  defp body_json(conn), do: Jason.decode!(conn.resp_body)

  defp with_idempotency_key(conn, key) do
    put_req_header(conn, "idempotency-key", key)
  end

  defp unique_key do
    "http-spawn-test-" <> Integer.to_string(System.unique_integer([:positive]))
  end

  describe "auth" do
    test "503 when no bearer is configured (fail closed)" do
      Application.delete_env(:agent_orchestrator, :bearer_token)
      conn = call(conn(:get, "/v1/health"))
      assert conn.status == 503
      assert body_json(conn)["error"] == "service_unavailable"
    end

    test "401 when the bearer is missing or wrong" do
      assert call(conn(:get, "/v1/health")).status == 401

      assert call(conn(:get, "/v1/health") |> put_req_header("authorization", "Bearer nope")).status ==
               401
    end

    test "accepts a case-insensitive scheme" do
      conn =
        call(conn(:get, "/v1/health") |> put_req_header("authorization", "bearer " <> @token))

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

      assert body["idempotency"] == %{
               "available" => true,
               "backend" => "memory",
               "durable" => false
             }

      assert body["protocol_version"] == HTTPRouter.protocol_version()
    end
  end

  describe "GET /v1/metrics" do
    test "reports the lifecycle aggregate, behind the same bearer" do
      conn = call(authed(:get, "/v1/metrics"))
      assert conn.status == 200
      metrics = body_json(conn)["metrics"]
      assert is_integer(metrics["started"])
      assert is_integer(metrics["running"])
    end

    test "401 without the bearer — spawn counts are operational detail" do
      assert call(conn(:get, "/v1/metrics")).status == 401
    end
  end

  describe "POST /v1/agents" do
    test "spawns an agent and the result is awaitable" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", args: ["hi there"]}))
      assert conn.status == 201
      spawn = body_json(conn)
      execution_id = spawn["execution_id"]
      assert is_binary(execution_id)
      assert spawn["agent_id"] == execution_id
      assert spawn["idempotent"] == false

      await = call(authed(:post, "/v1/agents/#{execution_id}/await", %{timeout_ms: 5_000}))
      assert await.status == 200
      result = body_json(await)["result"]
      assert result["execution_id"] == execution_id
      assert result["agent_id"] == execution_id
      assert result["cmd"] == "echo"
      assert is_binary(result["started_at"])
      assert result["exit_status"] == 0
      assert result["output"] == ["hi there"]
      assert result["running"] == false
    end

    test "422 when cmd is missing" do
      conn = call(authed(:post, "/v1/agents", %{args: ["x"]}))
      assert conn.status == 422
      assert body_json(conn)["error"] == "schema_invalid"
    end

    test "same Idempotency-Key and spec replay one execution" do
      key = unique_key()
      spec = %{cmd: "sh", args: ["-c", "sleep 5"]}

      first =
        authed(:post, "/v1/agents", spec)
        |> with_idempotency_key(key)
        |> call()

      replay =
        authed(:post, "/v1/agents", spec)
        |> with_idempotency_key(key)
        |> call()

      assert first.status == 201
      assert replay.status == 200
      assert body_json(first)["idempotent"] == false
      assert body_json(replay)["idempotent"] == true
      assert body_json(replay)["execution_id"] == body_json(first)["execution_id"]
    end

    test "same Idempotency-Key with a different spec returns 409" do
      key = unique_key()

      first =
        authed(:post, "/v1/agents", %{cmd: "sh", args: ["-c", "sleep 5"]})
        |> with_idempotency_key(key)
        |> call()

      conflict =
        authed(:post, "/v1/agents", %{cmd: "sh", args: ["-c", "sleep 6"]})
        |> with_idempotency_key(key)
        |> call()

      assert first.status == 201
      assert conflict.status == 409
      assert body_json(conflict)["error"] == "idempotency_conflict"
    end

    test "a crash-ambiguous reservation returns 409 without spawning" do
      key = unique_key()
      execution_id = "ex-11111111-1111-4111-8111-111111111111"
      spec = %{cmd: "true", args: [], env: []}

      digest =
        spec
        |> :erlang.term_to_binary([:deterministic])
        |> then(&:crypto.hash(:sha256, &1))
        |> Base.encode16(case: :lower)

      assert {:ok, :reserved} =
               MemoryIdempotencyLedger.reserve(
                 SpawnGate.hash_key(key),
                 digest,
                 execution_id,
                 60_000
               )

      before_count = AgentOrchestrator.count()

      conn =
        authed(:post, "/v1/agents", %{cmd: "true"})
        |> with_idempotency_key(key)
        |> call()

      assert conn.status == 409
      assert body_json(conn)["error"] == "idempotency_outcome_unknown"
      assert body_json(conn)["execution_id"] == execution_id
      assert AgentOrchestrator.count() == before_count
    end

    test "a failed spawn does not poison its Idempotency-Key" do
      key = unique_key()

      failed =
        authed(:post, "/v1/agents", %{cmd: "definitely-not-a-real-binary-xyz"})
        |> with_idempotency_key(key)
        |> call()

      retried =
        authed(:post, "/v1/agents", %{cmd: "true"})
        |> with_idempotency_key(key)
        |> call()

      assert failed.status == 422
      assert retried.status == 201
    end

    test "rejects an invalid Idempotency-Key" do
      conn =
        authed(:post, "/v1/agents", %{cmd: "true"})
        |> with_idempotency_key(String.duplicate("x", 201))
        |> call()

      assert conn.status == 422
      assert body_json(conn)["error"] == "schema_invalid"
    end

    test "422 when args is not a list of strings" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", args: [1, 2]}))
      assert conn.status == 422
    end

    test "422 when stdin is not a recognised disposition" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", stdin: "inherit"}))
      assert conn.status == 422
      assert body_json(conn)["error"] == "schema_invalid"
    end

    test "accepts both stdin dispositions" do
      for disposition <- ["close", "pipe"] do
        conn = call(authed(:post, "/v1/agents", %{cmd: "true", stdin: disposition}))
        assert conn.status == 201
      end
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

    test "415 on an unsupported media type" do
      conn =
        conn(:post, "/v1/agents", "cmd=echo")
        |> put_req_header("content-type", "text/plain")
        |> put_req_header("authorization", "Bearer " <> @token)
        |> call()

      assert conn.status == 415
      assert body_json(conn)["error"] == "unsupported_media_type"
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
        call(
          authed(:post, "/v1/agents", %{cmd: "echo", lineage: %{parent_agent_uuid: "not-a-uuid"}})
        )

      assert conn.status == 422
      assert body_json(conn)["detail"] =~ "invalid lineage"
    end

    test "client_session_id threads through to the child env (thread-anchor resume)" do
      conn =
        call(
          authed(:post, "/v1/agents", %{
            cmd: "sh",
            args: ["-c", "echo $UNITARES_CLIENT_SESSION_ID"],
            client_session_id: "agent:/thread-discord-7"
          })
        )

      assert conn.status == 201
      agent_id = body_json(conn)["agent_id"]

      await = call(authed(:post, "/v1/agents/#{agent_id}/await", %{timeout_ms: 5_000}))
      assert await.status == 200
      assert body_json(await)["result"]["output"] == ["agent:/thread-discord-7"]
    end

    test "422 on a blank client_session_id (runner refuses the spawn)" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", client_session_id: "  "}))
      assert conn.status == 422
      assert body_json(conn)["detail"] =~ "invalid client_session_id"
    end

    test "422 on a non-string client_session_id" do
      conn = call(authed(:post, "/v1/agents", %{cmd: "echo", client_session_id: 123}))
      assert conn.status == 422
      assert body_json(conn)["detail"] =~ "client_session_id must be a string"
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

  describe "execution-scoped lifecycle routes" do
    test "list, snapshot, await, and stop use the immutable execution id" do
      spawn = call(authed(:post, "/v1/agents", %{cmd: "sh", args: ["-c", "sleep 5"]}))
      execution_id = body_json(spawn)["execution_id"]

      listed = call(authed(:get, "/v1/executions"))
      assert listed.status == 200
      listed_body = body_json(listed)
      assert execution_id in listed_body["executions"]

      details = Enum.find(listed_body["execution_details"], &(&1["execution_id"] == execution_id))
      assert details["agent_id"] == execution_id
      assert details["cmd"] == "sh"
      assert is_binary(details["started_at"])
      assert details["running"] == true
      refute Map.has_key?(details, "output")

      snapshot = call(authed(:get, "/v1/executions/#{execution_id}"))
      assert snapshot.status == 200
      assert body_json(snapshot)["result"]["execution_id"] == execution_id
      assert body_json(snapshot)["result"]["running"] == true

      await =
        call(
          authed(:post, "/v1/executions/#{execution_id}/await", %{
            timeout_ms: 50
          })
        )

      assert await.status == 504
      assert body_json(await)["execution_id"] == execution_id

      stop = call(authed(:delete, "/v1/executions/#{execution_id}"))
      assert stop.status == 200
      assert body_json(stop)["execution_id"] == execution_id
    end
  end

  describe "snapshot / stop / unknown" do
    test "404 snapshot for an unknown id" do
      assert call(authed(:get, "/v1/agents/ag-nope")).status == 404
      assert call(authed(:get, "/v1/executions/ex-nope")).status == 404
    end

    test "404 stop for an unknown id" do
      assert call(authed(:delete, "/v1/agents/ag-nope")).status == 404
      assert call(authed(:delete, "/v1/executions/ex-nope")).status == 404
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
