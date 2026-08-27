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

  @proof_header "x-unitares-identity-proof"

  @type refusal :: :identity_proof_invalid | :identity_verification_unavailable

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
  def authorize(expected_holder_uuid, proof), do: authorize(expected_holder_uuid, proof, nil)

  @spec authorize(String.t(), String.t() | nil, String.t() | nil) ::
          :ok | {:error, refusal()}
  def authorize(expected_holder_uuid, proof, surface_kind)
      when is_binary(expected_holder_uuid) do
    case effective_mode(surface_kind) do
      :off ->
        :ok

      :log ->
        case verify(expected_holder_uuid, proof) do
          :ok ->
            :ok

          {:error, reason} ->
            Logger.warning(
              "lease identity binding would refuse holder=#{short_uuid(expected_holder_uuid)} reason=#{reason}"
            )

            :ok
        end

      :enforce ->
        verify(expected_holder_uuid, proof)
    end
  end

  def authorize(_expected_holder_uuid, _proof, _surface_kind),
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

  defp mode, do: Application.get_env(:lease_plane, :identity_binding_mode, :off)

  defp effective_mode(surface_kind) do
    if required_for_surface?(surface_kind), do: mode(), else: :off
  end

  defp verify(_expected_holder_uuid, nil), do: {:error, :identity_proof_invalid}

  defp verify(expected_holder_uuid, proof) do
    verifier =
      Application.get_env(
        :lease_plane,
        :identity_verifier,
        UnitaresLeasePlane.GovernanceIdentityClient
      )

    result = call_verifier(verifier, expected_holder_uuid, proof)

    case result do
      :ok -> :ok
      {:error, :invalid} -> {:error, :identity_proof_invalid}
      {:error, _reason} -> {:error, :identity_verification_unavailable}
      _other -> {:error, :identity_verification_unavailable}
    end
  end

  defp call_verifier(verifier, expected_holder_uuid, proof) do
    try do
      if is_function(verifier, 2) do
        verifier.(expected_holder_uuid, proof)
      else
        verifier.verify(expected_holder_uuid, proof)
      end
    rescue
      _exception -> {:error, :identity_verifier_exception}
    catch
      _kind, _reason -> {:error, :identity_verifier_exception}
    end
  end

  defp short_uuid(uuid), do: binary_part(uuid, 0, min(byte_size(uuid), 8))
end
