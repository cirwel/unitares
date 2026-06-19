defmodule Wave3aHandlers.Handlers.HealthCheckTest do
  @moduledoc """
  ExUnit coverage for the first BEAM-side handler (RFC §5 PR #5).

  Covers (per the PR #5 brief):

    * Mocked ProbeClient.health_snapshot/0 returns a fixture envelope →
      assert response envelope shape + content (success path).
    * 500ms timeout discipline — when ProbeClient is mocked to sleep
      longer than 500ms, the handler returns a timeout envelope (mapped
      via the ProbeClient's `{:error, :timeout}` atom).
    * Idempotent shape — two calls in a row produce structurally identical
      responses modulo volatile fields (timestamps, counters), which the
      Python probe's `mask_timestamps` helper normalizes on parity tests.
    * Lite-filter fidelity — `lite=true` default strips per-check detail;
      `lite=false` passes the snapshot through verbatim. Mirrors
      `src/mcp_handlers/admin/handlers.py:316-353`.
    * HTTP router dispatch — POST /v1/handlers/health_check with mocked
      probe goes end-to-end via Plug.Test, returns the §2.2 envelope shape.

  Mocking strategy: `Application.put_env(:wave3a_handlers, :probe_token,
  nil)` toggles ProbeClient to return `{:error, :probe_token_unset}`,
  which lets us drive the error path without spinning a real HTTP server.
  Success-path tests use `meck` to stub `ProbeClient.health_snapshot/0`
  directly — keeps the test fast (no Finch round-trip) and deterministic
  (no port allocation).
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.Handlers.HealthCheck
  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  # Fixture mirroring the Python probe's `/v1/probe/health_snapshot`
  # envelope. The probe wraps the snapshot under `data` (see
  # `src/mcp_handlers/wave3a_probe.py::_envelope_ok`). All non-volatile
  # field names are exactly the names the Python handler's lite filter
  # reads from `src/mcp_handlers/admin/handlers.py:316-353`.
  @snapshot_data %{
    "status" => "healthy",
    "version" => "0.42.0",
    "redis_present" => true,
    "identity_continuity_mode" => "session_based",
    "status_breakdown" => %{"healthy" => 7, "degraded" => 0, "failed" => 0},
    "operator_summary" => "all systems nominal",
    "timestamp" => "2026-05-30T05:00:00Z",
    "checks" => %{
      "postgres" => %{
        "status" => "healthy",
        "mode" => "executor_pool",
        "details" => "connections=10",
        # `details` is NOT in the lite-filter passthrough list — it gets
        # dropped from the lite response. The byte-for-byte assertion below
        # depends on this.
        "extra_diagnostic_field" => "this is dropped by lite filter"
      },
      "redis" => %{
        "status" => "healthy",
        "redis_present" => true,
        "warning" => "ttl < 60s"
      }
    },
    "_cache" => %{
      "age_seconds" => 15.2,
      "produced_at" => 1_780_140_848.0,
      "stale" => false,
      "probe_interval_seconds" => 30.0,
      "staleness_threshold_seconds" => 90.0
    }
  }

  @probe_envelope %{
    "ok" => true,
    "protocol_version" => "wave3a.v1",
    "data" => @snapshot_data
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
    :meck.expect(Wave3aHandlers.ProbeClient, :health_snapshot, fn -> {:ok, envelope} end)
  end

  defp stub_probe_error(reason) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])
    :meck.expect(Wave3aHandlers.ProbeClient, :health_snapshot, fn -> {:error, reason} end)
  end

  describe "HealthCheck.call/1 success path" do
    test "lite (default) returns the §2.2 payload shape with filtered checks" do
      stub_probe_ok()

      assert {:ok, body, 200} = HealthCheck.call(%{})

      assert body["status"] == "healthy"
      assert body["version"] == "0.42.0"
      assert body["redis_present"] == true

      assert body["status_breakdown"] == %{
               "healthy" => 7,
               "degraded" => 0,
               "failed" => 0
             }

      assert body["_note"] == "Use lite=false for full diagnostic detail"
      assert is_map(body["_cache"]), "_cache must surface through from the probe payload"

      # Lite filter drops anything not on the passthrough allowlist. The
      # `extra_diagnostic_field` is the canary.
      postgres = body["checks"]["postgres"]
      assert postgres["status"] == "healthy"
      assert postgres["mode"] == "executor_pool"

      refute Map.has_key?(postgres, "extra_diagnostic_field"),
             "lite filter MUST drop diagnostic fields not on the passthrough list"

      refute Map.has_key?(postgres, "details"),
             "lite filter MUST drop `details` (not on the passthrough list)"

      # `warning` and `note` are explicitly preserved by the lite filter.
      redis = body["checks"]["redis"]
      assert redis["status"] == "healthy"
      assert redis["redis_present"] == true
      assert redis["warning"] == "ttl < 60s"
    end

    test "lite=false passes the snapshot through verbatim" do
      stub_probe_ok()

      assert {:ok, body, 200} = HealthCheck.call(%{"lite" => false})

      # Verbatim passthrough — diagnostic fields are present.
      assert body["checks"]["postgres"]["extra_diagnostic_field"] ==
               "this is dropped by lite filter"

      assert body["checks"]["postgres"]["details"] == "connections=10"
      assert body["_cache"]["age_seconds"] == 15.2
    end

    test "two calls in a row produce structurally identical responses (idempotent)" do
      stub_probe_ok()

      {:ok, body_a, status_a} = HealthCheck.call(%{})
      {:ok, body_b, status_b} = HealthCheck.call(%{})

      assert status_a == status_b
      # The fixture is fully deterministic — both calls return literally
      # the same map. Under production traffic, volatile fields (_cache
      # timestamps) drift; those are masked by the Python parity test.
      assert body_a == body_b
    end

    test "extracts data from the probe's wave3a envelope wrapper" do
      stub_probe_ok()
      assert {:ok, body, 200} = HealthCheck.call(%{})
      # If the handler accidentally returned the entire envelope, `ok` /
      # `protocol_version` would leak into the body. Guard against that.
      refute Map.has_key?(body, "ok")
      refute Map.has_key?(body, "protocol_version")
      refute Map.has_key?(body, "data")
    end
  end

  describe "HealthCheck.call/1 error paths (500ms budget + probe-side failures)" do
    test "ProbeClient.timeout → 504 with stable error tag" do
      # The 500ms budget lives inside ProbeClient (Finch receive_timeout in
      # `probe_client.ex`). Here we simulate what ProbeClient returns when
      # that budget expires — the handler MUST translate to a typed
      # envelope, not raise.
      stub_probe_error(:timeout)

      assert {:ok, body, 504} = HealthCheck.call(%{})
      assert body[:error] == "probe_timeout"
      assert body[:reason] == "Python probe exceeded 500ms budget"
    end

    test "ProbeClient.probe_token_unset → 503" do
      stub_probe_error(:probe_token_unset)

      assert {:ok, body, 503} = HealthCheck.call(%{})
      assert body[:error] == "service_unavailable"
    end

    test "ProbeClient.connect_error → 502" do
      stub_probe_error(:connect_error)

      assert {:ok, body, 502} = HealthCheck.call(%{})
      assert body[:error] == "probe_unavailable"
    end

    test "ProbeClient.{:non_200, status} surfaces the upstream status code" do
      stub_probe_error({:non_200, 503})

      assert {:ok, body, 502} = HealthCheck.call(%{})
      assert body[:error] == "probe_non_200"
      assert body[:probe_status] == 503
    end

    test "ProbeClient.envelope_invalid → 502" do
      stub_probe_error(:envelope_invalid)

      assert {:ok, body, 502} = HealthCheck.call(%{})
      assert body[:error] == "probe_envelope_invalid"
    end
  end

  describe "500ms timeout discipline (end-to-end via mocked ProbeClient)" do
    test "a 600ms sleep inside the mocked probe still yields a typed envelope" do
      # The brief: "if ProbeClient.health_snapshot takes longer than 500ms
      # (mocked via :timer.sleep), assert the handler returns a timeout
      # envelope." The 500ms budget is enforced inside the real ProbeClient
      # via Finch's `receive_timeout`. With the mocked ProbeClient we
      # can't exercise Finch's wall-clock budget directly — the mock IS
      # the call. But we CAN assert that when the mocked call returns the
      # timeout atom (which is what the real Finch path would do on a
      # 600ms upstream), the handler returns a typed envelope rather than
      # raising or hanging.
      stub_probe_ok(@probe_envelope)

      :meck.expect(Wave3aHandlers.ProbeClient, :health_snapshot, fn ->
        :timer.sleep(600)
        {:error, :timeout}
      end)

      start = System.monotonic_time(:millisecond)
      assert {:ok, body, 504} = HealthCheck.call(%{})
      elapsed = System.monotonic_time(:millisecond) - start

      assert body[:error] == "probe_timeout"
      # The mock itself sleeps 600ms — the handler MUST NOT add measurable
      # latency on top. Cap generously to avoid CI flake.
      assert elapsed < 1_000,
             "handler added unexpected latency: elapsed=#{elapsed}ms"
    end
  end

  describe "HTTP dispatch via Plug.Test (POST /v1/handlers/health_check)" do
    test "authenticated POST routes through to the handler and returns the §2.2 envelope" do
      stub_probe_ok()

      resp =
        :post
        |> conn("/v1/handlers/health_check", Jason.encode!(%{}))
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      body = parsed(resp)

      assert body["ok"] == true
      assert body["protocol_version"] == "wave3a.v1"
      assert body["status"] == "healthy"
      assert body["version"] == "0.42.0"
      assert is_map(body["checks"])
      assert is_map(body["_cache"])
    end

    test "POST with {arguments: {lite: false}} passes lite=false through to the handler" do
      stub_probe_ok()

      body = Jason.encode!(%{"arguments" => %{"lite" => false}})

      resp =
        :post
        |> conn("/v1/handlers/health_check", body)
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      decoded = parsed(resp)
      # lite=false ⇒ verbatim snapshot ⇒ `details` field is present.
      assert decoded["checks"]["postgres"]["details"] == "connections=10"
    end

    test "probe-side failure produces a typed error envelope at the HTTP surface" do
      stub_probe_error(:connect_error)

      resp =
        :post
        |> conn("/v1/handlers/health_check", Jason.encode!(%{}))
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 502
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "probe_unavailable"
    end

    test "unwired tool names still return the 501 envelope" do
      # Sanity that names absent from the dispatch table go through the 501
      # fallback. Use a deliberately fake tool name so this test does not
      # drift when future Wave 3a cutovers wire real handlers.
      unwired_tool_name = "__unwired_tool_for_501_test__"

      resp =
        :post
        |> conn("/v1/handlers/#{unwired_tool_name}", Jason.encode!(%{}))
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 501
      body = parsed(resp)
      assert body["error"] == "not_implemented"
      assert body["tool_name"] == unwired_tool_name
    end
  end
end
