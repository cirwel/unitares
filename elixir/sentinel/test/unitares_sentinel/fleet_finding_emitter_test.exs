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
    # This emitter passes :lease_opts WITHOUT a :surface_id. It used to fall
    # through to LeaseAdvisory's default — ForcedReleasePoller's
    # resident:/sentinel_cycle — which is the collision application.ex's
    # distinct-surface comment says must not happen, and which also gave both
    # residents one LeaseStarvation sidecar file and one finding fingerprint.
    # The emitter now names its own surface.
    assert acquire_body["surface_id"] == "resident:/sentinel_fleet_emit"
    assert acquire_body["intent"] == "sentinel analysis cycle"

    assert_receive {:posted, body}, 1_000
    assert body["agent_id"] == "sentinel-test"
    assert body["finding_type"] == "coordinated_degradation"

    assert_receive {:lease_release, %{"lease_id" => ^lease_id, "release_reason" => "normal"}},
                   1_000

    GenServer.stop(pid)
  end

  test "an explicit nil surface_id in lease_opts falls back instead of crash-looping init" do
    parent = self()

    # `Keyword.put_new/3` keys on the key being PRESENT, not on its value, so an
    # explicit `surface_id: nil` walked past the default above, reached
    # `Keyword.fetch!/2`, and handed `LeaseStarvation.require_surface_id/1` a nil
    # — which raises inside `init/1` and turns one mistyped option into a
    # supervisor restart loop. The comment at the call site read as if the
    # surface were defended when only OMISSION was.
    lease_http_post = fn url, body, _headers, _timeout_ms ->
      cond do
        String.ends_with?(url, "/v1/lease/acquire") ->
          send(parent, {:lease_acquire, body})

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             lease: %{
               lease_id: "dddddddd-dddd-dddd-dddd-dddddddddddd",
               surface_id: body["surface_id"],
               expires_at: "2026-07-31T23:59:59Z"
             }
           })}

        String.ends_with?(url, "/v1/lease/release") ->
          {:ok, 200, ~s({"ok":true})}
      end
    end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_fleet_finding_emitter_nil_surface_#{System.unique_integer([:positive])}",
        initial_delay_ms: 60_000,
        interval_ms: 60_000,
        jitter_ms: 0,
        lease_advisory: true,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          holder_agent_uuid: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
          http_post: lease_http_post,
          surface_id: nil
        ],
        snapshot: %{agents: %{}, events: []},
        analysis_fun: fn _s, _o -> [] end,
        self_agent_id: "sentinel-test",
        findings_opts: [http_post: fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end]
      )

    # It started at all — that is the regression. And it fell back to the
    # EMITTER's own surface, not to LeaseAdvisory's poller default, so the
    # distinct-surface invariant still holds.
    assert :sys.get_state(pid).lease_blocked_surface_id == "resident:/sentinel_fleet_emit"

    send(pid, :tick)
    assert_receive {:lease_acquire, acquire_body}, 1_000
    assert acquire_body["surface_id"] == "resident:/sentinel_fleet_emit"

    GenServer.stop(pid)
  end

  @blocking_lease_id "b583498a-51a8-4fc4-8e69-8796423f7491"
  @blocking_holder_uuid "cccccccc-cccc-cccc-cccc-cccccccccccc"

  defp held_by_other_body do
    Jason.encode!(%{
      ok: false,
      error: "held_by_other",
      held_by_uuid: @blocking_holder_uuid,
      blocking_lease_id: @blocking_lease_id
    })
  end

  # Every starvation-path test injects BOTH channels: `{:lease_acquire, _}` for
  # the lease plane and `{:finding_posted, _}` for /api/findings. Sharing one
  # stub, or leaving `findings_opts: []`, would let a POST fall through to
  # `Findings.finch_post/4` against the real UNITARES_FINDINGS_URL — the exact
  # silent non-hermetic dependency PR #1410 landed to kill.
  defp blocked_lease_post(parent) do
    fn url, body, _headers, _timeout_ms ->
      if String.ends_with?(url, "/v1/lease/acquire") do
        send(parent, {:lease_acquire, body})
        {:ok, 409, held_by_other_body()}
      else
        send(parent, {:unexpected_release, body})
        {:ok, 200, ~s({"ok":true})}
      end
    end
  end

  defp finding_post(parent) do
    fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end
  end

  defp start_enforced_emitter(parent, prefix, opts) do
    FleetFindingEmitter.start_link(
      Keyword.merge(
        [
          name: :"test_fleet_finding_emitter_#{prefix}_#{System.unique_integer([:positive])}",
          initial_delay_ms: 60_000,
          interval_ms: 60_000,
          jitter_ms: 0,
          lease_advisory: true,
          lease_opts: [
            base_url: "http://lease.test",
            bearer_token: "test-token",
            surface_id: "resident:/sentinel_fleet_emit",
            enforced_surface_kinds: MapSet.new(["resident"]),
            http_post: blocked_lease_post(parent)
          ],
          snapshot: %{agents: %{}, events: []},
          self_agent_id: "sentinel-test",
          findings_opts: [http_post: finding_post(parent)],
          # Off unless a test opts in — the sidecar file is a real filesystem
          # side effect and this suite must stay hermetic.
          lease_blocked_state_path: false
        ],
        opts
      )
    )
  end

  test "GenServer skips runtime tick when lease enforcement blocks" do
    parent = self()

    analysis_fun = fn _snapshot, _analysis_opts ->
      send(parent, :analysis_ran)
      [fleet_finding()]
    end

    {:ok, pid} =
      start_enforced_emitter(parent, "enforced",
        analysis_fun: analysis_fun,
        # Far above anything this test can reach, so the blocked branch does
        # everything EXCEPT emit.
        lease_blocked_alert_after_seconds: 86_400
      )

    send(pid, :tick)

    assert_receive {:lease_acquire, _body}, 1_000
    refute_receive :analysis_ran, 100
    refute_receive {:unexpected_release, _body}, 100
    refute_receive {:finding_posted, _body}, 100

    GenServer.stop(pid)
  end

  # 2026-07-31 immortal-lease incident: this branch used to log
  # "tick skipped by lease enforcement" and reschedule, for hours at a stretch,
  # while launchctl / the live PID / the absence of a crash all read healthy.
  test "GenServer emits a lease-starvation self finding once the episode passes the threshold" do
    parent = self()

    {:ok, pid} =
      start_enforced_emitter(parent, "starved",
        analysis_fun: fn _s, _o -> [] end,
        lease_blocked_alert_after_seconds: 60
      )

    send(pid, :tick)
    assert_receive {:lease_acquire, _body}, 1_000
    refute_receive {:finding_posted, _body}, 100

    # Backdate the episode instead of sleeping past a real threshold — the
    # ladder trips on elapsed seconds, so this is the whole condition.
    :sys.replace_state(pid, fn state ->
      %{state | lease_blocked_since: DateTime.add(DateTime.utc_now(), -61, :second)}
    end)

    send(pid, :tick)

    assert_receive {:finding_posted, finding}, 1_000
    assert finding["type"] == "sentinel_finding"
    assert finding["finding_type"] == "sentinel_lease_starved"
    assert finding["severity"] == "high"
    assert finding["violation_class"] == "BEH"
    assert finding["agent_id"] == "sentinel-test"
    assert finding["self_observation"] == true
    assert finding["surface_id"] == "resident:/sentinel_fleet_emit"
    assert finding["blocking_lease_id"] == @blocking_lease_id
    assert finding["held_by_uuid"] == @blocking_holder_uuid
    assert finding["message"] =~ "resident:/sentinel_fleet_emit"
    assert finding["message"] =~ "/v1/lease/force-release"
    assert is_binary(finding["change_token"])

    state = :sys.get_state(pid)
    assert state.lease_blocked_streak == 2
    assert state.lease_blocked_last_emitted_multiple == 1

    GenServer.stop(pid)
  end

  test "a granted lease clears the lease-blocked episode" do
    parent = self()
    lease_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    {:ok, counter} = Agent.start_link(fn -> 0 end)

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      cond do
        String.ends_with?(url, "/v1/lease/acquire") ->
          send(parent, {:lease_acquire, body})

          case Agent.get_and_update(counter, fn n -> {n, n + 1} end) do
            0 ->
              {:ok, 409, held_by_other_body()}

            _ ->
              {:ok, 200,
               Jason.encode!(%{ok: true, idempotent: false, lease: %{lease_id: lease_id}})}
          end

        true ->
          send(parent, {:lease_release, body})
          {:ok, 200, ~s({"ok":true})}
      end
    end

    {:ok, pid} =
      start_enforced_emitter(parent, "cleared",
        analysis_fun: fn _s, _o -> [] end,
        lease_blocked_alert_after_seconds: 60,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          surface_id: "resident:/sentinel_fleet_emit",
          enforced_surface_kinds: MapSet.new(["resident"]),
          http_post: lease_http_post
        ]
      )

    send(pid, :tick)
    assert_receive {:lease_acquire, _}, 1_000
    assert :sys.get_state(pid).lease_blocked_streak == 1

    send(pid, :tick)
    assert_receive {:lease_release, _}, 1_000

    state = :sys.get_state(pid)
    assert state.lease_blocked_streak == 0
    assert state.lease_blocked_since == nil

    # The episode never reached the alert threshold, so clearing it is silent —
    # no closure finding for something no operator was ever told about.
    refute_receive {:finding_posted, _}, 100

    Agent.stop(counter)
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

  test "GenServer does not re-attempt recovery after a GRANTED resume that did not clear the pause" do
    parent = self()

    # Recovery is GRANTED, but process_agent_update keeps reporting paused (the
    # resume did not take / re-paused). A buggy gate that only disarms on
    # :refused would re-attempt every cycle; the once-per-episode invariant must
    # disarm on any attempt, granted included.
    checkin_http_post = fn _url, body, _headers, _timeout_ms ->
      case body["name"] do
        "process_agent_update" ->
          {:ok, 200,
           Jason.encode!(%{
             "success" => true,
             "result" => %{"success" => false, "error_code" => "AGENT_PAUSED", "status" => "paused"}
           })}

        "self_recovery" ->
          send(parent, :recovery_attempted)

          {:ok, 200,
           Jason.encode!(%{"success" => true, "result" => %{"lifecycle_status" => "active"}})}
      end
    end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_fleet_finding_emitter_granted_#{System.unique_integer([:positive])}",
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

    send(pid, :tick)
    refute_receive :recovery_attempted, 300

    GenServer.stop(pid)
  end

  # Mirror of the ForcedReleasePoller reclaim wiring test: the LeaseReclaim
  # threading (init merge → acquire_opts → absorb) is hand-copied into this
  # GenServer, which is exactly the drift class that left both residents with
  # the same starvation blind spot in July. Keep both wired paths pinned.
  test "GenServer reclaims its own stranded lease across two ticks" do
    parent = self()
    {:ok, calls} = Agent.start_link(fn -> [] end)
    stranded_lease_id = "77777777-7777-7777-7777-777777777777"

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 ++ [{url, body}]))
      recorded = Agent.get(calls, & &1)

      cond do
        length(recorded) in [1, 2] ->
          {:error, :timeout}

        String.ends_with?(url, "/v1/lease/acquire") and length(recorded) == 3 ->
          [{_, first_attempt} | _] = recorded

          {:ok, 409,
           Jason.encode!(%{
             ok: false,
             error: "held_by_other",
             held_by_uuid: first_attempt["holder_agent_uuid"],
             blocking_lease_id: stranded_lease_id
           })}

        String.ends_with?(url, "/v1/lease/release") ->
          send(parent, {:released, body["lease_id"], body["release_reason"]})
          {:ok, 200, ~s({"ok":true})}

        true ->
          send(parent, :reacquired)

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: "88888888-8888-8888-8888-888888888888"},
             drift_warning: []
           })}
      end
    end

    {:ok, pid} =
      FleetFindingEmitter.start_link(
        name: :"test_emitter_reclaim_wiring_#{System.unique_integer([:positive])}",
        initial_delay_ms: 60_000,
        interval_ms: 60_000,
        jitter_ms: 0,
        emit_findings: false,
        lease_advisory: true,
        lease_blocked_state_path: false,
        snapshot: %{agents: %{}, events: []},
        self_agent_id: "sentinel-test",
        lease_opts: [
          bearer_token: "test-token",
          surface_id: "resident:/sentinel_fleet_emit",
          enforced_surface_kinds: MapSet.new(["resident"]),
          http_post: lease_http_post
        ]
      )

    send(pid, :tick)

    state = :sys.get_state(pid)
    [{_, first_attempt} | _] = Agent.get(calls, & &1)
    stranded_uuid = first_attempt["holder_agent_uuid"]
    assert Enum.map(state.lease_reclaim_candidates, &elem(&1, 0)) == [stranded_uuid]

    send(pid, :tick)

    assert_receive {:released, ^stranded_lease_id, "reclaimed_lost_acquire"}, 2_000
    assert_receive :reacquired, 2_000
    assert_receive {:released, "88888888-8888-8888-8888-888888888888", _reason}, 5_000

    remembered = Enum.map(:sys.get_state(pid).lease_reclaim_candidates, &elem(&1, 0))
    assert stranded_uuid in remembered

    GenServer.stop(pid)
  end
end
