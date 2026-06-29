defmodule UnitaresLeasePlane.EffectRecovery do
  @moduledoc """
  Boot-time recovery for governed-effect EXECUTE (§5b). On startup, drains any
  orphaned `effects.payloads` rows (pre-image captured, never committed) left by
  a node crash, reconciling each by content hash via `EffectReconcile`.

  Runs SYNCHRONOUSLY in `init/1` and is placed in the supervision tree BEFORE
  the HTTP listener, so the plane never accepts a new effect while orphans from
  a prior crash are unresolved. The in-process (`:transient` custodian restart)
  recovery path is a separate slice; this covers the full-node-crash case.

  FAIL-SOFT: boot must never crash from recovery. A missing `effects.*` schema
  (the normal state until migration 052 is applied) or any query error is logged
  and skipped — the scanner returns `{:ok, ...}` regardless. `repo` is injectable
  for tests.
  """

  use GenServer

  alias UnitaresLeasePlane.{EffectReconcile, EffectRepo}

  require Logger

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  # Default periodic sweep interval. The boot scan only catches orphans left by a
  # full VM restart; a single-request crash (handler process dies, VM stays up)
  # would otherwise leave its orphan until the next reboot. The sweep reconciles
  # those promptly. Set :effect_recovery_sweep_ms to 0 to disable (boot-only).
  @default_sweep_ms 60_000

  def init(opts) do
    repo = Keyword.get(opts, :repo, EffectRepo)

    sweep_ms =
      Keyword.get(
        opts,
        :sweep_ms,
        Application.get_env(:lease_plane, :effect_recovery_sweep_ms, @default_sweep_ms)
      )

    result = scan(repo)
    schedule_sweep(sweep_ms)
    {:ok, %{repo: repo, sweep_ms: sweep_ms, last: result}}
  end

  @impl true
  def handle_info(:sweep, %{repo: repo, sweep_ms: sweep_ms} = state) do
    result = scan(repo)
    schedule_sweep(sweep_ms)
    {:noreply, %{state | last: result}}
  end

  defp schedule_sweep(ms) when is_integer(ms) and ms > 0,
    do: Process.send_after(self(), :sweep, ms)

  defp schedule_sweep(_), do: :ok

  @doc """
  Run the orphan scan once. Returns a summary map. Never raises — a missing
  table or any DB error degrades to `%{scanned: 0, skipped: reason}`.
  """
  @spec scan(module()) :: map()
  def scan(repo \\ EffectRepo) do
    case safe_orphans(repo) do
      {:ok, []} ->
        %{scanned: 0, recovered: 0}

      {:ok, orphans} ->
        outcomes = Enum.map(orphans, &reconcile_one(&1, repo))
        counts = Enum.frequencies_by(outcomes, &outcome_kind/1)
        Logger.warning("effect_recovery: drained #{length(orphans)} orphan(s): #{inspect(counts)}")
        %{scanned: length(orphans), outcomes: counts}

      {:skip, reason} ->
        # Expected before migration 052 is applied, or on a transient DB error.
        Logger.info("effect_recovery: orphan scan skipped (#{inspect(reason)})")
        %{scanned: 0, skipped: reason}
    end
  end

  defp safe_orphans(repo) do
    case repo.orphaned_payloads() do
      {:ok, rows} -> {:ok, rows}
      {:error, reason} -> {:skip, reason}
    end
  rescue
    e -> {:skip, {:exception, Exception.message(e)}}
  end

  defp reconcile_one(payload, repo) do
    EffectReconcile.reconcile_payload(payload, repo)
  rescue
    e ->
      Logger.error("effect_recovery: reconcile crashed for #{inspect(Map.get(payload, :effect_id))}: #{Exception.message(e)}")
      {:quarantined, :reconcile_crash}
  end

  defp outcome_kind(:committed), do: :committed
  defp outcome_kind(:tombstoned), do: :tombstoned
  defp outcome_kind({:quarantined, _}), do: :quarantined
  defp outcome_kind(_), do: :unknown
end
