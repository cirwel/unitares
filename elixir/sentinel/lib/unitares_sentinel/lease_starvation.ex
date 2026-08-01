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

  alias UnitaresSentinel.{AtomicWrite, CycleState, Findings}

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
  @max_closure_retries 5

  # Sidecar schema. Bumped from the implicit v1 (no key) when the episode gained
  # an explicit `"state"`: a v1 file cannot express "closing", so reading one as
  # if it could would be the same misinterpretation this version exists to fix.
  # `episode_state/1` therefore matches versions EXACTLY and treats anything it
  # does not know as unreadable — degrade to "no resumed episode", never crash,
  # never guess. A future v3 adds a clause; it does not widen a comparison.
  @schema_version 2

  # `finding_type`, not `type`. The `_FINDING_TYPE_SUFFIX = "_finding"` gate at
  # `src/http_api.py:2655` is checked against `payload["type"]`, which
  # `Findings.finding_body/2` hardcodes to `"sentinel_finding"` — and keeping it
  # there is load-bearing, because `sentinel_finding` is in
  # `_SENTINEL_FINDING_EVENT_TYPES` (`http_api.py:1821`) so the finding reaches
  # `audit.events` and the backlog endpoint. The kind-within-channel rides in
  # the ungated `finding_type`, matching the `sentinel_self_pause` precedent.
  @finding_type "sentinel_lease_starved"
  @cleared_finding_type "sentinel_lease_starvation_cleared"

  # `_SENTINEL_BACKLOG_DEFAULT_SEVERITIES = {"high", "critical"}`
  # (`src/http_api.py:1824`). Anything below `high` does not appear in the
  # operator's default "what did I miss across restarts?" query — which is
  # precisely the query this incident should have answered. Forced, not stylistic.
  @starved_severity "high"

  @default_lease_plane_base_url "http://127.0.0.1:8788"

  # The conflict keys that survive into the sidecar and back. Whitelisted rather
  # than round-tripped wholesale so a schema change on the lease plane cannot
  # smuggle unexpected atoms through `String.to_existing_atom/1`-shaped code.
  @persisted_conflict_keys [:blocking_lease_id, :held_by_uuid, :expires_at]

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
      lease_blocked_last_emitted_multiple: 0,
      lease_blocked_pending_closure: nil
    }

    case load_episode(state_path, surface_id, resident, alert_after_seconds, now) do
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
    * Actually retiring a finding is an operator action:
      `POST /v1/sentinel/adjudicate {fingerprint, status, reason?}`
      (`src/http_api.py:3100`), operator-credential gated and idempotent per
      fingerprint.

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
        closure_episode(tracker, now)
      end

    Logger.warning(
      "#{tracker.lease_blocked_resident}: lease starvation CLEARED on " <>
        "#{tracker.lease_blocked_surface_id} after " <>
        "#{format_duration(blocked_seconds(tracker, now))} " <>
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
  # unconditionally after `owe_closure/2`. `owe_closure(tracker, nil)` is a
  # pass-through, so an unconditional pipe fires `retry_pending_closure/2`
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
  def cleared_finding(tracker, now), do: closure_finding(closure_episode(tracker, now))

  # Freeze an ending episode into the minimal set of facts the closure finding
  # is a pure function of. Everything downstream — the in-memory debt, the
  # sidecar record, the retried POST — works from THIS, so there is exactly one
  # description of the episode and nothing to keep in sync.
  @spec closure_episode(tracker(), DateTime.t()) :: closure_episode()
  defp closure_episode(tracker, now) do
    %{
      surface_id: tracker.lease_blocked_surface_id,
      resident: tracker.lease_blocked_resident,
      since: tracker.lease_blocked_since,
      ended_at: now,
      ticks: tracker.lease_blocked_streak,
      counts: stringify_counts(tracker.lease_blocked_outcome_counts)
    }
  end

  @spec closure_finding(closure_episode()) :: map()
  defp closure_finding(episode) do
    # `ended_at - since`, never `now - since`: the duration belongs to the
    # episode, not to whenever the POST finally lands.
    seconds = max(DateTime.diff(episode.ended_at, episode.since, :second), 0)

    %{
      type: @cleared_finding_type,
      severity: "info",
      violation_class: "BEH",
      fingerprint_extra: [episode.surface_id],
      change_token: change_token(episode.since, "cleared"),
      summary:
        "Sentinel resident #{episode.resident} lease starvation CLEARED: surface " <>
          "#{episode.surface_id} acquired its lease again after " <>
          "#{format_duration(seconds)} dark (#{tick_phrase(episode.ticks)} observed by this " <>
          "process). Closes the sentinel_lease_starved finding for the episode " <>
          "that started #{DateTime.to_iso8601(episode.since)}.",
      extra: %{
        self_observation: true,
        surface_id: episode.surface_id,
        resident: episode.resident,
        blocked_since: DateTime.to_iso8601(episode.since),
        blocked_seconds: seconds,
        blocked_ticks_this_process: episode.ticks,
        lease_outcome_counts: episode.counts
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
  defp owe_closure(tracker, nil), do: tracker

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
        "findings stay in the backlog and need /v1/sentinel/adjudicate)"
    )

    forget_closure(tracker)
  end

  defp retry_pending_closure(%{lease_blocked_pending_closure: pending} = tracker, opts) do
    if Keyword.get(opts, :emit_findings?, true) do
      case post_closure(tracker, closure_finding(pending.episode), opts) do
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
    discard_persisted(tracker)
    tracker
  end

  defp sync_sidecar(tracker) do
    persist(tracker)
    tracker
  end

  # ---- summaries ---------------------------------------------------------

  # Scoped to this PROCESS, not to the lease plane. The earlier wording ("The
  # lease plane reported NO blocking lease at any point in this episode") is an
  # assertion about the whole episode that this process is not in a position to
  # make: an episode resumed from the sidecar after a restart carries its clock
  # and its ladder, and — since a review — its sticky blocker, but a sidecar that
  # is absent, unreadable or from an older schema restores no conflict at all.
  # If the first blocked tick after such a restart is one of the
  # `service_unavailable` bursts and a rung is already due, the old sentence told
  # the operator there was nothing to force-release while an immortal lease was
  # in fact holding the surface — the "worse than saying nothing" outcome the
  # sticky-blocker design exists to prevent. `tick_phrase/1` was already careful
  # this way; this sentence now is too.
  defp starved_summary(tracker, seconds, nil) do
    preamble(tracker, seconds) <>
      " No blocking lease was observed by THIS PROCESS during the episode (a restart mid-episode " <>
      "resumes the clock, not necessarily the observations that preceded it), so this resident " <>
      "has nothing to force-release by name: check the lease plane is up on " <>
      "$LEASE_PLANE_BASE_URL (default #{@default_lease_plane_base_url}), that " <>
      "LEASE_PLANE_BEARER_TOKEN is set for this resident, and query the plane directly for a " <>
      "lease on this surface before concluding there is none."
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
      "#{tick_phrase(tracker.lease_blocked_streak)} observed by this process; " <>
      "#{outcome_phrase(tracker)}). This resident is doing NO governance work while its OS " <>
      "process, launchd job and liveness checks all read healthy."
  end

  # The streak is honestly per-process: a resident restarted mid-episode resumes
  # the episode (and its ladder) but counts its own ticks from zero. Say
  # "observed by this process" rather than implying a total. Takes the count and
  # not a tracker, because a closure re-rendered from a persisted debt reports
  # the count the ENDED episode had, not this process's.
  defp tick_phrase(1), do: "1 blocked tick"
  defp tick_phrase(n), do: "#{n} blocked ticks"

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
    payload =
      %{
        "schema_version" => @schema_version,
        "surface_id" => tracker.lease_blocked_surface_id,
        "resident" => tracker.lease_blocked_resident
      }
      |> Map.merge(episode_payload(tracker))
      # An owed closure is orthogonal to the episode phase: it may be the ONLY
      # thing in the file ("closing"), or it may ride alongside a newer episode
      # that opened while it was still undelivered ("open"). Both are real and
      # both used to be unrepresentable, which is how one got destroyed by the
      # other.
      |> maybe_put("owed_closure", encode_owed_closure(tracker.lease_blocked_pending_closure))

    AtomicWrite.write(tracker.lease_blocked_state_path, Jason.encode!(payload))
    :ok
  rescue
    e ->
      Logger.debug("LeaseStarvation.persist failed: #{inspect(e)}")
      :ok
  end

  # "closing" carries no live episode fields on purpose: there is no clock still
  # running, and a reader that finds none cannot accidentally restart one.
  defp episode_payload(%{lease_blocked_since: nil}), do: %{"state" => "closing"}

  defp episode_payload(tracker) do
    %{
      "state" => "open",
      "blocked_since" => DateTime.to_iso8601(tracker.lease_blocked_since),
      "last_blocked_at" => DateTime.to_iso8601(tracker.lease_blocked_last_blocked_at),
      "last_emitted_multiple" => tracker.lease_blocked_last_emitted_multiple
    }
    # The sticky blocker rides along. Without it a resumed episode started with
    # `last_conflict: nil`, so a restart mid-episode could make the very next
    # finding assert that the lease plane had named no blocking lease — while
    # an immortal lease held the surface. The episode's most load-bearing fact
    # is the one thing the sidecar used to drop.
    |> maybe_put("last_conflict", encode_conflict(tracker.lease_blocked_last_conflict))
  end

  # `blocked_seconds` is deliberately NOT written: it is `ended_at - since`, and
  # a stored copy is one more thing that can disagree with the pair it is derived
  # from. Same reason `surface_id` / `resident` are read back off the top level.
  defp encode_owed_closure(nil), do: nil

  defp encode_owed_closure(%{episode: episode, attempts: attempts}) do
    %{
      "blocked_since" => DateTime.to_iso8601(episode.since),
      "ended_at" => DateTime.to_iso8601(episode.ended_at),
      "blocked_ticks" => episode.ticks,
      "outcome_counts" => episode.counts,
      "attempts" => attempts
    }
  end

  defp discard_persisted(%{lease_blocked_state_path: nil}), do: :ok

  defp discard_persisted(tracker) do
    _ = File.rm(tracker.lease_blocked_state_path)
    :ok
  end

  # Read the sidecar and restore whatever it says is true: an open episode, an
  # owed closure, both, or neither. Every failure mode — missing file, bad JSON,
  # another writer, a schema version this build does not know, a stale record —
  # lands on "nothing restored". Never crashes: `init/1` calls this.
  defp load_episode(nil, _surface_id, _resident, _alert_after_seconds, _now), do: nil

  defp load_episode(path, surface_id, resident, alert_after_seconds, now) do
    with {:ok, raw} <- File.read(path),
         {:ok, %{} = decoded} <- Jason.decode(raw),
         true <- same_writer?(decoded, surface_id, resident) do
      decoded
      |> load_state(episode_state(decoded), surface_id, resident, alert_after_seconds, now)
      |> presence()
    else
      _ -> nil
    end
  rescue
    _ -> nil
  end

  # A HEAD-era file and a current-era file both exist on real disks, and neither
  # carries a version key. Reading them as "open" is not a guess: it is exactly
  # what they meant, because before the explicit state existed the sidecar was
  # deleted unconditionally the moment an episode ended, so every v1 file on
  # disk IS an in-progress episode. (The narrow exception is a file left by the
  # intermediate build that retained a sidecar on a lost closure without marking
  # it — never released, so at most one dev tree, and the resume window bounds
  # even that to one threshold of over-patience rather than a fabricated multi-
  # day outage.)
  defp episode_state(%{"schema_version" => @schema_version} = decoded),
    do: normalize_state(Map.get(decoded, "state"))

  defp episode_state(decoded) when not is_map_key(decoded, "schema_version"), do: "open"

  # A version from the future. Do not try to interpret its fields.
  defp episode_state(_decoded), do: nil

  defp normalize_state(state) when state in ["open", "closing"], do: state
  defp normalize_state(_state), do: nil

  defp load_state(decoded, "open", surface_id, resident, alert_after_seconds, now) do
    decoded
    |> resume_open_episode(alert_after_seconds, now)
    |> merge_owed_closure(decoded, surface_id, resident, alert_after_seconds, now)
  end

  # The whole point of the state field: a closed episode is NOT resumed. Its
  # clock does not restart, its ladder position is gone, and a later blocked tick
  # opens a fresh episode at rung 1 with an honest duration. Only the debt
  # survives.
  defp load_state(decoded, "closing", surface_id, resident, alert_after_seconds, now),
    do: merge_owed_closure(%{}, decoded, surface_id, resident, alert_after_seconds, now)

  defp load_state(_decoded, _unknown_state, _surface_id, _resident, _alert_after_seconds, _now),
    do: %{}

  # Resume an episode only if the process was down for less than the alert
  # threshold. A longer gap means a fresh episode would reach the threshold in
  # the same time anyway, and resuming a days-old file would make the very first
  # blocked tick claim a multi-day outage that never happened. A stale episode
  # does not veto a still-live owed closure: they are independent records.
  defp resume_open_episode(decoded, alert_after_seconds, now) do
    with {:ok, since, _} <- parse_datetime(Map.get(decoded, "blocked_since")),
         {:ok, last_blocked_at, _} <- parse_datetime(Map.get(decoded, "last_blocked_at")),
         true <- DateTime.diff(now, last_blocked_at, :second) <= alert_after_seconds do
      %{
        lease_blocked_since: since,
        lease_blocked_last_blocked_at: last_blocked_at,
        lease_blocked_last_conflict: decode_conflict(Map.get(decoded, "last_conflict")),
        lease_blocked_last_emitted_multiple:
          non_neg_integer(Map.get(decoded, "last_emitted_multiple"))
      }
    else
      _ -> %{}
    end
  end

  defp merge_owed_closure(acc, decoded, surface_id, resident, alert_after_seconds, now) do
    case decode_owed_closure(
           Map.get(decoded, "owed_closure"),
           surface_id,
           resident,
           alert_after_seconds,
           now
         ) do
      nil -> acc
      pending -> Map.put(acc, :lease_blocked_pending_closure, pending)
    end
  end

  # Both bounds that survive a restart live here: the attempt budget (so the
  # cap is not reset by the crash it is most likely to follow) and staleness
  # against `ended_at` (so a sidecar found after a long downtime is not posted
  # as if the surface had just recovered).
  defp decode_owed_closure(raw, surface_id, resident, alert_after_seconds, now)
       when is_map(raw) do
    with {:ok, since, _} <- parse_datetime(Map.get(raw, "blocked_since")),
         {:ok, ended_at, _} <- parse_datetime(Map.get(raw, "ended_at")),
         attempts when attempts < @max_closure_retries <-
           non_neg_integer(Map.get(raw, "attempts")),
         true <- DateTime.diff(now, ended_at, :second) <= alert_after_seconds,
         true <- DateTime.compare(ended_at, since) != :lt do
      %{
        episode: %{
          surface_id: surface_id,
          resident: resident,
          since: since,
          ended_at: ended_at,
          ticks: non_neg_integer(Map.get(raw, "blocked_ticks")),
          counts: decode_counts(Map.get(raw, "outcome_counts"))
        },
        attempts: attempts
      }
    else
      _ -> nil
    end
  end

  defp decode_owed_closure(_raw, _surface_id, _resident, _alert_after_seconds, _now), do: nil

  # Left as string keys deliberately. These are display-only (the finding
  # stringifies them anyway) and never merge back into
  # `:lease_blocked_outcome_counts`, so a sidecar cannot seed the atom table.
  defp decode_counts(raw) when is_map(raw) do
    for {outcome, count} <- raw,
        is_binary(outcome),
        is_integer(count),
        count > 0,
        into: %{},
        do: {outcome, count}
  end

  defp decode_counts(_raw), do: %{}

  defp presence(loaded) when map_size(loaded) == 0, do: nil
  defp presence(loaded), do: loaded

  # `persist/1` has always written these two keys and nothing ever read them.
  # They are the natural guard, because `slug/1` collapses every non-alphanumeric
  # run to "_": `resident:/sentinel_cycle` and `resident.sentinel-cycle` derive
  # the SAME filename, and resuming another writer's episode would put one
  # resident's outage clock on the other's ladder. Fails closed — a mismatch
  # starts a fresh episode, which is at worst one threshold of extra patience and
  # never a fabricated outage.
  defp same_writer?(decoded, surface_id, resident) do
    Map.get(decoded, "surface_id") == surface_id and Map.get(decoded, "resident") == resident
  end

  defp encode_conflict(conflict) when is_map(conflict) and map_size(conflict) > 0,
    do: Map.new(conflict, fn {key, value} -> {Atom.to_string(key), value} end)

  defp encode_conflict(_conflict), do: nil

  # Only a conflict that names a lease is worth restoring: `sticky_conflict/2`
  # only ever stores one, and `finding/3` selects the force-release summary on
  # exactly that key.
  defp decode_conflict(%{"blocking_lease_id" => lease_id} = raw)
       when is_binary(lease_id) and lease_id != "" do
    Enum.reduce(@persisted_conflict_keys, %{}, fn key, acc ->
      case Map.get(raw, Atom.to_string(key)) do
        value when is_binary(value) and value != "" -> Map.put(acc, key, value)
        _ -> acc
      end
    end)
  end

  defp decode_conflict(_raw), do: nil

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
        Map.take(conflict, @persisted_conflict_keys)

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
