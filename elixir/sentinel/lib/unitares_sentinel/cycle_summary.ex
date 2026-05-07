defmodule UnitaresSentinel.CycleSummary do
  @moduledoc """
  Python-compatible Sentinel cycle check-in summary builder.

  This is a pure boundary for the `CycleResult` values Python Sentinel returns
  before the SDK calls `process_agent_update`. It does not perform HTTP writes.
  """

  @default_response_mode "compact"

  @doc """
  Build the `process_agent_update` argument subset for one Sentinel cycle.
  """
  @spec build(keyword()) :: map()
  def build(opts \\ []) do
    fleet_findings = Keyword.get(opts, :fleet_findings, [])
    self_findings = Keyword.get(opts, :self_findings, [])
    ws_connected? = Keyword.get(opts, :ws_connected?, Keyword.get(opts, :ws_connected, false))
    active_agents = active_agents(Keyword.get(opts, :snapshot, %{}))
    cycle_count = Keyword.get(opts, :cycle_count, 1)

    parts =
      [
        "Cycle #{cycle_count}",
        "Fleet: #{active_agents} agents",
        "WS: #{if(ws_connected?, do: "connected", else: "DISCONNECTED")}"
      ] ++ Enum.map(fleet_findings, &fleet_line/1) ++ Enum.map(self_findings, &self_line/1)

    high_issues = Enum.count(fleet_findings, &(map_get(&1, :severity, "") == "high"))

    %{
      response_text: "Sentinel analysis: #{Enum.join(parts, " | ")}",
      complexity:
        min(1.0, 0.2 + length(fleet_findings) * 0.15 + if(ws_connected?, do: 0, else: 0.1)),
      confidence: max(0.4, 0.85 - high_issues * 0.1 - if(ws_connected?, do: 0, else: 0.15)),
      response_mode: Keyword.get(opts, :response_mode, @default_response_mode)
    }
  end

  defp fleet_line(finding) do
    severity = finding |> map_get(:severity, "") |> String.upcase()
    violation_class = map_get(finding, :violation_class, "")
    cls_tag = if violation_class == "", do: "", else: "[#{violation_class}] "

    "[#{severity}] #{cls_tag}#{map_get(finding, :summary, "")}"
  end

  defp self_line(finding), do: "[SELF] #{map_get(finding, :summary, "")}"

  defp active_agents(%{active_agents: count}) when is_integer(count), do: count
  defp active_agents(%{"active_agents" => count}) when is_integer(count), do: count
  defp active_agents(%{summary: summary}) when is_map(summary), do: map_size(summary)
  defp active_agents(%{"summary" => summary}) when is_map(summary), do: map_size(summary)
  defp active_agents(%{agents: agents}) when is_map(agents), do: map_size(agents)
  defp active_agents(%{"agents" => agents}) when is_map(agents), do: map_size(agents)
  defp active_agents(_snapshot), do: 0

  defp map_get(map, key, default) when is_map(map) and is_atom(key) do
    value = Map.get(map, key) || Map.get(map, Atom.to_string(key), default)

    case value do
      nil -> default
      value -> to_string(value)
    end
  end
end
