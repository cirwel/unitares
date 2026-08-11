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
    with :ok <- maybe_attest_substrate(opts) do
      summary
      |> body(opts)
      |> post_json(opts)
    end
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
      # ⛔ Deliberately NO "agent_id" on a check-in.
      #
      # Identity is presented by ECHOING the client_session_id the server
      # issued, never by declaring the uuid. The REST prebind checks an
      # explicit agent_id FIRST (`http_api._bind_explicit_http_agent`) and
      # accepts it on a shape test alone — 36 chars, 4 hyphens, no lookup — so
      # a declared uuid short-circuits resolution before the CSID is ever
      # consulted. The strict gate then never sees a miss and never emits its
      # typed refusal, and the PG session row never renews: binding loss
      # becomes unobservable while every check-in still looks successful.
      #
      # Measured on this agent 2026-08-10: with a valid CSID in the anchor AND
      # a live core.sessions row for identity 3701, a full fleet-emit cycle
      # still left last_active unchanged. The row was inert purely because
      # agent_id won the resolution. That is what this line changes.
      #
      # anima_broker drops the key for the same reason and proved it live on
      # 2026-07-03 — its Redis-wipe acceptance test passed WITHOUT exercising
      # recovery until the key was dropped. `recover/1` below keeps agent_id:
      # self_recovery is not a check-in and legitimately names its subject.
      # Bind by CSID, address by agent_id.
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
    url = Keyword.get(opts, :url, governance_tools_url())
    timeout_ms = Keyword.get(opts, :timeout_ms, governance_timeout_ms())

    result =
      case Keyword.fetch(opts, :http_post) do
        {:ok, http_post} -> http_post.(url, body, headers(), timeout_ms)
        :error -> finch_post(url, body, headers(), timeout_ms, opts)
      end

    case result do
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

  defp finch_post(url, body, headers, timeout_ms, opts) do
    request = build_request(url, body, headers, opts)

    case Finch.request(request, UnitaresSentinel.Finch, receive_timeout: timeout_ms) do
      {:ok, %Finch.Response{status: status, body: response_body}} ->
        {:ok, status, response_body}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc false
  @spec build_request(String.t(), map(), [{String.t(), String.t()}], keyword()) ::
          Finch.Request.t()
  def build_request(url, body, headers, opts \\ []) do
    json = Jason.encode!(body)

    socket =
      case Keyword.fetch(opts, :uds_socket) do
        {:ok, value} -> value
        :error -> governance_uds_socket()
      end

    Finch.build(:post, url, headers, json, unix_socket_opts(socket))
  end

  defp decode_response(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{"success" => true, "result" => %{} = result}} ->
        classify_tool_result(result)

      {:ok, %{"success" => false} = decoded} ->
        {:error, {:tool_error, Map.get(decoded, "error", "unknown")}}

      {:ok, decoded} ->
        # Not the {"success": true, "result": {}} shape this client requires.
        # Before calling it merely "invalid", ask the shared envelope contract
        # whether it is a *refusal*: a typed strict identity refusal carries
        # neither success:false nor an action, so it lands in this branch
        # looking like unparseable noise. Naming it is the difference between
        # "the server said something odd" and "we are not authenticated" —
        # which is what kept canonical Lumen governance-dark ~3 days after the
        # 2026-06-30 Redis wipe.
        case UnitaresSdk.Envelope.classify(decoded) do
          {:error, {:refused, _} = refusal} -> {:error, refusal}
          _ -> {:error, {:invalid_response, decoded}}
        end

      {:error, reason} ->
        {:error, {:invalid_json, reason}}
    end
  end

  # A circuit-breaker / governance pause is NOT an ordinary tool error: the
  # agent is dark to governance until recovered, and silently swallowing it
  # (as a generic tool_error logged at :debug) is exactly how a paused
  # resident stayed invisible for ~18h. Classify it distinctly so the caller
  # can surface it and attempt a bounded, server-gated self-recovery.
  defp ensure_tool_success(%{"success" => false, "error_code" => "AGENT_PAUSED"} = result),
    do: {:error, {:agent_paused, pause_detail(result)}}

  defp ensure_tool_success(%{"success" => false} = result),
    do: {:error, {:tool_error, Map.get(result, "error", "unknown")}}

  defp ensure_tool_success(_result), do: :ok

  # The HTTP bridge wraps handler output under `result`. A strict identity
  # refusal is itself a success-shaped handler response, so checking only the
  # outer envelope (or only result["success"]) silently turns the refusal into
  # {:ok, result}. Classify the inner payload after preserving the more
  # detailed paused/tool-error forms above.
  defp classify_tool_result(result) do
    case ensure_tool_success(result) do
      :ok ->
        case UnitaresSdk.Envelope.classify(result) do
          {:ok, _classified} -> {:ok, result}
          {:error, _reason} = error -> error
        end

      {:error, _reason} = error ->
        error
    end
  end

  # A substrate claim is meaningful only when governance verifies the kernel-
  # attested UDS peer against the enrolled launchd label and executable. PATH 2
  # session resolution alone is continuity, not that process attestation, so a
  # UDS-enabled resident proves its UUID with `identity` before every sensitive
  # write. Sentinel checks in every five minutes; one small local request per
  # cycle is preferable to caching an attestation longer than the process that
  # earned it.
  defp maybe_attest_substrate(opts) do
    if uds_socket_configured?(opts) do
      anchor = Keyword.get(opts, :anchor, %{})
      expected_uuid = Keyword.get(opts, :agent_uuid) || Map.get(anchor, "agent_uuid")

      if is_binary(expected_uuid) and expected_uuid != "" do
        arguments =
          %{"agent_uuid" => expected_uuid, "resume" => true}
          |> put_optional(
            "client_session_id",
            Keyword.get(opts, :client_session_id) || Map.get(anchor, "client_session_id")
          )

        case post_json(%{"name" => "identity", "arguments" => arguments}, opts) do
          {:ok, result} -> verify_attested_uuid(result, expected_uuid)
          {:error, _reason} = error -> error
        end
      else
        {:error, :missing_substrate_agent_uuid}
      end
    else
      :ok
    end
  end

  defp verify_attested_uuid(result, expected_uuid) do
    case Map.get(result, "uuid") || Map.get(result, "agent_uuid") do
      ^expected_uuid -> :ok
      actual -> {:error, {:attestation_identity_mismatch, actual}}
    end
  end

  defp pause_detail(result) do
    %{
      "error" => Map.get(result, "error", "Agent is paused and cannot process updates"),
      "paused_at" => Map.get(result, "paused_at"),
      "status" => Map.get(result, "status", "paused"),
      "recovery" => Map.get(result, "recovery")
    }
  end

  @doc """
  Attempt a bounded self-recovery for a paused Sentinel identity.

  Posts `self_recovery` (default `action=quick`) to the same `/v1/tools/call`
  surface with the session anchor identity. Recovery is **server-gated**:
  governance grants a quick resume only for safe states and refuses while the
  underlying risk is still high (or coherence sits below the quick-resume gate),
  so this never forces a resume or neuters the circuit breaker — it asks, and
  governance decides. A refusal (e.g. `NOT_SAFE_FOR_QUICK_RESUME`) comes back as
  `{:error, {:tool_error, _}}` and the caller stays surfaced for the operator.
  """
  @spec recover(keyword()) :: {:ok, map()} | {:error, term()}
  def recover(opts \\ []) do
    anchor = Keyword.get(opts, :anchor, %{})

    arguments =
      %{"action" => Keyword.get(opts, :recovery_action, "quick")}
      |> put_optional(
        "reason",
        Keyword.get(opts, :reason, "sentinel automated bounded recovery after governance pause")
      )
      |> put_optional("agent_id", Keyword.get(opts, :agent_id) || Map.get(anchor, "agent_uuid"))
      |> put_optional(
        "client_session_id",
        Keyword.get(opts, :client_session_id) || Map.get(anchor, "client_session_id")
      )
      |> put_optional(
        "continuity_token",
        Keyword.get(opts, :continuity_token) || Map.get(anchor, "continuity_token")
      )

    with :ok <- maybe_attest_substrate(opts) do
      %{"name" => "self_recovery", "arguments" => arguments}
      |> post_json(opts)
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

  defp governance_tools_url do
    Application.get_env(:unitares_sentinel, :governance_tools_url) ||
      System.get_env("UNITARES_GOVERNANCE_TOOLS_URL") ||
      @default_url
  end

  defp governance_uds_socket do
    Application.get_env(:unitares_sentinel, :governance_uds_socket) ||
      System.get_env("UNITARES_UDS_SOCKET")
  end

  defp uds_socket_configured?(opts) do
    socket =
      case Keyword.fetch(opts, :uds_socket) do
        {:ok, value} -> value
        :error -> governance_uds_socket()
      end

    is_binary(socket) and socket != ""
  end

  defp unix_socket_opts(nil), do: []
  defp unix_socket_opts(""), do: []

  defp unix_socket_opts(path) when is_binary(path) do
    if Path.type(path) == :absolute do
      [unix_socket: path]
    else
      raise ArgumentError, "UNITARES_UDS_SOCKET must be an absolute path"
    end
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
