defmodule UnitaresLeasePlane.IdentityBinding do
  @moduledoc """
  Rollout gate for cryptographically attributed lease mutations.

  Modes:

    * `:off` — compatibility posture; no identity proof is inspected.
    * `:log` — verify when a proof is present and log failures, but allow.
    * `:enforce` — every lease mutation requires a governance-verified proof
      bound to the UUID that owns (or will own) the lease.

  The standard lease bearer authenticates access to the service.  Identity
  binding separately proves which governance principal is acting.
  """

  require Logger

  alias UnitaresLeasePlane.{IdentityMetrics, RawBodyReader}

  @proof_header "x-unitares-identity-proof"

  @type refusal ::
          :identity_proof_invalid
          | :identity_proof_replayed
          | :identity_verification_unavailable

  @type request_context :: %{method: String.t(), path: String.t(), body_sha256: String.t()}

  @spec proof_header() :: String.t()
  def proof_header, do: @proof_header

  @spec enabled?() :: boolean()
  def enabled?, do: mode() != :off

  @spec proof_from_conn(Plug.Conn.t()) :: String.t() | nil
  def proof_from_conn(conn) do
    case Plug.Conn.get_req_header(conn, @proof_header) do
      [proof] when is_binary(proof) and proof != "" -> proof
      _ -> nil
    end
  end

  @spec request_context(Plug.Conn.t()) :: request_context()
  def request_context(conn) do
    %{
      method: conn.method,
      path: conn.request_path,
      body_sha256: :crypto.hash(:sha256, RawBodyReader.body(conn)) |> Base.encode16(case: :lower)
    }
  end

  @spec required_for_surface?(String.t() | nil) :: boolean()
  def required_for_surface?(surface_kind) do
    case Application.get_env(:lease_plane, :identity_bound_surface_kinds, :all) do
      :all -> true
      %MapSet{} = kinds -> is_binary(surface_kind) and MapSet.member?(kinds, surface_kind)
      kinds when is_list(kinds) -> surface_kind in kinds
      _ -> false
    end
  end

  @spec authorize(String.t(), String.t() | nil) :: :ok | {:error, refusal()}
  def authorize(expected_holder_uuid, proof), do: authorize(expected_holder_uuid, proof, nil, nil)

  @spec authorize(String.t(), String.t() | nil, String.t() | nil) ::
          :ok | {:error, refusal()}
  def authorize(expected_holder_uuid, proof, surface_kind),
    do: authorize(expected_holder_uuid, proof, surface_kind, nil)

  @spec authorize(String.t(), String.t() | nil, String.t() | nil, request_context() | nil) ::
          :ok | {:error, refusal()}
  def authorize(expected_holder_uuid, proof, surface_kind, request_context)
      when is_binary(expected_holder_uuid) do
    case effective_mode(surface_kind) do
      :off ->
        :ok

      :log ->
        case verify(expected_holder_uuid, proof, request_context) do
          :ok ->
            :ok

          {:error, reason} ->
            Logger.warning(
              "lease identity binding would refuse holder=#{short_uuid(expected_holder_uuid)} reason=#{reason}"
            )

            :ok
        end

      :enforce ->
        verify(expected_holder_uuid, proof, request_context)
    end
  end

  def authorize(_expected_holder_uuid, _proof, _surface_kind, _request_context),
    do: {:error, :identity_proof_invalid}

  @spec parse_mode(String.t() | nil) :: :off | :log | :enforce
  def parse_mode(nil), do: :off
  def parse_mode(""), do: :off

  def parse_mode(value) when is_binary(value) do
    case String.downcase(String.trim(value)) do
      "off" -> :off
      "log" -> :log
      "enforce" -> :enforce
      other -> raise ArgumentError, "invalid UNITARES_LEASE_IDENTITY_BINDING mode: #{other}"
    end
  end

  @spec parse_surface_kinds(String.t() | nil) :: :all | MapSet.t(String.t())
  def parse_surface_kinds(nil), do: :all
  def parse_surface_kinds(""), do: :all

  def parse_surface_kinds(value) when is_binary(value) do
    value
    |> String.split(",", trim: true)
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
    |> MapSet.new()
  end

  @spec parse_proof_format(String.t() | nil) :: :legacy | :hybrid | :attestation
  def parse_proof_format(nil), do: :hybrid
  def parse_proof_format(""), do: :hybrid

  def parse_proof_format(value) when is_binary(value) do
    case String.downcase(String.trim(value)) do
      "legacy" ->
        :legacy

      "hybrid" ->
        :hybrid

      "attestation" ->
        :attestation

      other ->
        raise ArgumentError,
              "invalid UNITARES_LEASE_IDENTITY_PROOF_FORMAT: #{other} (expected legacy, hybrid, or attestation)"
    end
  end

  @spec parse_trusted_issuers(String.t() | nil) :: %{optional(String.t()) => String.t()}
  def parse_trusted_issuers(nil), do: %{}
  def parse_trusted_issuers(""), do: %{}

  def parse_trusted_issuers(value) when is_binary(value) do
    case Jason.decode(value) do
      {:ok, %{} = issuers} when map_size(issuers) <= 64 ->
        if Enum.all?(issuers, fn {issuer, url} ->
             is_binary(issuer) and byte_size(issuer) in 1..256 and
               not String.match?(issuer, ~r/\s/) and
               is_binary(url) and byte_size(url) in 1..2_048 and valid_trusted_url?(url)
           end) do
          issuers
        else
          raise ArgumentError,
                "UNITARES_LEASE_TRUSTED_ISSUERS must map non-empty issuer strings to JWKS URLs"
        end

      _ ->
        raise ArgumentError,
              "UNITARES_LEASE_TRUSTED_ISSUERS must be a JSON object with at most 64 entries"
    end
  end

  defp mode, do: Application.get_env(:lease_plane, :identity_binding_mode, :off)

  defp effective_mode(surface_kind) do
    if required_for_surface?(surface_kind), do: mode(), else: :off
  end

  defp verify(expected_holder_uuid, proof, request_context) do
    proof_type = proof_type(proof)
    started = System.monotonic_time(:microsecond)
    result = verify_with_selected_format(expected_holder_uuid, proof, request_context)
    elapsed = System.monotonic_time(:microsecond) - started

    case result do
      :ok ->
        IdentityMetrics.record(proof_type, :verified, elapsed)
        :ok

      {:error, :invalid} ->
        IdentityMetrics.record(proof_type, :invalid, elapsed)
        {:error, :identity_proof_invalid}

      {:error, :replayed} ->
        IdentityMetrics.record(proof_type, :replayed, elapsed)
        {:error, :identity_proof_replayed}

      {:error, _reason} ->
        IdentityMetrics.record(proof_type, :unavailable, elapsed)
        {:error, :identity_verification_unavailable}

      _other ->
        IdentityMetrics.record(proof_type, :unavailable, elapsed)
        {:error, :identity_verification_unavailable}
    end
  end

  defp verify_with_selected_format(_expected_holder_uuid, nil, _request_context),
    do: {:error, :invalid}

  defp verify_with_selected_format(expected_holder_uuid, proof, request_context) do
    case Application.get_env(:lease_plane, :identity_verifier) do
      nil -> verify_by_format(expected_holder_uuid, proof, request_context)
      override -> call_verifier(override, expected_holder_uuid, proof, request_context)
    end
  end

  defp verify_by_format(expected_holder_uuid, proof, request_context) do
    format = Application.get_env(:lease_plane, :identity_proof_format, :hybrid)
    attestation? = String.starts_with?(proof, "lat.v1.")

    cond do
      attestation? and format == :legacy ->
        {:error, :invalid}

      attestation? and is_map(request_context) ->
        UnitaresLeasePlane.FederatedIdentityVerifier.verify(
          expected_holder_uuid,
          proof,
          request_context
        )

      attestation? ->
        {:error, :invalid}

      format == :attestation ->
        {:error, :invalid}

      true ->
        UnitaresLeasePlane.GovernanceIdentityClient.verify(expected_holder_uuid, proof)
    end
  end

  defp call_verifier(verifier, expected_holder_uuid, proof, request_context) do
    try do
      cond do
        is_function(verifier, 3) ->
          verifier.(expected_holder_uuid, proof, request_context)

        is_function(verifier, 2) ->
          verifier.(expected_holder_uuid, proof)

        function_exported?(verifier, :verify, 3) ->
          verifier.verify(expected_holder_uuid, proof, request_context)

        true ->
          verifier.verify(expected_holder_uuid, proof)
      end
    rescue
      _exception -> {:error, :identity_verifier_exception}
    catch
      _kind, _reason -> {:error, :identity_verifier_exception}
    end
  end

  defp proof_type(nil), do: :missing
  defp proof_type("lat.v1." <> _rest), do: :attestation
  defp proof_type(_proof), do: :legacy

  defp valid_trusted_url?(url) do
    uri = URI.parse(url)

    uri.scheme in ["http", "https"] and is_binary(uri.host) and uri.host != "" and
      is_nil(uri.userinfo) and is_nil(uri.fragment)
  rescue
    _ -> false
  end

  defp short_uuid(uuid), do: binary_part(uuid, 0, min(byte_size(uuid), 8))
end
