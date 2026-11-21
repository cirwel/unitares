defmodule AgentOrchestrator.LeasePlaneClient do
  @moduledoc """
  Thin HTTP client for the lease plane's RFC §5 surface, used by `AgentRunner`
  to bind an ephemeral agent to a lease.

  Supports two local transports with no third-party HTTP dependency:
  - Unix domain socket (`UNITARES_LEASE_PLANE_UNIX_SOCKET` /
    `LEASE_PLANE_UNIX_SOCKET` / `:lease_plane_unix_socket_path`, or a `unix:///`
    base URL) via `AgentOrchestrator.UnixHTTPClient` (`:gen_tcp` `{:local, path}`).
  - Localhost TCP HTTP(S) (`LEASE_PLANE_BASE_URL` / `:lease_plane_base_url`,
    default `http://127.0.0.1:8788`) via OTP's `:httpc`.

  Bearer authentication fails closed when the bearer is absent. When a lease
  identity proof is configured (or supplied per call), the client exchanges it
  with governance `POST /v1/lease-holder/attest` for a short-lived,
  request-bound `lat.v1` Ed25519 attestation hashed over the exact method, path,
  and raw JSON body bytes, and sends that attestation in
  `x-unitares-identity-proof`. Raw continuity credentials are never forwarded
  to the lease plane or logged. When
  `AGENT_ORCHESTRATOR_REQUIRE_LEASE_IDENTITY_PROOF` (`:require_lease_identity_proof`)
  is enabled, missing proofs or failed/malformed attestations fail closed before
  any lease-plane call is attempted.

  The functions form a behaviour so tests (and standalone runs without a live
  plane) can inject a stub via the `:lease_client` spec key.
  """

  @behaviour AgentOrchestrator.LeasePlaneClient.Behaviour

  require Logger

  alias AgentOrchestrator.UnixHTTPClient

  @attestation_prefix "lat.v1."
  @max_attestation_bytes 8_192
  @max_attestation_response_bytes 16_384
  @default_governance_url "http://127.0.0.1:8767"
  @default_base_url "http://127.0.0.1:8788"

  @impl true
  def acquire(surface_id, holder_agent_uuid, holder_kind, ttl_s, opts \\ []) do
    body = %{
      surface_id: surface_id,
      holder_agent_uuid: holder_agent_uuid,
      holder_kind: holder_kind,
      ttl_s: ttl_s
    }

    # Reply-reading is SDK-owned (UnitaresSdk.LeasePlaneEnvelope): one
    # authoritative decode, eligible to stop drifting from dispatch_beam's
    # once that repo's pinned SDK ref moves past this commit. SDK error terms
    # pass through UNCHANGED — held_by_other is a 3-tuple whose payload carries
    # retry_after_hint_ms/blocking_lease_id; AgentRunner treats reasons
    # opaquely, so no re-narrowing adapter.
    post_opts = Keyword.put_new(opts, :holder_agent_uuid, holder_agent_uuid)

    case post("/v1/lease/acquire", body, post_opts) do
      {:ok, status, payload} ->
        UnitaresSdk.LeasePlaneEnvelope.classify_acquire(status, payload)

      {:error, reason} ->
        {:error, reason}
    end
  end

  @impl true
  def release(lease_id, reason, opts \\ []) do
    # `release_reason` is a CLOSED server-side enum (http_router.ex release
    # handler): normal | down_local | reaped_after_supervisor_failed |
    # reaped_local_ttl | reaped_remote_ttl | handoff | reclaimed_lost_acquire
    # ("forced" is rejected here — use /v1/lease/force-release). An ad-hoc
    # reason gets 422 schema_invalid, classifies as {:release_refused, ...},
    # and the lease STAYS HELD (live-verified 2026-08-22). Deliberately not
    # allowlisted client-side: a stale copy of the enum on this fail path
    # would refuse valid reasons after a server addition.
    case post("/v1/lease/release", %{lease_id: lease_id, release_reason: reason}, opts) do
      {:ok, status, payload} ->
        UnitaresSdk.LeasePlaneEnvelope.classify_release(status, payload)

      {:error, reason} ->
        {:error, reason}
    end
  end

  # ---------- internals ----------

  defp post(path, body, opts) do
    # Serialize once so the attestation SHA-256 and the transmitted wire bytes
    # are guaranteed identical.
    raw_body = Jason.encode!(body)

    with {:ok, token} <- bearer(),
         {:ok, attestation} <-
           resolve_identity_proof(
             "POST",
             path,
             raw_body,
             Keyword.get(opts, :holder_agent_uuid),
             opts
           ),
         {:ok, transport} <- resolve_lease_transport() do
      headers = build_lease_headers(token, attestation)
      send_lease_request(transport, path, headers, raw_body, opts)
    end
  end

  defp build_lease_headers(token, attestation) do
    base = [
      {"authorization", "Bearer " <> token},
      {"accept", "application/json"},
      {"content-type", "application/json"}
    ]

    if is_binary(attestation) do
      base ++ [{"x-unitares-identity-proof", attestation}]
    else
      base
    end
  end

  defp resolve_lease_transport do
    case configured_unix_socket_path() do
      socket_path when is_binary(socket_path) ->
        {:ok, {:uds, socket_path}}

      nil ->
        url = base_url()

        if String.starts_with?(url, "unix://") do
          case url do
            "unix:///" <> rest when rest != "" ->
              {:ok, {:uds, "/" <> rest}}

            _ ->
              {:error, {:unix_socket, :invalid_url}}
          end
        else
          {:ok, {:http, String.trim_trailing(url, "/")}}
        end
    end
  end

  defp send_lease_request({:uds, socket_path}, path, headers, raw_body, opts) do
    uds_opts =
      opts
      |> Keyword.take([:timeout_ms, :connect_timeout_ms, :expected_uid, :max_body_bytes])

    case UnixHTTPClient.post(socket_path, path, headers, raw_body, uds_opts) do
      {:ok, status, _resp_headers, resp_body} ->
        {:ok, status, decode(resp_body)}

      {:error, reason} ->
        {:error, {:unix_socket, reason}}
    end
  end

  defp send_lease_request({:http, url_base}, path, headers, raw_body, opts) do
    url = url_base <> path

    charlist_headers =
      headers
      |> Enum.reject(fn {name, _value} -> String.downcase(name) == "content-type" end)
      |> Enum.map(fn {name, value} -> {String.to_charlist(name), String.to_charlist(value)} end)

    request = {String.to_charlist(url), charlist_headers, ~c"application/json", raw_body}

    case :httpc.request(:post, request, http_options(url, opts), body_format: :binary) do
      {:ok, {{_http, status, _reason}, _resp_headers, resp_body}} ->
        {:ok, status, decode(resp_body)}

      {:error, reason} ->
        {:error, {:http, reason}}
    end
  end

  # ---------- request-bound identity attestation ----------

  defp resolve_identity_proof(method, path, raw_body, holder_agent_uuid, opts) do
    require_proof? = require_identity_proof?(opts)

    case extract_source_proof(opts) do
      nil ->
        if require_proof? do
          {:error, {:identity_attestation_required, :missing_proof}}
        else
          {:ok, nil}
        end

      @attestation_prefix <> _ = token ->
        if valid_attestation_token?(token) do
          {:ok, token}
        else
          {:error, {:identity_attestation_failed, :malformed_attestation}}
        end

      source_proof ->
        case mint_attestation(source_proof, method, path, raw_body, holder_agent_uuid, opts) do
          {:ok, attestation} ->
            {:ok, attestation}

          {:error, reason} ->
            if require_proof? do
              {:error, {:identity_attestation_failed, reason}}
            else
              Logger.warning(
                "lease identity attestation unavailable for #{path} (#{inspect(reason)}); " <>
                  "sending request without x-unitares-identity-proof"
              )

              {:ok, nil}
            end
        end
    end
  end

  defp mint_attestation(source_proof, method, path, raw_body, holder_agent_uuid, opts) do
    with {:ok, gov_base} <- validate_governance_url(governance_url(opts)),
         {:ok, auth_headers} <- governance_headers(opts) do
      url = gov_base <> "/v1/lease-holder/attest"
      body_sha256 = :crypto.hash(:sha256, raw_body) |> Base.encode16(case: :lower)

      attest_map =
        %{
          "identity_proof" => source_proof,
          "method" => method,
          "path" => path,
          "body_sha256" => body_sha256
        }
        |> maybe_put_holder(holder_agent_uuid)

      attest_payload = Jason.encode!(attest_map)
      headers = [{~c"accept", ~c"application/json"} | auth_headers]
      request = {String.to_charlist(url), headers, ~c"application/json", attest_payload}

      case :httpc.request(:post, request, http_options(url, opts), body_format: :binary) do
        {:ok, {{_http, 200, _reason}, _resp_headers, resp_body}} ->
          parse_attestation_response(resp_body, holder_agent_uuid)

        {:ok, {{_http, status, _reason}, _resp_headers, _resp_body}} ->
          {:error, {:attestation_http_status, status}}

        {:error, reason} ->
          {:error, {:attestation_unreachable, reason}}
      end
    end
  end

  defp parse_attestation_response(resp_body, _expected_holder)
       when not is_binary(resp_body) or byte_size(resp_body) > @max_attestation_response_bytes do
    {:error, :malformed_attestation_response}
  end

  defp parse_attestation_response(resp_body, expected_holder) do
    with {:ok, %{"ok" => true, "attestation" => attestation} = decoded} <- Jason.decode(resp_body),
         true <- valid_attestation_token?(attestation),
         true <- holder_matches?(decoded, expected_holder) do
      {:ok, attestation}
    else
      _ -> {:error, :malformed_attestation_response}
    end
  end

  defp holder_matches?(_decoded, nil), do: true
  defp holder_matches?(_decoded, ""), do: true

  defp holder_matches?(%{"holder_agent_uuid" => returned}, expected) when is_binary(returned),
    do: returned == expected

  defp holder_matches?(_decoded, _expected), do: false

  defp valid_attestation_token?(token) when is_binary(token) do
    byte_size(token) > byte_size(@attestation_prefix) and
      byte_size(token) <= @max_attestation_bytes and
      String.starts_with?(token, @attestation_prefix) and
      not String.match?(token, ~r/[\s\r\n\0]/)
  end

  defp valid_attestation_token?(_other), do: false

  defp maybe_put_holder(map, holder) when is_binary(holder) and holder != "",
    do: Map.put(map, "holder_agent_uuid", holder)

  defp maybe_put_holder(map, _holder), do: map

  @doc false
  def validate_governance_url(url) when is_binary(url) do
    trimmed = url |> String.trim() |> String.trim_trailing("/")
    uri = URI.parse(trimmed)
    host = if is_binary(uri.host), do: String.downcase(uri.host), else: ""

    cond do
      host == "" or uri.userinfo != nil or uri.query != nil or uri.fragment != nil ->
        {:error, :invalid_governance_url}

      uri.scheme == "https" ->
        {:ok, trimmed}

      uri.scheme == "http" and (loopback_host?(host) or insecure_governance_host?(host)) ->
        {:ok, trimmed}

      uri.scheme == "http" ->
        {:error, :insecure_governance_url}

      true ->
        {:error, :invalid_governance_url}
    end
  rescue
    _ -> {:error, :invalid_governance_url}
  end

  def validate_governance_url(_other), do: {:error, :invalid_governance_url}

  defp loopback_host?("localhost"), do: true

  defp loopback_host?(host) do
    case :inet.parse_address(String.to_charlist(host)) do
      {:ok, {127, _, _, _}} -> true
      {:ok, {0, 0, 0, 0, 0, 0, 0, 1}} -> true
      {:ok, {0, 0, 0, 0, 0, 0xFFFF, hi, _}} when div(hi, 256) == 127 -> true
      _ -> false
    end
  end

  defp insecure_governance_host?(host) do
    case Application.get_env(:agent_orchestrator, :insecure_governance_hosts, MapSet.new()) do
      %MapSet{} = set -> MapSet.member?(set, host)
      list when is_list(list) -> host in Enum.map(list, &String.downcase/1)
      _ -> false
    end
  end

  defp governance_url(opts) do
    Keyword.get(opts, :governance_url) ||
      Application.get_env(:agent_orchestrator, :governance_url, @default_governance_url)
  end

  defp governance_headers(opts) do
    token =
      Keyword.get(opts, :governance_api_token) ||
        Application.get_env(:agent_orchestrator, :governance_api_token)

    case token do
      nil ->
        {:ok, []}

      "" ->
        {:ok, []}

      value when is_binary(value) ->
        if String.contains?(value, ["\r", "\n", <<0>>]) do
          {:error, :invalid_governance_token}
        else
          {:ok, [{~c"authorization", String.to_charlist("Bearer " <> value)}]}
        end

      _other ->
        {:error, :invalid_governance_token}
    end
  end

  defp extract_source_proof(opts) do
    raw =
      case Keyword.fetch(opts, :identity_proof) do
        {:ok, fun} when is_function(fun, 0) -> fun.()
        {:ok, value} -> value
        :error -> Application.get_env(:agent_orchestrator, :lease_identity_proof)
      end

    case raw do
      value when is_binary(value) ->
        trimmed = String.trim(value)
        if trimmed == "", do: nil, else: trimmed

      _ ->
        nil
    end
  end

  defp require_identity_proof?(opts) do
    case Keyword.fetch(opts, :require_identity_proof) do
      {:ok, flag} when is_boolean(flag) -> flag
      _ -> Application.get_env(:agent_orchestrator, :require_lease_identity_proof, false) == true
    end
  end

  defp http_options(url, opts) do
    timeout_ms = Keyword.get(opts, :timeout_ms, 5_000)
    connect_timeout_ms = Keyword.get(opts, :connect_timeout_ms, 2_000)
    base = [timeout: timeout_ms, connect_timeout: connect_timeout_ms]

    if URI.parse(url).scheme == "https" do
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

  defp bearer do
    case Application.get_env(:agent_orchestrator, :lease_plane_bearer_token) do
      nil ->
        {:error, :no_bearer}

      "" ->
        {:error, :no_bearer}

      token when is_binary(token) ->
        if String.contains?(token, ["\r", "\n", <<0>>]) do
          {:error, :invalid_bearer}
        else
          {:ok, token}
        end

      _ ->
        {:error, :no_bearer}
    end
  end

  defp configured_unix_socket_path do
    case Application.get_env(:agent_orchestrator, :lease_plane_unix_socket_path) do
      path when is_binary(path) ->
        trimmed = String.trim(path)
        if trimmed == "", do: nil, else: trimmed

      _ ->
        nil
    end
  end

  defp base_url do
    Application.get_env(:agent_orchestrator, :lease_plane_base_url, @default_base_url) ||
      @default_base_url
  end

  defp decode(body) when is_list(body), do: decode(IO.iodata_to_binary(body))

  defp decode(body) when is_binary(body) do
    case Jason.decode(body) do
      {:ok, map} when is_map(map) -> map
      {:ok, _other} -> %{"raw" => body}
      {:error, _} -> %{"raw" => body}
    end
  end
end
