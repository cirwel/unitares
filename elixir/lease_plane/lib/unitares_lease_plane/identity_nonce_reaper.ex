defmodule UnitaresLeasePlane.IdentityNonceReaper do
  @moduledoc """
  Removes expired request-attestation nonce rows in bounded batches.

  Registered as a `PeriodicWorker`, whose contract is `perform/1` returning
  `{:ok, summary}`. This module previously exported only `run_once/0`, so every
  scheduled tick raised `undefined or private` and the purge never ran once —
  leaving `lease_plane.consumed_identity_attestations` to grow without bound
  (621 rows, all past `expires_at`, when this was found). Migration 067's
  "rows may be purged only after expires_at" is this worker; keep the arity the
  scheduler actually calls.
  """

  @spec perform(map()) :: {:ok, %{purged: non_neg_integer()}} | {:error, term()}
  def perform(_args \\ %{}) do
    case UnitaresLeasePlane.Repo.purge_expired_identity_attestations() do
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
