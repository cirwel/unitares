defmodule UnitaresSentinel.Findings do
  @moduledoc """
  Best-effort `/api/findings` client for BEAM Sentinel.

  Mirrors `agents.common.findings.post_finding`: callers get a boolean,
  network/API failures return `false`, and exceptions never leave the hot
  cycle path. `ForcedReleasePoller` uses this for Surface 2 forced-release
  alarm emission.
  """

  require Logger

  @default_url "http://localhost:8767/api/findings"
  @default_timeout_ms 3_000
  @default_agent_id "sentinel"
  @default_agent_name "Sentinel"

  @type http_post ::
          (String.t(), map(), [{String.t(), String.t()}], pos_integer() ->
             {:ok, non_neg_integer(), String.t()} | {:error, term()})

  @doc """
  POST one forced-release alarm as a `sentinel_alarm_finding` event.

  The `_finding` suffix is required by the governance `/api/findings`
  gateway (see `_FINDING_TYPE_SUFFIX` in `src/http_api.py`). The granular
  alarm kind (`ad_hoc` / `deprecation_batch` / `conflict_batch`) rides in
  the `alarm_kind` field for downstream consumers.
  """
  @spec post_alarm(UnitaresSentinel.ForcedReleasePoller.Logic.alarm(), keyword()) :: boolean()
  def post_alarm(alarm, opts \\ []) when is_map(alarm) do
    alarm
    |> alarm_body(opts)
    |> post_json(opts)
  end

  @doc """
  POST one fleet analysis finding as a `sentinel_finding` event.
  """
  @spec post_finding(map(), keyword()) :: boolean()
  def post_finding(finding, opts \\ []) when is_map(finding) do
    finding
    |> finding_body(opts)
    |> post_json(opts)
  end

  @doc false
  @spec alarm_body(UnitaresSentinel.ForcedReleasePoller.Logic.alarm(), keyword()) :: map()
  def alarm_body(alarm, opts \\ []) when is_map(alarm) do
    base = %{
      "type" => Keyword.get(opts, :event_type, "sentinel_alarm_finding"),
      "severity" => Map.fetch!(alarm, :severity),
      "message" => Map.fetch!(alarm, :summary),
      "agent_id" => agent_id(opts),
      "agent_name" => agent_name(opts),
      "fingerprint" => Map.fetch!(alarm, :fingerprint),
      "alarm_kind" => Map.fetch!(alarm, :kind)
    }

    alarm
    |> Map.get(:extra, %{})
    |> stringify_keys()
    |> Map.merge(base, fn _key, _extra_value, base_value -> base_value end)
  end

  @doc false
  @spec finding_body(map(), keyword()) :: map()
  def finding_body(finding, opts \\ []) when is_map(finding) do
    finding_type = map_fetch!(finding, :type)
    violation_class = map_get(finding, :violation_class, "")
    agent_id = agent_id(opts)

    %{
      "type" => Keyword.get(opts, :event_type, "sentinel_finding"),
      "severity" => map_fetch!(finding, :severity),
      "message" => map_fetch!(finding, :summary),
      "agent_id" => agent_id,
      "agent_name" => agent_name(opts),
      "fingerprint" => compute_fingerprint(["sentinel", finding_type, violation_class, agent_id]),
      "violation_class" => violation_class,
      "finding_type" => finding_type
    }
  end

  @doc false
  @spec compute_fingerprint(Enumerable.t()) :: String.t()
  def compute_fingerprint(parts) do
    parts
    |> Enum.map(&to_string/1)
    |> Enum.join("|")
    |> then(&:crypto.hash(:sha256, &1))
    |> Base.encode16(case: :lower)
    |> binary_part(0, 16)
  end

  @doc false
  @spec post_json(map(), keyword()) :: boolean()
  def post_json(body, opts \\ []) when is_map(body) do
    http_post = Keyword.get(opts, :http_post, &finch_post/4)
    url = Keyword.get(opts, :url, findings_url())
    timeout_ms = Keyword.get(opts, :timeout_ms, findings_timeout_ms())

    case http_post.(url, body, headers(), timeout_ms) do
      {:ok, 200, response_body} ->
        accepted?(response_body)

      {:ok, status, _response_body} ->
        Logger.debug("UnitaresSentinel.Findings.post_json non-200: #{inspect(status)}")
        false

      {:error, reason} ->
        Logger.debug("UnitaresSentinel.Findings.post_json failed: #{inspect(reason)}")
        false
    end
  rescue
    e ->
      Logger.debug("UnitaresSentinel.Findings.post_json raised: #{inspect(e)}")
      false
  catch
    :exit, reason ->
      Logger.debug("UnitaresSentinel.Findings.post_json exited: #{inspect(reason)}")
      false
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

  defp accepted?(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{"success" => true} = decoded} -> not Map.get(decoded, "deduped", false)
      _ -> false
    end
  end

  defp headers do
    base = [{"Content-Type", "application/json"}]

    case System.get_env("UNITARES_HTTP_API_TOKEN") do
      nil -> base
      "" -> base
      token -> [{"Authorization", "Bearer #{token}"} | base]
    end
  end

  defp findings_url do
    Application.get_env(:unitares_sentinel, :findings_url) ||
      System.get_env("UNITARES_FINDINGS_URL") ||
      @default_url
  end

  defp findings_timeout_ms do
    Application.get_env(:unitares_sentinel, :findings_timeout_ms, @default_timeout_ms)
  end

  defp agent_id(opts) do
    Keyword.get(opts, :agent_id) ||
      Application.get_env(:unitares_sentinel, :findings_agent_id) ||
      System.get_env("UNITARES_SENTINEL_AGENT_ID") ||
      @default_agent_id
  end

  defp agent_name(opts) do
    Keyword.get(opts, :agent_name) ||
      Application.get_env(:unitares_sentinel, :findings_agent_name, @default_agent_name)
  end

  defp stringify_keys(map) when is_map(map) do
    Map.new(map, fn
      {key, value} when is_atom(key) -> {Atom.to_string(key), value}
      {key, value} when is_binary(key) -> {key, value}
    end)
  end

  defp map_fetch!(map, key) when is_atom(key) do
    Map.fetch!(map, key)
  rescue
    KeyError -> Map.fetch!(map, Atom.to_string(key))
  end

  defp map_get(map, key, default) when is_atom(key) do
    Map.get(map, key) || Map.get(map, Atom.to_string(key), default)
  end
end
