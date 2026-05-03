defmodule UnitaresLeasePlane.HTTPRouter do
  @moduledoc """
  HTTP surface for the lease plane. Routes match RFC v0.5 §5 exactly so the
  Python client at `src/lease_plane/` is the conformance target.

  Bind is local-only (`127.0.0.1`); a single shared bearer token from
  `~/.config/cirwel/secrets.env` (`LEASE_PLANE_BEARER_TOKEN`) gates every
  route. Body shapes are validated by Pattern + JSON-decode errors map to
  `schema_invalid` per the typed-absence protocol — there is no leaky 400
  HTML page.

  Handoff (`/v1/lease/handoff/{offer,accept}`) uses the release-and-reacquire
  pattern: accepting closes the old lease with `release_reason='handoff'` and
  creates a new remote-heartbeat lease for the recipient.
  """

  use Plug.Router
  use Plug.ErrorHandler

  require Logger

  alias UnitaresLeasePlane
  alias UnitaresLeasePlane.Canonicalize

  plug(:match)

  # HTTPAuth runs BEFORE body parsing so an unauthenticated caller cannot
  # probe endpoint existence by sending malformed JSON (which would otherwise
  # raise inside Plug.Parsers and surface a generic 400 *before* auth ran).
  # Bearer auth doesn't need the parsed body; it only reads the Authorization
  # header.
  plug(UnitaresLeasePlane.HTTPAuth)

  # SafeParsers wraps Plug.Parsers so JSON / media-type errors become typed
  # 422 / 415 schema_invalid responses instead of Bandit's default empty 400.
  plug(UnitaresLeasePlane.SafeParsers,
    parsers: [:json],
    pass: ["application/json"],
    json_decoder: Jason
  )

  plug(:dispatch)

  # Plug.ErrorHandler catches anything raised inside route handlers
  # (Postgrex connection failures, unexpected nil dereferences, etc.) and
  # converts them into a typed-absence 503 with a redacted reason. The full
  # error is logged server-side; the wire body never carries inspect output.
  @impl Plug.ErrorHandler
  def handle_errors(conn, %{kind: kind, reason: reason, stack: _stack}) do
    Logger.error(
      "lease plane HTTP error: kind=#{kind} reason=#{Exception.format_banner(kind, reason)}"
    )

    body =
      Jason.encode!(%{
        ok: false,
        error: "service_unavailable",
        reason: "internal error"
      })

    conn
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(503, body)
  end

  # ---------- /v1/lease/acquire ----------
  post "/v1/lease/acquire" do
    case extract_acquire_params(conn.body_params) do
      {:ok, params} ->
        case UnitaresLeasePlane.acquire_local_beam(params) do
          {:ok, lease, kind} ->
            json(conn, 200, %{
              ok: true,
              lease: present_lease(lease),
              idempotent: kind == :idempotent,
              drift_warning: []
            })

          {:error, :held_by_other, info} ->
            # PR 5 council BLOCK fix: emit all 5 fields the v0.7 §7.3.2
            # AcquireHeldByOther typed-absence shape requires (was 2 pre-PR-5;
            # missing fields caused production 409s to fail Pydantic validation
            # → degraded to AcquireSchemaInvalid → acquire_with_retry never retried).
            now = DateTime.utc_now()
            remaining_ms =
              max(0, DateTime.diff(info.expires_at, now, :millisecond))
            retry_after_hint_ms = min(remaining_ms, 5_000)
            json(conn, 409, %{
              ok: false,
              error: "held_by_other",
              surface_id: Map.get(info, :surface_id),
              blocking_lease_id: Map.get(info, :blocking_lease_id),
              held_by_uuid: info.held_by_uuid,
              expires_at: DateTime.to_iso8601(info.expires_at),
              retry_after_hint_ms: retry_after_hint_ms
            })

          {:error, reason} ->
            Logger.error("lease plane acquire failed: #{inspect(reason)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:permission_denied, reason} ->
        # RFC §4.4 / §7.3.5 — application-layer typed-absence for policy
        # rejection (e.g. holder_class="role"). Distinct from the auth-layer
        # 401 (http_auth.ex) by carrying a machine-readable reason atom and
        # using the 200 + ok:false envelope per §7.3.5 ("HTTP 409 on
        # held_by_other; 200 + ok:false otherwise"). The reason field is the
        # discriminator (e.g. "role_holders_unsupported").
        json(conn, 200, %{ok: false, error: "permission_denied", reason: reason})

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/lease/status ----------
  get "/v1/lease/status" do
    case Map.get(conn.query_params, "surface_id") do
      nil ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: "surface_id required"})

      raw_surface_id ->
        # PR 7 — server-side canonicalization (RFC v0.8 §7.12.1). Mirrors the
        # Python field_validator so non-Python callers cannot bypass and
        # produce split-brain rows.
        case Canonicalize.canonicalize(raw_surface_id) do
          {:error, reason} ->
            json(conn, 422, %{
              ok: false,
              error: "schema_invalid",
              detail: "surface_id canonicalization failed: #{reason}"
            })

          {:ok, surface_id} ->
            case UnitaresLeasePlane.status(surface_id) do
              {:ok, nil} ->
                json(conn, 200, %{ok: true, lease: nil})

              {:ok, lease} ->
                json(conn, 200, %{ok: true, lease: present_lease(lease)})

              {:error, reason} ->
                Logger.error("lease plane status failed: #{inspect(reason)}")
                json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
            end
        end
    end
  end

  # ---------- /v1/lease/renew ----------
  post("/v1/lease/renew", do: renew_or_heartbeat(conn))

  # ---------- /v1/lease/heartbeat ----------
  post("/v1/lease/heartbeat", do: renew_or_heartbeat(conn))

  # ---------- /v1/lease/release ----------
  post "/v1/lease/release" do
    case extract_release_params(conn.body_params) do
      {:ok, lease_id, reason} ->
        case UnitaresLeasePlane.release(lease_id, reason) do
          :ok ->
            json(conn, 200, %{ok: true})

          {:error, :not_found} ->
            json(conn, 404, %{ok: false, error: "not_found"})

          {:error, reason_atom} ->
            Logger.error("lease plane release failed: #{inspect(reason_atom)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:permission_denied, reason} ->
        # RFC §7.10 — release_reason='forced' must arrive at /v1/lease/force-release
        # so HTTPAuth gates it with the elevated token. Same envelope shape as
        # other application-layer policy rejections (cf. holder_class='role').
        json(conn, 200, %{ok: false, error: "permission_denied", reason: reason})

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/lease/force-release (RFC §7.10) ----------
  # Operator-only. Gated at the contract layer by HTTPAuth's per-path token
  # check (`:force_release_token`, sourced from `LEASE_FORCE_RELEASE_TOKEN`).
  # The regular bearer cannot reach this route — see http_auth.ex.
  post "/v1/lease/force-release" do
    case extract_force_release_params(conn.body_params) do
      {:ok, lease_id} ->
        case UnitaresLeasePlane.force_release(lease_id) do
          :ok ->
            json(conn, 200, %{ok: true})

          {:error, :not_found} ->
            json(conn, 404, %{ok: false, error: "not_found"})

          {:error, reason_atom} ->
            Logger.error("lease plane force-release failed: #{inspect(reason_atom)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  post "/v1/lease/handoff/offer" do
    case extract_handoff_offer_params(conn.body_params) do
      {:ok, lease_id, to_holder_agent_uuid, ttl_s} ->
        case UnitaresLeasePlane.handoff_offer(lease_id, to_holder_agent_uuid, ttl_s) do
          {:ok, handoff_id} ->
            json(conn, 200, %{ok: true, handoff_id: handoff_id})

          {:error, :not_found} ->
            json(conn, 404, %{ok: false, error: "not_found"})

          {:error, reason} ->
            Logger.error("lease plane handoff offer failed: #{inspect(reason)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  post "/v1/lease/handoff/accept" do
    case extract_handoff_accept_params(conn.body_params) do
      {:ok, handoff_id} ->
        case UnitaresLeasePlane.handoff_accept(handoff_id) do
          :ok ->
            json(conn, 200, %{ok: true})

          {:error, :not_found} ->
            json(conn, 404, %{ok: false, error: "not_found"})

          {:error, :expired} ->
            json(conn, 409, %{ok: false, error: "expired"})

          {:error, reason} ->
            Logger.error("lease plane handoff accept failed: #{inspect(reason)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- catch-all ----------
  match _ do
    json(conn, 404, %{ok: false, error: "not_found"})
  end

  # ---------- helpers ----------

  defp renew_or_heartbeat(conn) do
    case Map.get(conn.body_params, "lease_id") do
      lease_id when is_binary(lease_id) and byte_size(lease_id) > 0 ->
        case UnitaresLeasePlane.renew(lease_id) do
          :ok ->
            json(conn, 200, %{ok: true})

          {:error, :not_found} ->
            json(conn, 404, %{ok: false, error: "not_found"})

          {:error, reason} ->
            Logger.error("lease plane renew failed: #{inspect(reason)}")
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      _ ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: "lease_id required"})
    end
  end

  defp extract_acquire_params(%{} = body) do
    # surface_kind dropped from required + params map per RFC v0.8 §7.2.3:
    # post-migration-026, surface_kind is a generated column derived from
    # split_part(surface_id, ':', 1). Including it in the Repo INSERT params
    # would raise `ERROR: column "surface_kind" is a generated column`.
    # Caller-supplied surface_kind in the body is silently ignored.
    required = ["surface_id", "holder_agent_uuid", "holder_kind", "ttl_s"]
    missing = Enum.filter(required, fn k -> is_nil(Map.get(body, k)) end)
    raw_surface_id = Map.get(body, "surface_id")

    # PR 7 — server-side canonicalization (RFC v0.8 §7.12.1). Mirror of the
    # Python field_validator on AcquireRequest.surface_id; prevents split-brain
    # from non-Python callers (curl, future Hermes/Codex/Elixir clients).
    canonical_or_error =
      if is_binary(raw_surface_id), do: Canonicalize.canonicalize(raw_surface_id), else: nil

    cond do
      missing != [] ->
        {:error, "missing required fields: #{Enum.join(missing, ", ")}"}

      not is_binary(raw_surface_id) ->
        {:error, "surface_id must be a string"}

      match?({:error, _}, canonical_or_error) ->
        {:error, reason} = canonical_or_error
        {:error, "surface_id canonicalization failed: #{reason}"}

      Map.get(body, "holder_class") == "role" ->
        # RFC §4.4 line 481: roles cannot hold leases — surface this as
        # application-layer permission_denied with a machine-readable reason,
        # NOT as schema_invalid. holder_class is structurally valid; the
        # rejection is a policy decision, not a malformed request. Closes
        # §9 gate `test http_router returns 200 on permission_denied`.
        {:permission_denied, "role_holders_unsupported"}

      Map.get(body, "holder_class") not in [nil, "process_instance", "substrate_earned"] ->
        # Unknown/unsupported holder_class values (anything that isn't
        # process_instance, substrate_earned, or the policy-rejected "role")
        # remain schema_invalid — they're genuinely malformed input.
        {:error, "holder_class must be process_instance or substrate_earned (RFC §7.1)"}

      not is_integer(Map.get(body, "ttl_s")) ->
        {:error, "ttl_s must be an integer"}

      Map.get(body, "ttl_s") <= 0 or Map.get(body, "ttl_s") > 3600 ->
        {:error, "ttl_s must be in (0, 3600]"}

      true ->
        {:ok, canonical_surface_id} = canonical_or_error

        {:ok,
         %{
           surface_id: canonical_surface_id,
           holder_agent_uuid: body["holder_agent_uuid"],
           holder_class: Map.get(body, "holder_class", "process_instance"),
           holder_kind: body["holder_kind"],
           ttl_s: body["ttl_s"],
           intent: Map.get(body, "intent"),
           audit_session: Map.get(body, "audit_session"),
           holder_pid: Map.get(body, "holder_pid")
         }}
    end
  end

  defp extract_acquire_params(_), do: {:error, "body must be a JSON object"}

  defp extract_release_params(%{"lease_id" => lease_id} = body)
       when is_binary(lease_id) and byte_size(lease_id) > 0 do
    reason = Map.get(body, "release_reason", "normal")

    cond do
      # RFC §7.10: force-release requires the elevated bearer at the contract
      # layer. The /v1/lease/release endpoint is gated by the regular bearer
      # (HTTPAuth), so accepting release_reason='forced' here would route
      # around the operator-only authority check. Reject as application-layer
      # permission_denied with a machine-readable reason that points the
      # caller at the correct endpoint.
      reason == "forced" ->
        {:permission_denied, "forced_release_requires_force_release_endpoint"}

      reason in [
        "normal",
        "down_local",
        "reaped_after_supervisor_failed",
        "reaped_local_ttl",
        "reaped_remote_ttl",
        "handoff"
      ] ->
        {:ok, lease_id, reason}

      true ->
        {:error, "invalid release_reason: #{inspect(reason)}"}
    end
  end

  defp extract_release_params(_), do: {:error, "lease_id required"}

  # /v1/lease/force-release takes only lease_id. release_reason is implicit
  # ('forced') so callers can't accidentally bypass the elevated-bearer gate
  # by, e.g., passing release_reason='normal'.
  defp extract_force_release_params(%{} = body) do
    case Map.get(body, "lease_id") do
      lease_id when is_binary(lease_id) and byte_size(lease_id) > 0 ->
        {:ok, lease_id}

      _ ->
        {:error, "lease_id required"}
    end
  end

  defp extract_force_release_params(_), do: {:error, "lease_id required"}

  defp extract_handoff_offer_params(%{} = body) do
    lease_id = Map.get(body, "lease_id")
    to_holder_agent_uuid = Map.get(body, "to_holder_agent_uuid")
    ttl_s = Map.get(body, "ttl_s")

    cond do
      not (is_binary(lease_id) and byte_size(lease_id) > 0) ->
        {:error, "lease_id required"}

      not (is_binary(to_holder_agent_uuid) and byte_size(to_holder_agent_uuid) > 0) ->
        {:error, "to_holder_agent_uuid required"}

      not is_integer(ttl_s) ->
        {:error, "ttl_s must be an integer"}

      ttl_s <= 0 or ttl_s > 3600 ->
        {:error, "ttl_s must be in (0, 3600]"}

      true ->
        {:ok, lease_id, to_holder_agent_uuid, ttl_s}
    end
  end

  defp extract_handoff_offer_params(_), do: {:error, "body must be a JSON object"}

  defp extract_handoff_accept_params(%{"handoff_id" => handoff_id})
       when is_binary(handoff_id) and byte_size(handoff_id) > 0 do
    {:ok, handoff_id}
  end

  defp extract_handoff_accept_params(_), do: {:error, "handoff_id required"}

  defp present_lease(%{} = lease) do
    %{
      lease_id: lease.lease_id,
      surface_id: lease.surface_id,
      surface_kind: lease.surface_kind,
      holder_agent_uuid: lease.holder_agent_uuid,
      holder_class: lease.holder_class,
      holder_kind: lease.holder_kind,
      holder_pid: lease.holder_pid,
      heartbeat_required: lease.heartbeat_required,
      intent: lease.intent,
      acquired_at: iso(lease.acquired_at),
      expires_at: iso(lease.expires_at),
      last_heartbeat_at: iso(lease.last_heartbeat_at),
      released_at: iso(lease.released_at),
      release_reason: lease.release_reason,
      audit_session: lease.audit_session,
      original_ttl_s: lease.original_ttl_s,
      earned_status: lease.earned_status
    }
  end

  defp iso(nil), do: nil
  defp iso(%DateTime{} = dt), do: DateTime.to_iso8601(dt)

  defp json(conn, status, body) do
    conn
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(status, Jason.encode!(body))
  end
end
