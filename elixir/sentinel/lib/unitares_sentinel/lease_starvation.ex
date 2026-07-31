defmodule UnitaresSentinel.LeaseStarvation do
  @moduledoc """
  Self-reporting for a Sentinel resident that is being refused its advisory
  lease tick after tick.

  ## Why this module exists

  On 2026-07-31 the BEAM Sentinel went governance-dark. `FleetFindingEmitter`
  (300s cadence, surface `resident:/sentinel_fleet_emit`) and
  `ForcedReleasePoller` (30s cadence, surface `resident:/sentinel_cycle`) both
  hit `lease_enforcement_blocked?/1`, logged
  `"tick skipped by lease enforcement"`, rescheduled, and did nothing else. The
  log accumulated **5,703** consecutive such warnings and not one alert was
  raised: launchctl reported the job up, the OS process was alive, no crash, no
  supervisor restart. Every liveness signal read healthy while the residents did
  zero governance work.

  Root cause on the other side of the wire was an "immortal lease" — an acquire
  that succeeded server-side but timed out client-side, stranding a lease whose
  `LeaseHolder` GenServer auto-renews forever (`holder_pid` NULL, `expires_at`
  never in the past, so the Reaper never sweeps it).

  **The insight that makes self-reporting possible at all**: the findings POST
  goes to `/api/findings` on gov-MCP (:8767), a different process on a different
  port from the lease plane (:8788), and `http_record_finding`
  (`src/http_api.py:2635`) gates on `_check_http_auth` only — there is no lease
  check anywhere in that handler. A lease-starved resident can therefore still
  report its own starvation. Verified 2026-07-31; if that ever stops being true
  this module goes silent in exactly the condition it exists for, so treat it as
  a load-bearing invariant.

  ## Design decisions (and the live data behind them)

  **Duration, not tick count.** `poller_interval_ms` is an env-tunable knob and
  ticks are jittered (observed poller spacing 26-36s), so "N consecutive ticks"
  means a fixed wall-clock only under today's config. Retuning the interval to
  300s would silently push a count-based alert from 12 minutes to 2 hours. The
  trip condition, the escalation ladder and the `blocked for 1h36m` phrase in
  the summary are all the same quantity: seconds since the episode started.

  **Escalating re-emission, owned by the resident.** Emit at 1x, 2x, 4x, 8x, 16x
  the threshold, then every 16x forever. With the 720s default that is
  12m / 24m / 48m / 1h36m / 3h12m, then every 3h12m. The floor matters: the
  poller was starved for ~14 of the 16.5 hours preceding this change, in four
  separate episodes (7h41m, 4h48m, 1h12m, and one still running when the change
  was written) — an alert that fires once and then goes quiet would have been
  indistinguishable from a resolved outage.

  Rate-limiting lives here rather than in the server's 30-minute fingerprint
  window because that window would silently eat the ladder (a 24m re-emit lands
  inside it; a 30m re-emit is a coin flip). The findings carry a `change_token`,
  which flips `src/event_detector.py` `record_event` from fingerprint dedup to
  time-independent emit-on-change.

  **Delivery-aware.** `Findings.post_finding_result/2` distinguishes delivered
  from lost. A lost POST does NOT burn its rung on the ladder — the same rung is
  retried on the next tick until it lands. This is not hypothetical: gov-MCP on
  this box is periodically jetsam-killed, and a governance plane that is down is
  *correlated* with residents starving, so the densest and most valuable early
  alerts are precisely the ones a fire-and-forget design would drop.

  **Sticky blocker.** The remedy sentence is chosen from the whole episode, not
  from whichever tick happened to land on a ladder rung. Live data: 62
  `service_unavailable` ticks (Finch transport timeouts) are interleaved in
  bursts of four among 1,683 `held_by_other` ticks, and the emitter's *first*
  blocked tick of the 2026-07-31 episode was one of them. A boundary-tick remedy
  would have told the operator "the lease plane reported NO blocking lease,
  check that it is up" while an immortal lease held by `788992bb` was in fact
  the cause and the plane was up — worse than saying nothing.

  **Episode survives a restart.** `KeepAlive` is true with a 30s
  `ThrottleInterval` and the poller's first tick lands 1s after boot, so a
  crash-looping resident would never accumulate a streak and would stay silent
  forever while fully dark. The likelier variant is an operator running
  `launchctl kickstart -k` because "sentinel looks stuck" — which under
  in-memory-only state buys another 12 minutes of silence at exactly the moment
  someone is looking. The episode start is therefore persisted (single-writer
  sidecar file, see `resolve_state_path/2`) and reloaded in `init/1`. Do not
  "simplify" that away.

  ## Known gap (deliberate, tracked)

  A surface that alternates blocked/granted faster than the threshold never
  trips, however dark the resident effectively is. Today's data does not exhibit
  it (all four poller episodes ran unbroken past the threshold), and the cheap
  fix — decaying rather than clearing the episode on a granted tick — would
  eventually emit a finding whose message claims a contiguous outage that never
  happened. A partial-starvation ratio needs its own independently-named
  counter and its own message; it is not folded in here.
  """

  alias UnitaresSentinel.{AtomicWrite, CycleState, Findings, LeaseAdvisory}

  require Logger

  # 12 minutes. Chosen against the live cadences (30s poller, 300s emitter) so
  # both residents alert on comparable wall clock — NOT because they starve
  # together. They do not: on 2026-07-31 the emitter acquired cleanly at
  # 13:43-13:53 and 15:11-15:31 while the poller was blocked throughout, on a
  # different surface held by a different holder.
  @default_alert_after_seconds 720
  @max_backoff_multiplier 16

  # `finding_type`, not `type`. The `_FINDING_TYPE_SUFFIX = "_finding"` gate at
  # `src/http_api.py:2655` is checked against `payload["type"]`, which
  # `Findings.finding_body/2` hardcodes to `"sentinel_finding"` — and keeping it
  # there is load-bearing, because `sentinel_finding` is in
  # `_SENTINEL_FINDING_EVENT_TYPES` (`http_api.py:1819`) so the finding reaches
  # `audit.events` and the backlog endpoint. The kind-within-channel rides in
  # the ungated `finding_type`, matching the `sentinel_self_pause` precedent.
  @finding_type "sentinel_lease_starved"
  @cleared_finding_type "sentinel_lease_starvation_cleared"

  # `_SENTINEL_BACKLOG_DEFAULT_SEVERITIES = {"high", "critical"}`
  # (`src/http_api.py:1822`). Anything below `high` does not appear in the
  # operator's default "what did I miss across restarts?" query — which is
  # precisely the query this incident should have answered. Forced, not stylistic.
  @starved_severity "high"

  @default_lease_plane_base_url "http://127.0.0.1:8788"

  @typedoc """
  The `lease_blocked_*` slice of a resident GenServer's state.

  Namespaced so the tracker can be merged straight into either GenServer's state
  map and updated with `%{state | ...}` without either side knowing the other's
  shape.
  """
  @type tracker :: %{
          required(:lease_blocked_resident) => String.t(),
          required(:lease_blocked_surface_id) => String.t(),
          required(:lease_blocked_alert_after_seconds) => pos_integer(),
          required(:lease_blocked_state_path) => Path.t() | nil,
          required(:lease_blocked_streak) => non_neg_integer(),
          required(:lease_blocked_since) => DateTime.t() | nil,
          required(:lease_blocked_last_blocked_at) => DateTime.t() | nil,
          required(:lease_blocked_last_conflict) => map() | nil,
          required(:lease_blocked_outcome_counts) => %{atom() => pos_integer()},
          required(:lease_blocked_last_emitted_multiple) => non_neg_integer(),
          optional(any()) => any()
        }

  @doc """
  Build the tracker fields for a resident, resuming any persisted episode.

  Options:
    * `:resident` (required) — human name used in the finding, e.g. `"ForcedReleasePoller"`
    * `:surface_id` — defaults to `LeaseAdvisory.cycle_surface_id/0`
    * `:alert_after_seconds` — overrides app env / the 720s default
    * `:state_path` — explicit sidecar path; `false` disables persistence
      entirely (tests), `nil` / omitted derives it from the Sentinel state file
    * `:now` — injected clock for tests
  """
  @spec new(keyword()) :: tracker()
  def new(opts) do
    resident = Keyword.fetch!(opts, :resident)
    surface_id = Keyword.get(opts, :surface_id) || LeaseAdvisory.cycle_surface_id()
    alert_after_seconds = resolve_alert_after_seconds(opts)
    state_path = resolve_state_path(Keyword.get(opts, :state_path, :derive), surface_id)
    now = Keyword.get(opts, :now) || DateTime.utc_now()

    base = %{
      lease_blocked_resident: resident,
      lease_blocked_surface_id: surface_id,
      lease_blocked_alert_after_seconds: alert_after_seconds,
      lease_blocked_state_path: state_path,
      lease_blocked_streak: 0,
      lease_blocked_since: nil,
      lease_blocked_last_blocked_at: nil,
      lease_blocked_last_conflict: nil,
      lease_blocked_outcome_counts: %{},
      lease_blocked_last_emitted_multiple: 0
    }

    case load_episode(state_path, alert_after_seconds, now) do
      nil -> base
      episode -> Map.merge(base, episode)
    end
  end

  @doc """
  Record one lease-enforcement-blocked tick.

  Stamps the episode start on the 0 -> 1 transition, tallies the
  pre-enforcement outcome, and keeps the last *named* blocking lease sticky for
  the whole episode.
  """
  @spec record_blocked(tracker(), map(), DateTime.t() | nil) :: tracker()
  def record_blocked(tracker, scope, now \\ nil) do
    now = now || DateTime.utc_now()
    conflict = scope_conflict(scope)

    tracker =
      %{
        tracker
        | lease_blocked_streak: tracker.lease_blocked_streak + 1,
          lease_blocked_since: tracker.lease_blocked_since || now,
          lease_blocked_last_blocked_at: now,
          lease_blocked_surface_id:
            Map.get(conflict, :surface_id) || tracker.lease_blocked_surface_id,
          lease_blocked_outcome_counts:
            bump(tracker.lease_blocked_outcome_counts, blocked_outcome(scope, conflict)),
          lease_blocked_last_conflict:
            sticky_conflict(tracker.lease_blocked_last_conflict, conflict)
      }

    persist(tracker)
    tracker
  end

  @doc """
  Emit a starvation self-finding if the episode has reached an un-reported rung
  of the escalation ladder.

  Options: `:emit_findings?` (default true), `:findings_opts`, `:now`.

  The ladder rung is only marked reported when the POST is confirmed delivered
  (`:accepted` or `:deduped`); a transport failure leaves it due so the next
  tick retries it.
  """
  @spec maybe_emit(tracker(), keyword()) :: tracker()
  def maybe_emit(tracker, opts \\ []) do
    now = Keyword.get(opts, :now) || DateTime.utc_now()
    due = due_multiple(tracker, now)

    cond do
      due <= tracker.lease_blocked_last_emitted_multiple ->
        tracker

      # An operator who turned findings off turned them off. The starvation
      # finding is not exempt; it just means the resident stays silent and the
      # `emit_findings` flag is the thing to look at.
      not Keyword.get(opts, :emit_findings?, true) ->
        tracker

      true ->
        deliver(tracker, finding(tracker, now, due), due, opts)
    end
  end

  @doc """
  Clear the episode after a tick that was NOT lease-blocked.

  Emits one `info` closure finding if the episode had actually alerted —
  without it the operator is left holding several `high`-severity findings on a
  surface that flaps at episode granularity and no way to answer "is it still
  bad?" from the backlog. No-op (and no file I/O) when no episode is open, which
  is the overwhelmingly common case.

  Options: `:emit_findings?` (default true), `:findings_opts`, `:now`.
  """
  @spec clear(tracker(), keyword()) :: tracker()
  def clear(tracker, opts \\ [])

  def clear(%{lease_blocked_since: nil} = tracker, _opts), do: tracker

  def clear(tracker, opts) do
    now = Keyword.get(opts, :now) || DateTime.utc_now()

    if tracker.lease_blocked_last_emitted_multiple > 0 and
         Keyword.get(opts, :emit_findings?, true) do
      Findings.post_finding_result(
        cleared_finding(tracker, now),
        Keyword.get(opts, :findings_opts, [])
      )
    end

    Logger.warning(
      "#{tracker.lease_blocked_resident}: lease starvation CLEARED on " <>
        "#{tracker.lease_blocked_surface_id} after " <>
        "#{format_duration(blocked_seconds(tracker, now))} " <>
        "(#{tracker.lease_blocked_streak} blocked ticks this process)"
    )

    discard_persisted(tracker)
    reset(tracker)
  end

  @doc """
  Pure field reset. Does not emit and does not touch the sidecar file.
  """
  @spec reset(tracker()) :: tracker()
  def reset(tracker) do
    %{
      tracker
      | lease_blocked_streak: 0,
        lease_blocked_since: nil,
        lease_blocked_last_blocked_at: nil,
        lease_blocked_last_conflict: nil,
        lease_blocked_outcome_counts: %{},
        lease_blocked_last_emitted_multiple: 0
    }
  end

  @doc """
  The highest escalation rung currently earned by the episode, or `0`.

  Rungs are 1x, 2x, 4x, 8x, 16x the threshold, then every 16x. Expressed as
  "largest scheduled multiple <= elapsed/threshold" rather than "exact hit"
  precisely so a missed emission can be retried on a later tick without the rung
  having slipped past.
  """
  @spec due_multiple(tracker(), DateTime.t()) :: non_neg_integer()
  def due_multiple(%{lease_blocked_since: nil}, _now), do: 0

  def due_multiple(
        %{lease_blocked_since: since, lease_blocked_alert_after_seconds: threshold},
        now
      )
      when is_integer(threshold) and threshold > 0 do
    ratio = div(max(DateTime.diff(now, since, :second), 0), threshold)

    cond do
      ratio < 1 ->
        0

      ratio >= @max_backoff_multiplier ->
        div(ratio, @max_backoff_multiplier) * @max_backoff_multiplier

      true ->
        largest_power_of_two_at_most(ratio)
    end
  end

  def due_multiple(_tracker, _now), do: 0

  @doc false
  @spec finding(tracker(), DateTime.t(), pos_integer()) :: map()
  def finding(tracker, now, multiple) do
    seconds = blocked_seconds(tracker, now)
    conflict = tracker.lease_blocked_last_conflict || %{}
    blocking_lease_id = Map.get(conflict, :blocking_lease_id)

    %{
      type: @finding_type,
      severity: @starved_severity,
      violation_class: "BEH",
      # Keyed on the SURFACE, not on agent_id. Without this the two residents
      # get distinct fingerprints only by accident — the emitter carries the
      # anchor agent_uuid (`Application.maybe_add_self_agent_id/1`) while the
      # poller, started as a bare module atom, falls back to the literal
      # "sentinel". Two residents on one agent_id would dedup one outage into
      # the other's.
      fingerprint_extra: [tracker.lease_blocked_surface_id],
      change_token: change_token(tracker.lease_blocked_since, multiple),
      summary: starved_summary(tracker, seconds, blocking_lease_id),
      extra:
        %{
          # `handle_checkin_pause/3` sets this as a top-level key, where
          # `finding_body/2` has always silently dropped it. Routed through
          # `:extra` so it actually ships.
          self_observation: true,
          surface_id: tracker.lease_blocked_surface_id,
          resident: tracker.lease_blocked_resident,
          blocked_since: DateTime.to_iso8601(tracker.lease_blocked_since),
          blocked_seconds: seconds,
          blocked_ticks_this_process: tracker.lease_blocked_streak,
          escalation_multiple: multiple,
          alert_after_seconds: tracker.lease_blocked_alert_after_seconds,
          lease_outcome_counts: stringify_counts(tracker.lease_blocked_outcome_counts)
        }
        |> maybe_put(:blocking_lease_id, blocking_lease_id)
        |> maybe_put(:held_by_uuid, Map.get(conflict, :held_by_uuid))
        |> maybe_put(:lease_expires_at, Map.get(conflict, :expires_at))
    }
  end

  @doc false
  @spec cleared_finding(tracker(), DateTime.t()) :: map()
  def cleared_finding(tracker, now) do
    seconds = blocked_seconds(tracker, now)

    %{
      type: @cleared_finding_type,
      severity: "info",
      violation_class: "BEH",
      fingerprint_extra: [tracker.lease_blocked_surface_id],
      change_token: change_token(tracker.lease_blocked_since, "cleared"),
      summary:
        "Sentinel resident #{tracker.lease_blocked_resident} lease starvation CLEARED: surface " <>
          "#{tracker.lease_blocked_surface_id} acquired its lease again after " <>
          "#{format_duration(seconds)} dark (#{tick_phrase(tracker)} observed by this " <>
          "process). Closes the sentinel_lease_starved finding for the episode " <>
          "that started #{DateTime.to_iso8601(tracker.lease_blocked_since)}.",
      extra: %{
        self_observation: true,
        surface_id: tracker.lease_blocked_surface_id,
        resident: tracker.lease_blocked_resident,
        blocked_since: DateTime.to_iso8601(tracker.lease_blocked_since),
        blocked_seconds: seconds,
        blocked_ticks_this_process: tracker.lease_blocked_streak,
        lease_outcome_counts: stringify_counts(tracker.lease_blocked_outcome_counts)
      }
    }
  end

  # ---- emission ----------------------------------------------------------

  defp deliver(tracker, finding, due, opts) do
    case Findings.post_finding_result(finding, Keyword.get(opts, :findings_opts, [])) do
      result when result in [:accepted, :deduped] ->
        tracker = %{tracker | lease_blocked_last_emitted_multiple: due}
        persist(tracker)
        tracker

      {:error, reason} ->
        # Leave the rung un-marked so the very next tick retries it. See the
        # moduledoc: this failure mode is correlated with the condition being
        # reported, so "emit once and hope" loses the alerts that matter most.
        Logger.warning(
          "#{tracker.lease_blocked_resident}: lease-starvation finding POST failed " <>
            "(#{inspect(reason)}) — rung #{due} stays due, retrying next tick"
        )

        tracker
    end
  end

  # ---- summaries ---------------------------------------------------------

  defp starved_summary(tracker, seconds, nil) do
    preamble(tracker, seconds) <>
      " The lease plane reported NO blocking lease at any point in this episode, so there is " <>
      "nothing to force-release: check the lease plane is up on $LEASE_PLANE_BASE_URL " <>
      "(default #{@default_lease_plane_base_url}) and that LEASE_PLANE_BEARER_TOKEN is set for " <>
      "this resident."
  end

  defp starved_summary(tracker, seconds, lease_id) do
    conflict = tracker.lease_blocked_last_conflict || %{}

    held_by =
      case Map.get(conflict, :held_by_uuid) do
        uuid when is_binary(uuid) and uuid != "" -> " held_by=#{uuid}"
        _ -> ""
      end

    preamble(tracker, seconds) <>
      " Most recent blocking lease_id=#{lease_id}#{held_by}. If that holder is an immortal lease " <>
      "(holder_pid NULL, auto-renewing, expires_at never in the past), clear it: " <>
      ~s|POST $LEASE_PLANE_BASE_URL/v1/lease/force-release {"lease_id": "#{lease_id}"} | <>
      "(default base #{@default_lease_plane_base_url})."
  end

  defp preamble(tracker, seconds) do
    "Sentinel resident #{tracker.lease_blocked_resident} is LEASE-STARVED: surface " <>
      "#{tracker.lease_blocked_surface_id} has been refused by lease enforcement for " <>
      "#{format_duration(seconds)} (since #{DateTime.to_iso8601(tracker.lease_blocked_since)}; " <>
      "#{tick_phrase(tracker)} observed by this process; " <>
      "#{outcome_phrase(tracker)}). This resident is doing NO governance work while its OS " <>
      "process, launchd job and liveness checks all read healthy."
  end

  # The streak is honestly per-process: a resident restarted mid-episode resumes
  # the episode (and its ladder) but counts its own ticks from zero. Say
  # "observed by this process" rather than implying a total.
  defp tick_phrase(%{lease_blocked_streak: 1}), do: "1 blocked tick"
  defp tick_phrase(%{lease_blocked_streak: n}), do: "#{n} blocked ticks"

  defp outcome_phrase(%{lease_blocked_outcome_counts: counts}) when map_size(counts) == 0,
    do: "outcomes: none recorded"

  defp outcome_phrase(%{lease_blocked_outcome_counts: counts}) do
    rendered =
      counts
      |> Enum.sort_by(fn {outcome, count} -> {-count, to_string(outcome)} end)
      |> Enum.map_join(", ", fn {outcome, count} -> "#{outcome}=#{count}" end)

    "outcomes: " <> rendered
  end

  @doc false
  @spec format_duration(non_neg_integer()) :: String.t()
  def format_duration(seconds) when seconds < 60, do: "#{seconds}s"
  def format_duration(seconds) when seconds < 3600, do: "#{div(seconds, 60)}m"

  def format_duration(seconds),
    do: "#{div(seconds, 3600)}h#{div(rem(seconds, 3600), 60)}m"

  # The blocking lease id is deliberately NOT part of the token. A genuinely
  # contended surface with per-tick holder churn would otherwise emit on every
  # tick, reintroducing exactly the spam this ladder exists to avoid. The
  # re-emissions pick up the CURRENT holder anyway, because the summary is
  # rendered at emit time.
  defp change_token(%DateTime{} = since, suffix),
    do: "#{DateTime.to_iso8601(since)}|#{suffix}"

  # ---- persistence -------------------------------------------------------
  #
  # Single-writer sidecar, one file per surface, derived from the Sentinel state
  # file path so no new config key is introduced. NOT the shared
  # `.sentinel_state.beam` shadow file: that one is load-modify-save and is
  # already written by ForcedReleasePoller's cursor advance, so two residents
  # writing starvation state into it could interleave and clobber the cursor.
  # Per-surface files keep every writer alone with its own file.

  defp resolve_state_path(false, _surface_id), do: nil
  defp resolve_state_path(path, _surface_id) when is_binary(path), do: path
  defp resolve_state_path(_derive, surface_id), do: derive_state_path(surface_id)

  defp derive_state_path(surface_id) do
    CycleState.resolve_canonical_path() <> ".lease_starvation." <> slug(surface_id)
  rescue
    # STATE_FILE unset (tests, ad-hoc runs): persistence is simply off. Never a
    # reason to fail a tick.
    _ -> nil
  end

  defp slug(surface_id), do: String.replace(surface_id, ~r/[^A-Za-z0-9]+/, "_")

  defp persist(%{lease_blocked_state_path: nil}), do: :ok

  defp persist(tracker) do
    payload = %{
      "surface_id" => tracker.lease_blocked_surface_id,
      "resident" => tracker.lease_blocked_resident,
      "blocked_since" => DateTime.to_iso8601(tracker.lease_blocked_since),
      "last_blocked_at" => DateTime.to_iso8601(tracker.lease_blocked_last_blocked_at),
      "last_emitted_multiple" => tracker.lease_blocked_last_emitted_multiple
    }

    AtomicWrite.write(tracker.lease_blocked_state_path, Jason.encode!(payload))
    :ok
  rescue
    e ->
      Logger.debug("LeaseStarvation.persist failed: #{inspect(e)}")
      :ok
  end

  defp discard_persisted(%{lease_blocked_state_path: nil}), do: :ok

  defp discard_persisted(tracker) do
    _ = File.rm(tracker.lease_blocked_state_path)
    :ok
  end

  # Resume an episode only if the process was down for less than the alert
  # threshold. A longer gap means a fresh episode would reach the threshold in
  # the same time anyway, and resuming a days-old file would make the very first
  # blocked tick claim a multi-day outage that never happened.
  defp load_episode(nil, _alert_after_seconds, _now), do: nil

  defp load_episode(path, alert_after_seconds, now) do
    with {:ok, raw} <- File.read(path),
         {:ok, %{} = decoded} <- Jason.decode(raw),
         {:ok, since, _} <- parse_datetime(Map.get(decoded, "blocked_since")),
         {:ok, last_blocked_at, _} <- parse_datetime(Map.get(decoded, "last_blocked_at")),
         true <- DateTime.diff(now, last_blocked_at, :second) <= alert_after_seconds do
      %{
        lease_blocked_since: since,
        lease_blocked_last_blocked_at: last_blocked_at,
        lease_blocked_last_emitted_multiple:
          non_neg_integer(Map.get(decoded, "last_emitted_multiple"))
      }
    else
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp parse_datetime(value) when is_binary(value), do: DateTime.from_iso8601(value)
  defp parse_datetime(_value), do: :error

  defp non_neg_integer(value) when is_integer(value) and value >= 0, do: value
  defp non_neg_integer(_value), do: 0

  # ---- small helpers -----------------------------------------------------

  defp resolve_alert_after_seconds(opts) do
    configured =
      Keyword.get(opts, :alert_after_seconds) ||
        Application.get_env(
          :unitares_sentinel,
          :lease_blocked_alert_after_seconds,
          @default_alert_after_seconds
        )

    if is_integer(configured) and configured > 0,
      do: configured,
      else: @default_alert_after_seconds
  end

  defp scope_conflict(scope) when is_map(scope), do: Map.get(scope, :conflict) || %{}
  defp scope_conflict(_scope), do: %{}

  # `:enforcement_blocked` is a conflation; `enforce_scope/3` preserves the
  # pre-enforcement outcome in the conflict so the tally says something real.
  defp blocked_outcome(scope, conflict) do
    Map.get(conflict, :blocked_outcome) || Map.get(scope, :outcome) || :unknown
  end

  defp bump(counts, outcome), do: Map.update(counts, outcome, 1, &(&1 + 1))

  defp sticky_conflict(previous, conflict) do
    case Map.get(conflict, :blocking_lease_id) do
      lease_id when is_binary(lease_id) and lease_id != "" ->
        Map.take(conflict, [:blocking_lease_id, :held_by_uuid, :expires_at])

      _ ->
        previous
    end
  end

  defp blocked_seconds(%{lease_blocked_since: nil}, _now), do: 0

  defp blocked_seconds(%{lease_blocked_since: since}, now),
    do: max(DateTime.diff(now, since, :second), 0)

  defp stringify_counts(counts), do: Map.new(counts, fn {k, v} -> {to_string(k), v} end)

  defp largest_power_of_two_at_most(n), do: walk_power_of_two(1, n)

  defp walk_power_of_two(acc, n) when acc * 2 <= n, do: walk_power_of_two(acc * 2, n)
  defp walk_power_of_two(acc, _n), do: acc

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)
end
