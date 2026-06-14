defmodule UnitaresSentinel.FleetFindingEmitterTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.FleetFindingEmitter

  defp fleet_finding do
    %{
      type: "coordinated_degradation",
      violation_class: "CON",
      severity: "high",
      summary: "Coordinated coherence drop: Agent A(-0.20), Agent B(-0.20)"
    }
  end

  defp self_finding do
    %{
      type: "entropy_outlier",
      violation_class: "ENT",
      severity: "info",
      summary: "Sentinel entropy outlier (z=2.8, S=1.000)",
      self_observation: true
    }
  end

  test "tick emits fleet findings and skips self observations" do
    parent = self()

    analysis_fun = fn snapshot, analysis_opts ->
      assert snapshot == %{agents: %{}, events: []}
      assert analysis_opts[:self_agent_id] == "sentinel-test"
      [fleet_finding(), self_finding()]
    end

    http_post = fn _url, body, _headers, _timeout_ms ->
      send(parent, {:posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    result =
      FleetFindingEmitter.tick(
        snapshot: %{agents: %{}, events: []},
        analysis_fun: analysis_fun,
        self_agent_id: "sentinel-test",
        findings_opts: [
          agent_name: "Sentinel",
          http_post: http_post
        ]
      )

    assert result.posted_count == 1
    assert result.fleet_findings == [fleet_finding()]
    assert result.self_findings == [self_finding()]

    assert_receive {:posted, body}
    assert body["type"] == "sentinel_finding"
    assert body["agent_id"] == "sentinel-test"
    assert body["finding_type"] == "coordinated_degradation"
    assert body["violation_class"] == "CON"
  end

  test "tick can opt in to governance check-in emission" do
    parent = self()

    analysis_fun = fn _snapshot, _analysis_opts -> [fleet_finding(), self_finding()] end

    checkin_http_post = fn url, body, _headers, timeout_ms ->
      send(parent, {:checkin_posted, url, body, timeout_ms})

      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "result" => %{"decision" => %{"action" => "proceed"}}
       })}
    end

    result =
      FleetFindingEmitter.tick(
        snapshot: %{active_agents: 2, agents: %{}, events: []},
        analysis_fun: analysis_fun,
        emit_findings: false,
        emit_checkins: true,
        cycle_count: 4,
        ws_connected?: false,
        checkin_opts: [
          url: "http://example.test/v1/tools/call",
          timeout_ms: 123,
          http_post: checkin_http_post,
          agent_id: "sentinel-test"
        ]
      )

    assert result.posted_count == 0
    assert result.checkin.response_mode == "compact"
    assert {:ok, %{"decision" => %{"action" => "proceed"}}} = result.checkin_result

    assert_receive {:checkin_posted, url, body, timeout_ms}
    assert url == "http://example.test/v1/tools/call"
    assert timeout_ms == 123
    assert body["name"] == "process_agent_update"

    args = body["arguments"]
    assert args["agent_id"] == "sentinel-test"
    assert args["response_mode"] == "compact"
    assert_in_delta args["complexity"], 0.45, 0.000_001
    assert_in_delta args["confidence"], 0.6, 0.000_001

    assert args["response_text"] ==
             "Sentinel analysis: Cycle 4 | Fleet: 2 agents | WS: DISCONNECTED | " <>
               "[HIGH] [CON] Coordinated coherence drop: Agent A(-0.20), Agent B(-0.20) | " <>
               "[SELF] Sentinel entropy outlier (z=2.8, S=1.000)"
  end

  test "GenServer wraps runtime tick in advisory lease acquire and release" do
    parent = self()
    lease_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      cond do
        String.ends_with?(url, "/v1/lease/acquire") ->
          send(parent, {:lease_acquire, body})

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: lease_id},
             drift_warning: []
           })}

        String.ends_with?(url, "/v1/lease/release") ->
          send(parent, {:lease_release, body})
          {:ok, 200, ~s({"ok":true})}
      end
    end

    findings_http_post = fn _url, body, _headers, _timeout_ms ->
      send(parent, {:posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    analysis_fun = fn _snapshot, _analysis_opts -> [fleet_finding()] end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_fleet_finding_emitter_#{System.unique_integer([:positive])}",
        initial_delay_ms: 60_000,
        interval_ms: 60_000,
        jitter_ms: 0,
        lease_advisory: true,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          holder_agent_uuid: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
          http_post: lease_http_post
        ],
        snapshot: %{agents: %{}, events: []},
        analysis_fun: analysis_fun,
        self_agent_id: "sentinel-test",
        findings_opts: [http_post: findings_http_post]
      )

    send(pid, :tick)

    assert_receive {:lease_acquire, acquire_body}, 1_000
    assert acquire_body["surface_id"] == "resident:/sentinel_cycle"
    assert acquire_body["intent"] == "sentinel analysis cycle"

    assert_receive {:posted, body}, 1_000
    assert body["agent_id"] == "sentinel-test"
    assert body["finding_type"] == "coordinated_degradation"

    assert_receive {:lease_release, %{"lease_id" => ^lease_id, "release_reason" => "normal"}},
                   1_000

    GenServer.stop(pid)
  end

  test "GenServer skips runtime tick when lease enforcement blocks" do
    parent = self()

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      if String.ends_with?(url, "/v1/lease/acquire") do
        send(parent, {:lease_acquire, body})

        {:ok, 409,
         Jason.encode!(%{
           ok: false,
           error: "held_by_other",
           held_by_uuid: "cccccccc-cccc-cccc-cccc-cccccccccccc"
         })}
      else
        send(parent, {:unexpected_release, body})
        {:ok, 200, ~s({"ok":true})}
      end
    end

    analysis_fun = fn _snapshot, _analysis_opts ->
      send(parent, :analysis_ran)
      [fleet_finding()]
    end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_fleet_finding_emitter_enforced_#{System.unique_integer([:positive])}",
        initial_delay_ms: 60_000,
        interval_ms: 60_000,
        jitter_ms: 0,
        lease_advisory: true,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          enforced_surface_kinds: MapSet.new(["resident"]),
          http_post: lease_http_post
        ],
        snapshot: %{agents: %{}, events: []},
        analysis_fun: analysis_fun,
        self_agent_id: "sentinel-test",
        findings_opts: []
      )

    send(pid, :tick)

    assert_receive {:lease_acquire, _body}, 1_000
    refute_receive :analysis_ran, 100
    refute_receive {:unexpected_release, _body}, 100

    GenServer.stop(pid)
  end

  defp paused_checkin_post(parent) do
    fn _url, body, _headers, _timeout_ms ->
      case body["name"] do
        "process_agent_update" ->
          send(parent, :checkin_attempted)

          {:ok, 200,
           Jason.encode!(%{
             "success" => true,
             "result" => %{
               "success" => false,
               "error" => "Agent is paused and cannot process updates",
               "error_code" => "AGENT_PAUSED",
               "paused_at" => "2026-06-13T23:40:11Z",
               "status" => "paused"
             }
           })}

        "self_recovery" ->
          send(parent, {:recovery_attempted, body["arguments"]["action"]})

          {:ok, 200,
           Jason.encode!(%{"success" => true, "result" => %{"lifecycle_status" => "active"}})}
      end
    end
  end

  test "tick surfaces a governance pause and attempts a bounded server-gated recovery" do
    parent = self()

    findings_http_post = fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true})}
    end

    result =
      FleetFindingEmitter.tick(
        snapshot: %{agents: %{}, events: []},
        analysis_fun: fn _s, _o -> [] end,
        self_agent_id: "sentinel-test",
        emit_checkins: true,
        findings_opts: [http_post: findings_http_post],
        checkin_opts: [
          url: "http://example.test/v1/tools/call",
          http_post: paused_checkin_post(parent),
          agent_id: "sentinel-test"
        ]
      )

    assert {:error, {:agent_paused, _}} = result.checkin_result
    assert result.checkin_pause["status"] == "paused"
    assert result.recovery_outcome == :recovered

    assert_receive :checkin_attempted
    assert_receive {:recovery_attempted, "quick"}
    assert_receive {:finding_posted, finding}
    assert finding["finding_type"] == "sentinel_self_pause"
    assert finding["severity"] == "high"
    assert finding["agent_id"] == "sentinel-test"
  end

  test "tick still surfaces a pause but does not attempt recovery when disarmed" do
    parent = self()

    findings_http_post = fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true})}
    end

    result =
      FleetFindingEmitter.tick(
        snapshot: %{agents: %{}, events: []},
        analysis_fun: fn _s, _o -> [] end,
        self_agent_id: "sentinel-test",
        emit_checkins: true,
        recovery_armed?: false,
        findings_opts: [http_post: findings_http_post],
        checkin_opts: [
          url: "http://example.test/v1/tools/call",
          http_post: paused_checkin_post(parent)
        ]
      )

    assert result.recovery_outcome == :not_attempted
    assert_receive {:finding_posted, finding}
    assert finding["finding_type"] == "sentinel_self_pause"
    refute_receive {:recovery_attempted, _action}, 100
  end

  test "GenServer attempts recovery only once per pause episode (no pause->resume loop)" do
    parent = self()

    # process_agent_update always reports paused; self_recovery is REFUSED, so
    # the episode never clears and a buggy implementation would retry forever.
    checkin_http_post = fn _url, body, _headers, _timeout_ms ->
      case body["name"] do
        "process_agent_update" ->
          {:ok, 200,
           Jason.encode!(%{
             "success" => true,
             "result" => %{
               "success" => false,
               "error_code" => "AGENT_PAUSED",
               "status" => "paused"
             }
           })}

        "self_recovery" ->
          send(parent, :recovery_attempted)

          {:ok, 200,
           Jason.encode!(%{
             "success" => true,
             "result" => %{"success" => false, "error" => "Recovery thresholds not met"}
           })}
      end
    end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_fleet_finding_emitter_pause_#{System.unique_integer([:positive])}",
        initial_delay_ms: 60_000,
        interval_ms: 60_000,
        jitter_ms: 0,
        lease_advisory: false,
        snapshot: %{agents: %{}, events: []},
        analysis_fun: fn _s, _o -> [] end,
        self_agent_id: "sentinel-test",
        emit_checkins: true,
        findings_opts: [http_post: fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end],
        checkin_opts: [url: "http://example.test/v1/tools/call", http_post: checkin_http_post]
      )

    send(pid, :tick)
    assert_receive :recovery_attempted, 1_000

    # Episode is now disarmed; a second tick on the same (still-paused) episode
    # must NOT attempt recovery again.
    send(pid, :tick)
    refute_receive :recovery_attempted, 300

    GenServer.stop(pid)
  end
end
