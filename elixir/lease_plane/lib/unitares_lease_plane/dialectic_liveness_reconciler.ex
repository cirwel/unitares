defmodule UnitaresLeasePlane.DialecticLivenessReconciler do
  @moduledoc """
  Periodic reconciler that keeps a `DialecticLiveness` process alive for every
  active dialectic session (Slice 2). Because BEAM does not yet own session
  *creation* (Python still mints sessions), this bridges the gap: it reads the
  active set and ensures a watcher exists for each. Liveness processes
  self-terminate when their session goes terminal, so the reconciler only needs
  to *start* missing ones — it never has to stop anything.

  Run by `PeriodicWorker` alongside the lease Reaper / saga reaper.
  """

  alias UnitaresLeasePlane.{DialecticSaga, DialecticLivenessSupervisor}

  require Logger

  @spec perform(map()) :: {:ok, map()} | {:error, term()}
  def perform(_args) do
    case DialecticSaga.live_sessions(500) do
      {:ok, sessions} ->
        started =
          sessions
          |> Enum.map(& &1.session_id)
          |> Enum.map(&DialecticLivenessSupervisor.ensure_started/1)
          |> Enum.count(&(&1 == :started))

        {:ok, %{active_sessions: length(sessions), started: started}}

      {:error, reason} ->
        {:error, reason}
    end
  end
end
