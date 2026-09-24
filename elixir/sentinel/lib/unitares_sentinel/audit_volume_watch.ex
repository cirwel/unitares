defmodule UnitaresSentinel.AuditVolumeWatch do
  @moduledoc """
  Watches `audit.events` for one writer suddenly producing far more rows than
  anything legitimate does, and files a `sentinel_finding` when it happens.

  Why this exists: from 2026-09-16 23:25 to 09-24 one client re-sent a dead
  session id about 140,000 times a day. Every call wrote an audit row, it was
  the largest source of audit-table growth, and nothing flagged it: each row
  was individually valid, and no check looked at volume. See
  `UnitaresSentinel.AuditVolumeWatch.Logic` for the rules and calibration.

  One query per tick (default every 5 minutes) over the last 24 hours, grouped
  by `(event_type, agent_id-or-session_id)`. Rows whose payload carries a
  numeric `suppressed_since_last` count for `1 + suppressed_since_last`, so a
  server-side write throttle (like the one on `session_resolve_miss_observed`)
  cannot hide a storm from this check. A `throttle_flush` row stands for no
  event of its own and can be appended long after the misses it summarises,
  so it contributes only its suppressed count, placed at `suppressed_last_at`
  (when those misses happened) rather than at the row's own timestamp. The
  writer stamps that field with its UTC offset, so it parses to the same
  instant in any session zone. Rows written before the offset was added carry
  naive local time and are read in the session zone, which matches the writer
  on a single-host deployment. A missing or unparseable value falls back to
  the row timestamp (`pg_input_is_valid`, PostgreSQL 16+), so one bad payload
  cannot abort the query.

  Findings re-alert through `UnitaresSentinel.ReAlert`: once, then at 1h, 2h,
  4h ... capped at 24h while
  the condition holds, and immediately on escalation from the relative rule to
  the absolute one.

  Read-only against the database; no lease, because it neither contends for
  nor mutates any shared surface.
  """

  use GenServer

  require Logger

  alias UnitaresSentinel.{AuditVolumeWatch.Logic, Findings, ReAlert}

  @default_interval_ms 300_000
  @default_initial_delay_ms 60_000
  @default_tick_timeout_ms 30_000
  @max_pending 200
  # At the 3s findings timeout, 5 stalled POSTs cost 15s of a tick.
  @max_deliveries_per_tick 5

  @volume_sql """
  WITH e AS (
    SELECT event_type,
           coalesce(nullif(agent_id, ''), nullif(session_id, ''), '') AS source,
           ts,
           coalesce(payload->>'resolution_source' = 'throttle_flush', false) AS flush,
           CASE WHEN jsonb_typeof(payload->'suppressed_since_last') = 'number'
                THEN greatest((payload->>'suppressed_since_last')::numeric, 0)::bigint
                ELSE 0 END AS suppressed,
           CASE WHEN payload->>'resolution_source' = 'throttle_flush'
                     AND pg_input_is_valid(payload->>'suppressed_last_at', 'timestamptz')
                THEN (payload->>'suppressed_last_at')::timestamptz
                ELSE ts END AS suppressed_at
    FROM audit.events
    WHERE ts > now() - interval '24 hours'
  ),
  w AS (
    SELECT event_type,
           source,
           CASE WHEN flush THEN suppressed_at ELSE ts END AS at,
           CASE WHEN flush THEN suppressed ELSE 1 + suppressed END AS weight
    FROM e
  )
  SELECT event_type,
         source,
         coalesce(sum(weight) FILTER (WHERE at > now() - interval '1 hour'), 0) AS recent,
         coalesce(sum(weight) FILTER (WHERE at > now() - interval '24 hours'
                                        AND at <= now() - interval '1 hour'), 0) AS prior
  FROM w
  GROUP BY event_type, source
  HAVING coalesce(sum(weight) FILTER (WHERE at > now() - interval '1 hour'), 0) >= $1
  """

  @doc false
  def volume_sql, do: @volume_sql

  @doc """
  Run one check. Returns `{posted, realert_state, pending}`.

  `pending` holds findings whose POST failed; pass it back as `:pending` on the
  next tick and they are resent before anything new is evaluated, so an alert
  raised during a governance outage is delivered afterwards even if the spike
  behind it has already subsided. Bounded at 200 findings.

  Options: `:query_fun` (`fn floor -> {:ok, rows} | {:error, term} end`),
  `:now_ms`, `:realert`, `:pending`, `:realert_opts`, `:logic_opts`,
  `:emit_findings`, `:findings_opts`, `:db`.
  """
  @spec tick(keyword()) :: {[map()], ReAlert.t(), [map()]}
  def tick(opts \\ []) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    realert = opts |> Keyword.get(:realert, ReAlert.new()) |> ReAlert.prune(now_ms)
    logic_opts = Keyword.get(opts, :logic_opts, [])
    query_fun = Keyword.get(opts, :query_fun, default_query_fun(Keyword.get(opts, :db)))

    {new_findings, realert} =
      case query_fun.(Logic.query_floor(logic_opts)) do
        {:ok, rows} ->
          rows
          |> Logic.evaluate(logic_opts)
          |> Enum.reduce({[], realert}, fn anomaly, {acc, state} ->
            case ReAlert.decide(
                   state,
                   {anomaly.event_type, anomaly.source},
                   ReAlert.rank(anomaly.severity),
                   now_ms,
                   Keyword.get(opts, :realert_opts, [])
                 ) do
              {:emit, info, state} ->
                finding =
                  anomaly
                  |> Logic.to_finding(info)
                  |> Map.put(:change_token, ReAlert.change_token(info, anomaly.severity))

                {[finding | acc], state}

              {:suppress, state} ->
                {acc, state}
            end
          end)
          |> then(fn {acc, state} -> {Enum.reverse(acc), state} end)

        {:error, reason} ->
          Logger.warning("AuditVolumeWatch: volume query failed — #{inspect(reason)}")
          {[], realert}
      end

    # The backoff advances whether or not the POST lands: an undelivered
    # finding is queued, not forgotten, so it is not emitted twice. The query
    # runs before any delivery, and deliveries per tick are capped (new
    # findings first, then the queue), so a stalled endpoint cannot starve the
    # check.
    {delivered, undelivered} =
      Findings.deliver_bounded(
        new_findings ++ Keyword.get(opts, :pending, []),
        Keyword.get(opts, :max_deliveries, @max_deliveries_per_tick),
        opts,
        "AuditVolumeWatch"
      )

    {delivered, realert, Findings.cap_queue(undelivered, @max_pending)}
  end

  defp default_query_fun(db) do
    db = db || UnitaresSentinel.DB
    fn floor -> query_rows(db, floor) end
  end

  @doc "Run the volume query on a Postgrex pool or connection."
  @spec query_rows(Postgrex.conn() | atom(), pos_integer()) :: {:ok, [map()]} | {:error, term()}
  def query_rows(conn, floor) do
    case Postgrex.query(conn, @volume_sql, [floor]) do
      {:ok, %{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [event_type, source, recent, prior] ->
           %{event_type: event_type, source: source, recent: recent, prior: prior}
         end)}

      {:error, _} = err ->
        err
    end
  end

  # ---- GenServer -------------------------------------------------------

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  def init(opts) do
    state = %{
      opts:
        opts
        |> Keyword.put_new(
          :emit_findings,
          Application.get_env(:unitares_sentinel, :emit_findings, true)
        ),
      interval_ms:
        Keyword.get(
          opts,
          :interval_ms,
          Application.get_env(:unitares_sentinel, :audit_volume_interval_ms, @default_interval_ms)
        ),
      tick_timeout_ms: Keyword.get(opts, :tick_timeout_ms, @default_tick_timeout_ms),
      realert: ReAlert.new(),
      pending: []
    }

    Process.send_after(
      self(),
      :tick,
      Keyword.get(opts, :initial_delay_ms, @default_initial_delay_ms)
    )

    {:ok, state}
  end

  @impl true
  def handle_info(:tick, state) do
    tick_opts =
      state.opts
      |> Keyword.put(:realert, state.realert)
      |> Keyword.put(:pending, state.pending)

    task = Task.async(fn -> tick(tick_opts) end)
    Process.unlink(task.pid)

    {realert, pending} =
      case Task.yield(task, state.tick_timeout_ms) || Task.shutdown(task, :brutal_kill) do
        {:ok, {_posted, realert, pending}} ->
          {realert, pending}

        _ ->
          Logger.warning("AuditVolumeWatch: tick failed or exceeded #{state.tick_timeout_ms}ms")
          {state.realert, state.pending}
      end

    Process.send_after(self(), :tick, state.interval_ms)
    {:noreply, %{state | realert: realert, pending: pending}}
  end
end
