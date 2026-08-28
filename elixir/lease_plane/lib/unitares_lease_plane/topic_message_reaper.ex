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
  bounded batch, periodic, no state.
  """

  @spec run_once() :: :ok | {:error, term()}
  def run_once do
    case UnitaresLeasePlane.Repo.purge_expired_messages() do
      {:ok, _count} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end
end
