defmodule UnitaresSentinel.GovernanceCheckin do
  @moduledoc """
  Best-effort REST client for Sentinel `process_agent_update` check-ins.

  Wave 1 binds BEAM Sentinel to the existing HTTP tool-call surface:
  `POST /v1/tools/call` with `name=process_agent_update`. This module keeps
  that boundary explicit and fail-soft; transport errors return `{:error, ...}`
  rather than escaping the runtime analysis cycle.
  """

  require Logger

  @default_url "http://localhost:8767/v1/tools/call"
  @default_timeout_ms 45_000

  @type http_post ::
          (String.t(), map(), [{String.t(), String.t()}], pos_integer() ->
             {:ok, non_neg_integer(), String.t()} | {:error, term()})

  @doc """
  POST one cycle summary to `process_agent_update`.
  """
  @spec checkin(map(), keyword()) :: {:ok, map()} | {:error, term()}
  def checkin(summary, opts \\ []) when is_map(summary) do
    summary
    |> body(opts)
    |> post_json(opts)
  end

  @doc false
  @spec body(map(), keyword()) :: map()
  def body(summary, opts \\ []) when is_map(summary) do
    anchor = Keyword.get(opts, :anchor, %{})

    arguments =
      %{
        "response_text" => map_fetch!(summary, :response_text),
        "complexity" => map_fetch!(summary, :complexity),
        "confidence" => map_fetch!(summary, :confidence),
        "response_mode" => map_get(summary, :response_mode, "compact")
      }
      |> put_optional("agent_id", Keyword.get(opts, :agent_id) || Map.get(anchor, "agent_uuid"))
      |> put_optional(
        "client_session_id",
        Keyword.get(opts, :client_session_id) || Map.get(anchor, "client_session_id")
      )
      |> put_optional(
        "continuity_token",
        Keyword.get(opts, :continuity_token) || Map.get(anchor, "continuity_token")
      )

    %{"name" => "process_agent_update", "arguments" => arguments}
  end

  @doc false
  @spec post_json(map(), keyword()) :: {:ok, map()} | {:error, term()}
  def post_json(body, opts \\ []) when is_map(body) do
    http_post = Keyword.get(opts, :http_post, &finch_post/4)
    url = Keyword.get(opts, :url, governance_tools_url())
    timeout_ms = Keyword.get(opts, :timeout_ms, governance_timeout_ms())

    case http_post.(url, body, headers(), timeout_ms) do
      {:ok, 200, response_body} ->
        decode_response(response_body)

      {:ok, status, response_body} ->
        Logger.debug("UnitaresSentinel.GovernanceCheckin.post_json non-200: #{inspect(status)}")

        {:error, {:http_status, status, response_body}}

      {:error, reason} ->
        Logger.debug("UnitaresSentinel.GovernanceCheckin.post_json failed: #{inspect(reason)}")
        {:error, reason}
    end
  rescue
    e ->
      Logger.debug("UnitaresSentinel.GovernanceCheckin.post_json raised: #{inspect(e)}")
      {:error, e}
  catch
    :exit, reason ->
      Logger.debug("UnitaresSentinel.GovernanceCheckin.post_json exited: #{inspect(reason)}")
      {:error, {:exit, reason}}
  end

  defp finch_post(url, body, headers, timeout_ms) do
    json = Jason.encode!(body)
    request = Finch.build(:post, url, headers, json)

    case Finch.request(request, UnitaresSentinel.Finch, receive_timeout: timeout_ms) do
      {:ok, %Finch.Response{status: status, body: response_body}} ->
        {:ok, status, response_body}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp decode_response(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{"success" => true, "result" => %{} = result}} ->
        case ensure_tool_success(result) do
          :ok -> {:ok, result}
          {:error, _reason} = error -> error
        end

      {:ok, %{"success" => false} = decoded} ->
        {:error, {:tool_error, Map.get(decoded, "error", "unknown")}}

      {:ok, decoded} ->
        {:error, {:invalid_response, decoded}}

      {:error, reason} ->
        {:error, {:invalid_json, reason}}
    end
  end

  defp ensure_tool_success(%{"success" => false} = result),
    do: {:error, {:tool_error, Map.get(result, "error", "unknown")}}

  defp ensure_tool_success(_result), do: :ok

  defp headers do
    base = [{"Content-Type", "application/json"}]

    case System.get_env("UNITARES_HTTP_API_TOKEN") do
      nil -> base
      "" -> base
      token -> [{"Authorization", "Bearer #{token}"} | base]
    end
  end

  defp governance_tools_url do
    Application.get_env(:unitares_sentinel, :governance_tools_url) ||
      System.get_env("UNITARES_GOVERNANCE_TOOLS_URL") ||
      @default_url
  end

  defp governance_timeout_ms do
    Application.get_env(:unitares_sentinel, :governance_checkin_timeout_ms, @default_timeout_ms)
  end

  defp put_optional(payload, _key, nil), do: payload
  defp put_optional(payload, _key, ""), do: payload
  defp put_optional(payload, key, value) when is_binary(value), do: Map.put(payload, key, value)

  defp map_fetch!(map, key) when is_atom(key) do
    Map.fetch!(map, key)
  rescue
    KeyError -> Map.fetch!(map, Atom.to_string(key))
  end

  defp map_get(map, key, default) when is_atom(key) do
    Map.get(map, key) || Map.get(map, Atom.to_string(key), default)
  end
end
