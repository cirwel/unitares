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
  def init(opts) do
    repo = Keyword.get(opts, :repo, EffectRepo)
    result = scan(repo)
    {:ok, result}
  end

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
        Logger.warning("effect_recovery: drained #{length(orphans)} orphan(s) at boot: #{inspect(counts)}")
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
