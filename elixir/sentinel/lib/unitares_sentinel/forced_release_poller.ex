defmodule UnitaresSentinel.ForcedReleasePoller do
  @moduledoc """
  Surface 1 cycle worker plus Surface 2 forced-release findings emission and
  Surface 3 Phase A lease advisory.

  Reads the cursor from `CycleState`, queries `lease_plane.lease_plane_events`
  for all three forced-release alarm classes, builds alarms via
  `ForcedReleasePoller.Logic.build_all_alarms/4`, emits each alarm to
  `/api/findings`, then persists the candidate cursor via `CycleState.save/2`.

  Cursor persistence happens after the emit loop in the GenServer path.
  This restores the Python ordering at `agents/sentinel/agent.py:681-699`
  and closes the Surface-1-only pre-emit-persist gap called out by v0.1.3
  §B4. The public `tick/1` API remains an alarm builder/poller for tests and
  explicit callers; the GenServer is the runtime writer that emits and then
  persists.

  First runtime boot is bounded: if both the in-memory cursor and file cursor
  are absent, the GenServer polls from `:first_boot_lookback_seconds` ago
  instead of scanning the full historical `lease_plane_events` table. Explicit
  `tick(prior_cursor: nil)` calls keep their manual "fetch all" semantics.

  Runtime GenServer ticks are wrapped in the advisory lease scope
  `resident:/sentinel_cycle`, matching Python `SentinelAgent.run_cycle/1`.
  Advisory outcomes never gate polling, findings emission, or cursor advance.

  The runtime tick body runs inside a `Task` with a 30s budget by default.
  This is the BEAM-side replacement for Python's anyio/asyncio escape hatch:
  no anyio loop exists here, but the cycle still gets a hard runtime bound.

  ## Phase-B promotion scope

  Python feeds `conflict_batch` alarms into `_emit_phase_b_transitions/2`.
  Wave 1 BEAM does not port that evaluator; conflict_batch findings emit,
  but phase-B promotion remains Python/Wave-2 scope.

  ## Tick API

  `tick/1` is the unit of work — call it from the GenServer's tick loop
  OR from tests with explicit options. The GenServer itself is a thin
  scheduler; the testable behavior lives in `tick/1`.

  ## Cursor topology (binding for follow-up PRs)

  This module is **the sole writer** of `forced_release_alarm.last_event_ts`
  during the ad_hoc-only scope. The follow-up PRs that add the
  `lease.deprecation_swept` (deprecation-batch) and `conflict_held_by_other`
  (conflict-batch) query classes **MUST NOT** ship as sibling GenServers
  that independently write the same cursor key.

  The Python reference at `agents/sentinel/forced_release_alarm.py:87`
  (`_poll_inner`) advances **one cursor** across all three classes in a
  single pass — the cursor is `max(ts)` across every row seen. Three
  independent pollers writing one cursor file would mean: poller A
  advances past T, then poller B's next query filters `ts > T`,
  silently skipping any rows older than T that B hadn't yet read.

  Pick one topology before the next PR ships:
    1. **Combined poller** — one GenServer with three SQL passes per
       tick (matches Python parity, simplest).
    2. **Per-class cursor keys** — `forced_release_alarm.last_event_ts`
       becomes `forced_release_alarm.last_event_ts.ad_hoc` etc. Requires
       a Python-compat read shim (Python's `agent.py:663` reads the
       single key — would need to migrate or read the union of keys).
    3. **Coordinator + workers** — fan-out to three workers, fan-in
       on a single cursor advance. Most code; cleanest topology.

  Architect council fold for #376 made this binding. Whichever path the
  next PR picks, it must be explicit in the RFC before any code lands.
  """

  use GenServer

  require Logger

  alias UnitaresSentinel.{CycleState, Findings, ForcedReleasePoller.Logic, LeaseAdvisory}

  @type opts :: [
          prior_cursor: DateTime.t() | nil,
          db: GenServer.server(),
          persist: boolean(),
          state_path: Path.t() | nil,
          emit_findings: boolean(),
          findings_opts: keyword(),
          lease_advisory: boolean(),
          lease_opts: keyword(),
          tick_timeout_ms: pos_integer(),
          first_boot_lookback_seconds: non_neg_integer()
        ]

  # ---- Public tick API --------------------------------------------------

  @doc """
  Run one poll cycle.

  Options:
    * `:prior_cursor` — cursor to filter against (`nil` = no filter, fetch all)
    * `:db` — Postgrex registered name (default: `UnitaresSentinel.DB`)
    * `:persist` — when true, write the new cursor via `CycleState.save/2`
       (default: false; the GenServer flips this to true at runtime, tests
       opt in selectively)
    * `:state_path` — explicit shadow path to persist into; only used when
       `:persist` is true and overrides the default config-resolved path

  Returns `{alarms, new_cursor}` where `new_cursor` is `DateTime.t() | nil`.
  """
  @spec tick(opts()) :: {[Logic.alarm()], DateTime.t() | nil}
  def tick(opts \\ []) do
    db = Keyword.get(opts, :db, UnitaresSentinel.DB)
    prior_cursor = Keyword.get(opts, :prior_cursor)
    persist? = Keyword.get(opts, :persist, false)

    case query_all_rows_in_transaction(db, prior_cursor) do
      {:ok, %{ad_hoc: ad_hoc, deprecation: dep, conflict: conf}} ->
        {alarms, new_cursor} =
          Logic.build_all_alarms(ad_hoc, dep, conf, prior_cursor)

        # Persist only on actual advance (not every nil-cursor tick), to avoid
        # needless file writes that would also bump the file's mtime and
        # confuse `mix sentinel.cursor_diff` operators tracking activity.
        # Use DateTime.compare/2 (not `!=`) because two DateTime structs with
        # the same instant but different `:microsecond` precision tuples
        # would compare unequal by struct identity. Architect #3 in PR #378
        # council fold.
        if persist? and new_cursor != nil and not same_cursor?(new_cursor, prior_cursor) do
          persist_cursor(new_cursor, opts)
        end

        {alarms, new_cursor}

      {:error, reason} ->
        # v0.1.3 §B6 all-or-nothing cursor advance — covers the `{:error, _}`
        # return class (real DB errors: connection drop mid-transaction,
        # constraint violation, server-side rollback, query-level error).
        # The cursor MUST NOT advance and the persist MUST NOT happen.
        # Returning `{[], prior_cursor}` preserves both invariants.
        #
        # The other failure class is process exit (e.g. `:noproc` if the
        # registered Postgrex name doesn't exist). That bypasses this match
        # arm entirely — the GenServer dies, the supervisor restarts it,
        # `init/1` re-reads the cursor from disk, and the on-disk cursor
        # was never advanced (because no successful tick wrote it). Both
        # paths preserve the §B6 invariant; only this path returns cleanly.
        # Mirrors Python's caught-exception early return at agent.py:671-673
        # where save_state is never reached on poll failure.
        Logger.warning(
          "ForcedReleasePoller.tick: transaction failed — #{inspect(reason)} — cursor unchanged"
        )

        {[], prior_cursor}
    end
  end

  defp same_cursor?(nil, nil), do: true
  defp same_cursor?(nil, _), do: false
  defp same_cursor?(_, nil), do: false

  defp same_cursor?(%DateTime{} = a, %DateTime{} = b),
    do: DateTime.compare(a, b) == :eq

  # v0.1.3 §B5 single-Postgrex-connection-per-tick binding. The transaction
  # wrapper checks out one connection from the pool and runs all queries
  # against it. With one query (this PR), the snapshot consistency is
  # trivial; it matters when the next PR adds the deprecation_batch and
  # conflict_batch queries — they MUST share one snapshot to avoid the
  # multi-connection lost-event class (architect #2 in the v0.1.3 council).
  #
  # File I/O (CycleState.save) MUST happen OUTSIDE this function so the
  # connection is returned to the pool before the file write. Holding a
  # DB connection across file I/O is the BEAM-side analogue of the
  # anyio-asyncio coupling pattern documented in CLAUDE.md.
  #
  # v0.1.3 §B5 single-Postgrex-connection-per-tick: all three queries
  # share one snapshot. If ANY query returns {:error, _}, Postgrex.rollback
  # is called → outer transaction returns {:error, _} → §B6 all-or-nothing
  # cursor advance kicks in (no partial advance possible).
  defp query_all_rows_in_transaction(db, prior_cursor) do
    Postgrex.transaction(db, fn conn ->
      with {:ok, ad_hoc} <- query_forced_rows(conn, prior_cursor),
           {:ok, dep} <- query_deprecation_batch_rows(conn, prior_cursor),
           {:ok, conf} <- query_conflict_batch_rows(conn, prior_cursor) do
        %{ad_hoc: ad_hoc, deprecation: dep, conflict: conf}
      else
        {:error, e} -> Postgrex.rollback(conn, e)
      end
    end)
  end

  defp query_forced_rows(conn, prior_cursor) do
    sql = """
    SELECT event_id::text AS event_id,
           ts,
           lease_id::text AS lease_id,
           surface_id,
           surface_kind
    FROM lease_plane.lease_plane_events
    WHERE event_type = 'forced'
      AND ($1::timestamptz IS NULL OR ts > $1)
    ORDER BY ts
    """

    case Postgrex.query(conn, sql, [prior_cursor]) do
      {:ok, %{rows: rows, columns: cols}} -> {:ok, Enum.map(rows, &row_to_map(cols, &1))}
      {:error, _} = err -> err
    end
  end

  # v0.1.3 §B2 asymmetry: filter on `ds.sweep_completed_at`, NOT `e.ts`.
  # Cursor still advances on max(e.last_ts) via Logic.build_deprecation_batch_alarms.
  # Mirrors agents/sentinel/forced_release_alarm.py:113-134.
  defp query_deprecation_batch_rows(conn, prior_cursor) do
    sql = """
    SELECT
      ds.deprecation_id::text AS deprecation_id,
      ds.surface_kind,
      ds.sweep_completed_at,
      count(e.event_id) AS event_count,
      min(e.ts) AS first_ts,
      max(e.ts) AS last_ts
    FROM lease_plane.lease_plane_events e
    JOIN lease_plane.deprecated_schemes ds
      ON ds.deprecation_id::text = e.payload->>'deprecation_id'
    WHERE e.event_type = 'lease.deprecation_swept'
      AND ds.sweep_completed_at IS NOT NULL
      AND ($1::timestamptz IS NULL OR ds.sweep_completed_at > $1)
    GROUP BY ds.deprecation_id, ds.surface_kind, ds.sweep_completed_at
    """

    case Postgrex.query(conn, sql, [prior_cursor]) do
      {:ok, %{rows: rows, columns: cols}} ->
        {:ok, Enum.map(rows, &row_to_map(cols, &1)) |> Enum.map(&coerce_event_count/1)}

      {:error, _} = err ->
        err
    end
  end

  # GROUP BY surface_id within this poll cycle. Mirrors
  # agents/sentinel/forced_release_alarm.py:148-167.
  defp query_conflict_batch_rows(conn, prior_cursor) do
    sql = """
    SELECT
      surface_id,
      surface_kind,
      count(event_id) AS event_count,
      min(ts) AS first_ts,
      max(ts) AS last_ts
    FROM lease_plane.lease_plane_events
    WHERE event_type = 'conflict_held_by_other'
      AND ($1::timestamptz IS NULL OR ts > $1)
    GROUP BY surface_id, surface_kind
    """

    case Postgrex.query(conn, sql, [prior_cursor]) do
      {:ok, %{rows: rows, columns: cols}} ->
        {:ok, Enum.map(rows, &row_to_map(cols, &1)) |> Enum.map(&coerce_event_count/1)}

      {:error, _} = err ->
        err
    end
  end

  # Postgrex returns count() as Decimal — coerce to integer for shape parity
  # with Python's asyncpg int return.
  defp coerce_event_count(%{event_count: %Decimal{} = d} = row) do
    %{row | event_count: Decimal.to_integer(d)}
  end

  defp coerce_event_count(row), do: row

  defp row_to_map(columns, row) do
    columns
    |> Enum.zip(row)
    |> Enum.into(%{}, fn {col, val} -> {String.to_atom(col), val} end)
  end

  defp persist_cursor(new_cursor, opts) do
    # Council fold: reviewer Critical-1 (PR #376). Building from %{} would
    # silently erase any sibling keys in the shadow file — most importantly
    # the v0.1.2 §B3 `runtime: "beam_canonical"` cutover flag. Load the
    # existing state first, then update only the cursor, preserving every
    # other key.
    #
    # OPT-KEY ASYMMETRY (load=:shadow, save=:path) is intentional:
    # `CycleState.load/1` reads BOTH the canonical (Python) file and the
    # shadow (BEAM) file for max-on-boot semantics — `:shadow` overrides
    # the BEAM-side path while canonical resolves from config.
    # `CycleState.save/2` writes ONE file — `:path` is that target. The
    # asymmetry follows the "load is two-file, save is one-file" semantic
    # split in CycleState; harmonizing the keys would force one side or
    # the other to lie about its file-count semantics.
    save_opts =
      case Keyword.get(opts, :state_path) do
        nil -> []
        path -> [path: path]
      end

    load_opts =
      case Keyword.get(opts, :state_path) do
        nil -> []
        path -> [shadow: path]
      end

    existing = CycleState.load(load_opts)
    state = CycleState.update_last_event_ts(existing, DateTime.to_iso8601(new_cursor))

    CycleState.save(state, save_opts)
  end

  # ---- GenServer scheduler ----------------------------------------------

  @doc """
  Start the poller GenServer. Reads cursor from CycleState on init,
  schedules first tick after `:poller_initial_delay_ms`, then ticks
  every `:poller_interval_ms`.

  Both intervals are config-driven so tests can inject short values.
  """
  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  def init(opts) do
    db = Keyword.get(opts, :db, UnitaresSentinel.DB)

    interval_ms =
      Keyword.get(
        opts,
        :interval_ms,
        Application.get_env(:unitares_sentinel, :poller_interval_ms, 30_000)
      )

    initial_delay_ms =
      Keyword.get(
        opts,
        :initial_delay_ms,
        Application.get_env(:unitares_sentinel, :poller_initial_delay_ms, 1_000)
      )

    jitter_ms =
      Keyword.get(
        opts,
        :jitter_ms,
        Application.get_env(:unitares_sentinel, :poller_jitter_ms, 5_000)
      )

    first_boot_lookback_seconds =
      Keyword.get(
        opts,
        :first_boot_lookback_seconds,
        Application.get_env(:unitares_sentinel, :first_boot_lookback_seconds, 7 * 24 * 60 * 60)
      )

    cursor = load_cursor_from_state()

    state = %{
      db: db,
      cursor: cursor,
      interval_ms: interval_ms,
      jitter_ms: jitter_ms,
      first_boot_lookback_seconds: first_boot_lookback_seconds,
      emit_findings?:
        Keyword.get(
          opts,
          :emit_findings,
          Application.get_env(:unitares_sentinel, :emit_findings, true)
        ),
      findings_opts: Keyword.get(opts, :findings_opts, []),
      lease_advisory?:
        Keyword.get(
          opts,
          :lease_advisory,
          Application.get_env(:unitares_sentinel, :lease_advisory_enabled, true)
        ),
      lease_opts: Keyword.get(opts, :lease_opts, []),
      tick_timeout_ms:
        Keyword.get(
          opts,
          :tick_timeout_ms,
          Application.get_env(:unitares_sentinel, :poller_tick_timeout_ms, 30_000)
        ),
      # v0.1.3 §C2 tick-skip guard. Under self-scheduling (next :tick is
      # only enqueued AFTER the current tick returns), this flag will never
      # actually be true at message-arrival time — BEAM serializes handle_*
      # callbacks per process. The guard exists to defend against external
      # `send(pid, :tick)` from tests or operator scripts that bypass the
      # scheduler discipline. Option 1 of the v0.1.3 §C2 binding.
      running?: false
    }

    Process.send_after(self(), :tick, initial_delay_ms + sample_jitter(jitter_ms))
    {:ok, state}
  end

  @impl true
  def handle_info(:tick, %{running?: true} = state) do
    # v0.1.3 §C2 guard fires: a :tick message arrived while the previous
    # tick body was still executing. Under serialized handle_info this is
    # only reachable when an external sender bypasses the scheduler.
    Logger.warning(
      "ForcedReleasePoller: skipping :tick — previous tick still in flight (mailbox guard)"
    )

    {:noreply, state}
  end

  @impl true
  def handle_info(:tick, state) do
    state = %{state | running?: true}
    lease = acquire_runtime_lease(state)

    try do
      case await_runtime_tick(state) do
        {:ok, next_state} ->
          {:noreply, next_state}

        :timeout ->
          Logger.warning(
            "ForcedReleasePoller: runtime tick exceeded #{state.tick_timeout_ms}ms — cursor unchanged"
          )

          schedule_next_tick(state)
          {:noreply, %{state | running?: false}}
      end
    after
      release_runtime_lease(lease, state)
    end
  end

  defp await_runtime_tick(%{tick_timeout_ms: timeout_ms} = state) do
    task = Task.async(fn -> run_runtime_tick(state) end)

    # Keep task exits observable via Task.yield/2 while letting the caller's
    # `after` cleanup release the advisory lease before propagating the exit.
    Process.unlink(task.pid)

    case Task.yield(task, timeout_ms) || Task.shutdown(task, :brutal_kill) do
      {:ok, next_state} -> {:ok, next_state}
      {:exit, reason} -> exit(reason)
      nil -> :timeout
    end
  end

  defp run_runtime_tick(state) do
    # Council fold: architect #2 (PR #376). Re-read the file cursor each tick
    # and use max(in-memory, file). Defends against an operator (or a future
    # cursor_repair task) writing the shadow file between ticks; without this,
    # the GenServer would clobber the operator's edit on the next tick.
    file_cursor = load_cursor_from_state()

    effective_prior =
      state.cursor
      |> max_cursor(file_cursor)
      |> apply_first_boot_lookback(state)

    {alarms, new_cursor} =
      tick(prior_cursor: effective_prior, db: state.db, persist: false)

    emit_findings(alarms, state)

    if new_cursor != nil and not same_cursor?(new_cursor, effective_prior) do
      persist_cursor(new_cursor, [])
    end

    # Jitter the next tick to avoid Python/BEAM lockstep races after
    # simultaneous boots (architect #5).
    schedule_next_tick(state)

    %{state | running?: false, cursor: new_cursor}
  end

  defp schedule_next_tick(state) do
    Process.send_after(self(), :tick, state.interval_ms + sample_jitter(state.jitter_ms))
  end

  defp acquire_runtime_lease(%{lease_advisory?: false}),
    do: %{outcome: :service_unavailable, lease_id: nil}

  defp acquire_runtime_lease(%{lease_opts: lease_opts}),
    do: LeaseAdvisory.acquire_cycle(lease_opts)

  defp release_runtime_lease(_lease, %{lease_advisory?: false}), do: :ok

  defp release_runtime_lease(lease, %{lease_opts: lease_opts}),
    do: LeaseAdvisory.release(lease, lease_opts)

  # Symmetric ±jitter_ms uniform sample. Non-negative result clamp avoids
  # ever scheduling a tick into the past if jitter_ms ever exceeds interval_ms
  # (which would be a config error, but cheap to guard against).
  defp sample_jitter(0), do: 0

  defp sample_jitter(jitter_ms) when is_integer(jitter_ms) and jitter_ms > 0 do
    :rand.uniform(2 * jitter_ms + 1) - jitter_ms - 1
  end

  defp max_cursor(nil, b), do: b
  defp max_cursor(a, nil), do: a

  defp max_cursor(%DateTime{} = a, %DateTime{} = b) do
    if DateTime.compare(a, b) == :gt, do: a, else: b
  end

  defp apply_first_boot_lookback(nil, %{first_boot_lookback_seconds: seconds})
       when is_integer(seconds) and seconds > 0 do
    DateTime.utc_now() |> DateTime.add(-seconds, :second)
  end

  defp apply_first_boot_lookback(cursor, _state), do: cursor

  defp emit_findings(_alarms, %{emit_findings?: false}), do: :ok

  defp emit_findings(alarms, %{findings_opts: findings_opts}) when is_list(alarms) do
    Enum.each(alarms, &Findings.post_alarm(&1, findings_opts))
  end

  defp load_cursor_from_state do
    case CycleState.get_last_event_ts(CycleState.load()) do
      nil ->
        nil

      ts when is_binary(ts) ->
        case DateTime.from_iso8601(ts) do
          {:ok, dt, _} -> dt
          _ -> nil
        end
    end
  rescue
    _ -> nil
  end
end
