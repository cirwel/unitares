defmodule UnitaresSentinel.CycleSummaryTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.CycleSummary

  test "build mirrors Python Sentinel cycle check-in fields" do
    summary =
      CycleSummary.build(
        cycle_count: 7,
        snapshot: %{active_agents: 3},
        ws_connected?: false,
        fleet_findings: [
          %{
            severity: "high",
            violation_class: "CON",
            summary: "Coordinated coherence drop: Agent A(-0.20), Agent B(-0.20)"
          },
          %{
            severity: "medium",
            violation_class: "",
            summary: "4 governance events in 10min: lifecycle_pause, identity_resume"
          }
        ],
        self_findings: [
          %{
            severity: "info",
            violation_class: "ENT",
            summary: "Sentinel entropy outlier (z=2.8, S=1.000)"
          }
        ]
      )

    assert summary.response_text ==
             "Sentinel analysis: Cycle 7 | Fleet: 3 agents | WS: DISCONNECTED | " <>
               "[HIGH] [CON] Coordinated coherence drop: Agent A(-0.20), Agent B(-0.20) | " <>
               "[MEDIUM] 4 governance events in 10min: lifecycle_pause, identity_resume | " <>
               "[SELF] Sentinel entropy outlier (z=2.8, S=1.000)"

    assert summary.complexity == 0.6
    assert summary.confidence == 0.6
    assert summary.response_mode == "compact"
  end

  test "build derives active agent count from FleetState snapshots" do
    summary =
      CycleSummary.build(
        cycle_count: 1,
        snapshot: %{summary: %{"a" => %{}, "b" => %{}}},
        ws_connected?: true,
        fleet_findings: [],
        self_findings: []
      )

    assert summary.response_text ==
             "Sentinel analysis: Cycle 1 | Fleet: 2 agents | WS: connected"

    assert summary.complexity == 0.2
    assert summary.confidence == 0.85
  end
end
