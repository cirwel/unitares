defmodule UnitaresLeasePlane.FederatedIdentityVerifier do
  @moduledoc """
  Offline verifier for request-bound `lat.v1` operator attestations.

  The untrusted envelope is inspected only far enough to select an explicitly
  allowlisted issuer and key id.  Public Ed25519 keys are fetched from the
  operator's configured JWKS URL and cached briefly.  A valid claim is consumed
  in PostgreSQL before the mutation proceeds, making its `jti` single-use.
  """

  alias UnitaresLeasePlane.{OperatorKeyCache, Repo}

  @prefix "lat.v1."
  @clock_skew_seconds 5
  @max_token_bytes 8_192
  @max_jwks_bytes 65_536

  @type request_context :: %{method: String.t(), path: String.t(), body_sha256: String.t()}

  @spec verify(String.t(), String.t(), request_context()) ::
          :ok | {:error, :invalid | :replayed | term()}
  def verify(holder_agent_uuid, token, context)
      when is_binary(holder_agent_uuid) and is_binary(token) and is_map(context) do
    with {:ok, audience} <- configured_audience(),
         {:ok, claims, signing_input, signature} <- parse_token(token),
         {:ok, issuer, kid, jwks_url} <- trusted_key_location(claims),
         {:ok, public_key} <- resolve_public_key(issuer, kid, jwks_url, audience),
         true <- valid_signature?(signing_input, signature, public_key),
         :ok <- validate_claims(claims, holder_agent_uuid, context, current_time(), audience),
         :ok <- consume_nonce(issuer, claims["jti"], claims["exp"]) do
      :ok
    else
      false -> {:error, :invalid}
      {:error, :replayed} -> {:error, :replayed}
      {:error, :invalid} -> {:error, :invalid}
      {:error, reason} -> {:error, reason}
      _ -> {:error, :invalid}
    end
  rescue
    _exception -> {:error, :verification_exception}
  catch
    _kind, _reason -> {:error, :verification_exception}
  end

  def verify(_holder_agent_uuid, _token, _context), do: {:error, :invalid}

  @doc false
  def parse_token(token) when is_binary(token) and byte_size(token) <= @max_token_bytes do
    if String.starts_with?(token, @prefix) do
      rest = binary_part(token, byte_size(@prefix), byte_size(token) - byte_size(@prefix))

      case String.split(rest, ".", parts: 2) do
        [payload_b64, signature_b64] ->
          with {:ok, payload} <- decode_b64url(payload_b64),
               {:ok, signature} <- decode_b64url(signature_b64),
               true <- byte_size(signature) == 64,
               {:ok, %{} = claims} <- Jason.decode(payload) do
            {:ok, claims, @prefix <> payload_b64, signature}
          else
            _ -> {:error, :invalid}
          end

        _ ->
          {:error, :invalid}
      end
    else
      {:error, :invalid}
    end
  end

  def parse_token(_token), do: {:error, :invalid}

  @doc false
  def validate_claims(claims, holder_agent_uuid, context, now \\ System.system_time(:second)) do
    case configured_audience() do
      {:ok, audience} -> validate_claims(claims, holder_agent_uuid, context, now, audience)
      {:error, reason} -> {:error, reason}
    end
  end

  @doc false
  def validate_claims(claims, holder_agent_uuid, context, now, audience) do
    iat = claims["iat"]
    nbf = claims["nbf"]
    exp = claims["exp"]
    jti = claims["jti"]

    valid =
      claims["v"] == 1 and
        claims["typ"] == "lease-attestation" and
        claims["alg"] == "EdDSA" and
        claims["aud"] == audience and
        claims["sub"] == holder_agent_uuid and
        claims["mth"] == context.method and
        claims["pth"] == context.path and
        claims["bsha"] == context.body_sha256 and
        is_integer(iat) and is_integer(nbf) and is_integer(exp) and
        nbf == iat and exp > iat and exp - iat <= 60 and
        iat <= now + @clock_skew_seconds and
        nbf <= now + @clock_skew_seconds and exp > now and
        valid_jti?(jti)

    if valid, do: :ok, else: {:error, :invalid}
  end

  defp trusted_key_location(claims) do
    issuer = claims["iss"]
    kid = claims["kid"]
    trusted = Application.get_env(:lease_plane, :trusted_identity_issuers, %{})

    cond do
      not is_binary(issuer) or byte_size(issuer) == 0 or byte_size(issuer) > 256 ->
        {:error, :invalid}

      not is_binary(kid) or byte_size(kid) == 0 or byte_size(kid) > 128 ->
        {:error, :invalid}

      not is_map(trusted) or map_size(trusted) == 0 ->
        {:error, :trusted_identity_issuers_unset}

      map_size(trusted) > 1 ->
        {:error, :multiple_trusted_identity_issuers_unsupported}

      not Map.has_key?(trusted, issuer) ->
        {:error, :invalid}

      true ->
        {:ok, issuer, kid, Map.fetch!(trusted, issuer)}
    end
  end

  defp resolve_public_key(issuer, kid, jwks_url, audience) do
    case OperatorKeyCache.get(issuer, kid) do
      public_key when is_binary(public_key) -> {:ok, public_key}
      :missing -> {:error, :invalid}
      nil -> fetch_public_key(issuer, kid, jwks_url, audience)
    end
  end

  @doc false
  def fetch_public_key(issuer, kid, jwks_url, audience) do
    fetcher = Application.get_env(:lease_plane, :operator_key_fetcher)

    result =
      cond do
        is_function(fetcher, 3) -> fetcher.(issuer, kid, jwks_url)
        true -> fetch_public_key_http(issuer, jwks_url, audience)
      end

    keyset_result =
      case result do
        {:ok, public_key} when is_binary(public_key) and byte_size(public_key) == 32 ->
          {:ok, %{kid => public_key}}

        {:ok, keys} when is_map(keys) and map_size(keys) in 1..64 ->
          {:ok, keys}

        other ->
          other
      end

    case keyset_result do
      {:ok, keys} ->
        :ok = OperatorKeyCache.put(issuer, keys)

        case Map.fetch(keys, kid) do
          {:ok, public_key} when is_binary(public_key) and byte_size(public_key) == 32 ->
            {:ok, public_key}

          _ ->
            {:error, :invalid}
        end

      {:error, :invalid} ->
        {:error, :invalid}

      _ ->
        {:error, :operator_keys_unavailable}
    end
  end

  defp fetch_public_key_http(issuer, jwks_url, audience) do
    with :ok <- validate_jwks_url(jwks_url),
         request = {String.to_charlist(jwks_url), [{~c"accept", ~c"application/json"}]},
         {:ok, {{_version, 200, _reason}, _headers, body}} <-
           :httpc.request(:get, request, http_options(jwks_url), body_format: :binary),
         true <- byte_size(body) <= @max_jwks_bytes,
         {:ok,
          %{
            "ok" => true,
            "issuer" => ^issuer,
            "audience" => ^audience,
            "keys" => keys
          }} <- Jason.decode(body),
         {:ok, public_keys} <- keys_from_jwks(keys) do
      {:ok, public_keys}
    else
      {:error, :invalid} -> {:error, :invalid}
      _ -> {:error, :operator_keys_unavailable}
    end
  end

  @doc false
  def http_options(jwks_url) do
    base = [timeout: 5_000, connect_timeout: 2_000]

    if URI.parse(jwks_url).scheme == "https" do
      ssl = [
        verify: :verify_peer,
        cacerts: :public_key.cacerts_get(),
        customize_hostname_check: [
          match_fun: :public_key.pkix_verify_hostname_match_fun(:https)
        ]
      ]

      Keyword.put(base, :ssl, ssl)
    else
      base
    end
  end

  @doc false
  def validate_jwks_url(url) when is_binary(url) do
    uri = URI.parse(url)
    insecure_urls = Application.get_env(:lease_plane, :insecure_operator_key_urls, MapSet.new())

    cond do
      uri.userinfo != nil or uri.fragment != nil or not is_binary(uri.host) ->
        {:error, :invalid}

      uri.scheme == "https" ->
        :ok

      uri.scheme == "http" and MapSet.member?(insecure_urls, url) ->
        :ok

      true ->
        {:error, :invalid}
    end
  end

  def validate_jwks_url(_url), do: {:error, :invalid}

  defp key_from_jwks(keys, kid) when is_list(keys) do
    case Enum.find(keys, fn
           %{"kid" => ^kid} -> true
           _ -> false
         end) do
      %{
        "kty" => "OKP",
        "crv" => "Ed25519",
        "use" => "sig",
        "alg" => "EdDSA",
        "x" => encoded
      }
      when is_binary(encoded) ->
        case decode_b64url(encoded) do
          {:ok, public_key} when byte_size(public_key) == 32 -> {:ok, public_key}
          _ -> {:error, :invalid}
        end

      _ ->
        {:error, :invalid}
    end
  end

  defp key_from_jwks(_keys, _kid), do: {:error, :invalid}

  defp keys_from_jwks(keys) when is_list(keys) and length(keys) in 1..64 do
    Enum.reduce_while(keys, {:ok, %{}}, fn
      %{"kid" => kid} = jwk, {:ok, acc}
      when is_binary(kid) and byte_size(kid) in 1..128 ->
        with false <- Map.has_key?(acc, kid),
             {:ok, public_key} <- key_from_jwks([jwk], kid) do
          {:cont, {:ok, Map.put(acc, kid, public_key)}}
        else
          _ -> {:halt, {:error, :invalid}}
        end

      _jwk, _acc ->
        {:halt, {:error, :invalid}}
    end)
  end

  defp keys_from_jwks(_keys), do: {:error, :invalid}

  defp valid_signature?(signing_input, signature, public_key) do
    :crypto.verify(:eddsa, :none, signing_input, signature, [public_key, :ed25519])
  rescue
    _ -> false
  end

  defp consume_nonce(issuer, jti, exp) do
    consumer = Application.get_env(:lease_plane, :identity_nonce_consumer, Repo)

    result =
      if is_function(consumer, 3) do
        consumer.(issuer, jti, exp)
      else
        consumer.consume_identity_attestation(issuer, jti, exp)
      end

    case result do
      :ok -> :ok
      {:error, :replayed} -> {:error, :replayed}
      _ -> {:error, :identity_nonce_store_unavailable}
    end
  end

  defp current_time do
    case Application.get_env(:lease_plane, :identity_clock) do
      clock when is_function(clock, 0) -> clock.()
      _ -> System.system_time(:second)
    end
  end

  defp configured_audience do
    case Application.get_env(:lease_plane, :identity_attestation_audience) do
      audience when is_binary(audience) and byte_size(audience) in 1..256 -> {:ok, audience}
      _ -> {:error, :identity_attestation_audience_unset}
    end
  end

  defp valid_jti?(jti) when is_binary(jti) and byte_size(jti) in 1..128,
    do: String.match?(jti, ~r/\A[A-Za-z0-9_-]+\z/)

  defp valid_jti?(_jti), do: false

  defp decode_b64url(value) when is_binary(value), do: Base.url_decode64(value, padding: false)
  defp decode_b64url(_value), do: :error
end
