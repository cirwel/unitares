defmodule DialecticLive.Governance do
  @moduledoc """
  Read client for the Python governance MCP (:8767).

  Dialectic is not a REST resource on the server — it is reached through the
  generic tool-call endpoint `POST /v1/tools/call` with
  `{name: "dialectic", arguments: %{action: ...}}`. The bearer token is held
  server-side in this app's config and never leaves the BEAM node, so the
  browser never sees it (unlike the buildless dashboard's in-browser authFetch).

  This is the B1 data source: `list_sessions/0` and `get_session/1` over what the
  current server already exposes. True per-turn streaming waits on the engine
  emitting `dialectic_*` broadcast events (#1167 Ask 1) — not wired here.
  """

  require Logger

  @doc "List dialectic sessions. Returns `{:ok, [session_map]}` or `{:error, reason}`."
  def list_sessions(limit \\ 50) do
    case call("dialectic", %{action: "list", limit: limit}) do
      {:ok, result} -> {:ok, normalize_sessions(result)}
      {:error, _} = err -> err
    end
  end

  @doc "Fetch one dialectic session by id. Returns `{:ok, map}` or `{:error, reason}`."
  def get_session(session_id) when is_binary(session_id) do
    call("dialectic", %{action: "get", session_id: session_id})
  end

  @doc false
  def config, do: Application.get_env(:dialectic_live, :governance, [])

  # --- internals ---

  defp call(name, arguments) do
    cfg = config()
    url = Keyword.get(cfg, :tools_url)
    token = Keyword.get(cfg, :api_token)

    headers =
      case token do
        t when is_binary(t) and t != "" -> [{"authorization", "Bearer " <> t}]
        _ -> []
      end

    body = %{name: name, arguments: arguments}

    case Req.post(url, json: body, headers: headers, receive_timeout: 8_000) do
      {:ok, %Req.Response{status: 200, body: resp}} ->
        {:ok, unwrap(resp)}

      {:ok, %Req.Response{status: status, body: resp}} ->
        Logger.warning("governance tool-call #{name} -> HTTP #{status}: #{inspect(resp)}")
        {:error, {:http_status, status}}

      {:error, reason} ->
        Logger.warning("governance tool-call #{name} failed: #{inspect(reason)}")
        {:error, reason}
    end
  end

  # The /v1/tools/call envelope wraps tool output under "result"; fall back to the
  # whole body if the server returned the result flat.
  defp unwrap(%{"result" => result}), do: result
  defp unwrap(other), do: other

  # The exact list shape is the engine's to settle; accept the common shapes so a
  # server-side rename doesn't silently blank the pane.
  defp normalize_sessions(result) when is_list(result), do: result
  defp normalize_sessions(%{"sessions" => s}) when is_list(s), do: s
  defp normalize_sessions(%{"items" => s}) when is_list(s), do: s
  defp normalize_sessions(%{"data" => s}) when is_list(s), do: s
  defp normalize_sessions(_), do: []
end
