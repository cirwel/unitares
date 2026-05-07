defmodule UnitaresSentinel.FleetAnalysis do
  @moduledoc """
  Pure fleet finding reducer for BEAM Sentinel.

  Ports the Python Sentinel's `FleetState.analyze/1` rules onto the BEAM
  `FleetState` snapshot without performing notifications, check-ins, or HTTP
  emission.
  """

  alias UnitaresSentinel.FleetState
  alias UnitaresSentinel.FleetState.AgentSnapshot

  @coherence_drop_threshold 0.15
  @coordinated_window_ms 10 * 60 * 1_000
  @coordinated_min_agents 2
  @entropy_sigma 2.0
  @entropy_window_ms 60 * 60 * 1_000
  @verdict_shift_min_observations 5
  @verdict_shift_pause_rate 0.20
  @typed_event_prefixes ["lifecycle_", "circuit_breaker_", "identity_", "knowledge_"]

  @doc """
  Analyze a FleetState reducer or snapshot and return Sentinel finding maps.
  """
  def analyze(state_or_snapshot, opts \\ [])

  def analyze(%FleetState{} = state, opts) do
    state
    |> FleetState.snapshot_state()
    |> analyze(opts)
  end

  def analyze(%{agents: agents, events: events}, opts) when is_map(agents) and is_list(events) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    self_agent_id = Keyword.get(opts, :self_agent_id, "")

    []
    |> Kernel.++(coordinated_degradation(agents, now_ms))
    |> Kernel.++(entropy_outliers(agents, now_ms, self_agent_id))
    |> Kernel.++(verdict_shift(agents, now_ms))
    |> Kernel.++(correlated_events(events, now_ms))
  end

  defp coordinated_degradation(agents, now_ms) do
    degraded =
      agents
      |> Enum.reject(fn {_agent_id, snapshot} ->
        now_ms - snapshot.last_seen_ms > @coordinated_window_ms * 2
      end)
      |> Enum.map(fn {agent_id, snapshot} ->
        {agent_id, snapshot.name, coherence_drop(snapshot, now_ms, @coordinated_window_ms)}
      end)
      |> Enum.filter(fn {_agent_id, _name, drop} -> drop >= @coherence_drop_threshold end)

    if length(degraded) >= @coordinated_min_agents do
      agents_str =
        Enum.map_join(degraded, ", ", fn {agent_id, name, drop} ->
          "#{display_name(agent_id, name)}(-#{format_float(drop, 2)})"
        end)

      [
        %{
          type: "coordinated_degradation",
          violation_class: "CON",
          severity: "high",
          summary: "Coordinated coherence drop: #{agents_str}",
          agents: Enum.map(degraded, fn {agent_id, _name, _drop} -> agent_id end),
          details:
            Map.new(degraded, fn {agent_id, _name, drop} ->
              {agent_id, Float.round(drop * 1.0, 3)}
            end)
        }
      ]
    else
      []
    end
  end

  defp entropy_outliers(agents, now_ms, self_agent_id) do
    entropies =
      agents
      |> Enum.reject(fn {_agent_id, snapshot} ->
        now_ms - snapshot.last_seen_ms > @entropy_window_ms
      end)
      |> Enum.map(fn {agent_id, snapshot} ->
        {agent_id, snapshot.name, mean_entropy(snapshot, now_ms, @entropy_window_ms)}
      end)
      |> Enum.filter(fn {_agent_id, _name, entropy} -> entropy > 0 end)

    if length(entropies) >= 3 do
      values = Enum.map(entropies, fn {_agent_id, _name, entropy} -> entropy end)
      mean = Enum.sum(values) / length(values)
      std = sample_std(values, mean)

      if std > 0 do
        entropies
        |> Enum.map(fn {agent_id, name, entropy} ->
          {agent_id, name, entropy, (entropy - mean) / std}
        end)
        |> Enum.filter(fn {_agent_id, _name, _entropy, z} -> z >= @entropy_sigma end)
        |> Enum.map(fn {agent_id, name, entropy, z} ->
          self_observation? = agent_id == self_agent_id

          %{
            type: "entropy_outlier",
            violation_class: "ENT",
            severity: if(self_observation?, do: "info", else: "medium"),
            summary:
              "#{display_name(agent_id, name)} entropy outlier (z=#{format_float(z, 1)}, S=#{format_float(entropy, 3)})",
            agents: [agent_id],
            self_observation: self_observation?
          }
        end)
      else
        []
      end
    else
      []
    end
  end

  defp verdict_shift(agents, now_ms) do
    recent_verdicts =
      agents
      |> Enum.reject(fn {_agent_id, snapshot} ->
        now_ms - snapshot.last_seen_ms > @coordinated_window_ms
      end)
      |> Enum.flat_map(fn {_agent_id, snapshot} ->
        snapshot.eisv_history
        |> Enum.filter(fn history -> history.ts_ms >= now_ms - @coordinated_window_ms end)
        |> Enum.map(& &1.verdict)
      end)

    if length(recent_verdicts) >= @verdict_shift_min_observations do
      pause_count = Enum.count(recent_verdicts, &(&1 in ["pause", "reject"]))
      pause_rate = pause_count / length(recent_verdicts)

      if pause_rate >= @verdict_shift_pause_rate do
        [
          %{
            type: "verdict_shift",
            violation_class: "ENT",
            severity: "high",
            summary:
              "Pause rate #{round(pause_rate * 100)}% in last #{div(@coordinated_window_ms, 60_000)}min (#{pause_count}/#{length(recent_verdicts)})",
            details: %{pause_rate: Float.round(pause_rate * 1.0, 3), pause_count: pause_count}
          }
        ]
      else
        []
      end
    else
      []
    end
  end

  defp correlated_events(events, now_ms) do
    recent_typed =
      events
      |> Enum.filter(&typed_event?/1)
      |> Enum.filter(&(event_age_ms(&1, now_ms) < @coordinated_window_ms))

    if length(recent_typed) >= 3 do
      event_types =
        recent_typed
        |> Enum.map(&string_value(&1, "type"))
        |> Enum.uniq()
        |> Enum.sort()

      if length(event_types) >= 2 do
        [
          %{
            type: "correlated_events",
            violation_class: "BEH",
            severity: "medium",
            summary:
              "#{length(recent_typed)} governance events in #{div(@coordinated_window_ms, 60_000)}min: #{Enum.join(event_types, ", ")}",
            details: %{event_types: event_types, count: length(recent_typed)}
          }
        ]
      else
        []
      end
    else
      []
    end
  end

  defp coherence_drop(%AgentSnapshot{} = snapshot, now_ms, window_ms) do
    recent =
      Enum.filter(snapshot.eisv_history, fn history ->
        history.ts_ms >= now_ms - window_ms
      end)

    if length(recent) >= 2 do
      List.first(recent).coherence - List.last(recent).coherence
    else
      0.0
    end
  end

  defp mean_entropy(%AgentSnapshot{} = snapshot, now_ms, window_ms) do
    values =
      snapshot.eisv_history
      |> Enum.filter(fn history -> history.ts_ms >= now_ms - window_ms end)
      |> Enum.map(&Map.fetch!(&1, :S))

    if values == [] do
      0.0
    else
      Enum.sum(values) / length(values)
    end
  end

  defp sample_std([_value], _mean), do: 0.0

  defp sample_std(values, mean) do
    variance =
      values
      |> Enum.map(fn value -> :math.pow(value - mean, 2) end)
      |> Enum.sum()
      |> Kernel./(length(values) - 1)

    :math.sqrt(variance)
  end

  defp typed_event?(event) do
    event_type = string_value(event, "type")
    Enum.any?(@typed_event_prefixes, &String.starts_with?(event_type, &1))
  end

  defp event_age_ms(event, now_ms) do
    with timestamp when timestamp != "" <- string_value(event, "timestamp"),
         {:ok, datetime, _offset} <- DateTime.from_iso8601(timestamp) do
      now_ms - DateTime.to_unix(datetime, :millisecond)
    else
      _ -> :infinity
    end
  end

  defp display_name(_agent_id, name) when is_binary(name) and name != "", do: name
  defp display_name(agent_id, _name), do: String.slice(agent_id, 0, 8)

  defp format_float(value, decimals) do
    :erlang.float_to_binary(value * 1.0, decimals: decimals)
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
