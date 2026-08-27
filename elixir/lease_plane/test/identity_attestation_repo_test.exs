defmodule UnitaresLeasePlane.IdentityAttestationRepoTest do
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.Repo

  test "nonce rows survive the expiry boundary and purge after the safety margin" do
    suffix = System.unique_integer([:positive])
    issuer = "nonce-retention-test-#{suffix}"
    recent_jti = "recent-#{suffix}"
    old_jti = "old-#{suffix}"
    now = System.system_time(:second)

    on_exit(fn ->
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "DELETE FROM lease_plane.consumed_identity_attestations WHERE issuer = $1",
        [issuer]
      )
    end)

    assert :ok = Repo.consume_identity_attestation(issuer, recent_jti, now - 30)
    assert :ok = Repo.consume_identity_attestation(issuer, old_jti, now - 120)
    assert {:ok, _count} = Repo.purge_expired_identity_attestations()

    assert {:error, :replayed} =
             Repo.consume_identity_attestation(issuer, recent_jti, now - 30)

    assert :ok = Repo.consume_identity_attestation(issuer, old_jti, now - 120)
  end
end
