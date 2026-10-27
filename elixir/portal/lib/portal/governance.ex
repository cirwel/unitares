defmodule Portal.Governance do
  @moduledoc """
  The hub's read client for the governance server: `GET /v1/timeline`, and
  nothing else. It only reads.

  It sends the hub's read-only key (the server's `UNITARES_READ_ONLY_TOKENS`;
  operator decision 2026-10-27, KG `2026-10-27T19:26:49.785412+00:00`), which
  the server accepts on the timeline and its stream and refuses everywhere
  else. The key is held here, in the hub's configuration, and never reaches
  the browser, a LiveView assign, or a log line.

  Copied from `dialectic_live`'s client (portal-hub-v0 H2d-2b), without its
  dialectic tool calls. Every call accepts `req_options:` (merged into the Req
  request) so tests can route it through `Req.Test`.
  """

  require Logger

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
  def config, do: Application.get_env(:portal, :governance, [])

  defp auth_headers(cfg) do
    case Keyword.get(cfg, :read_token) do
      t when is_binary(t) and t != "" -> [{"authorization", "Bearer " <> t}]
      _ -> []
    end
  end
end
