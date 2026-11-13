defmodule UnitaresSentinel.BearerClient do
  @moduledoc """
  Shared HTTP base-URL resolution, dual-token Bearer selection, 401 fallback,
  and Finch GET transport for BEAM Sentinel.

  Mirrors `agents/common/http_client.py` so Mac-resident BEAM Sentinel works
  against both local loopback Core (`UNITARES_HTTP_API_TOKEN`) and remote strict
  Core (`UNITARES_REST_STRICT=1`, `UNITARES_MCP_BEARER_TOKEN`) without manual
  token juggling or duplicate `401` round-trips.
  """

  @default_base_url "http://localhost:8767"
  @default_timeout_ms 10_000

  @endpoint_suffixes [
    "/v1/lease-plane/alarm-events",
    "/v1/audit/volume-summary",
    "/v1/tools/call",
    "/api/findings",
    "/ws/eisv",
    "/mcp"
  ]

  @type env_reader :: (String.t() -> String.t() | nil)
  @type headers :: [{String.t(), String.t()}]
  @type http_response :: {:ok, non_neg_integer(), String.t()} | {:error, term()}

  @doc """
  Resolve the normalized Core HTTP base URL (`scheme://host[:port][/prefix]`),
  stripping known endpoint suffixes while preserving reverse-proxy path prefixes.
  """
  @spec governance_base_url(keyword()) :: String.t()
  def governance_base_url(opts \\ []) do
    env = env_reader(opts)

    raw =
      nonempty(Keyword.get(opts, :base_url)) ||
        nonempty(Keyword.get(opts, :url)) ||
        nonempty(Application.get_env(:unitares_sentinel, :governance_base_url)) ||
        nonempty(env.("GOV_REST_URL")) ||
        nonempty(Application.get_env(:unitares_sentinel, :governance_tools_url)) ||
        nonempty(env.("UNITARES_GOVERNANCE_TOOLS_URL")) ||
        nonempty(Application.get_env(:unitares_sentinel, :findings_url)) ||
        nonempty(env.("UNITARES_FINDINGS_URL")) ||
        nonempty(env.("UNITARES_URL")) ||
        @default_base_url

    normalize_base_url(raw)
  end

  @doc """
  Normalize a raw Core or endpoint URL into an HTTP(S) base URL without trailing `/`.
  """
  @spec normalize_base_url(String.t()) :: String.t()
  def normalize_base_url(raw) when is_binary(raw) do
    trimmed =
      raw
      |> String.trim()
      |> convert_ws_scheme()
      |> String.trim_trailing("/")

    uri = URI.parse(trimmed)

    case {uri.scheme, uri.host} do
      {scheme, host} when scheme in ["http", "https"] and is_binary(host) and host != "" ->
        cleaned_path = strip_endpoint_suffix(uri.path || "")

        %URI{
          scheme: scheme,
          host: host,
          port: default_port_to_nil(scheme, uri.port),
          path: if(cleaned_path == "", do: nil, else: cleaned_path)
        }
        |> URI.to_string()
        |> String.trim_trailing("/")

      _ ->
        @default_base_url
    end
  end

  @doc """
  True when `url` points at a loopback host (`localhost`, `127.0.0.1`, `::1`).
  """
  @spec loopback_url?(String.t()) :: boolean()
  def loopback_url?(url) when is_binary(url) do
    case URI.parse(convert_ws_scheme(String.trim(url))) do
      %URI{host: host} when is_binary(host) ->
        normalized =
          host
          |> String.trim()
          |> String.trim_leading("[")
          |> String.trim_trailing("]")
          |> String.downcase()

        normalized in ["localhost", "127.0.0.1", "::1"]

      _ ->
        false
    end
  end

  @doc """
  True when `url` uses confidential transport (`https://` / `wss://`), targets a
  loopback host (`localhost`, `127.0.0.1`, `::1`), or connects over an explicit
  / configured local Unix domain socket (`UNITARES_UDS_SOCKET`).
  """
  @spec secure_or_loopback_url?(String.t(), keyword()) :: boolean()
  def secure_or_loopback_url?(url, opts \\ []) when is_binary(url) do
    if uds_socket_path(url, opts) != nil do
      true
    else
      case URI.parse(convert_ws_scheme(String.trim(url))) do
        %URI{scheme: "https", host: host} when is_binary(host) and host != "" ->
          true

        %URI{scheme: "http", host: host} when is_binary(host) and host != "" ->
          loopback_url?(url)

        _ ->
          false
      end
    end
  end

  @doc """
  Validate that `url` will not carry credentials or session proofs over cleartext
  non-loopback TCP.
  """
  @spec ensure_secure_transport(String.t(), keyword()) ::
          :ok | {:error, {:insecure_remote_transport, String.t()}}
  def ensure_secure_transport(url, opts \\ []) when is_binary(url) do
    if secure_or_loopback_url?(url, opts) do
      :ok
    else
      {:error, {:insecure_remote_transport, url}}
    end
  end

  @doc """
  Resolve Finch `unix_socket:` options when an explicit `:uds_socket` is passed
  or `UNITARES_UDS_SOCKET` is configured for a loopback target.
  """
  @spec uds_socket_opts(String.t(), keyword()) :: keyword()
  def uds_socket_opts(url, opts \\ []) when is_binary(url) do
    case uds_socket_path(url, opts) do
      nil ->
        []

      path ->
        if Path.type(path) == :absolute do
          [unix_socket: path]
        else
          raise ArgumentError, "UNITARES_UDS_SOCKET must be an absolute path"
        end
    end
  end

  @doc """
  Return ordered candidate Bearer tokens for `opts[:url]` (or resolved base URL).

  Non-loopback URLs and `UNITARES_REST_STRICT=1` try `UNITARES_MCP_BEARER_TOKEN`
  first so local `UNITARES_HTTP_API_TOKEN` is never sent to a remote strict server
  unless the MCP bearer was absent or rejected with `401`. Loopback URLs without
  strict mode try `UNITARES_HTTP_API_TOKEN` first. Cleartext non-loopback `http://`
  and `ws://` URLs return `[]` so Bearer tokens never traverse unencrypted networks.
  """
  @spec bearer_candidates(keyword()) :: [String.t()]
  def bearer_candidates(opts \\ []) do
    target_url = Keyword.get(opts, :url) || governance_base_url(opts)

    if not secure_or_loopback_url?(target_url, opts) do
      []
    else
      explicit_bearer = nonempty(Keyword.get(opts, :bearer_token))
      explicit_api = nonempty(Keyword.get(opts, :api_token))

      if explicit_bearer != nil or explicit_api != nil do
        dedup_nonempty([explicit_bearer, explicit_api])
      else
        env = env_reader(opts)
        mcp = nonempty(env.("UNITARES_MCP_BEARER_TOKEN"))
        api = nonempty(env.("UNITARES_HTTP_API_TOKEN"))

        strict? =
          case nonempty(env.("UNITARES_REST_STRICT")) do
            nil -> not loopback_url?(target_url)
            raw -> truthy?(raw)
          end

        ordered =
          case preferred_source(target_url) do
            :mcp -> [{:mcp, mcp}, {:api, api}]
            :api -> [{:api, api}, {:mcp, mcp}]
            nil when strict? -> [{:mcp, mcp}, {:api, api}]
            nil -> [{:api, api}, {:mcp, mcp}]
          end

        ordered
        |> Enum.map(&elem(&1, 1))
        |> dedup_nonempty()
      end
    end
  end

  @doc """
  Build request headers with `Authorization: Bearer <token>` (replacing any prior
  Authorization header so retries never duplicate the header).
  """
  @spec auth_headers(headers(), String.t() | nil) :: headers()
  def auth_headers(base_headers, nil), do: strip_auth_header(base_headers)
  def auth_headers(base_headers, ""), do: strip_auth_header(base_headers)

  def auth_headers(base_headers, token) when is_binary(token) do
    [{"Authorization", "Bearer " <> token} | strip_auth_header(base_headers)]
  end

  @doc """
  Execute `send_fn.(headers)` with ordered Bearer candidates, retrying once on
  `{:ok, 401, _}` when a second distinct candidate is configured and caching the
  winning token source per origin.
  """
  @spec request_with_fallback(String.t(), headers(), keyword(), (headers() -> http_response())) ::
          http_response()
  def request_with_fallback(url, base_headers, opts \\ [], send_fn)
      when is_binary(url) and is_list(base_headers) and is_function(send_fn, 1) do
    candidates = bearer_candidates(Keyword.put(opts, :url, url))

    case candidates do
      [] ->
        send_fn.(strip_auth_header(base_headers))

      [only] ->
        case send_fn.(auth_headers(base_headers, only)) do
          {:ok, status, _body} = ok when status != 401 ->
            record_winner(url, only, opts)
            ok

          other ->
            other
        end

      [first, second | _] ->
        case send_fn.(auth_headers(base_headers, first)) do
          {:ok, 401, _first_body} ->
            case send_fn.(auth_headers(base_headers, second)) do
              {:ok, status, _second_body} = retry_ok when status != 401 ->
                record_winner(url, second, opts)
                retry_ok

              retry_other ->
                retry_other
            end

          {:ok, status, _body} = ok when status != 401 ->
            record_winner(url, first, opts)
            ok

          other ->
            other
        end
    end
  end

  @doc """
  Perform an authenticated HTTP GET via `UnitaresSentinel.Finch` (or injectable
  `opts[:http_get]` of arity 3: `fn url, headers, timeout_ms -> response end`).
  """
  @spec get(String.t(), keyword()) :: http_response()
  def get(url, opts \\ []) when is_binary(url) do
    timeout_ms = Keyword.get(opts, :timeout_ms, @default_timeout_ms)
    base_headers = Keyword.get(opts, :headers, [{"Accept", "application/json"}])
    http_get = Keyword.get(opts, :http_get, fn u, h, t -> finch_get(u, h, t, opts) end)

    request_with_fallback(url, base_headers, opts, fn req_headers ->
      http_get.(url, req_headers, timeout_ms)
    end)
  rescue
    e -> {:error, {:raised, e}}
  catch
    :exit, reason -> {:error, {:exited, reason}}
  end

  @doc """
  Record which env token (`:mcp` or `:api`) succeeded for `url`'s origin.
  """
  @spec record_winner(String.t(), String.t() | nil, keyword()) :: :ok
  def record_winner(_url, nil, _opts), do: :ok
  def record_winner(_url, "", _opts), do: :ok

  def record_winner(url, token, opts) when is_binary(url) and is_binary(token) do
    if Keyword.has_key?(opts, :bearer_token) or Keyword.has_key?(opts, :api_token) do
      :ok
    else
      env = env_reader(opts)
      mcp = nonempty(env.("UNITARES_MCP_BEARER_TOKEN"))
      api = nonempty(env.("UNITARES_HTTP_API_TOKEN"))

      cond do
        mcp != nil and api != nil and mcp != api and token == mcp ->
          put_preferred_source(url, :mcp)

        mcp != nil and api != nil and mcp != api and token == api ->
          put_preferred_source(url, :api)

        true ->
          :ok
      end
    end
  end

  @doc """
  Clear cached per-origin token preferences (used by tests).
  """
  @spec clear_preference_cache() :: :ok
  def clear_preference_cache do
    for {{__MODULE__, :preferred_source, _origin} = key, _val} <- :persistent_term.get() do
      :persistent_term.erase(key)
    end

    :ok
  end

  # ---- Internal helpers -------------------------------------------------

  defp finch_get(url, headers, timeout_ms, opts) do
    with :ok <- ensure_secure_transport(url, opts) do
      finch_opts = uds_socket_opts(url, opts)
      request = Finch.build(:get, url, headers, nil, finch_opts)
      pool_timeout = min(timeout_ms, 5_000)

      case Finch.request(request, UnitaresSentinel.Finch,
             receive_timeout: timeout_ms,
             pool_timeout: pool_timeout
           ) do
        {:ok, %Finch.Response{status: status, body: response_body}} ->
          {:ok, status, response_body}

        {:error, reason} ->
          {:error, reason}
      end
    end
  end

  defp uds_socket_path(url, opts) do
    case Keyword.fetch(opts, :uds_socket) do
      {:ok, explicit} ->
        nonempty(explicit)

      :error ->
        if loopback_url?(url) do
          env = env_reader(opts)

          nonempty(Application.get_env(:unitares_sentinel, :governance_uds_socket)) ||
            nonempty(env.("UNITARES_UDS_SOCKET"))
        else
          nil
        end
    end
  end

  defp preferred_source(url) do
    case origin_key(url) do
      nil -> nil
      origin -> :persistent_term.get({__MODULE__, :preferred_source, origin}, nil)
    end
  end

  defp put_preferred_source(url, source) when source in [:mcp, :api] do
    case origin_key(url) do
      nil -> :ok
      origin -> :persistent_term.put({__MODULE__, :preferred_source, origin}, source)
    end
  end

  defp origin_key(url) do
    case URI.parse(convert_ws_scheme(String.trim(url))) do
      %URI{scheme: scheme, host: host, port: port}
      when scheme in ["http", "https"] and is_binary(host) and host != "" ->
        effective_port =
          cond do
            is_integer(port) -> port
            scheme == "https" -> 443
            true -> 80
          end

        {scheme, String.downcase(host), effective_port}

      _ ->
        nil
    end
  end

  defp convert_ws_scheme(url) do
    cond do
      String.starts_with?(url, "ws://") ->
        "http://" <> String.replace_prefix(url, "ws://", "")

      String.starts_with?(url, "wss://") ->
        "https://" <> String.replace_prefix(url, "wss://", "")

      true ->
        url
    end
  end

  defp strip_endpoint_suffix(path) do
    trimmed = String.trim_trailing(path, "/")

    Enum.find_value(@endpoint_suffixes, trimmed, fn suffix ->
      if String.ends_with?(trimmed, suffix) do
        String.replace_suffix(trimmed, suffix, "")
      else
        nil
      end
    end)
  end

  defp default_port_to_nil("http", 80), do: nil
  defp default_port_to_nil("https", 443), do: nil
  defp default_port_to_nil(_scheme, port), do: port

  defp strip_auth_header(headers) do
    Enum.reject(headers, fn {k, _v} -> String.downcase(to_string(k)) == "authorization" end)
  end

  defp env_reader(opts) do
    case Keyword.get(opts, :env) do
      fun when is_function(fun, 1) -> fun
      _ -> &System.get_env/1
    end
  end

  defp nonempty(nil), do: nil

  defp nonempty(val) when is_binary(val) do
    case String.trim(val) do
      "" -> nil
      trimmed -> trimmed
    end
  end

  defp dedup_nonempty(values) do
    values
    |> Enum.reject(&is_nil/1)
    |> Enum.uniq()
  end

  defp truthy?(raw) when is_binary(raw) do
    String.downcase(String.trim(raw)) in ["1", "true", "yes", "on"]
  end
end
