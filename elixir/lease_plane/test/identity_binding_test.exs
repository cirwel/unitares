defmodule UnitaresLeasePlane.IdentityBindingTest do
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.IdentityBinding

  setup do
    previous_mode = Application.get_env(:lease_plane, :identity_binding_mode)
    previous_verifier = Application.get_env(:lease_plane, :identity_verifier)
    previous_kinds = Application.get_env(:lease_plane, :identity_bound_surface_kinds)
    previous_format = Application.get_env(:lease_plane, :identity_proof_format)

    on_exit(fn ->
      restore_env(:identity_binding_mode, previous_mode)
      restore_env(:identity_verifier, previous_verifier)
      restore_env(:identity_bound_surface_kinds, previous_kinds)
      restore_env(:identity_proof_format, previous_format)
    end)

    :ok
  end

  test "off preserves the compatibility path without consulting governance" do
    Application.put_env(:lease_plane, :identity_binding_mode, :off)
    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> flunk("called") end)

    assert :ok = IdentityBinding.authorize("11111111-1111-4111-8111-111111111111", nil)
  end

  test "enforce accepts only an explicitly verified matching proof" do
    holder = "11111111-1111-4111-8111-111111111111"
    proof = "v1.payload.signature"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(:lease_plane, :identity_verifier, fn
      ^holder, ^proof -> :ok
      _, _ -> {:error, :invalid}
    end)

    assert :ok = IdentityBinding.authorize(holder, proof)
    assert {:error, :identity_proof_invalid} = IdentityBinding.authorize(holder, nil)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize(holder, "v1.other.signature")
  end

  test "enforce distinguishes invalid proof from verifier unavailability" do
    holder = "11111111-1111-4111-8111-111111111111"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> {:error, :invalid} end)
    assert {:error, :identity_proof_invalid} = IdentityBinding.authorize(holder, "bad")

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> {:error, :timeout} end)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize(holder, "proof")

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> raise "boom" end)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize(holder, "proof")
  end

  test "mode parser fails closed on a misspelled rollout value" do
    assert IdentityBinding.parse_mode(nil) == :off
    assert IdentityBinding.parse_mode("LOG") == :log
    assert IdentityBinding.parse_mode(" enforce ") == :enforce

    assert_raise ArgumentError, fn -> IdentityBinding.parse_mode("enabled") end
  end

  test "proof-format and trusted-issuer parsers fail closed" do
    assert IdentityBinding.parse_proof_format(nil) == :hybrid
    assert IdentityBinding.parse_proof_format(" ATTESTATION ") == :attestation
    assert_raise ArgumentError, fn -> IdentityBinding.parse_proof_format("signed-ish") end

    assert IdentityBinding.parse_trusted_issuers(nil) == %{}

    assert IdentityBinding.parse_trusted_issuers(
             ~s({"operator-a":"https://operator-a.example/keys"})
           ) == %{"operator-a" => "https://operator-a.example/keys"}

    assert_raise ArgumentError, fn -> IdentityBinding.parse_trusted_issuers("[]") end

    assert_raise ArgumentError, fn ->
      IdentityBinding.parse_trusted_issuers(~s({"operator-a":"file:///tmp/key"}))
    end

    assert_raise ArgumentError, fn ->
      IdentityBinding.parse_trusted_issuers(
        ~s({"operator-a":"https://a.example/keys","operator-b":"https://b.example/keys"})
      )
    end

    assert IdentityBinding.parse_attestation_audience(" plane-a ") == "plane-a"
    assert_raise ArgumentError, fn -> IdentityBinding.parse_attestation_audience("two words") end

    assert IdentityBinding.parse_insecure_http_urls(
             "http://governance-mcp:8767/v1/lease-holder/keys"
           ) == MapSet.new(["http://governance-mcp:8767/v1/lease-holder/keys"])

    assert_raise ArgumentError, fn ->
      IdentityBinding.parse_insecure_http_urls("https://operator.example/keys")
    end
  end

  test "attestation-only mode refuses legacy proof before any governance call" do
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
    Application.put_env(:lease_plane, :identity_proof_format, :attestation)
    Application.delete_env(:lease_plane, :identity_verifier)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize(
               "11111111-1111-4111-8111-111111111111",
               "v1.legacy.signature",
               "agent",
               %{
                 method: "POST",
                 path: "/v1/lease/acquire",
                 body_sha256: String.duplicate("0", 64)
               }
             )
  end

  test "request context hashes exact retained body bytes" do
    raw = ~s({"lease_id":"abc", "spacing":"is-significant"})
    conn = Plug.Test.conn(:post, "/v1/lease/renew", raw)
    assert {:ok, ^raw, conn} = UnitaresLeasePlane.RawBodyReader.read_body(conn, [])

    assert IdentityBinding.request_context(conn) == %{
             method: "POST",
             path: "/v1/lease/renew",
             body_sha256: :crypto.hash(:sha256, raw) |> Base.encode16(case: :lower)
           }
  end

  test "surface-kind rollout can enforce maintenance without blocking agent presence" do
    holder = "11111111-1111-4111-8111-111111111111"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(
      :lease_plane,
      :identity_bound_surface_kinds,
      IdentityBinding.parse_surface_kinds("maintenance,file")
    )

    assert IdentityBinding.required_for_surface?("maintenance")
    assert IdentityBinding.required_for_surface?("file")
    refute IdentityBinding.required_for_surface?("agent")

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize(holder, nil, "maintenance")

    assert :ok = IdentityBinding.authorize(holder, nil, "agent")
  end

  defp restore_env(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore_env(key, value), do: Application.put_env(:lease_plane, key, value)
end
