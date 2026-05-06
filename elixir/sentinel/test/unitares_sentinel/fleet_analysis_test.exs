defmodule UnitaresSentinel.FleetAnalysisTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.FleetAnalysis
  alias UnitaresSentinel.FleetState

  @now_ms 1_776_512_400_000

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

    assert finding.summary == "Coordinated coherence drop: Agent A(-0.20), Agent B(-0.20)"
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

  defp ingest_eisv(state, now_ms, agent_id, agent_name, coherence, entropy, verdict) do
    state
    |> Map.put(:clock, fn :millisecond -> now_ms end)
    |> FleetState.ingest_event(%{
      "type" => "eisv_update",
      "agent_id" => agent_id,
      "agent_name" => agent_name,
      "eisv" => %{"E" => 0.2, "I" => 0.3, "S" => entropy, "V" => 0.4},
      "coherence" => coherence,
      "decision" => %{"action" => verdict}
    })
  end

  defp ingest_event(state, event), do: FleetState.ingest_event(state, event)
end
