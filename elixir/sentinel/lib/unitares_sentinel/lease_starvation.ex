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
  log accumulated those warnings by the thousand and not one alert was raised:
  launchctl reported the job up, the OS process was alive, no crash, no
  supervisor restart. Every liveness signal read healthy while the residents did
  zero governance work.

  Measured on the BEAM run current when this was written, `resident:/sentinel_cycle`
  was refused on **1,742 of 2,012** tick attempts, across four episodes whose
  blocked streaks were 918, 577, 145 and 102 ticks — roughly 14 of the preceding
  16.5 hours dark. Only the 145 was ever noticed, and only because a human went
  looking.

  (An earlier draft of this comment cited a single "5,703 consecutive" figure.
  That was a whole-file `grep -c` spanning several BEAM runs and both GenServers,
  not one episode. The per-episode streaks above are the real shape, and they are
  what the escalation ladder below is sized against — in particular the 16x floor,
  which exists because a 7h41m episode must keep reminding.)

  Root cause on the other side of the wire was an "immortal lease" — an acquire
  that succeeded server-side but timed out client-side, stranding a lease whose
  `LeaseHolder` GenServer auto-renews forever (`holder_pid` NULL, `expires_at`
  never in the past, so the Reaper never sweeps it).

  **The insight that makes self-reporting possible at all**: the findings POST
  goes to `/api/findings` on gov-MCP (:8767), a different process on a different
  port from the lease plane (:8788), and `http_record_finding`
  (`src/http_api.py:2634`) gates on `_check_http_auth` only — there is no lease
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
  alerts are precisely the ones a fire-and-forget design would drop. The same
  reasoning applies with even more force to the CLOSURE finding, and for the
  same reason: a jetsam-restarted gov-MCP is itself a plausible explanation for
  why the lease plane suddenly freed up, so the moment a closure is emitted is
  correlated with the moment a POST is most likely to be lost. `clear/2`
  therefore retries a lost closure (bounded) instead of destroying the episode.

  **The episode's phase is explicit and persisted, never inferred.** "The
  episode ended but its closure is still owed" used to be encoded as the
  conjunction of two independent halves — the sidecar file still existing, and
  an in-memory `:lease_blocked_pending_closure`. Two halves that *can* disagree
  *did*, both ways, and both were live HIGH defects:

    * a later, NON-alerting episode reached the settle path with "this episode
      owes nothing", which was implemented as "no closure is owed at all". One
      `service_unavailable` blip far under the threshold (there are 62 of them
      interleaved in the live data, so this is ordinary traffic) silently
      destroyed the previous episode's closure debt with zero POSTs attempted,
      leaving its `high` finding open in the backlog forever; and
    * a retained sidecar carried no "episode ended" marker, so a restart — the
      `kickstart -k` / jetsam trigger this persistence exists for — resumed a
      CLOSED episode: it kept the old `blocked_since`, skipped ladder rungs, and
      emitted a finding claiming a contiguous outage across an interval during
      which the surface had been GRANTED. That is precisely the fabricated
      outage the "Known gap" section below commits to never producing.

  The sidecar is therefore the single source of truth for both facts. It carries
  a `"schema_version"` and an explicit `"state"` — `"open"` (starving now) vs
  `"closing"` (episode over, closure owed) — plus an optional `"owed_closure"`
  record that may ride along in EITHER state, because a new episode can
  legitimately open while a previous episode's closure is still undelivered.
  `persist/1`, `load_episode/5` and `clear/2` branch on that field instead of
  inferring the phase, and `sync_sidecar/1` is the single function that decides
  what the file says. Because the debt itself is persisted, a resident restarted
  mid-retry really does deliver the closure on its next granted tick, with the
  duration frozen at the moment the episode ended — that used to be a comment
  describing behavior the code did not have.

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
  someone is looking. The episode start, its ladder position AND its sticky
  blocker are therefore persisted (single-writer sidecar file, see
  `resolve_state_path/2`) and reloaded in `init/1`. Do not "simplify" that away.

  ## Known gap (deliberate, tracked)

  A surface that alternates blocked/granted faster than the threshold never
  trips, however dark the resident effectively is. Today's data does not exhibit
  it (all four poller episodes ran unbroken past the threshold), and the cheap
  fix — decaying rather than clearing the episode on a granted tick — would
  eventually emit a finding whose message claims a contiguous outage that never
  happened. A partial-starvation ratio needs its own independently-named
  counter and its own message; it is not folded in here.
  """

  alias UnitaresSentinel.Findings
  alias UnitaresSentinel.LeaseStarvation.Sidecar

  require Logger

  # 12 minutes. Chosen against the live cadences (30s poller, 300s emitter) so
  # both residents alert on comparable wall clock — NOT because they starve
  # together. They do not: on 2026-07-31 the emitter acquired cleanly at
  # 13:43-13:53 and 15:11-15:31 while the poller was blocked throughout, on a
  # different surface held by a different holder.
  @default_alert_after_seconds 720
  @max_backoff_multiplier 16

  # Delivery attempts for a closure finding before the episode is dropped. See
  # `retry_pending_closure/2` for why the retry terminates.
  @max_closure_retries Sidecar.max_closure_retries()

  # The conflict keys that survive into the sidecar and back. Whitelisted rather
  # than round-tripped wholesale so a schema change on the lease plane cannot
  # smuggle unexpected atoms through `String.to_existing_atom/1`-shaped code.
  @persisted_conflict_keys Sidecar.persisted_conflict_keys()

  @typedoc """
  An episode that has ENDED, frozen at the moment it ended.

  The closure finding is rendered from these facts on demand rather than stored
  pre-rendered, so the copy retried after a restart is identical by construction
  to the one the ending process would have sent — `blocked_seconds` is
  `ended_at - since`, not "now minus since", and cannot drift while the debt
  sits unpaid.
  """
  @type closure_episode :: %{
          required(:surface_id) => String.t(),
          required(:resident) => String.t(),
          required(:since) => DateTime.t(),
          required(:ended_at) => DateTime.t(),
          required(:ticks) => non_neg_integer(),
          required(:counts) => %{String.t() => pos_integer()}
        }

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
          required(:lease_blocked_pending_closure) =>
            %{episode: closure_episode(), attempts: non_neg_integer()} | nil,
          optional(any()) => any()
        }

  @doc """
  Build the tracker fields for a resident, resuming any persisted episode.

  Options:
    * `:resident` (required) — human name used in the finding, e.g. `"ForcedReleasePoller"`
    * `:surface_id` (required) — the lease surface this resident acquires. Must be
      a non-empty string; see `require_surface_id/1` for why it is not defaulted.
    * `:alert_after_seconds` — overrides app env / the 720s default
    * `:state_path` — explicit sidecar path; `false` disables persistence
      entirely (tests), `nil` / omitted derives it from the Sentinel state file
    * `:now` — injected clock for tests
  """
  @spec new(keyword()) :: tracker()
  def new(opts) do
    resident = Keyword.fetch!(opts, :resident)
    surface_id = require_surface_id(Keyword.fetch!(opts, :surface_id))
    alert_after_seconds = resolve_alert_after_seconds(opts)
    state_path = Sidecar.resolve_state_path(Keyword.get(opts, :state_path, :derive), surface_id)
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
      lease_blocked_last_emitted_multiple: 0,
      lease_blocked_pending_closure: nil
    }

    case Sidecar.load_episode(state_path, surface_id, resident, alert_after_seconds, now) do
      nil -> base
      episode -> Map.merge(base, episode)
    end
  end

  # `:surface_id` used to default to `LeaseAdvisory.cycle_surface_id/0` while
  # `:resident` was already `fetch!`-required. A review caught what that
  # asymmetry costs: BOTH the sidecar path (`derive_state_path/1`) and the
  # finding fingerprint (`fingerprint_extra`) are keyed on this string, so two
  # residents that both take the default become two writers on one file whose
  # outages dedup into each other — exactly the two properties the comments on
  # those lines claim are prevented. `Keyword.fetch!` on its own is not enough
  # either: both call sites read the surface out of `:lease_opts`, where a
  # missing key yields `nil` and `fetch!` would happily accept it. A resident
  # with no surface is a misconfiguration; fail loudly at `init/1` rather than
  # silently collide.
  defp require_surface_id(surface_id) when is_binary(surface_id) and surface_id != "",
    do: surface_id

  defp require_surface_id(other) do
    raise ArgumentError,
          "LeaseStarvation requires a non-empty :surface_id (the starvation sidecar path and " <>
            "the finding fingerprint are both keyed on it, so two residents sharing one value " <>
            "collide); got: #{inspect(other)}"
  end

  @doc """
  Stamp a resident's own lease surface onto `lease_opts` unless the caller
  supplied a REAL one.

  Both residents used to do this with `Keyword.put_new/3`, which keys on key
  PRESENCE, not on value: `lease_opts: [surface_id: nil]` sails straight past it,
  reaches `Keyword.fetch!/2` at the `new/1` call site, and hands
  `require_surface_id/1` a `nil` — which raises inside `init/1` and turns a
  mistyped option into a supervisor restart loop. The comments at those call
  sites already claimed the surface was defended; only omission actually was.
  This is the whole defense, so it lives next to the requirement it satisfies
  rather than being re-derived at each caller.
  """
  @spec put_default_surface_id(keyword(), String.t()) :: keyword()
  def put_default_surface_id(lease_opts, default) do
    case Keyword.get(lease_opts, :surface_id) do
      surface_id when is_binary(surface_id) and surface_id != "" -> lease_opts
      _ -> Keyword.put(lease_opts, :surface_id, default)
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

    Sidecar.persist(tracker)
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

  Emits one `info` closure finding if the episode had actually alerted. What
  that closure does and does not buy, precisely (an earlier version of this
  docstring claimed more than the server can deliver):

    * It lands in the live event stream and, via
      `_SENTINEL_FINDING_EVENT_TYPES` (`src/http_api.py:1821`), durably in
      `audit.events`. It is readable at
      `GET /v1/sentinel/backlog?severity=all`.
    * It does NOT retire anything. `_SENTINEL_BACKLOG_DEFAULT_SEVERITIES`
      is `{"high","critical"}` (`src/http_api.py:1824`) and the `severity` query
      param takes `all` or exactly one value — there is no `{high, info}` view —
      so the operator's default backlog query still shows the open
      `sentinel_lease_starved` rows for the episode.
    * Nothing here retires a finding; the backlog shows it until it ages out
      of the query window. (The operator adjudication route that once did was
      removed 2026-09-27.)

  `info` is deliberate anyway: a closure is not an alarm, and promoting it to
  `high` would pollute the very backlog it exists to make readable.

  Delivery is not assumed. A closure lost to a transport error used to take the
  whole episode with it (`discard_persisted/1` ran unconditionally), leaving the
  tracker with no memory that an episode had ever existed — and gov-MCP's
  jetsam-kill 502 window is *correlated* with the lease plane freeing up, so the
  loss lands exactly when closures are emitted. A lost closure is now kept and
  retried on later ticks, bounded by `@max_closure_retries`, and the debt is
  written into the sidecar so a restart mid-retry does not silently forgive it.

  The debt owed by a PREVIOUS episode is settled first and is settled
  independently of whatever this episode owes. That ordering is the fix for a
  HIGH regression: "this episode owes nothing" (`closure == nil`, the normal
  outcome for any episode that never reached the alert threshold) used to route
  straight into an unconditional `forget_episode/1`, so a single
  `service_unavailable` blip between the loss and the retry destroyed the older
  debt with ZERO POSTs attempted. There are 62 such blips in the live data.

  No-op (and no file I/O) when no episode is open and no closure is owed, which
  is the overwhelmingly common case.

  Options: `:emit_findings?` (default true), `:findings_opts`, `:now`.
  """
  @spec clear(tracker(), keyword()) :: tracker()
  def clear(tracker, opts \\ [])

  # Nothing open, nothing owed: the hot path, and it must not touch the disk.
  def clear(%{lease_blocked_since: nil, lease_blocked_pending_closure: nil} = tracker, _opts),
    do: tracker

  # No open episode, but a closure owed by a PREVIOUS episode still has to get
  # its retry — including one restored from the sidecar by `new/1`, which is the
  # only way a debt outlives the process that incurred it.
  def clear(%{lease_blocked_since: nil} = tracker, opts),
    do: tracker |> retry_pending_closure(opts) |> sync_sidecar()

  def clear(tracker, opts) do
    now = Keyword.get(opts, :now) || DateTime.utc_now()

    # Freeze the episode's facts BEFORE resetting. A retry three ticks (or one
    # restart) later must still describe the episode that actually ended, not a
    # duration that kept growing after it did.
    closure =
      if tracker.lease_blocked_last_emitted_multiple > 0 and
           Keyword.get(opts, :emit_findings?, true) do
        Sidecar.closure_episode(tracker, now)
      end

    Logger.warning(
      "#{tracker.lease_blocked_resident}: lease starvation CLEARED on " <>
        "#{tracker.lease_blocked_surface_id} after " <>
        "#{format_duration(Sidecar.blocked_seconds(tracker, now))} " <>
        "(#{tracker.lease_blocked_streak} blocked ticks this process)"
    )

    tracker
    |> reset()
    # An OLDER debt is a separate obligation to a separate episode. Pay it on
    # its own before this episode's closure can supersede it; `closure == nil`
    # must never be read as "nothing is owed by anyone".
    |> retry_pending_closure(opts)
    |> adopt_and_deliver(closure, opts)
    |> sync_sidecar()
  end

  # Adopt THIS episode's closure and give it its first delivery attempt.
  #
  # The second attempt is guarded on `closure != nil` rather than piped
  # unconditionally after `owe_closure/2`. A nil closure owes nothing, so an
  # unconditional pipe fires `retry_pending_closure/2`
  # TWICE in one granted tick whenever this episode never alerted (the normal
  # case) and an older debt is still pending: the identical finding is POSTed
  # twice and two of the five attempts are burned at once. Measured on the
  # blip-interleaved traffic this machinery exists to survive — a single
  # sub-threshold `service_unavailable` tick between episodes, 62 of which
  # appear in the 2026-07-31 log — the retry window collapsed from 5 granted
  # ticks to 3, while doubling POST volume against the already-degraded
  # gov-MCP that caused the loss. It also silently falsified the cap comment
  # below, which promises one attempt per granted tick.
  defp adopt_and_deliver(tracker, nil, _opts), do: tracker

  defp adopt_and_deliver(tracker, closure, opts) do
    tracker
    |> owe_closure(closure)
    |> retry_pending_closure(opts)
  end

  @doc """
  Pure field reset. Does not emit and does not touch the sidecar file.

  Deliberately does NOT touch `:lease_blocked_pending_closure`: an undelivered
  closure is a debt owed for an episode that has already ended, not part of the
  episode's own state, and dropping it here would reintroduce the
  fire-and-forget hole `clear/2` exists to close.
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
  defdelegate finding(tracker, now, multiple), to: Sidecar

  @doc false
  @spec cleared_finding(tracker(), DateTime.t()) :: map()
  defdelegate cleared_finding(tracker, now), to: Sidecar

  @doc false
  @spec format_duration(non_neg_integer()) :: String.t()
  defdelegate format_duration(seconds), to: Sidecar

  # ---- emission ----------------------------------------------------------

  defp deliver(tracker, finding, due, opts) do
    case Findings.post_finding_result(finding, Keyword.get(opts, :findings_opts, [])) do
      result when result in [:accepted, :deduped] ->
        tracker = %{tracker | lease_blocked_last_emitted_multiple: due}
        Sidecar.persist(tracker)
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

  # ---- closure delivery --------------------------------------------------
  #
  # Nothing here may block or crash the calling GenServer: a resident whose
  # governance plane is down must keep ticking. Every path returns a tracker.

  # Adopt an ended episode as the debt. At most one closure is ever owed: a
  # second episode's closure supersedes an older undelivered one, which requires
  # gov-MCP to be down across two whole episodes and, by then, names the outage
  # the operator actually cares about. The older debt has already had its own
  # delivery attempt this tick (see `clear/2`) — superseding is what happens
  # after that attempt fails, not instead of it.
  defp owe_closure(%{lease_blocked_pending_closure: nil} = tracker, episode),
    do: %{tracker | lease_blocked_pending_closure: %{episode: episode, attempts: 0}}

  defp owe_closure(tracker, episode) do
    Logger.warning(
      "#{tracker.lease_blocked_resident}: a newer lease-starvation closure supersedes an " <>
        "undelivered one (gov-MCP has been unreachable across two episodes on " <>
        "#{tracker.lease_blocked_surface_id})"
    )

    %{tracker | lease_blocked_pending_closure: %{episode: episode, attempts: 0}}
  end

  # The common case, and the reason `clear/2`'s no-episode clause stays free of
  # file I/O.
  defp retry_pending_closure(%{lease_blocked_pending_closure: nil} = tracker, _opts), do: tracker

  # Termination, three independent ways, because an unbounded retry against a
  # down gov-MCP would outlive the incident it describes and turn an
  # informational courtesy into a background loop:
  #   1. this cap — 5 attempts, each consuming one granted (non-blocked) tick, so
  #      ~2.5 minutes at the poller's 30s cadence and ~25 minutes at the
  #      emitter's 300s one. The count is PERSISTED, so restarting no longer
  #      resets the budget;
  #   2. staleness — `decode_owed_closure/5` refuses a debt whose episode ended
  #      more than one alert threshold ago, so a sidecar found on disk after a
  #      long downtime is not paid out as news;
  #   3. delivery, which is the expected outcome once gov-MCP is back.
  defp retry_pending_closure(
         %{lease_blocked_pending_closure: %{attempts: attempts}} = tracker,
         _opts
       )
       when attempts >= @max_closure_retries do
    Logger.warning(
      "#{tracker.lease_blocked_resident}: lease-starvation closure undelivered after " <>
        "#{attempts} attempts — dropping it (the open sentinel_lease_starved " <>
        "findings stay in the backlog)"
    )

    forget_closure(tracker)
  end

  defp retry_pending_closure(%{lease_blocked_pending_closure: pending} = tracker, opts) do
    if Keyword.get(opts, :emit_findings?, true) do
      case post_closure(tracker, Sidecar.closure_finding(pending.episode), opts) do
        :delivered ->
          forget_closure(tracker)

        :lost ->
          %{tracker | lease_blocked_pending_closure: %{pending | attempts: pending.attempts + 1}}
      end
    else
      # Findings were switched off between the failure and the retry. Nothing is
      # owed to a plane the operator asked us not to talk to.
      forget_closure(tracker)
    end
  end

  defp post_closure(tracker, closure, opts) do
    case Findings.post_finding_result(closure, Keyword.get(opts, :findings_opts, [])) do
      result when result in [:accepted, :deduped] ->
        :delivered

      {:error, reason} ->
        Logger.warning(
          "#{tracker.lease_blocked_resident}: lease-starvation closure POST failed " <>
            "(#{inspect(reason)}) — keeping the episode, retrying on a later granted tick"
        )

        :lost
    end
  end

  # Drop the debt ONLY. It deliberately does not touch the file: `sync_sidecar/1`
  # owns that, from the tracker's whole state. Coupling "forget the debt" to
  # "delete the file" is what let a nil closure delete an episode that was not
  # its own.
  defp forget_closure(tracker), do: %{tracker | lease_blocked_pending_closure: nil}

  # The one place that decides what is on disk, from the two facts that define
  # the phase. Nothing open and nothing owed means the file has no reason to
  # exist; anything else is written in full, so the file and the tracker cannot
  # describe different episodes.
  defp sync_sidecar(%{lease_blocked_since: nil, lease_blocked_pending_closure: nil} = tracker) do
    Sidecar.discard_persisted(tracker)
    tracker
  end

  defp sync_sidecar(tracker) do
    Sidecar.persist(tracker)
    tracker
  end

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
        Map.take(conflict, @persisted_conflict_keys)

      _ ->
        previous
    end
  end

  defp largest_power_of_two_at_most(n), do: walk_power_of_two(1, n)

  defp walk_power_of_two(acc, n) when acc * 2 <= n, do: walk_power_of_two(acc * 2, n)
  defp walk_power_of_two(acc, _n), do: acc
end
