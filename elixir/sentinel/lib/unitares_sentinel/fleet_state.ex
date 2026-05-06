defmodule UnitaresSentinel.FleetState do
  @moduledoc """
  Rolling in-memory EISV state for the BEAM Sentinel.

  This mirrors the Python Sentinel's `FleetState.ingest/1` contract: all
  decoded WebSocket events are retained in a bounded recent-event buffer, while
  only flat `%{"type" => "eisv_update", "agent_id" => ...}` payloads mutate
  per-agent EISV windows.
  """

  use GenServer

  @default_eisv_window_size 72
  @default_event_window_size 500
  @active_window_ms 60 * 60 * 1_000

  defmodule AgentSnapshot do
    @moduledoc false

    @enforce_keys [:agent_id]
    defstruct agent_id: nil,
              name: "",
              eisv_history: [],
              coherence_history: [],
              last_seen_ms: 0,
              last_verdict: "",
              last_coherence: 1.0
  end

  defstruct agents: %{},
            events: [],
            eisv_window_size: @default_eisv_window_size,
            event_window_size: @default_event_window_size,
            clock: nil

  @doc false
  def child_spec(opts) do
    opts = Keyword.put_new(opts, :name, __MODULE__)

    %{
      id: Keyword.get(opts, :name),
      start: {__MODULE__, :start_link, [opts]}
    }
  end

  def start_link(opts \\ []) do
    {name, opts} = Keyword.pop(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  def init(opts) do
    {:ok, new(opts)}
  end

  @doc """
  Build an empty FleetState reducer state.

  Public so tests and later analysis code can exercise the same reducer without
  going through a process.
  """
  def new(opts \\ []) do
    %__MODULE__{
      eisv_window_size: Keyword.get(opts, :eisv_window_size, @default_eisv_window_size),
      event_window_size: Keyword.get(opts, :event_window_size, @default_event_window_size),
      clock: Keyword.get(opts, :clock, &System.system_time/1)
    }
  end

  def ingest(event) when is_map(event), do: ingest(__MODULE__, event)

  def ingest(server, event) when is_map(event) do
    GenServer.call(server, {:ingest, event})
  end

  def snapshot(server \\ __MODULE__) do
    GenServer.call(server, :snapshot)
  end

  @doc """
  Pure reducer used by the GenServer and fixture tests.
  """
  def ingest_event(%__MODULE__{} = state, event) when is_map(event) do
    state = %{state | events: append_bounded(state.events, event, state.event_window_size)}

    with "eisv_update" <- string_value(event, "type"),
         agent_id when agent_id != "" <- string_value(event, "agent_id") do
      now_ms = state.clock.(:millisecond)
      agents = Map.update(state.agents, agent_id, new_snapshot(agent_id, event), & &1)
      snapshot = record_agent(Map.fetch!(agents, agent_id), event, now_ms, state.eisv_window_size)

      %{state | agents: Map.put(agents, agent_id, snapshot)}
    else
      _ -> state
    end
  end

  @doc """
  Compact, deterministic view of the reducer state.
  """
  def snapshot_state(%__MODULE__{} = state) do
    now_ms = state.clock.(:millisecond)

    active_agents =
      state.agents
      |> Enum.filter(fn {_agent_id, snapshot} ->
        now_ms - snapshot.last_seen_ms < @active_window_ms
      end)
      |> Enum.map(fn {agent_id, snapshot} -> {agent_id, summarize_agent(snapshot, now_ms)} end)
      |> Map.new()

    %{
      active_agents: map_size(active_agents),
      agents: state.agents,
      event_count: length(state.events),
      events: state.events,
      summary: active_agents
    }
  end

  def handle_call({:ingest, event}, _from, state) do
    state = ingest_event(state, event)
    {:reply, :ok, state}
  end

  def handle_call(:snapshot, _from, state) do
    {:reply, snapshot_state(state), state}
  end

  defp new_snapshot(agent_id, event) do
    %AgentSnapshot{agent_id: agent_id, name: string_value(event, "agent_name")}
  end

  defp record_agent(%AgentSnapshot{} = snapshot, event, now_ms, window_size) do
    eisv = map_value(event, "eisv")
    coherence = number_value(event, "coherence", 0)
    verdict = verdict(event)

    history_entry = %{
      ts_ms: now_ms,
      E: number_value(eisv, "E", 0),
      I: number_value(eisv, "I", 0),
      S: number_value(eisv, "S", 0),
      V: number_value(eisv, "V", 0),
      coherence: coherence,
      verdict: verdict
    }

    %{
      snapshot
      | name: string_value(event, "agent_name", snapshot.name),
        eisv_history: append_bounded(snapshot.eisv_history, history_entry, window_size),
        coherence_history: append_bounded(snapshot.coherence_history, coherence, window_size),
        last_seen_ms: now_ms,
        last_verdict: verdict,
        last_coherence: coherence
    }
  end

  defp summarize_agent(%AgentSnapshot{} = snapshot, now_ms) do
    %{
      agent_id: snapshot.agent_id,
      name: snapshot.name,
      coherence: Float.round(snapshot.last_coherence * 1.0, 3),
      verdict: snapshot.last_verdict,
      age_min: Float.round((now_ms - snapshot.last_seen_ms) / 60_000, 1),
      history_size: length(snapshot.eisv_history)
    }
  end

  defp append_bounded(values, value, max_size) do
    values = values ++ [value]

    if length(values) > max_size do
      Enum.take(values, -max_size)
    else
      values
    end
  end

  defp verdict(event) do
    case map_value(event, "decision") do
      %{} = decision -> string_value(decision, "action")
      _ -> ""
    end
  end

  defp number_value(map, key, default) do
    case value(map, key) do
      number when is_number(number) -> number
      _ -> default
    end
  end

  defp map_value(map, key) do
    case value(map, key) do
      %{} = nested -> nested
      _ -> %{}
    end
  end

  defp string_value(map, key, default \\ "")

  defp string_value(map, key, default) do
    case value(map, key) do
      nil -> default
      "" -> default
      value when is_binary(value) -> value
      value -> to_string(value)
    end
  end

  defp value(map, key) when is_map(map) do
    Map.get(map, key) || Map.get(map, String.to_atom(key))
  end
end
