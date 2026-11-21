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

  test "an unset mode verifies and logs rather than inspecting nothing" do
    assert IdentityBinding.parse_mode(nil) == :log
    assert IdentityBinding.parse_mode("") == :log
    assert IdentityBinding.parse_mode("off") == :off

    Application.delete_env(:lease_plane, :identity_binding_mode)
    assert IdentityBinding.mode() == :log
    assert IdentityBinding.enabled?()
  end

  test "posture warning names anything short of enforce on every kind" do
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
    Application.put_env(:lease_plane, :identity_bound_surface_kinds, :all)
    assert IdentityBinding.posture_warning() == nil
    assert IdentityBinding.bound_surface_kinds() == :all

    Application.put_env(
      :lease_plane,
      :identity_bound_surface_kinds,
      MapSet.new(["maintenance", "agent"])
    )

    assert IdentityBinding.bound_surface_kinds() == ["agent", "maintenance"]
    assert IdentityBinding.posture_warning() =~ "enforces only surface kinds [agent,maintenance]"

    Application.put_env(:lease_plane, :identity_binding_mode, :log)
    Application.put_env(:lease_plane, :identity_bound_surface_kinds, :all)
    assert IdentityBinding.posture_warning() =~ "mode is log"

    Application.put_env(:lease_plane, :identity_binding_mode, :off)
    assert IdentityBinding.posture_warning() =~ "mode is off"
  end

  test "mode parser fails closed on a misspelled rollout value" do
    assert IdentityBinding.parse_mode("LOG") == :log
    assert IdentityBinding.parse_mode(" enforce ") == :enforce

    assert_raise ArgumentError, fn -> IdentityBinding.parse_mode("enabled") end
  end

  test "surface-kind parser recognizes the all sentinel" do
    assert IdentityBinding.parse_surface_kinds(nil) == :all
    assert IdentityBinding.parse_surface_kinds(" ALL ") == :all

    assert IdentityBinding.parse_surface_kinds("maintenance,agent,dialectic") ==
             MapSet.new(["maintenance", "agent", "dialectic"])
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

    assert IdentityBinding.parse_trusted_issuers(
             ~s({"operator-a":"https://a.example/keys","operator-b":"https://b.example/keys"})
           ) == %{
             "operator-a" => "https://a.example/keys",
             "operator-b" => "https://b.example/keys"
           }

    too_many =
      1..33
      |> Enum.into(%{}, fn i -> {"operator-#{i}", "https://op-#{i}.example/keys"} end)
      |> Jason.encode!()

    assert_raise ArgumentError, ~r/at most 32/, fn ->
      IdentityBinding.parse_trusted_issuers(too_many)
    end

    assert_raise ArgumentError, fn ->
      IdentityBinding.parse_trusted_issuers(~s({"bad issuer":"https://a.example/keys"}))
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

  test "authorize_lease_principal returns verified principal and enforces expected_issuer" do
    subject = "11111111-1111-4111-8111-111111111111"
    ctx = %{method: "POST", path: "/v1/lease/acquire", body_sha256: String.duplicate("a", 64)}

    # :off never consults verifier and never returns a principal
    Application.put_env(:lease_plane, :identity_binding_mode, :off)
    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> flunk("called") end)

    assert {:ok, nil} =
             IdentityBinding.authorize_lease_principal(subject, "anything", "agent", ctx)

    # :log returns verified principal on success, nil on failure (never unverified principal)
    Application.put_env(:lease_plane, :identity_binding_mode, :log)

    Application.put_env(:lease_plane, :identity_verifier, fn
      ^subject, "good" -> {:ok, %{issuer: "op-a", subject: subject}}
      _, _ -> {:error, :invalid}
    end)

    assert {:ok, %{issuer: "op-a", subject: ^subject}} =
             IdentityBinding.authorize_lease_principal(subject, "good", "agent", ctx)

    assert {:ok, nil} =
             IdentityBinding.authorize_lease_principal(subject, "bad", "agent", ctx)

    # :enforce returns verified principal, checks expected_issuer, and rejects legacy :ok when expected_issuer is set
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    assert {:ok, %{issuer: "op-a", subject: ^subject}} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "good",
               "agent",
               ctx,
               expected_issuer: "op-a"
             )

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "good",
               "agent",
               ctx,
               expected_issuer: "op-b"
             )

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> :ok end)

    assert {:ok, nil} =
             IdentityBinding.authorize_lease_principal(subject, "legacy-proof", "agent", ctx)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "legacy-proof",
               "agent",
               ctx,
               expected_issuer: "op-a"
             )
  end

  test "already-attested lease refuses legacy proof even in hybrid mode" do
    subject = "11111111-1111-4111-8111-111111111111"
    ctx = %{method: "POST", path: "/v1/lease/renew", body_sha256: String.duplicate("b", 64)}

    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
    Application.put_env(:lease_plane, :identity_proof_format, :hybrid)
    Application.delete_env(:lease_plane, :identity_verifier)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "v1.legacy.signature",
               "agent",
               ctx,
               expected_issuer: "operator-a"
             )

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               nil,
               "agent",
               ctx,
               expected_issuer: "operator-a"
             )

    Application.put_env(:lease_plane, :identity_binding_mode, :log)

    assert {:ok, nil} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "v1.legacy.signature",
               "agent",
               ctx,
               expected_issuer: "operator-a"
             )
  end

  test "require_attestation refuses legacy proofs in hybrid mode and :ok verifier overrides" do
    subject = "11111111-1111-4111-8111-111111111111"
    ctx = %{method: "POST", path: "/v1/lease/acquire", body_sha256: String.duplicate("d", 64)}

    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
    Application.put_env(:lease_plane, :identity_proof_format, :hybrid)
    Application.delete_env(:lease_plane, :identity_verifier)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "v1.legacy.signature",
               "agent",
               ctx,
               require_attestation: true
             )

    Application.put_env(:lease_plane, :identity_verifier, fn ^subject, "legacy-for-a" -> :ok end)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize_lease_principal(
               subject,
               "legacy-for-a",
               "agent",
               ctx,
               require_attestation: true
             )
  end

  test "multi-issuer config succeeds on authorize_lease_principal while authorize/4 and authorize_strict/3 stay fail-closed" do
    {pub_a, priv_a} = :crypto.generate_key(:eddsa, :ed25519)
    {pub_b, _priv_b} = :crypto.generate_key(:eddsa, :ed25519)
    subject = "11111111-1111-4111-8111-111111111111"
    now = System.system_time(:second)
    ctx = %{method: "POST", path: "/v1/lease/acquire", body_sha256: String.duplicate("c", 64)}

    prev_trusted = Application.get_env(:lease_plane, :trusted_identity_issuers)
    prev_fetcher = Application.get_env(:lease_plane, :operator_key_fetcher)
    prev_consumer = Application.get_env(:lease_plane, :identity_nonce_consumer)
    prev_audience = Application.get_env(:lease_plane, :identity_attestation_audience)
    prev_clock = Application.get_env(:lease_plane, :identity_clock)

    on_exit(fn ->
      restore_env(:trusted_identity_issuers, prev_trusted)
      restore_env(:operator_key_fetcher, prev_fetcher)
      restore_env(:identity_nonce_consumer, prev_consumer)
      restore_env(:identity_attestation_audience, prev_audience)
      restore_env(:identity_clock, prev_clock)
    end)

    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
    Application.put_env(:lease_plane, :identity_proof_format, :attestation)
    Application.delete_env(:lease_plane, :identity_verifier)
    Application.put_env(:lease_plane, :identity_attestation_audience, "plane-test")
    Application.put_env(:lease_plane, :identity_clock, fn -> now end)
    Application.put_env(:lease_plane, :identity_nonce_consumer, fn _, _, _ -> :ok end)

    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      "operator-a" => "https://a.example/keys",
      "operator-b" => "https://b.example/keys"
    })

    Application.put_env(:lease_plane, :operator_key_fetcher, fn
      "operator-a", "kid-a", _ -> {:ok, pub_a}
      "operator-b", "kid-b", _ -> {:ok, pub_b}
      _, _, _ -> {:error, :invalid}
    end)

    UnitaresLeasePlane.OperatorKeyCache.evict_issuer("operator-a")
    UnitaresLeasePlane.OperatorKeyCache.evict_issuer("operator-b")

    claims = %{
      "v" => 1,
      "typ" => "lease-attestation",
      "alg" => "EdDSA",
      "iss" => "operator-a",
      "kid" => "kid-a",
      "aud" => "plane-test",
      "sub" => subject,
      "mth" => ctx.method,
      "pth" => ctx.path,
      "bsha" => ctx.body_sha256,
      "iat" => now,
      "nbf" => now,
      "exp" => now + 30,
      "jti" => Base.url_encode64(:crypto.strong_rand_bytes(16), padding: false)
    }

    payload_b64 = claims |> Jason.encode!() |> Base.url_encode64(padding: false)
    signing_input = "lat.v1." <> payload_b64
    sig = :crypto.sign(:eddsa, :none, signing_input, [priv_a, :ed25519])
    token = signing_input <> "." <> Base.url_encode64(sig, padding: false)

    assert {:ok, %{issuer: "operator-a", subject: ^subject}} =
             IdentityBinding.authorize_lease_principal(subject, token, "agent", ctx)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize_lease_principal(
               subject,
               token,
               "agent",
               ctx,
               allow_multiple_issuers: false
             )

    refute IdentityBinding.single_or_no_trusted_issuer?()

    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      "operator-a" => "https://a.example/keys"
    })

    assert IdentityBinding.single_or_no_trusted_issuer?()

    assert {:ok, %{issuer: "operator-a", subject: ^subject}} =
             IdentityBinding.authorize_lease_principal(
               subject,
               token,
               "agent",
               ctx,
               allow_multiple_issuers: false
             )

    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      "operator-a" => "https://a.example/keys",
      "operator-b" => "https://b.example/keys"
    })

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize(subject, token, "dialectic_beam", ctx)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize_strict(subject, token, ctx)
  end

  defp restore_env(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore_env(key, value), do: Application.put_env(:lease_plane, key, value)
end
