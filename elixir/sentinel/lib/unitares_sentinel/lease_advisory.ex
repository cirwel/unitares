defmodule UnitaresSentinel.LeaseAdvisory do
  @moduledoc """
  Best-effort Phase A lease-plane advisory client for BEAM Sentinel.

  Mirrors `src/lease_plane/advisory.py`: missing token, network errors,
  schema errors, and held-by-other responses are telemetry only. Callers get
  a classified outcome, and lease-layer failures never block Sentinel work.
  """

  require Logger

  @default_base_url "http://127.0.0.1:8788"
  @default_timeout_ms 2_000
  @cycle_surface_id "resident:/sentinel_cycle"
  @cycle_holder_kind "remote_heartbeat"
  @cycle_ttl_s 300
  @cycle_intent "sentinel analysis cycle"

  @type outcome ::
          :acquired_new
          | :acquired_idempotent
          | :enforcement_blocked
          | :held_by_other
          | :service_unavailable
          | :permission_denied
          | :schema_invalid
          | :client_error

  @type scope :: %{outcome: outcome(), lease_id: String.t() | nil}

  @type http_post ::
          (String.t(), map(), [{String.t(), String.t()}], pos_integer() ->
             {:ok, non_neg_integer(), String.t()} | {:error, term()})

  @doc """
  Acquire the Sentinel cycle advisory lease.

  The request shape intentionally matches Python's `lease_advisory_scope/1`
  wrapper around `SentinelAgent.run_cycle/1`.
  """
  @spec acquire_cycle(keyword()) :: scope()
  def acquire_cycle(opts \\ []) do
    body =
      %{
        "surface_id" => Keyword.get(opts, :surface_id, @cycle_surface_id),
        "holder_agent_uuid" => Keyword.get(opts, :holder_agent_uuid, new_holder_uuid()),
        "holder_class" => "process_instance",
        "holder_kind" => Keyword.get(opts, :holder_kind, @cycle_holder_kind),
        "ttl_s" => Keyword.get(opts, :ttl_s, @cycle_ttl_s),
        "intent" => Keyword.get(opts, :intent, @cycle_intent)
      }
      |> maybe_put("audit_session", audit_session(opts))

    acquire_advisory(body, opts)
  end

  @doc false
  @spec acquire_advisory(map(), keyword()) :: scope()
  def acquire_advisory(body, opts \\ []) when is_map(body) do
    surface_id = Map.get(body, "surface_id")

    scope =
      with {:ok, token} <- bearer_token(opts),
           {:ok, status, response_body} <- post_json("/v1/lease/acquire", body, token, opts) do
        classify_acquire(status, response_body, surface_id)
      else
        {:disabled, reason} ->
          Logger.debug("lease_advisory: disabled #{inspect(reason)}")
          scope(:service_unavailable)

        {:error, reason} ->
          Logger.debug("lease_advisory: acquire failed #{inspect(reason)}")
          scope(:service_unavailable)
      end

    enforce_scope(scope, surface_id, opts)
  rescue
    e ->
      Logger.debug("lease_advisory: acquire raised #{inspect(e)}")
      body |> Map.get("surface_id") |> then(&enforce_scope(scope(:client_error), &1, opts))
  catch
    :exit, reason ->
      Logger.debug("lease_advisory: acquire exited #{inspect(reason)}")
      body |> Map.get("surface_id") |> then(&enforce_scope(scope(:client_error), &1, opts))
  end

  @doc """
  Release a previously acquired advisory lease.

  No-op for non-acquire outcomes; all release failures are swallowed.
  """
  @spec release(scope() | String.t() | nil, keyword()) :: :ok
  def release(%{lease_id: nil}, _opts), do: :ok
  def release(nil, _opts), do: :ok

  def release(%{lease_id: lease_id}, opts) when is_binary(lease_id) do
    release(lease_id, opts)
  end

  def release(lease_id, opts) when is_binary(lease_id) do
    body = %{"lease_id" => lease_id, "release_reason" => "normal"}

    with {:ok, token} <- bearer_token(opts),
         {:ok, status, response_body} <- post_json("/v1/lease/release", body, token, opts) do
      log_release(status, response_body, lease_id)
    else
      {:disabled, reason} ->
        Logger.debug("lease_advisory: release skipped #{inspect(reason)}")

      {:error, reason} ->
        Logger.debug("lease_advisory: release failed #{inspect(reason)}")
    end

    :ok
  rescue
    e ->
      Logger.debug("lease_advisory: release raised lease_id=#{lease_id} err=#{inspect(e)}")
      :ok
  catch
    :exit, reason ->
      Logger.debug("lease_advisory: release exited lease_id=#{lease_id} err=#{inspect(reason)}")
      :ok
  end

  @doc false
  @spec new_holder_uuid() :: String.t()
  def new_holder_uuid do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)

    [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    |> Enum.map_join("-", &Base.encode16(&1, case: :lower))
  end

  defp post_json(path, body, token, opts) do
    http_post = Keyword.get(opts, :http_post, &finch_post/4)
    timeout_ms = Keyword.get(opts, :timeout_ms, lease_plane_timeout_ms())
    url = endpoint_url(Keyword.get(opts, :base_url, lease_plane_base_url()), path)

    http_post.(url, body, headers(token), timeout_ms)
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

  defp classify_acquire(status, response_body, surface_id) do
    case decode_object(response_body) do
      {:ok, %{"ok" => true, "lease" => %{"lease_id" => lease_id}} = decoded} ->
        outcome =
          if Map.get(decoded, "idempotent", false), do: :acquired_idempotent, else: :acquired_new

        Logger.info("lease_advisory: #{outcome} surface=#{surface_id} lease_id=#{lease_id}")
        scope(outcome, lease_id)

      {:ok, %{"ok" => false, "error" => "held_by_other"} = decoded} ->
        Logger.info(
          "lease_advisory: held_by_other surface=#{surface_id} held_by=#{Map.get(decoded, "held_by_uuid")} (Phase A: proceeding regardless)"
        )

        scope(:held_by_other)

      {:ok, %{"ok" => false, "error" => "permission_denied", "reason" => reason}} ->
        Logger.warning("lease_advisory: permission_denied surface=#{surface_id} reason=#{reason}")
        scope(:permission_denied)

      {:ok, %{"ok" => false, "error" => "schema_invalid", "detail" => detail}} ->
        Logger.warning(
          "lease_advisory: schema_invalid surface=#{surface_id} detail=#{inspect(detail)}"
        )

        scope(:schema_invalid)

      {:ok, %{"ok" => false, "error" => "service_unavailable"}} ->
        Logger.info("lease_advisory: service_unavailable surface=#{surface_id}")
        scope(:service_unavailable)

      {:ok, _payload} ->
        scope(:client_error)

      {:error, _detail} when status in [401, 403] ->
        Logger.warning("lease_advisory: permission_denied surface=#{surface_id} status=#{status}")
        scope(:permission_denied)

      {:error, _detail} when is_integer(status) and status >= 400 ->
        Logger.info("lease_advisory: service_unavailable surface=#{surface_id} status=#{status}")
        scope(:service_unavailable)

      {:error, _detail} ->
        scope(:schema_invalid)
    end
  end

  defp log_release(status, response_body, lease_id) do
    case decode_object(response_body) do
      {:ok, %{"ok" => true}} ->
        Logger.info("lease_advisory: released lease_id=#{lease_id} ok=true")

      {:ok, payload} ->
        Logger.debug(
          "lease_advisory: release non-ok lease_id=#{lease_id} status=#{status} body=#{inspect(payload)}"
        )

      {:error, detail} ->
        Logger.debug(
          "lease_advisory: release invalid response lease_id=#{lease_id} status=#{status} detail=#{inspect(detail)}"
        )
    end
  end

  defp decode_object(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{} = decoded} -> {:ok, decoded}
      {:ok, _} -> {:error, "response was not an object"}
      {:error, _} -> {:error, "response was not JSON"}
    end
  end

  defp scope(outcome, lease_id \\ nil), do: %{outcome: outcome, lease_id: lease_id}

  defp headers(token) do
    [
      {"Authorization", "Bearer #{token}"},
      {"Accept", "application/json"},
      {"Content-Type", "application/json"}
    ]
  end

  defp bearer_token(opts) do
    opts
    |> Keyword.get_lazy(:bearer_token, fn -> System.get_env("LEASE_PLANE_BEARER_TOKEN") end)
    |> case do
      token when is_binary(token) ->
        case String.trim(token) do
          "" -> {:disabled, :missing_bearer_token}
          trimmed -> {:ok, trimmed}
        end

      _ ->
        {:disabled, :missing_bearer_token}
    end
  end

  defp endpoint_url(base_url, path) do
    String.trim_trailing(base_url, "/") <> path
  end

  defp lease_plane_base_url do
    Application.get_env(:unitares_sentinel, :lease_plane_base_url) ||
      System.get_env("LEASE_PLANE_BASE_URL") ||
      @default_base_url
  end

  defp lease_plane_timeout_ms do
    Application.get_env(:unitares_sentinel, :lease_plane_timeout_ms, @default_timeout_ms)
  end

  defp audit_session(opts) do
    non_empty_string(Keyword.get(opts, :audit_session)) ||
      configured_audit_session() ||
      session_anchor_client_session_id(opts)
  end

  defp configured_audit_session do
    non_empty_string(Application.get_env(:unitares_sentinel, :lease_audit_session)) ||
      non_empty_string(System.get_env("UNITARES_SENTINEL_AUDIT_SESSION"))
  end

  defp enforce_scope(%{lease_id: nil, outcome: outcome} = scope, surface_id, opts) do
    if surface_enforced?(surface_id, opts) do
      Logger.warning("lease_enforcement: blocked surface=#{surface_id} outcome=#{outcome}")
      %{scope | outcome: :enforcement_blocked}
    else
      scope
    end
  end

  defp enforce_scope(scope, _surface_id, _opts), do: scope

  defp surface_enforced?(surface_id, opts) when is_binary(surface_id) do
    kinds = Keyword.get_lazy(opts, :enforced_surface_kinds, &configured_enforced_surface_kinds/0)
    "*" in kinds or surface_kind(surface_id) in kinds
  end

  defp surface_enforced?(_surface_id, _opts), do: false

  defp configured_enforced_surface_kinds do
    configured =
      Application.get_env(:unitares_sentinel, :lease_enforced_surface_kinds) ||
        System.get_env("LEASE_PLANE_ENFORCED_SURFACE_KINDS") ||
        ""

    configured
    |> split_surface_kinds()
    |> MapSet.new()
  end

  defp split_surface_kinds(value) when is_binary(value) do
    value
    |> String.split(",")
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp split_surface_kinds(values) when is_list(values), do: values
  defp split_surface_kinds(_value), do: []

  defp surface_kind(surface_id), do: surface_id |> String.split(":", parts: 2) |> hd()

  defp session_anchor_client_session_id(opts) do
    case Keyword.fetch(opts, :anchor) do
      {:ok, %{} = anchor} ->
        non_empty_string(Map.get(anchor, "client_session_id"))

      :error ->
        case UnitaresSentinel.SessionAnchor.load() do
          {:ok, anchor} -> non_empty_string(Map.get(anchor, "client_session_id"))
          {:error, _reason} -> nil
        end
    end
  end

  defp non_empty_string(value) when is_binary(value) do
    case String.trim(value) do
      "" -> nil
      trimmed -> trimmed
    end
  end

  defp non_empty_string(_value), do: nil

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, _key, ""), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)
end
