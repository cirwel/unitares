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
end
