defmodule UnitaresLeasePlane.IdentityNonceReaper do
  @moduledoc "Removes expired request-attestation nonce rows in bounded batches."

  @spec run_once() :: :ok | {:error, term()}
  def run_once do
    case UnitaresLeasePlane.Repo.purge_expired_identity_attestations() do
      {:ok, _count} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end
end
