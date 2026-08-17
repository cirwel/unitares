defmodule UnitaresSentinel.FleetAnalysisTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.Findings
  alias UnitaresSentinel.FleetAnalysis
  alias UnitaresSentinel.FleetState

  @now_ms 1_776_512_400_000
  @sentinel_uuid "sentinel-self-uuid"

  test "detects coordinated coherence drops across active agents" do
    state =
      FleetState.new()
      |> ingest_eisv(@now_ms - 300_000, "agent-a", "Agent A", 0.9, 0.2, "proceed")
      |> ingest_eisv(@now_ms, "agent-a", "Agent A", 0.7, 0.2, "guide")
      |> ingest_eisv(@now_ms - 300_000, "agent-b", "Agent B", 0.8, 0.3, "proceed")
      |> ingest_eisv(@now_ms, "agent-b", "Agent B", 0.6, 0.3, "guide")

    assert [
             %{
               type: "coordinated_degradation",
               violation_class: "CON",
               severity: "high",
               agents: ["agent-a", "agent-b"],
               details: %{"agent-a" => 0.2, "agent-b" => 0.2}
             } = finding
           ] = FleetAnalysis.analyze(state, now_ms: @now_ms)

    assert finding.summary ==
             "Coordinated legacy control-feedback drop (not a health diagnosis): " <>
               "Agent A(-0.20), Agent B(-0.20)"

    assert finding.coherence_source == "legacy_tanh_v"
    assert finding.coherence_role == "ode_control_feedback"
  end

  test "does not combine drops from different coherence producers" do
    state =
      FleetState.new()
      |> ingest_eisv(
        @now_ms - 300_000,
        "agent-a",
        "Agent A",
        0.9,
        0.2,
        "proceed",
        "legacy_tanh_v",
        "ode_control_feedback"
      )
      |> ingest_eisv(
        @now_ms,
        "agent-a",
        "Agent A",
        0.6,
        0.2,
        "guide",
        "legacy_tanh_v",
        "ode_control_feedback"
      )
      |> ingest_eisv(
        @now_ms - 300_000,
        "agent-b",
        "Agent B",
        0.9,
        0.2,
        "proceed",
        "manifold",
        "eis_structural_measurement"
      )
      |> ingest_eisv(
        @now_ms,
        "agent-b",
        "Agent B",
        0.6,
        0.2,
        "guide",
        "manifold",
        "eis_structural_measurement"
      )

    refute Enum.any?(
             FleetAnalysis.analyze(state, now_ms: @now_ms),
             &(&1.type == "coordinated_degradation")
           )
  end

  test "detects entropy outliers and tags self observations" do
    state =
      Enum.reduce(1..9, FleetState.new(), fn index, state ->
        ingest_eisv(state, @now_ms, "agent-#{index}", "Agent #{index}", 0.9, 0.1, "proceed")
      end)
      |> ingest_eisv(@now_ms, "self-agent", "Sentinel", 0.9, 1.0, "proceed")

    findings = FleetAnalysis.analyze(state, now_ms: @now_ms, self_agent_id: "self-agent")

    assert [
             %{
               type: "entropy_outlier",
               violation_class: "ENT",
               severity: "info",
               agents: ["self-agent"],
               self_observation: true
             } = finding
           ] = findings

    assert finding.summary == "Sentinel entropy outlier (z=2.8, S=1.000)"
  end

  test "detects verdict distribution shifts from recent pause and reject verdicts" do
    state =
      FleetState.new()
      |> ingest_eisv(@now_ms - 240_000, "agent-a", "Agent A", 0.9, 0.2, "proceed")
      |> ingest_eisv(@now_ms - 180_000, "agent-a", "Agent A", 0.9, 0.2, "proceed")
      |> ingest_eisv(@now_ms - 120_000, "agent-a", "Agent A", 0.9, 0.2, "guide")
      |> ingest_eisv(@now_ms - 60_000, "agent-a", "Agent A", 0.9, 0.2, "pause")
      |> ingest_eisv(@now_ms, "agent-a", "Agent A", 0.9, 0.2, "reject")

    assert [
             %{
               type: "verdict_shift",
               violation_class: "ENT",
               severity: "high",
               details: %{pause_count: 2, pause_rate: 0.4},
               summary: "Pause rate 40% in last 10min (2/5)"
             }
           ] = FleetAnalysis.analyze(state, now_ms: @now_ms)
  end

  test "detects correlated typed governance events" do
    now = DateTime.from_unix!(@now_ms, :millisecond)

    state =
      FleetState.new(event_window_size: 5)
      |> ingest_event(%{
        "type" => "lifecycle_paused",
        "agent_id" => "agent-a",
        "timestamp" => DateTime.to_iso8601(DateTime.add(now, -120, :second))
      })
      |> ingest_event(%{
        "type" => "identity_drift",
        "agent_id" => "agent-b",
        "timestamp" => DateTime.to_iso8601(DateTime.add(now, -60, :second))
      })
      |> ingest_event(%{
        "type" => "lifecycle_resumed",
        "agent_id" => "agent-a",
        "timestamp" => DateTime.to_iso8601(now)
      })

    assert [
             %{
               type: "correlated_events",
               violation_class: "BEH",
               severity: "medium",
               details: %{
                 event_types: ["identity_drift", "lifecycle_paused", "lifecycle_resumed"],
                 count: 3
               }
             } = finding
           ] = FleetAnalysis.analyze(state, now_ms: @now_ms)

    assert finding.summary ==
             "3 governance events in 10min: identity_drift, lifecycle_paused, lifecycle_resumed"
  end

  # The 4-part legacy fingerprint key ends in Sentinel's OWN agent_id, so any
  # finding type that can legitimately emit more than one instance per cycle
  # collapsed into a single dedup bucket server-side (`_dedup_window_seconds`
  # = 1800 in src/event_detector.py) and only the first instance survived the
  # window. These tests assert on the fingerprint that actually goes on the
  # wire — not on `:fingerprint_extra` — because the wire value is the contract.
  describe "finding fingerprints separate co-occurring subjects" do
    test "entropy_outlier keys on the subject agent, not the emitter" do
      # 9 agents at S=0.1 and 2 at S=1.0 puts BOTH high agents at z=2.0.
      state =
        Enum.reduce(1..9, FleetState.new(), fn index, state ->
          ingest_eisv(state, @now_ms, "agent-#{index}", "Agent #{index}", 0.9, 0.1, "proceed")
        end)
        |> ingest_eisv(@now_ms, "loud-a", "Loud A", 0.9, 1.0, "proceed")
        |> ingest_eisv(@now_ms, "loud-b", "Loud B", 0.9, 1.0, "proceed")

      outliers =
        state
        |> FleetAnalysis.analyze(now_ms: @now_ms, self_agent_id: @sentinel_uuid)
        |> Enum.filter(&(&1.type == "entropy_outlier"))

      assert length(outliers) == 2, "both agents should be detected as outliers"
      assert Enum.sort(Enum.flat_map(outliers, & &1.agents)) == ["loud-a", "loud-b"]

      [fp_a, fp_b] = Enum.map(outliers, &fingerprint_of/1)

      refute fp_a == fp_b,
             "two agents outlying in the same cycle must not share a dedup bucket"
    end

    test "entropy_outlier keys are stable for one subject across cycles" do
      builder = fn now_ms ->
        Enum.reduce(1..9, FleetState.new(), fn index, state ->
          ingest_eisv(state, now_ms, "agent-#{index}", "Agent #{index}", 0.9, 0.1, "proceed")
        end)
        |> ingest_eisv(now_ms, "loud-a", "Loud A", 0.9, 1.0, "proceed")
      end

      first =
        builder.(@now_ms)
        |> FleetAnalysis.analyze(now_ms: @now_ms, self_agent_id: @sentinel_uuid)
        |> Enum.find(&(&1.type == "entropy_outlier"))

      later_ms = @now_ms + 300_000

      second =
        builder.(later_ms)
        |> FleetAnalysis.analyze(now_ms: later_ms, self_agent_id: @sentinel_uuid)
        |> Enum.find(&(&1.type == "entropy_outlier"))

      assert fingerprint_of(first) == fingerprint_of(second),
             "the same agent re-detected next cycle must still dedup — the window " <>
               "exists for persisting conditions and widening the key must not defeat it"
    end

    test "coordinated_degradation keys on the coherence producer" do
      # Two producers, each with two agents dropping — one finding per producer,
      # emitted in the same cycle.
      state =
        FleetState.new()
        |> producer_drop("ode-a", "ODE A", "legacy_tanh_v", "ode_control_feedback")
        |> producer_drop("ode-b", "ODE B", "legacy_tanh_v", "ode_control_feedback")
        |> producer_drop("eis-a", "EIS A", "manifold", "eis_structural_measurement")
        |> producer_drop("eis-b", "EIS B", "manifold", "eis_structural_measurement")

      findings =
        state
        |> FleetAnalysis.analyze(now_ms: @now_ms)
        |> Enum.filter(&(&1.type == "coordinated_degradation"))

      assert length(findings) == 2, "each producer group is its own claim"

      assert findings |> Enum.map(&fingerprint_of/1) |> Enum.uniq() |> length() == 2,
             "a control-feedback drop must not dedup behind a structural-coherence drop"
    end

    test "correlated_events keys on the event-type set" do
      routine = correlated_state(["knowledge_read", "knowledge_write", "knowledge_read"])
      incident = correlated_state(["circuit_breaker_trip", "lifecycle_paused", "knowledge_read"])

      routine_finding = correlated_finding(routine)
      incident_finding = correlated_finding(incident)

      refute fingerprint_of(routine_finding) == fingerprint_of(incident_finding),
             "a circuit_breaker_trip burst must not dedup behind routine knowledge traffic"
    end

    test "correlated_events ignores the event count so dedup still bites" do
      three = correlated_state(["knowledge_read", "knowledge_write", "knowledge_read"])

      many =
        correlated_state(
          List.duplicate("knowledge_read", 8) ++ List.duplicate("knowledge_write", 4)
        )

      assert fingerprint_of(correlated_finding(three)) ==
               fingerprint_of(correlated_finding(many)),
             "count moves every cycle; keying on it would turn dedup off entirely"
    end
  end

  # Cross-runtime contract. `tests/test_sentinel_fleet_finding_fingerprint_parity.py`
  # asserts these SAME literals on the Python side; the two suites agreeing IS
  # the contract. Both Sentinels post to one `/api/findings`, which dedups on
  # the fingerprint string — if the runtimes disagree, the same fleet condition
  # double-fires across a cutover. Same convention as the forced-release parity
  # pair (`forced_release_poller_logic_3class_test.exs`).
  describe "fleet finding fingerprints are byte-equal across runtimes" do
    test "entropy_outlier golden" do
      assert Findings.compute_fingerprint([
               "sentinel",
               "entropy_outlier",
               "ENT",
               @sentinel_uuid,
               "loud-a"
             ]) == "4103a9bc17b90bc4"
    end

    test "coordinated_degradation golden" do
      assert Findings.compute_fingerprint([
               "sentinel",
               "coordinated_degradation",
               "CON",
               @sentinel_uuid,
               "legacy_tanh_v",
               "ode_control_feedback"
             ]) == "dbd36cff80a11f44"
    end

    test "correlated_events golden" do
      assert Findings.compute_fingerprint([
               "sentinel",
               "correlated_events",
               "BEH",
               @sentinel_uuid,
               "circuit_breaker_trip",
               "knowledge_read",
               "lifecycle_paused"
             ]) == "bcad93796db4f90e"
    end
  end

  defp fingerprint_of(finding) do
    finding
    |> Findings.finding_body(agent_id: @sentinel_uuid)
    |> Map.fetch!("fingerprint")
  end

  defp correlated_finding(state) do
    state
    |> FleetAnalysis.analyze(now_ms: @now_ms)
    |> Enum.find(&(&1.type == "correlated_events"))
  end

  defp correlated_state(types) do
    now = DateTime.from_unix!(@now_ms, :millisecond)

    types
    |> Enum.with_index()
    |> Enum.reduce(FleetState.new(event_window_size: 20), fn {type, index}, state ->
      ingest_event(state, %{
        "type" => type,
        "agent_id" => "agent-#{index}",
        "timestamp" => DateTime.to_iso8601(DateTime.add(now, -index * 10, :second))
      })
    end)
  end

  defp producer_drop(state, agent_id, name, source, role) do
    state
    |> ingest_eisv(@now_ms - 300_000, agent_id, name, 0.9, 0.2, "proceed", source, role)
    |> ingest_eisv(@now_ms, agent_id, name, 0.7, 0.2, "guide", source, role)
  end

  defp ingest_eisv(
         state,
         now_ms,
         agent_id,
         agent_name,
         coherence,
         entropy,
         verdict,
         source \\ "legacy_tanh_v",
         role \\ "ode_control_feedback"
       ) do
    state
    |> Map.put(:clock, fn :millisecond -> now_ms end)
    |> FleetState.ingest_event(%{
      "type" => "eisv_update",
      "agent_id" => agent_id,
      "agent_name" => agent_name,
      "eisv" => %{"E" => 0.2, "I" => 0.3, "S" => entropy, "V" => 0.4},
      "coherence" => coherence,
      "metrics" => %{"coherence_source" => source, "coherence_role" => role},
      "decision" => %{"action" => verdict}
    })
  end

  defp ingest_event(state, event), do: FleetState.ingest_event(state, event)
end
