defmodule AgentOrchestrator.ResultStore do
  @moduledoc """
  Short-lived retention of an ephemeral agent's FINAL result, keyed by agent_id,
  so a late `await`/`snapshot` survives the agent's process death instead of
  racing to `{:error, :not_found}`.

  ## Why this exists

  `AgentRunner` stops itself the instant its Port reports exit (it is
  `restart: :temporary`). A fast agent — `echo`, a sub-second tool worker — can
  therefore be gone before the orchestrator's `await` even reaches its mailbox:
  `whereis/0` returns `nil`, or the `GenServer.call` exits `:noproc` mid-flight.
  Pre-existing since #581, the final result was simply lost on that race; the
  documented workaround was "await before it exits or snapshot during the run,"
  which a fan-out orchestrator collecting results sequentially cannot always do.

  This store closes the race. `AgentRunner.finalize/2` writes the terminal
  `result/1` here BEFORE returning `{:stop, ...}`, so the write strictly
  happens-before the process dies (the GenServer only terminates after the
  callback returns). Any `await`/`snapshot` that observes the runner as dead is
  guaranteed to find the retained result already present.

  ## Ownership and concurrency

  The ETS table is `:public` and `:named_table`, owned by this GenServer purely
  for *lifetime* (the table dies with the orchestrator app, not with any one
  runner). Runners write directly with `:ets.insert/2` and readers with
  `:ets.lookup/2` — no message passing on the hot path, so the happens-before
  guarantee holds without a synchronous round-trip through this process.

  ## Eviction

  Retention is bounded so a high-churn fleet cannot grow the table without limit:

    * TTL — entries older than `:result_retention_ms` (default 300_000) are
      dropped. Expiry is enforced both lazily on `fetch/1` and by a periodic
      sweep every `:result_sweep_interval_ms` (default 60_000).
    * Soft cap — if a sweep finds more than `:result_store_max` (default 10_000)
      live entries, the oldest are evicted down to the cap. This is a safety
      backstop for a churn burst within one TTL window, not the primary path.
  """

  use GenServer

  @table __MODULE__

  @default_retention_ms 300_000
  @default_sweep_interval_ms 60_000
  @default_max_entries 10_000

  # ---------- public API ----------

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc """
  Retain `result` for `agent_id`. Called from `AgentRunner.finalize/2` before the
  runner stops. Overwrites any prior entry for the same id (random ids make
  collisions negligible; a re-used id keeps the newest result).
  """
  @spec put(String.t(), map()) :: :ok
  def put(agent_id, result) when is_binary(agent_id) and is_map(result) do
    # Direct write from the caller process — synchronous and ordered with the
    # finalize that issues it, so the retained result is visible the moment the
    # runner dies. Guard against a not-yet-started / torn-down table (e.g. in a
    # unit test that exercises a runner without the full app) rather than letting
    # a missing table crash the exiting runner.
    if table_ready?() do
      :ets.insert(@table, {agent_id, result, now_ms()})
    end

    :ok
  end

  @doc """
  Fetch a retained result, honoring the TTL. Returns `{:ok, result}` if a live
  (non-expired) entry exists, else `:error`.
  """
  @spec fetch(String.t()) :: {:ok, map()} | :error
  def fetch(agent_id) when is_binary(agent_id) do
    if table_ready?() do
      case :ets.lookup(@table, agent_id) do
        [{^agent_id, result, stored_at}] ->
          if expired?(stored_at) do
            # Lazy expiry: drop the stale row so a later sweep need not.
            :ets.delete(@table, agent_id)
            :error
          else
            {:ok, result}
          end

        [] ->
          :error
      end
    else
      :error
    end
  end

  # ---------- GenServer ----------

  @impl true
  def init(opts) do
    retention_ms = opt(opts, :result_retention_ms, @default_retention_ms)
    sweep_ms = opt(opts, :result_sweep_interval_ms, @default_sweep_interval_ms)
    max_entries = opt(opts, :result_store_max, @default_max_entries)

    # :public so runners write without routing through this process; this
    # GenServer is the table's owner only so the table outlives any one runner.
    :ets.new(@table, [:set, :public, :named_table, read_concurrency: true])

    schedule_sweep(sweep_ms)

    {:ok, %{retention_ms: retention_ms, sweep_ms: sweep_ms, max_entries: max_entries}}
  end

  @impl true
  def handle_info(:sweep, state) do
    sweep(state)
    schedule_sweep(state.sweep_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # ---------- internals ----------

  defp opt(opts, key, default) do
    Keyword.get(opts, key, Application.get_env(:agent_orchestrator, key, default))
  end

  defp schedule_sweep(sweep_ms), do: Process.send_after(self(), :sweep, sweep_ms)

  defp now_ms, do: System.monotonic_time(:millisecond)

  defp expired?(stored_at), do: now_ms() - stored_at > retention_ms()

  # Read retention from app env on each check so a config change (and the
  # lazy-expiry path, which has no GenServer state in scope) stays consistent
  # with the sweep's view.
  defp retention_ms,
    do: Application.get_env(:agent_orchestrator, :result_retention_ms, @default_retention_ms)

  defp table_ready?, do: :ets.whereis(@table) != :undefined

  # Drop expired rows, then enforce the soft cap by evicting the oldest survivors.
  defp sweep(%{retention_ms: retention_ms, max_entries: max_entries}) do
    cutoff = now_ms() - retention_ms

    # match_delete every row whose stored_at is at/under the cutoff. `:"$1"` binds
    # stored_at; the guard keeps the deletion to expired rows only.
    expired_spec = [{{:_, :_, :"$1"}, [{:"=<", :"$1", cutoff}], [true]}]
    :ets.select_delete(@table, expired_spec)

    enforce_cap(max_entries)
  end

  defp enforce_cap(max_entries) do
    case :ets.info(@table, :size) do
      size when is_integer(size) and size > max_entries ->
        # Over the cap within one TTL window: evict oldest-first down to the cap.
        # Rare backstop path, so the full-scan sort is acceptable.
        @table
        |> :ets.tab2list()
        |> Enum.sort_by(fn {_id, _result, stored_at} -> stored_at end)
        |> Enum.take(size - max_entries)
        |> Enum.each(fn {id, _result, _stored_at} -> :ets.delete(@table, id) end)

      _ ->
        :ok
    end
  end
end
