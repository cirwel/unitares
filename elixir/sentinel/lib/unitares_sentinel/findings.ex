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

  @doc """
  POST one finding and report *why* it did or did not land.

  `post_finding/2` collapses network error, non-200 and server-side dedup into
  a single `false`. That is fine for fire-and-forget fleet findings, but a
  caller that self-limits its own re-emission (see
  `UnitaresSentinel.LeaseStarvation`) must distinguish "delivered" from "lost":
  a lost POST has to be retried on the next tick, while a deduped POST is
  already recorded server-side and retrying it would be a storm against an
  endpoint that will keep answering `deduped: true`.

  This matters most in exactly the condition the starvation finding exists for:
  gov-MCP being unreachable (the documented jetsam 502 window) is *correlated*
  with residents starving, so the densest and most valuable early alerts are the
  ones most likely to be dropped. `src/http_api.py:2676` already puts
  `{"success": true, "deduped": true}` on the wire; `accepted?/1` just threw it
  away. 2026-07-31 immortal-lease incident.
  """
  @spec post_finding_result(map(), keyword()) :: :accepted | :deduped | {:error, term()}
  def post_finding_result(finding, opts \\ []) when is_map(finding) do
    finding
    |> finding_body(opts)
    |> post_json_result(opts)
  end

  @doc """
  POST a one-shot build-info finding (`sentinel_build_finding`, severity info)
  so the alert stream records exactly which commit the running node booted from
  — the queryable answer to "is the merged fix actually live?".

  The fingerprint includes the sha, so a boot onto NEW code emits a fresh
  finding while a reboot onto the SAME code dedups (no per-restart spam).
  Takes a `UnitaresSentinel.BuildInfo.t()` map.
  """
  @spec post_build_info(map(), keyword()) :: boolean()
  def post_build_info(info, opts \\ []) when is_map(info) do
    sha = Map.get(info, :sha, "unknown")

    %{
      "type" => "sentinel_build_finding",
      "severity" => "info",
      "message" => "BEAM Sentinel booted: " <> Map.get(info, :summary, sha),
      "agent_id" => agent_id(opts),
      "agent_name" => agent_name(opts),
      "fingerprint" => compute_fingerprint(["sentinel", "build", sha]),
      "git_sha" => sha,
      "version" => Map.get(info, :version, "unknown"),
      "dirty" => Map.get(info, :dirty, false)
    }
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

    # `:fingerprint_extra` appends discriminators to the legacy 4-part key.
    # Empty by default, so every pre-existing caller's fingerprint is
    # byte-identical (pinned by the golden `da9b8e957ab6971e` in findings_test).
    # Needed because two residents on ONE agent_id can starve on two different
    # lease surfaces, and keying only on [type, class, agent_id] would silently
    # dedup one resident's outage into the other's. 2026-07-31 incident.
    fingerprint_extra = map_get(finding, :fingerprint_extra, [])

    base = %{
      "type" => Keyword.get(opts, :event_type, "sentinel_finding"),
      "severity" => map_fetch!(finding, :severity),
      "message" => map_fetch!(finding, :summary),
      "agent_id" => agent_id,
      "agent_name" => agent_name(opts),
      "fingerprint" =>
        compute_fingerprint(
          ["sentinel", finding_type, violation_class, agent_id] ++ fingerprint_extra
        ),
      "violation_class" => violation_class,
      "finding_type" => finding_type
    }

    # Same extra-merge contract as `alarm_body/2`: caller context rides along,
    # base keys always win. `change_token` flips the governance detector
    # (`src/event_detector.py` `record_event`) from 30-minute fingerprint dedup
    # to time-independent emit-on-change, which is what lets a resident own its
    # own re-emission schedule instead of fighting a server-side window it
    # cannot see.
    finding
    |> map_get(:extra, %{})
    |> stringify_keys()
    |> Map.merge(base, fn _key, _extra_value, base_value -> base_value end)
    |> maybe_put("change_token", map_get(finding, :change_token, nil))
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
    post_json_result(body, opts) == :accepted
  end

  @doc false
  @spec post_json_result(map(), keyword()) :: :accepted | :deduped | {:error, term()}
  def post_json_result(body, opts \\ []) when is_map(body) do
    http_post = Keyword.get(opts, :http_post, &finch_post/4)
    url = Keyword.get(opts, :url, findings_url())
    timeout_ms = Keyword.get(opts, :timeout_ms, findings_timeout_ms())

    case http_post.(url, body, headers(), timeout_ms) do
      {:ok, 200, response_body} ->
        classify_response(response_body)

      {:ok, status, _response_body} ->
        Logger.debug("UnitaresSentinel.Findings.post_json non-200: #{inspect(status)}")
        {:error, {:http_status, status}}

      {:error, reason} ->
        Logger.debug("UnitaresSentinel.Findings.post_json failed: #{inspect(reason)}")
        {:error, reason}
    end
  rescue
    e ->
      Logger.debug("UnitaresSentinel.Findings.post_json raised: #{inspect(e)}")
      {:error, {:raised, e}}
  catch
    :exit, reason ->
      Logger.debug("UnitaresSentinel.Findings.post_json exited: #{inspect(reason)}")
      {:error, {:exited, reason}}
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

  # `deduped: true` is a DELIVERY, not a failure: the governance detector saw
  # this exact condition already. Callers that self-limit re-emission must not
  # retry it. `post_json/2` keeps the historical boolean contract by treating
  # anything other than `:accepted` as false.
  defp classify_response(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{"success" => true} = decoded} ->
        if Map.get(decoded, "deduped", false), do: :deduped, else: :accepted

      {:ok, decoded} ->
        {:error, {:rejected, decoded}}

      _ ->
        {:error, :undecodable_response}
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

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)
end
