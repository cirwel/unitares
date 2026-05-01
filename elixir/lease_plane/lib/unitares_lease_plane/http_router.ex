defmodule UnitaresLeasePlane.HTTPRouter do
  @moduledoc """
  HTTP surface for the lease plane. Routes match RFC v0.5 §5 exactly so the
  Python client at `src/lease_plane/` is the conformance target.

  Bind is local-only (`127.0.0.1`); a single shared bearer token from
  `~/.config/cirwel/secrets.env` (`LEASE_PLANE_BEARER_TOKEN`) gates every
  route. Body shapes are validated by Pattern + JSON-decode errors map to
  `schema_invalid` per the typed-absence protocol — there is no leaky 400
  HTML page.

  Handoff (`/v1/lease/handoff/{offer,accept}`) is intentionally not wired
  here — that work lands in a separate PR (release-and-reacquire pattern,
  Oban offer-window timer).
  """

  use Plug.Router
  use Plug.ErrorHandler

  require Logger

  alias UnitaresLeasePlane

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

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/lease/status ----------
  get "/v1/lease/status" do
    case Map.get(conn.query_params, "surface_id") do
      nil ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: "surface_id required"})

      surface_id ->
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

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/lease/handoff/{offer,accept} ----------
  # Wired in the next PR. Returns service_unavailable so callers see typed-absence,
  # not a 404 HTML page.
  post "/v1/lease/handoff/offer" do
    json(conn, 501, %{
      ok: false,
      error: "service_unavailable",
      reason: "handoff not implemented in this build"
    })
  end

  post "/v1/lease/handoff/accept" do
    json(conn, 501, %{
      ok: false,
      error: "service_unavailable",
      reason: "handoff not implemented in this build"
    })
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

    cond do
      missing != [] ->
        {:error, "missing required fields: #{Enum.join(missing, ", ")}"}

      Map.get(body, "holder_class") not in [nil, "process_instance", "substrate_earned"] ->
        # 'role' and other classes are rejected before the DB CHECK — surface this
        # as schema_invalid so the typed-absence error class is precise.
        {:error, "holder_class must be process_instance or substrate_earned (RFC §7.1)"}

      not is_integer(Map.get(body, "ttl_s")) ->
        {:error, "ttl_s must be an integer"}

      Map.get(body, "ttl_s") <= 0 or Map.get(body, "ttl_s") > 3600 ->
        {:error, "ttl_s must be in (0, 3600]"}

      true ->
        {:ok,
         %{
           surface_id: body["surface_id"],
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

    if reason in [
         "normal",
         "down_local",
         "reaped_after_supervisor_failed",
         "reaped_local_ttl",
         "reaped_remote_ttl",
         "handoff",
         "forced"
       ] do
      {:ok, lease_id, reason}
    else
      {:error, "invalid release_reason: #{inspect(reason)}"}
    end
  end

  defp extract_release_params(_), do: {:error, "lease_id required"}

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
