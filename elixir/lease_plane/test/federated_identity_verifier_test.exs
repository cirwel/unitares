defmodule UnitaresLeasePlane.FederatedIdentityVerifierTest do
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.{FederatedIdentityVerifier, OperatorKeyCache}

  @vector_path Path.expand("../../../tests/vectors/lease_attestation.json", __DIR__)

  setup do
    vector = @vector_path |> File.read!() |> Jason.decode!()
    jwk = vector["jwks"]["keys"] |> hd()
    public_key = jwk |> Map.fetch!("x") |> Base.url_decode64!(padding: false)
    expected_kid = Map.fetch!(jwk, "kid")

    previous = %{
      trusted: Application.get_env(:lease_plane, :trusted_identity_issuers),
      fetcher: Application.get_env(:lease_plane, :operator_key_fetcher),
      consumer: Application.get_env(:lease_plane, :identity_nonce_consumer),
      clock: Application.get_env(:lease_plane, :identity_clock),
      audience: Application.get_env(:lease_plane, :identity_attestation_audience),
      insecure_urls: Application.get_env(:lease_plane, :insecure_operator_key_urls)
    }

    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      vector["issuer"] => "https://operator-a.example/v1/lease-holder/keys"
    })

    Application.put_env(:lease_plane, :operator_key_fetcher, fn issuer, kid, _url ->
      if issuer == vector["issuer"] and kid == expected_kid do
        {:ok, public_key}
      else
        {:error, :invalid}
      end
    end)

    {:ok, consumed} = Agent.start_link(fn -> MapSet.new() end)

    Application.put_env(:lease_plane, :identity_nonce_consumer, fn issuer, jti, _exp ->
      Agent.get_and_update(consumed, fn seen ->
        key = {issuer, jti}

        if MapSet.member?(seen, key),
          do: {{:error, :replayed}, seen},
          else: {:ok, MapSet.put(seen, key)}
      end)
    end)

    Application.put_env(:lease_plane, :identity_clock, fn -> vector["now"] end)
    Application.put_env(
      :lease_plane,
      :identity_attestation_audience,
      vector["jwks"]["audience"]
    )

    OperatorKeyCache.evict_issuer(vector["issuer"])

    on_exit(fn ->
      restore_env(:trusted_identity_issuers, previous.trusted)
      restore_env(:operator_key_fetcher, previous.fetcher)
      restore_env(:identity_nonce_consumer, previous.consumer)
      restore_env(:identity_clock, previous.clock)
      restore_env(:identity_attestation_audience, previous.audience)
      restore_env(:insecure_operator_key_urls, previous.insecure_urls)
    end)

    {:ok, vector: vector}
  end

  test "verifies the Python vector and consumes its nonce once", %{vector: vector} do
    context = request_context(vector)

    assert :ok =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               context
             )

    assert {:error, :replayed} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               context
             )
  end

  test "refuses retargeting, body changes, expiry, and signature tampering", %{vector: vector} do
    context = request_context(vector)

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               "22222222-2222-4222-8222-222222222222",
               vector["token"],
               context
             )

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               %{context | body_sha256: String.duplicate("0", 64)}
             )

    {:ok, claims, _input, _signature} = FederatedIdentityVerifier.parse_token(vector["token"])

    assert {:error, :invalid} =
             FederatedIdentityVerifier.validate_claims(
               claims,
               vector["holder_agent_uuid"],
               context,
               2_000_000_030
             )

    assert {:error, :invalid} =
             FederatedIdentityVerifier.validate_claims(
               claims,
               vector["holder_agent_uuid"],
               context,
               2_000_000_031
             )

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"] <> "x",
               context
             )
  end

  test "non-allowlisted issuers are invalid while key outages are unavailable", %{vector: vector} do
    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      "operator-b" => "https://operator-b.example/keys"
    })

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )

    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      vector["issuer"] => "https://operator-a.example/keys"
    })

    Application.put_env(:lease_plane, :operator_key_fetcher, fn _, _, _ ->
      {:error, :unreachable}
    end)

    OperatorKeyCache.evict_issuer(vector["issuer"])

    assert {:error, :operator_keys_unavailable} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )
  end

  test "runtime configuration also refuses multiple trusted issuers", %{vector: vector} do
    Application.put_env(:lease_plane, :trusted_identity_issuers, %{
      vector["issuer"] => "https://operator-a.example/keys",
      "operator-b" => "https://operator-b.example/keys"
    })

    assert {:error, :multiple_trusted_identity_issuers_unsupported} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )
  end

  test "HTTPS operator keys require CA and hostname verification" do
    https_options =
      FederatedIdentityVerifier.http_options("https://operator-a.example/v1/lease-holder/keys")

    ssl = Keyword.fetch!(https_options, :ssl)
    assert Keyword.fetch!(ssl, :verify) == :verify_peer
    assert is_list(Keyword.fetch!(ssl, :cacerts))

    hostname = Keyword.fetch!(ssl, :customize_hostname_check)
    assert is_function(Keyword.fetch!(hostname, :match_fun))

    refute Keyword.has_key?(
             FederatedIdentityVerifier.http_options(
               "http://governance-mcp:8767/v1/lease-holder/keys"
             ),
             :ssl
           )
  end

  test "HTTP key retrieval is limited to an exact configured internal URL" do
    local_url = "http://governance-mcp:8767/v1/lease-holder/keys"
    Application.put_env(:lease_plane, :insecure_operator_key_urls, MapSet.new([local_url]))

    assert :ok = FederatedIdentityVerifier.validate_jwks_url(local_url)

    assert {:error, :invalid} =
             FederatedIdentityVerifier.validate_jwks_url(
               "http://remote.example/v1/lease-holder/keys"
             )

    assert :ok =
             FederatedIdentityVerifier.validate_jwks_url(
               "https://remote.example/v1/lease-holder/keys"
             )
  end

  test "configured destination audience is mandatory and exact", %{vector: vector} do
    Application.put_env(:lease_plane, :identity_attestation_audience, "other-plane")

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )

    Application.delete_env(:lease_plane, :identity_attestation_audience)

    assert {:error, :identity_attestation_audience_unset} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )
  end

  test "unknown key ids do not refetch a cached issuer document", %{vector: vector} do
    jwk = hd(vector["jwks"]["keys"])
    expected_kid = jwk["kid"]
    public_key = Base.url_decode64!(jwk["x"], padding: false)
    {:ok, fetch_count} = Agent.start_link(fn -> 0 end)

    Application.put_env(:lease_plane, :operator_key_fetcher, fn _issuer, kid, _url ->
      Agent.update(fetch_count, &(&1 + 1))

      if kid == expected_kid,
        do: {:ok, public_key},
        else: {:error, :invalid}
    end)

    assert :ok =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               vector["token"],
               request_context(vector)
             )

    {:ok, claims, _input, _signature} = FederatedIdentityVerifier.parse_token(vector["token"])
    ["lat", "v1", _payload, signature] = String.split(vector["token"], ".")

    unknown_payload =
      claims
      |> Map.put("kid", "unknown-key-id")
      |> Jason.encode!()
      |> Base.url_encode64(padding: false)

    unknown_token = "lat.v1.#{unknown_payload}.#{signature}"

    assert {:error, :invalid} =
             FederatedIdentityVerifier.verify(
               vector["holder_agent_uuid"],
               unknown_token,
               request_context(vector)
             )

    assert Agent.get(fetch_count, & &1) == 1
  end

  defp request_context(vector) do
    %{
      method: vector["method"],
      path: vector["path"],
      body_sha256: vector["body_sha256"]
    }
  end

  defp restore_env(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore_env(key, value), do: Application.put_env(:lease_plane, key, value)
end
