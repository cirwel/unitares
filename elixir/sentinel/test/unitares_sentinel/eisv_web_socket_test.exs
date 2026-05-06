defmodule UnitaresSentinel.EISVWebSocketTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.EISVWebSocket
  alias UnitaresSentinel.FleetState

  test "ingest_text decodes JSON text frames into FleetState" do
    fleet_state = unique_name("fleet")

    start_supervised!({FleetState, name: fleet_state, clock: fn :millisecond -> 1_000 end})

    message =
      Jason.encode!(%{
        type: "eisv_update",
        agent_id: "agent-a",
        agent_name: "Agent A",
        eisv: %{E: 0.1, I: 0.2, S: 0.3, V: 0.4},
        coherence: 0.91,
        decision: %{action: "proceed"}
      })

    assert :ok = EISVWebSocket.ingest_text(message, fleet_state)
    assert :ignored = EISVWebSocket.ingest_text("{not-json", fleet_state)
    assert :ignored = EISVWebSocket.ingest_text(~s(["not", "an", "object"]), fleet_state)

    snapshot = FleetState.snapshot(fleet_state)

    assert snapshot.event_count == 1
    assert snapshot.agents["agent-a"].last_coherence == 0.91
    assert [%{E: 0.1, verdict: "proceed"}] = snapshot.agents["agent-a"].eisv_history
  end

  test "consumer can be supervised without opening a socket" do
    fleet_state = unique_name("fleet")
    websocket = unique_name("ws")

    start_supervised!({FleetState, name: fleet_state})

    start_supervised!(
      {EISVWebSocket,
       name: websocket,
       fleet_state: fleet_state,
       connect_on_init?: false,
       url: "ws://localhost:8767/ws/eisv"}
    )

    refute EISVWebSocket.connected?(websocket)
  end

  defp unique_name(prefix) do
    :"#{prefix}_#{System.unique_integer([:positive])}"
  end
end
