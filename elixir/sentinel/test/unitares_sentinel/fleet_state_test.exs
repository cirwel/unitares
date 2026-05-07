defmodule UnitaresSentinel.FleetStateTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.FleetState

  test "ingest_event keeps recent events but only eisv_update mutates agents" do
    state =
      FleetState.new(
        eisv_window_size: 2,
        event_window_size: 3,
        clock: fn :millisecond -> 1_000 end
      )

    state =
      FleetState.ingest_event(state, %{
        "type" => "lifecycle_paused",
        "agent_id" => "agent-a",
        "timestamp" => "2026-05-06T12:00:00Z"
      })

    state =
      FleetState.ingest_event(state, %{
        "type" => "eisv_update",
        "agent_id" => "agent-a",
        "agent_name" => "Agent A",
        "eisv" => %{"E" => 0.1, "I" => 0.2, "S" => 0.3, "V" => 0.4},
        "coherence" => 0.9,
        "decision" => %{"action" => "proceed"}
      })

    state =
      %{state | clock: fn :millisecond -> 2_000 end}
      |> FleetState.ingest_event(%{
        "type" => "eisv_update",
        "agent_id" => "agent-a",
        "agent_name" => "Agent A",
        "eisv" => %{"E" => 0.2, "I" => 0.3, "S" => 0.4, "V" => 0.5},
        "coherence" => 0.8,
        "decision" => %{"action" => "guide"}
      })

    state =
      %{state | clock: fn :millisecond -> 3_000 end}
      |> FleetState.ingest_event(%{
        "type" => "eisv_update",
        "agent_id" => "agent-a",
        "agent_name" => "Agent A",
        "eisv" => %{"E" => 0.3, "I" => 0.4, "S" => 0.5, "V" => 0.6},
        "coherence" => 0.7,
        "decision" => %{"action" => "pause"}
      })

    snapshot = FleetState.snapshot_state(state)

    assert snapshot.event_count == 3
    assert snapshot.active_agents == 1

    agent = snapshot.agents["agent-a"]
    assert agent.name == "Agent A"
    assert agent.last_coherence == 0.7
    assert agent.last_verdict == "pause"
    assert agent.coherence_history == [0.8, 0.7]

    assert [%{E: 0.2, verdict: "guide"}, %{E: 0.3, verdict: "pause"}] =
             agent.eisv_history
  end

  test "ingest_event accepts atom-key fixture maps without changing the JSON contract" do
    state =
      FleetState.new(clock: fn :millisecond -> 1_000 end)
      |> FleetState.ingest_event(%{
        type: "eisv_update",
        agent_id: "agent-b",
        agent_name: "Agent B",
        eisv: %{E: 0.5, I: 0.6, S: 0.7, V: 0.8},
        coherence: 0.65,
        decision: %{action: :guide}
      })

    snapshot = FleetState.snapshot_state(state)
    agent = snapshot.agents["agent-b"]

    assert snapshot.active_agents == 1
    assert agent.last_verdict == "guide"
    assert [%{I: 0.6, S: 0.7, V: 0.8}] = agent.eisv_history
  end
end
