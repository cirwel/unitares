defmodule UnitaresSentinel.ForcedReleasePoller do
  @moduledoc """
  Surface 1 cycle worker — periodic poller that drives `CycleState`.

  Reads the cursor from `CycleState`, queries `lease_plane.lease_plane_events`
  for `event_type='forced'` rows past the cursor, builds alarms via
  `ForcedReleasePoller.Logic.build_alarms/2`, advances the cursor to
  max(rows.ts), and persists via `CycleState.save/2`.

  ## Scope (this PR)

  Ad_hoc forced events (`event_type='forced'`) only — the lowest-volume
  class. Deferred to follow-up PRs:

    * `event_type='lease.deprecation_swept'` deprecation-batch class
    * `event_type='conflict_held_by_other'` conflict-batch class

  ## Findings emit

  This module RETURNS alarms but does NOT POST them. Surface 2 (findings
  emit) is a separate writer-locked surface and lands in its own PR.
  Returning alarms keeps the API forward-compatible: when Surface 2 wires
  up, it calls `tick/1` and routes the alarms to the dashboard / Discord
  bridge.

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

  alias UnitaresSentinel.{CycleState, ForcedReleasePoller.Logic}

  @type opts :: [
          prior_cursor: DateTime.t() | nil,
          db: GenServer.server(),
          persist: boolean(),
          state_path: Path.t() | nil
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

    rows = query_forced_rows(db, prior_cursor)
    {alarms, new_cursor} = Logic.build_alarms(rows, prior_cursor)

    if persist? and new_cursor != nil do
      persist_cursor(new_cursor, opts)
    end

    {alarms, new_cursor}
  end

  defp query_forced_rows(db, prior_cursor) do
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

    case Postgrex.query(db, sql, [prior_cursor]) do
      {:ok, %{rows: rows, columns: cols}} ->
        Enum.map(rows, &row_to_map(cols, &1))

      {:error, e} ->
        Logger.warning(
          "ForcedReleasePoller.query_forced_rows: #{inspect(e)} — returning empty rows"
        )

        []
    end
  end

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
    interval_ms = Keyword.get(opts, :interval_ms, Application.get_env(:unitares_sentinel, :poller_interval_ms, 30_000))
    initial_delay_ms = Keyword.get(opts, :initial_delay_ms, Application.get_env(:unitares_sentinel, :poller_initial_delay_ms, 1_000))
    jitter_ms = Keyword.get(opts, :jitter_ms, Application.get_env(:unitares_sentinel, :poller_jitter_ms, 5_000))

    cursor = load_cursor_from_state()

    state = %{
      db: db,
      cursor: cursor,
      interval_ms: interval_ms,
      jitter_ms: jitter_ms
    }

    Process.send_after(self(), :tick, initial_delay_ms + sample_jitter(jitter_ms))
    {:ok, state}
  end

  @impl true
  def handle_info(:tick, state) do
    # Council fold: architect #2 (PR #376). Re-read the file cursor each tick
    # and use max(in-memory, file). Defends against an operator (or a future
    # cursor_repair task) writing the shadow file between ticks; without this,
    # the GenServer would clobber the operator's edit on the next tick.
    file_cursor = load_cursor_from_state()
    effective_prior = max_cursor(state.cursor, file_cursor)

    {_alarms, new_cursor} =
      tick(prior_cursor: effective_prior, db: state.db, persist: true)

    # Jitter the next tick to avoid Python/BEAM lockstep races after
    # simultaneous boots (architect #5).
    Process.send_after(self(), :tick, state.interval_ms + sample_jitter(state.jitter_ms))

    {:noreply, %{state | cursor: new_cursor}}
  end

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
