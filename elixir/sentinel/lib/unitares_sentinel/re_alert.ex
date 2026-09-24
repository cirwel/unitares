defmodule UnitaresSentinel.ReAlert do
  @moduledoc """
  Per-condition re-alert backoff for Sentinel checks that see the same
  condition on every tick.

  A condition that stays true should page once, then again after `base_ms`,
  then after twice that, capped at `cap_ms`. Without this, a condition that
  holds for a day is 288 findings at a 5-minute tick. The 30-day audit that
  motivated this (2026-09-24) found 111 of 280 `held-by-other` alarms were one
  surface re-paging every cycle.

  Rules, per key:

    * first sighting — emit; the next repeat waits `base_ms`
    * higher severity than the last emission — emit now, restart the backoff
      (an escalation is news, not a repeat)
    * seen again after the current interval has elapsed — emit, double the
      interval up to `cap_ms`
    * otherwise — suppress, and count it so the next emission can say how
      many repeats it stands for
    * unseen for longer than the current interval — the condition cleared;
      the entry is forgotten and the next sighting is a new episode

  Pure: callers own the state map and pass `now_ms`. State is in memory only,
  so a restart re-alerts once per open condition, which the governance
  detector's 30-minute fingerprint window absorbs for fast restart loops.
  """

  @type key :: term()
  @type entry :: %{
          episode_ms: integer(),
          next_ok_ms: integer(),
          interval_ms: pos_integer(),
          rank: integer(),
          last_seen_ms: integer(),
          emitted: pos_integer(),
          suppressed: non_neg_integer()
        }
  @type t :: %{optional(key()) => entry()}

  @default_base_ms 60 * 60 * 1_000
  @default_cap_ms 24 * 60 * 60 * 1_000

  @doc "Empty state."
  @spec new() :: t()
  def new, do: %{}

  @doc """
  Decide whether the condition `key` at `rank` should be emitted at `now_ms`.

  Returns `{:emit, info, state}` or `{:suppress, state}`. `info` carries
  `:suppressed_since_last` (repeats swallowed since the previous emission),
  `:emit_seq` (1 for an episode's first emission) and `:episode_ms`, which
  together make a per-emission `change_token`.
  """
  @spec decide(t(), key(), integer(), integer(), keyword()) ::
          {:emit, map(), t()} | {:suppress, t()}
  def decide(state, key, rank, now_ms, opts \\ []) do
    base_ms = Keyword.get(opts, :base_ms, @default_base_ms)
    cap_ms = max(base_ms, Keyword.get(opts, :cap_ms, @default_cap_ms))

    case live_entry(state, key, now_ms) do
      nil ->
        entry = %{
          episode_ms: now_ms,
          next_ok_ms: now_ms + base_ms,
          interval_ms: base_ms,
          rank: rank,
          last_seen_ms: now_ms,
          emitted: 1,
          suppressed: 0
        }

        {:emit, info(entry, 0), Map.put(state, key, entry)}

      %{rank: prior_rank} = entry when rank > prior_rank ->
        emitted = %{
          entry
          | next_ok_ms: now_ms + base_ms,
            interval_ms: base_ms,
            rank: rank,
            last_seen_ms: now_ms,
            emitted: entry.emitted + 1,
            suppressed: 0
        }

        {:emit, info(emitted, entry.suppressed), Map.put(state, key, emitted)}

      %{next_ok_ms: next_ok_ms} = entry when now_ms >= next_ok_ms ->
        interval = min(entry.interval_ms * 2, cap_ms)

        emitted = %{
          entry
          | next_ok_ms: now_ms + interval,
            interval_ms: interval,
            # The severity this emission carries, so a later rise from it is
            # an escalation even if an earlier emission was higher.
            rank: rank,
            last_seen_ms: now_ms,
            emitted: entry.emitted + 1,
            suppressed: 0
        }

        {:emit, info(emitted, entry.suppressed), Map.put(state, key, emitted)}

      entry ->
        {:suppress,
         Map.put(state, key, %{entry | last_seen_ms: now_ms, suppressed: entry.suppressed + 1})}
    end
  end

  @doc """
  Drop entries whose condition has not been seen for longer than their current
  interval. Call once per tick so state does not grow with conditions that
  cleared long ago.
  """
  @spec prune(t(), integer()) :: t()
  def prune(state, now_ms) do
    state
    |> Enum.reject(fn {_key, entry} -> expired?(entry, now_ms) end)
    |> Map.new()
  end

  @doc "Stable per-emission token: changes on every emission, never on a retry."
  @spec change_token(map(), String.t()) :: String.t()
  def change_token(%{episode_ms: episode_ms, emit_seq: seq}, severity),
    do: "#{episode_ms}:#{seq}:#{severity}"

  @doc "Severity ordering shared by callers."
  @spec rank(String.t()) :: integer()
  def rank("critical"), do: 4
  def rank("high"), do: 3
  def rank("medium"), do: 2
  def rank("low"), do: 1
  def rank(_), do: 0

  defp live_entry(state, key, now_ms) do
    case Map.get(state, key) do
      nil -> nil
      entry -> if expired?(entry, now_ms), do: nil, else: entry
    end
  end

  defp expired?(%{last_seen_ms: last_seen_ms, interval_ms: interval_ms}, now_ms),
    do: now_ms - last_seen_ms > interval_ms

  defp info(entry, suppressed) do
    %{suppressed_since_last: suppressed, emit_seq: entry.emitted, episode_ms: entry.episode_ms}
  end
end
