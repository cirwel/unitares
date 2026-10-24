defmodule DialecticLive.Governance do
  @moduledoc """
  Read client for the Python governance MCP (:8767).

  Dialectic is not a REST resource on the server — it is reached through the
  generic tool-call endpoint `POST /v1/tools/call` with
  `{name: "dialectic", arguments: %{action: ...}}`. The bearer token is held
  server-side in this app's config and never leaves the BEAM node, so the
  browser never sees it (unlike the buildless dashboard's in-browser authFetch).

  This is the B1 data source: `list_sessions/0` and `get_session/1` over what the
  current server already exposes. The server has emitted `dialectic_*` broadcast
  events since 2026-06-29 (#1251); the dialectic pane uses them as a doorbell to
  re-run these reads.

  `timeline/2` reads `GET /v1/timeline` for the Timeline pane. Unlike the
  tool-call reads it is a plain REST resource: the JSON body is the answer, so
  it is decoded without `UnitaresSdk.Envelope.classify/1`.

  Every call accepts `req_options:` (merged into the Req request) so tests can
  route it through `Req.Test`.
  """

  require Logger

  @doc "List dialectic sessions. Returns `{:ok, [session_map]}` or `{:error, reason}`."
  def list_sessions(limit \\ 50, opts \\ []) do
    case call("dialectic", %{action: "list", limit: limit}, opts) do
      {:ok, result} -> {:ok, normalize_sessions(result)}
      {:error, _} = err -> err
    end
  end

  @doc "Fetch one dialectic session by id. Returns `{:ok, map}` or `{:error, reason}`."
  def get_session(session_id, opts \\ []) when is_binary(session_id) do
    call("dialectic", %{action: "get", session_id: session_id}, opts)
  end

  @doc """
  Read `GET /v1/timeline` with `params` (a keyword list or map of query
  parameters). One attempt only (`retry: false`), 8 s receive timeout.

  Returns `{:ok, body}` for a 200 whose body is a JSON object,
  `{:error, {:http_status, status}}` for any other status,
  `{:error, :undecodable}` for a 200 that is not a JSON object, and
  `{:error, reason}` for a transport failure. It never raises.
  """
  def timeline(params, opts \\ []) do
    cfg = config()
    url = Keyword.get(cfg, :timeline_url)

    req_opts =
      [
        params: params,
        headers: auth_headers(cfg),
        retry: false,
        receive_timeout: 8_000
      ]
      |> Keyword.merge(Keyword.get(opts, :req_options, []))

    case Req.get(url, req_opts) do
      {:ok, %Req.Response{status: 200, body: %{} = body}} ->
        {:ok, body}

      {:ok, %Req.Response{status: 200, body: body}} when is_binary(body) ->
        case Jason.decode(body) do
          {:ok, %{} = decoded} -> {:ok, decoded}
          _ -> {:error, :undecodable}
        end

      {:ok, %Req.Response{status: 200}} ->
        {:error, :undecodable}

      {:ok, %Req.Response{status: status}} ->
        Logger.warning("governance timeline -> HTTP #{status}")
        {:error, {:http_status, status}}

      {:error, reason} ->
        Logger.warning("governance timeline failed: #{inspect(reason)}")
        {:error, reason}
    end
  rescue
    e ->
      Logger.warning("governance timeline raised: #{Exception.message(e)}")
      {:error, {:exception, Exception.message(e)}}
  end

  @doc false
  def config, do: Application.get_env(:dialectic_live, :governance, [])

  # --- internals ---

  defp auth_headers(cfg) do
    case Keyword.get(cfg, :api_token) do
      t when is_binary(t) and t != "" -> [{"authorization", "Bearer " <> t}]
      _ -> []
    end
  end

  defp call(name, arguments, opts) do
    cfg = config()
    url = Keyword.get(cfg, :tools_url)

    body = %{name: name, arguments: arguments}

    req_opts =
      [json: body, headers: auth_headers(cfg), receive_timeout: 8_000]
      |> Keyword.merge(Keyword.get(opts, :req_options, []))

    case Req.post(url, req_opts) do
      {:ok, %Req.Response{status: 200, body: resp}} ->
        # Req has already decoded the JSON, so classify the term directly.
        UnitaresSdk.Envelope.classify(resp)

      {:ok, %Req.Response{status: status, body: resp}} ->
        Logger.warning("governance tool-call #{name} -> HTTP #{status}: #{inspect(resp)}")
        {:error, {:http_status, status}}

      {:error, reason} ->
        Logger.warning("governance tool-call #{name} failed: #{inspect(reason)}")
        {:error, reason}
    end
  end

  # The exact list shape is the engine's to settle; accept the common shapes so a
  # server-side rename doesn't silently blank the pane.
  #
  # NOTE: this tolerance is why unwrapping had to move to the SDK. A governance
  # refusal is a map with none of these keys, so the old `unwrap/1` returned it
  # as a successful result and `normalize_sessions/1` turned it into `[]` — the
  # pane rendered "no sessions" for "governance refused you". `Envelope.classify/1`
  # now returns `{:error, {:refused, _}}` before this function is ever reached,
  # so the fallback below only ever sees genuinely unrecognised *success* shapes.
  defp normalize_sessions(result) when is_list(result), do: result
  defp normalize_sessions(%{"sessions" => s}) when is_list(s), do: s
  defp normalize_sessions(%{"items" => s}) when is_list(s), do: s
  defp normalize_sessions(%{"data" => s}) when is_list(s), do: s
  defp normalize_sessions(_), do: []
end
