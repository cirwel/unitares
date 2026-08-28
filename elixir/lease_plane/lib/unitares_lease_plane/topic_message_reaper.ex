defmodule UnitaresLeasePlane.TopicMessageReaper do
  @moduledoc """
  Deletes expired `lease_plane.topic_messages` rows in bounded batches.

  ⛔This worker is what makes the transport's central claim TRUE. The table
  exists because governance-KG channel notes had no natural end — 45 of 59 were
  still `status='open'` — and the answer was an expiry. But an expiry that is
  only ever applied as a read filter is not an expiry: the rows stay forever,
  and the table becomes the same permanently-accumulating store it replaced,
  just one nothing searches. Migration 069 says "ephemeral BY CONSTRUCTION";
  without this child in the supervision tree that comment is false.

  Mirrors `IdentityNonceReaper` (migration 067), which is the same shape:
  bounded batch, periodic, no state — including the arity bug both carried:
  `PeriodicWorker` calls `perform/1`, so exporting only `run_once/0` meant
  this worker raised on every tick and never deleted a single row.
  """

  @spec perform(map()) :: {:ok, %{purged: non_neg_integer()}} | {:error, term()}
  def perform(_args \\ %{}) do
    case UnitaresLeasePlane.Repo.purge_expired_messages() do
      {:ok, count} -> {:ok, %{purged: count}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Discard-the-count wrapper kept for direct callers and tests.

  `PeriodicWorker` calls `perform/1`; this is not the scheduled entrypoint.
  """
  @spec run_once() :: :ok | {:error, term()}
  def run_once do
    case perform(%{}) do
      {:ok, _summary} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end
end
