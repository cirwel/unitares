defmodule UnitaresSentinel.AuditVolumeWatch.Logic do
  @moduledoc """
  Pure rules for the audit-event volume check.

  Input rows are per `(event_type, source)` sums over the last hour (`recent`)
  and the 23 hours before it (`prior`), where `source` is the row's agent_id,
  else its session_id. Two rules:

    * **absolute** — `recent >= absolute_floor` (default 1000/h), severity
      `high`. No legitimate source has reached this in a month of data; every
      runaway client has.
    * **relative** — `recent >= relative_floor` (default 300/h) and at least
      `relative_multiplier` (default 10x) the source's own prior hourly mean,
      severity `medium`. Catches a slower runaway, or a brand-new source that
      arrives already loud.

  Calibration, measured 2026-09-24 over 30 days of `audit.events`: the largest
  legitimate per-source hour was 484 (`cross_device_call` from one
  orchestrator). Four runaway clients each wrote about 5,850
  `session_resolve_miss_observed` rows an hour, for days, from one session key
  apiece; one ran from 2026-09-16 23:25 until a manual restart on 09-24, and
  nothing flagged it.
  """

  @default_absolute_floor 1_000
  @default_relative_floor 300
  @default_relative_multiplier 10
  @prior_hours 23

  @type row :: %{
          required(:event_type) => String.t(),
          required(:source) => String.t(),
          required(:recent) => non_neg_integer(),
          required(:prior) => non_neg_integer()
        }

  @type anomaly :: %{
          event_type: String.t(),
          source: String.t(),
          recent: non_neg_integer(),
          prior: non_neg_integer(),
          prior_hourly_mean: float(),
          rule: :absolute | :relative,
          severity: String.t()
        }

  @doc "Floor below which a row cannot fire either rule; the SQL HAVING uses it."
  @spec query_floor(keyword()) :: pos_integer()
  def query_floor(opts \\ []) do
    min(absolute_floor(opts), relative_floor(opts))
  end

  @doc "Evaluate rows, returning one anomaly per firing `(event_type, source)`."
  @spec evaluate([row()], keyword()) :: [anomaly()]
  def evaluate(rows, opts \\ []) when is_list(rows) do
    abs_floor = absolute_floor(opts)
    rel_floor = relative_floor(opts)
    multiplier = Keyword.get(opts, :relative_multiplier, @default_relative_multiplier)
    exclude_sources = MapSet.new(Keyword.get(opts, :exclude_sources, []))

    rows
    |> Enum.reject(&MapSet.member?(exclude_sources, &1.source))
    |> Enum.flat_map(fn row ->
      recent = to_int(row.recent)
      prior = to_int(row.prior)
      mean = prior / @prior_hours

      rule =
        cond do
          recent >= abs_floor -> :absolute
          recent >= rel_floor and recent >= multiplier * max(mean, 1.0) -> :relative
          true -> nil
        end

      case rule do
        nil ->
          []

        rule ->
          [
            %{
              event_type: row.event_type,
              source: row.source,
              recent: recent,
              prior: prior,
              prior_hourly_mean: Float.round(mean, 1),
              rule: rule,
              severity: if(rule == :absolute, do: "high", else: "medium")
            }
          ]
      end
    end)
    |> Enum.sort_by(& &1.recent, :desc)
  end

  @doc "Sentinel finding for one anomaly. Fingerprint keys on event_type + source."
  @spec to_finding(anomaly(), map()) :: map()
  def to_finding(anomaly, emit_info \\ %{}) do
    suppressed = Map.get(emit_info, :suppressed_since_last, 0)

    rule_text =
      case anomaly.rule do
        :absolute -> "above the absolute ceiling"
        :relative -> "#{relative_ratio(anomaly)}x its own prior hourly mean"
      end

    %{
      type: "audit_volume_anomaly",
      violation_class: "BEH",
      severity: anomaly.severity,
      summary:
        "Audit volume: #{anomaly.recent} `#{anomaly.event_type}` rows in the last hour " <>
          "from #{short(anomaly.source)} (#{rule_text}; prior mean " <>
          "#{anomaly.prior_hourly_mean}/h)",
      fingerprint_extra: [anomaly.event_type, anomaly.source],
      extra: %{
        event_type_observed: anomaly.event_type,
        source: anomaly.source,
        recent_hour: anomaly.recent,
        prior_23h: anomaly.prior,
        prior_hourly_mean: anomaly.prior_hourly_mean,
        rule: Atom.to_string(anomaly.rule),
        suppressed_since_last: suppressed
      }
    }
  end

  defp relative_ratio(%{recent: recent, prior_hourly_mean: mean}) do
    recent
    |> Kernel./(max(mean, 1.0))
    |> Float.round(0)
    |> trunc()
  end

  defp short(source) when is_binary(source) and byte_size(source) > 24,
    do: String.slice(source, 0, 24) <> "…"

  defp short(""), do: "an unattributed writer"
  defp short(source), do: source

  defp absolute_floor(opts), do: Keyword.get(opts, :absolute_floor, @default_absolute_floor)
  defp relative_floor(opts), do: Keyword.get(opts, :relative_floor, @default_relative_floor)

  defp to_int(%Decimal{} = d), do: Decimal.to_integer(d)
  defp to_int(n) when is_integer(n), do: n
  defp to_int(n) when is_float(n), do: trunc(n)
  defp to_int(_), do: 0
end
