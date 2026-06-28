defmodule UnitaresLeasePlane.DialecticSagaReaper do
  @moduledoc """
  Periodic recovery worker for orphaned dialectic resolution sagas.

  A saga left `reserved` by a resolver that crashed mid-commit would otherwise
  hold the one-pending slot until that exact session is re-claimed. This worker
  reverts any such orphan on a timer (via `DialecticSaga.reclaim_all_stale/0`)
  so a crash can never permanently wedge a session, even one that is never
  resolved again. Always safe: it only touches `reserved` rows older than the
  staleness floor, of which there are normally zero, so it is a no-op until a
  real crash leaves one behind.

  Scheduled by `PeriodicWorker` from the application supervision tree, alongside
  the lease Reaper / HandoffTimeout / AuditOutboxForwarder.
  """

  alias UnitaresLeasePlane.DialecticSaga

  require Logger

  @spec perform(map()) :: {:ok, map()} | {:error, term()}
  def perform(_args) do
    case DialecticSaga.reclaim_all_stale() do
      {:ok, 0} ->
        {:ok, %{reclaimed: 0}}

      {:ok, n} ->
        Logger.warning("dialectic_saga_reaper: reverted #{n} orphaned reserved saga(s)")
        {:ok, %{reclaimed: n}}

      {:error, reason} ->
        {:error, reason}
    end
  end
end
