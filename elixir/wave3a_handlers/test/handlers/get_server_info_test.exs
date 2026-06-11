defmodule Wave3aHandlers.Handlers.GetServerInfoTest do
  @moduledoc """
  ExUnit coverage for the second BEAM-side handler (RFC §5 PR #6).

  Covers:

    * Mocked ProbeClient.server_info/0 returns a fixture envelope → the
      handler passes the `data` payload through verbatim (success path).
    * The probe envelope's `meta.probe_process` annotation is NOT
      forwarded — §2.6 parity requires the body to match the Python
      handler's success_response payload, which has no `meta` key.
    * Probe failure atoms map to the shared typed envelopes
      (`Wave3aHandlers.Handlers.ProbeErrors`) — same literals PR #5
      pinned for `health_check`.
    * HTTP router dispatch — POST /v1/handlers/get_server_info with
      mocked probe goes end-to-end via Plug.Test and returns the §2.2
      envelope shape (and no longer the PR #5-era 501).

  Mocking strategy mirrors `health_check_test.exs`: `meck` with
  `:passthrough` stubs `ProbeClient.server_info/0` directly.
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.Handlers.GetServerInfo
  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  # Fixture mirroring the Python probe's `/v1/probe/server_info` envelope.
  # The `data` payload carries the exact key set built by
  # `src/mcp_handlers/admin/handlers.py::build_server_info_payload` — the
  # single-sourced builder shared by the MCP handler and the probe. The
  # Python parity test pins the same key set against the golden fixture at
  # `tests/fixtures/wave3a_response_golden/get_server_info.json`.
  #
  # FIND-R3 / Q2: `current_pid` / `is_current` / `transport` describe the
  # Python backend process — accepted semantics, not a bug.
  @server_info_data %{
    "transport" => "HTTP",
    "server_version" => "0.42.0",
    "version" => "0.42.0",
    "build_date" => "2026-06-01",
    "tool_count" => 100,
    "current_pid" => 12_345,
    "current_uptime_seconds" => 5_400,
    "current_uptime_formatted" => "1h 30m",
    "total_server_processes" => 1,
    "server_processes" => [
      %{
        "pid" => 12_345,
        "is_current" => true,
        "uptime_seconds" => 5_400,
        "uptime_formatted" => "1h 30m",
        "status" => "running"
      }
    ],
    "pid_file_exists" => true,
    "pid_file" => "/repo/data/.mcp_server.pid",
    "max_keep_processes" => 3,
    "health" => "healthy"
  }

  @probe_envelope %{
    "ok" => true,
    "protocol_version" => "wave3a.v1",
    "data" => @server_info_data,
    "meta" => %{"probe_process" => true}
  }

  setup do
    Application.put_env(:wave3a_handlers, :beam_token, @bearer)
    on_exit(fn -> :meck.unload() end)
    :ok
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")
  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  defp stub_probe_ok(envelope \\ @probe_envelope) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])
    :meck.expect(Wave3aHandlers.ProbeClient, :server_info, fn -> {:ok, envelope} end)
  end

  defp stub_probe_error(reason) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])
    :meck.expect(Wave3aHandlers.ProbeClient, :server_info, fn -> {:error, reason} end)
  end

  describe "GetServerInfo.call/1 success path" do
    test "passes the probe data payload through verbatim" do
      stub_probe_ok()

      assert {:ok, body, 200} = GetServerInfo.call(%{})
      assert body == @server_info_data
    end

    test "does not forward the probe envelope wrapper or meta annotation" do
      stub_probe_ok()

      assert {:ok, body, 200} = GetServerInfo.call(%{})
      # If the handler returned the entire envelope, `ok` /
      # `protocol_version` / `meta` would leak into the body. §2.6 parity:
      # the Python handler's payload has none of these keys.
      refute Map.has_key?(body, "ok")
      refute Map.has_key?(body, "protocol_version")
      refute Map.has_key?(body, "data")
      refute Map.has_key?(body, "meta")
    end

    test "arguments are accepted and ignored (mirrors the Python handler)" do
      stub_probe_ok()

      assert {:ok, body_bare, 200} = GetServerInfo.call(%{})
      assert {:ok, body_args, 200} = GetServerInfo.call(%{"anything" => "ignored"})
      assert body_bare == body_args
    end

    test "two calls in a row produce identical responses (idempotent)" do
      stub_probe_ok()

      {:ok, body_a, status_a} = GetServerInfo.call(%{})
      {:ok, body_b, status_b} = GetServerInfo.call(%{})

      assert status_a == status_b
      assert body_a == body_b
    end
  end

  describe "GetServerInfo.call/1 error paths (shared ProbeErrors mapping)" do
    test "ProbeClient.timeout → 504 with stable error tag" do
      stub_probe_error(:timeout)

      assert {:ok, body, 504} = GetServerInfo.call(%{})
      assert body[:error] == "probe_timeout"
      assert body[:reason] == "Python probe exceeded 500ms budget"
    end

    test "ProbeClient.probe_token_unset → 503" do
      stub_probe_error(:probe_token_unset)

      assert {:ok, body, 503} = GetServerInfo.call(%{})
      assert body[:error] == "service_unavailable"
    end

    test "ProbeClient.connect_error → 502" do
      stub_probe_error(:connect_error)

      assert {:ok, body, 502} = GetServerInfo.call(%{})
      assert body[:error] == "probe_unavailable"
    end

    test "ProbeClient.{:non_200, status} surfaces the upstream status code" do
      stub_probe_error({:non_200, 503})

      assert {:ok, body, 502} = GetServerInfo.call(%{})
      assert body[:error] == "probe_non_200"
      assert body[:probe_status] == 503
    end

    test "ProbeClient.decode_error → 502" do
      stub_probe_error(:decode_error)

      assert {:ok, body, 502} = GetServerInfo.call(%{})
      assert body[:error] == "probe_decode_error"
    end

    test "ProbeClient.envelope_invalid → 502" do
      stub_probe_error(:envelope_invalid)

      assert {:ok, body, 502} = GetServerInfo.call(%{})
      assert body[:error] == "probe_envelope_invalid"
    end
  end

  describe "HTTP router dispatch" do
    test "POST /v1/handlers/get_server_info returns the §2.2 success envelope" do
      stub_probe_ok()

      resp =
        :post
        |> conn("/v1/handlers/get_server_info", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      body = parsed(resp)

      assert body["ok"] == true
      assert body["protocol_version"] == "wave3a.v1"
      # Payload keys are flat on the envelope — never nested under `data`.
      refute Map.has_key?(body, "data")
      assert body["transport"] == "HTTP"
      assert body["tool_count"] == 100
      assert body["health"] == "healthy"
      assert body["pid_file_exists"] == true
    end

    test "probe failure surfaces as ok: false at the probe-mapped status" do
      stub_probe_error(:connect_error)

      resp =
        :post
        |> conn("/v1/handlers/get_server_info", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 502
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "probe_unavailable"
    end
  end
end
