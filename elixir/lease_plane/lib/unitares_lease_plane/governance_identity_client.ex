defmodule UnitaresLeasePlane.GovernanceIdentityClient do
  @moduledoc """
  Verifies lease-holder identity proofs with the governance service.

  The proof remains opaque to BEAM.  The lease plane forwards it over the
  operator-configured governance channel and accepts only an explicit
  `verified: true` response whose holder UUID matches the claim.  The
  governance signing secret therefore stays out of the coordination process.

  No proof value is logged, returned, or persisted by this module.
  """

  @spec verify(String.t(), String.t()) :: :ok | {:error, :invalid | term()}
  def verify(holder_agent_uuid, identity_proof)
      when is_binary(holder_agent_uuid) and is_binary(identity_proof) do
    with {:ok, base} <- base_url() do
      body =
        Jason.encode!(%{
          "holder_agent_uuid" => holder_agent_uuid,
          "identity_proof" => identity_proof
        })

      request = {
        String.to_charlist(base <> "/v1/lease-holder/verify"),
        request_headers(),
        ~c"application/json",
        body
      }

      http_opts = [timeout: timeout_ms(), connect_timeout: 2_000]

      case :httpc.request(:post, request, http_opts, body_format: :binary) do
        {:ok, {{_version, status, _reason}, _headers, response}} ->
          classify_response(status, response, holder_agent_uuid)

        {:error, reason} ->
          {:error, {:identity_verifier_unreachable, reason}}
      end
    end
  end

  def verify(_holder_agent_uuid, _identity_proof), do: {:error, :invalid}

  @doc false
  @spec classify_response(non_neg_integer(), binary(), String.t()) ::
          :ok | {:error, :invalid | term()}
  def classify_response(200, response, expected_holder),
    do: parse_verified(response, expected_holder)

  def classify_response(403, _response, _expected_holder), do: {:error, :invalid}

  def classify_response(status, _response, _expected_holder),
    do: {:error, {:identity_verifier_status, status}}

  @doc false
  @spec parse_verified(binary(), String.t()) :: :ok | {:error, :invalid | term()}
  def parse_verified(response, expected_holder) when is_binary(response) do
    case Jason.decode(response) do
      {:ok,
       %{
         "ok" => true,
         "verified" => true,
         "holder_agent_uuid" => ^expected_holder
       }} ->
        :ok

      {:ok, %{"verified" => false}} ->
        {:error, :invalid}

      {:ok, _other} ->
        {:error, :invalid}

      {:error, _reason} ->
        {:error, :bad_identity_verifier_json}
    end
  end

  defp base_url do
    case Application.get_env(:lease_plane, :governance_url) do
      url when is_binary(url) and byte_size(url) > 0 ->
        {:ok, String.trim_trailing(url, "/")}

      _ ->
        {:error, :governance_url_unset}
    end
  end

  defp request_headers do
    case Application.get_env(:lease_plane, :governance_api_token) do
      token when is_binary(token) and byte_size(token) > 0 ->
        [{~c"authorization", String.to_charlist("Bearer " <> token)}]

      _ ->
        []
    end
  end

  defp timeout_ms,
    do: Application.get_env(:lease_plane, :governance_identity_timeout_ms, 5_000)
end
