defmodule UnitaresLeasePlane.GovernanceIdentityClientTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.GovernanceIdentityClient

  @holder "11111111-1111-4111-8111-111111111111"

  test "accepts only an explicit verified response for the expected holder" do
    body = Jason.encode!(%{ok: true, verified: true, holder_agent_uuid: @holder})
    assert GovernanceIdentityClient.parse_verified(body, @holder) == :ok

    other =
      Jason.encode!(%{
        ok: true,
        verified: true,
        holder_agent_uuid: "22222222-2222-4222-8222-222222222222"
      })

    assert GovernanceIdentityClient.parse_verified(other, @holder) == {:error, :invalid}
  end

  test "invalid and malformed responses fail closed" do
    denied = Jason.encode!(%{ok: false, verified: false, error: "identity_proof_invalid"})
    assert GovernanceIdentityClient.parse_verified(denied, @holder) == {:error, :invalid}

    assert GovernanceIdentityClient.parse_verified("not-json", @holder) ==
             {:error, :bad_identity_verifier_json}
  end

  test "only a proof rejection is invalid; operator and verifier failures are unavailable" do
    assert GovernanceIdentityClient.classify_response(403, "", @holder) == {:error, :invalid}

    assert GovernanceIdentityClient.classify_response(401, "", @holder) ==
             {:error, {:identity_verifier_status, 401}}

    assert GovernanceIdentityClient.classify_response(503, "", @holder) ==
             {:error, {:identity_verifier_status, 503}}
  end
end
