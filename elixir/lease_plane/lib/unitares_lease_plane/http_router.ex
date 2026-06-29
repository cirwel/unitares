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
    Logger.error("lease plane HTTP error: kind=#{kind} reason=#{safe_reason(reason)}")

    json(conn, 503, %{
      ok: false,
      error: "service_unavailable",
      reason: "internal error"
    })
  end

  # ---------- /v1/lease/acquire ----------
  post "/v1/lease/acquire" do
    case extract_acquire_params(conn.body_params) do
      {:ok, params} ->
        case acquire_for_surface(params) do
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

          {:error, %Postgrex.Error{postgres: %{code: :check_violation, constraint_name: name}}} ->
            # RFC §7.13.5 typed-error contract. Map any CHECK violation to
            # HTTP 422 schema_invalid with the constraint name as detail (one
            # of the four §7.13 substrate_state CHECKs). MUST precede the
            # generic {:error, reason} arm — falling through to 503 would
            # mask a writer bug as a transient outage.
            json(conn, 422, %{ok: false, error: "schema_invalid", detail: name})

          {:error, reason} ->
            Logger.error("lease plane acquire failed: #{safe_reason(reason)}")
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

  # ---------- /v1/effects (governed-effect record_only shadow) ----------
  # Phase 3 thin slice of docs/proposals/governed-effect-plane-v0.md. An agent
  # PROPOSES an effect; in record_only mode the plane observes (never acquires)
  # the declared required_leases and returns a durable effect_id + the
  # would-acquire observation. Nothing is enforced; execute mode is gated and
  # returns 501. See UnitaresLeasePlane.GovernedEffect.
  post "/v1/effects" do
    case UnitaresLeasePlane.GovernedEffect.handle(conn.body_params) do
      {:ok, body} ->
        json(conn, 202, body)

      {:error, :execute_not_implemented} ->
        json(conn, 501, %{
          ok: false,
          error: "not_implemented",
          reason: "execute mode not yet enabled; record_only only"
        })

      {:error, :idempotency_conflict} ->
        json(conn, 409, %{
          ok: false,
          error: "idempotency_conflict",
          reason: "idempotency_key already used for a materially different effect"
        })

      {:error, :persist_failed} ->
        json(conn, 503, %{
          ok: false,
          error: "persist_failed",
          reason: "could not durably record the proposal; nothing was recorded"
        })

      {:error, :spawn_failed} ->
        json(conn, 502, %{
          ok: false,
          error: "spawn_failed",
          reason: "execute custody could not complete the spawn via the orchestrator"
        })

      {:error, :governance_blocked} ->
        json(conn, 403, %{
          ok: false,
          error: "governance_blocked",
          reason: "governance vetoed the effect or could not affirmatively clear it"
        })

      {:error, :proposer_invalid} ->
        json(conn, 422, %{
          ok: false,
          error: "proposer_invalid",
          reason: "proposer.agent_uuid must be a valid UUID"
        })

      {:error, :lease_ttl_too_short} ->
        json(conn, 422, %{
          ok: false,
          error: "lease_ttl_too_short",
          reason: "every required lease must request ttl_s above the execute floor"
        })

      {:error, :lease_held} ->
        json(conn, 409, %{
          ok: false,
          error: "lease_held",
          reason: "a required surface is currently leased by another holder"
        })

      {:error, reason} when reason in [:lease_acquire_failed, :idempotency_lookup_failed] ->
        json(conn, 503, %{
          ok: false,
          error: Atom.to_string(reason),
          reason: "transient lease-plane error; nothing was committed"
        })

      # Executor validation rejections — client errors (bad payload/path), not 500s.
      {:error, reason}
      when reason in [
             :path_required,
             :surface_path_mismatch,
             :content_required,
             :bad_base64,
             :payload_too_large
           ] ->
        json(conn, 422, %{
          ok: false,
          error: Atom.to_string(reason),
          reason: "effect rejected at validation; nothing was written"
        })

      # The write could not be applied but was cleanly rolled back — the surface
      # is unchanged. Distinct from the quarantine case below.
      {:error, {:committed_failed_rolled_back, write_reason}} ->
        json(conn, 422, %{
          ok: false,
          error: "effect_write_failed",
          reason: "the write could not be applied and was rolled back: #{inspect(write_reason)}"
        })

      # Pre-image persist failed before any write attempt — durable record missing.
      {:error, {:persist_failed, persist_reason}} ->
        json(conn, 503, %{
          ok: false,
          error: "persist_failed",
          reason: "could not durably record the effect; nothing was written: #{inspect(persist_reason)}"
        })

      # Write failed AND the rollback also failed — the surface is quarantined and
      # needs operator review. A genuine 500, but reported, not a bare catch-all.
      {:error, :rollback_failed} ->
        json(conn, 500, %{
          ok: false,
          error: "rollback_failed",
          reason: "write failed and the rollback also failed; the surface is quarantined for operator review"
        })

      {:error, detail} when is_binary(detail) ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})

      # Catch-all: an unmapped error atom must never crash the handler into a
      # bare 500 — report it cleanly. Nothing is committed on any error path.
      {:error, other} ->
        json(conn, 500, %{ok: false, error: "internal", reason: inspect(other)})
    end
  end

  # ---------- /v1/dialectic/session ----------
  # BEAM-owned dialectic session creation (Slice 2): guarded INSERT + start a
  # liveness watcher at birth. Python computes the session_id + fields; BEAM owns
  # the write. Gated Python-side by UNITARES_DIALECTIC_BEAM_RESOLUTION.
  post "/v1/dialectic/session" do
    case extract_create_params(conn.body_params) do
      {:ok, params} ->
        case UnitaresLeasePlane.DialecticSaga.create_session(params) do
          {:ok, :created} ->
            json(conn, 201, %{ok: true, session_id: params.session_id, created: true})

          {:ok, :exists} ->
            json(conn, 200, %{ok: true, session_id: params.session_id, created: false})

          {:error, _} ->
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/dialectic/phase ----------
  # BEAM-owned non-terminal phase advance (thesis/antithesis/synthesis). Makes
  # BEAM sole writer of the session row across the whole lifecycle. Gated
  # Python-side by UNITARES_DIALECTIC_BEAM_RESOLUTION.
  post "/v1/dialectic/phase" do
    with %{"session_id" => sid, "phase" => phase} <- conn.body_params,
         true <- is_binary(sid) and byte_size(sid) > 0 and is_binary(phase) do
      case UnitaresLeasePlane.DialecticSaga.update_phase(sid, phase) do
        :ok ->
          json(conn, 200, %{ok: true, session_id: sid, phase: phase})

        {:error, :invalid_phase} ->
          json(conn, 422, %{
            ok: false,
            error: "schema_invalid",
            detail: "invalid non-terminal phase"
          })

        {:error, :session_not_found} ->
          json(conn, 404, %{ok: false, error: "session_not_found"})

        {:error, _} ->
          json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
      end
    else
      _ ->
        json(conn, 422, %{
          ok: false,
          error: "schema_invalid",
          detail: "session_id and phase required"
        })
    end
  end

  # ---------- /v1/dialectic/reviewer ----------
  # BEAM-owned reviewer assignment/reassignment — the last session-row column.
  post "/v1/dialectic/reviewer" do
    with %{"session_id" => sid, "reviewer_agent_id" => rev} <- conn.body_params,
         true <- is_binary(sid) and byte_size(sid) > 0 and is_binary(rev) and byte_size(rev) > 0 do
      case UnitaresLeasePlane.DialecticSaga.update_reviewer(sid, rev) do
        :ok ->
          json(conn, 200, %{ok: true, session_id: sid, reviewer_agent_id: rev})

        {:error, :session_not_found} ->
          json(conn, 404, %{ok: false, error: "session_not_found"})

        {:error, _} ->
          json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
      end
    else
      _ ->
        json(conn, 422, %{
          ok: false,
          error: "schema_invalid",
          detail: "session_id and reviewer_agent_id required"
        })
    end
  end

  # ---------- /v1/dialectic/resolve ----------
  # BEAM-owned dialectic SYNTHESIS->RESOLVED commit (dialectic-on-BEAM Slice 1).
  # Python computes the resolution payload (convergence + agent-state mutation
  # stay Python) and POSTs the finished payload here; BEAM is the serialization
  # owner (saga slot) and the sole writer of the terminal session row. Gated on
  # the Python side by UNITARES_DIALECTIC_BEAM_RESOLUTION (default off), so this
  # endpoint is dormant until an operator flips the flag.
  post "/v1/dialectic/resolve" do
    case extract_resolve_params(conn.body_params) do
      {:ok, params} ->
        case UnitaresLeasePlane.DialecticSaga.resolve(params) do
          {:ok, result} ->
            json(conn, 200, Map.put(result, :ok, true))

          {:error, :saga_in_flight} ->
            json(conn, 409, %{
              ok: false,
              error: "saga_in_flight",
              reason: "a resolution is already in progress for this session"
            })

          {:error, :session_not_found} ->
            json(conn, 404, %{ok: false, error: "session_not_found"})

          {:error, _other} ->
            json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
        end

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  # ---------- /v1/dialectic/presence ----------
  # BEAM-served liveness read: which dialectic sessions are alive right now, each
  # with phase, age, and whether a resolution saga is in flight. A coordination
  # signal sourced from BEAM rather than each consumer polling the DB directly.
  get "/v1/dialectic/presence" do
    limit =
      case Integer.parse(Map.get(conn.query_params, "limit", "100")) do
        {n, _} when n > 0 and n <= 500 -> n
        _ -> 100
      end

    case UnitaresLeasePlane.DialecticSaga.live_sessions(limit) do
      {:ok, sessions} ->
        json(conn, 200, %{ok: true, count: length(sessions), sessions: sessions})

      {:error, _} ->
        json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
    end
  end

  # ---------- /v1/health ----------
  # Wave 2 §"Lease-integration boundary hardening" — Phase C (supervised
  # health). Liveness signal for the boundary itself: if this responds 200,
  # the router is up, the auth path resolves, and the JSON envelope round-
  # trips. Bearer auth applies (the service is localhost-only, but health
  # info is sensitive and the auth plug runs globally — keeping it
  # consistent simplifies the surface). Used by Python's
  # LeasePlaneClient.health_check() and by future supervisor-side probes
  # (governance-mcp deep-health hook is a Phase C.5 follow-on).
  #
  # Intentionally minimal payload: `{ok: true, status: "ok"}`. The
  # `protocol_version` field is injected by `json/3`. Future phases extend
  # the payload (e.g., `db_ready`, `pool_size`, `inflight_lease_count`)
  # additively per Stability discipline; clients tolerate unknown fields.
  get "/v1/health" do
    json(conn, 200, %{ok: true, status: "ok"})
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
                Logger.error("lease plane status failed: #{safe_reason(reason)}")

                json(conn, 503, %{
                  ok: false,
                  error: "service_unavailable",
                  reason: "internal error"
                })
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
            Logger.error("lease plane release failed: #{safe_reason(reason_atom)}")
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
            Logger.error("lease plane force-release failed: #{safe_reason(reason_atom)}")
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
            Logger.error("lease plane handoff offer failed: #{safe_reason(reason)}")
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
            Logger.error("lease plane handoff accept failed: #{safe_reason(reason)}")
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
        # RFC §7.13: optional substrate_state + substrate_state_observed_at.
        # Pair-coherence is enforced server-side by the migration-034 CHECK;
        # client-side rejection happens in Pydantic for Python callers.
        substrate_state = Map.get(conn.body_params, "substrate_state")

        substrate_observed_at =
          case Map.get(conn.body_params, "substrate_state_observed_at") do
            nil ->
              nil

            iso when is_binary(iso) ->
              case DateTime.from_iso8601(iso) do
                {:ok, dt, _} -> dt
                _ -> :invalid_iso
              end

            _ ->
              :invalid_iso
          end

        cond do
          substrate_observed_at == :invalid_iso ->
            json(conn, 422, %{
              ok: false,
              error: "schema_invalid",
              detail: "substrate_state_observed_at must be ISO-8601 timestamp"
            })

          substrate_state != nil and not is_map(substrate_state) ->
            json(conn, 422, %{
              ok: false,
              error: "schema_invalid",
              detail: "substrate_state must be a JSON object"
            })

          true ->
            case UnitaresLeasePlane.renew(lease_id, substrate_state, substrate_observed_at) do
              :ok ->
                json(conn, 200, %{ok: true})

              {:error, :not_found} ->
                json(conn, 404, %{ok: false, error: "not_found"})

              {:error,
               %Postgrex.Error{postgres: %{code: :check_violation, constraint_name: name}}} ->
                # RFC §7.13.5 typed-error contract for renew CHECK violations.
                # MUST precede the generic 503 arm.
                json(conn, 422, %{ok: false, error: "schema_invalid", detail: name})

              {:error, reason} ->
                Logger.error("lease plane renew failed: #{safe_reason(reason)}")

                json(conn, 503, %{
                  ok: false,
                  error: "service_unavailable",
                  reason: "internal error"
                })
            end
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

        # RFC §7.13: optional substrate_state + substrate_state_observed_at.
        # Both nullable; type-checked here. Pair-coherence + resident-kind-only
        # + sensor.status enforcement happens in the DB CHECKs (typed-error
        # path returns 422 with constraint_name).
        substrate_state = Map.get(body, "substrate_state")

        substrate_observed_at =
          case Map.get(body, "substrate_state_observed_at") do
            nil ->
              {:ok, nil}

            iso when is_binary(iso) ->
              case DateTime.from_iso8601(iso) do
                {:ok, dt, _} -> {:ok, dt}
                _ -> {:error, "substrate_state_observed_at must be ISO-8601 timestamp"}
              end

            _ ->
              {:error, "substrate_state_observed_at must be ISO-8601 timestamp"}
          end

        cond do
          substrate_state != nil and not is_map(substrate_state) ->
            {:error, "substrate_state must be a JSON object"}

          match?({:error, _}, substrate_observed_at) ->
            {:error, detail} = substrate_observed_at
            {:error, detail}

          true ->
            {:ok, observed_at} = substrate_observed_at

            {:ok,
             %{
               surface_id: canonical_surface_id,
               holder_agent_uuid: body["holder_agent_uuid"],
               holder_class: Map.get(body, "holder_class", "process_instance"),
               holder_kind: body["holder_kind"],
               ttl_s: body["ttl_s"],
               intent: Map.get(body, "intent"),
               audit_session: Map.get(body, "audit_session"),
               holder_pid: Map.get(body, "holder_pid"),
               substrate_state: substrate_state,
               substrate_state_observed_at: observed_at
             }}
        end
    end
  end

  defp extract_acquire_params(_), do: {:error, "body must be a JSON object"}

  # Route the acquire to the correct lease lifecycle based on the surface scheme.
  #
  # `file://` surfaces are file-edit leases from the plugin's per-edit hook —
  # one-shot / session-scoped and MUST self-heal if the editing session dies
  # without releasing. They take the `remote_heartbeat` path: a pure DB row with
  # NO auto-renewing LeaseHolder, reaped by the Reaper at `expires_at` (the
  # editor's post-edit heartbeat extends it while the session is alive). Before
  # this, every file edit spawned an immortally-auto-renewing local_beam holder
  # that locked the file for the BEAM process's lifetime — memory files, with
  # their stable cross-session path, stayed locked for hours/days.
  #
  # `agent:/` surfaces are ephemeral-agent PRESENCE rows from the BEAM agent
  # orchestrator (elixir/agent_orchestrator). `maintenance:/` surfaces are
  # short-lived cleanup/repair jobs. Both are session-scoped like file edits and
  # MUST self-heal if the caller dies without releasing. They take the same
  # remote_heartbeat (pure-TTL-row) path. See migrations 042 and 049.
  #
  # Every other surface (resident:/ presence, migration:/, etc.) keeps the
  # local_beam auto-renew path. Residents are intentionally long-lived and rely
  # on server-side auto-renew for continuity, so the routing is scoped to the
  # file + agent + maintenance schemes precisely so it CANNOT regress resident
  # coordination.
  defp acquire_for_surface(%{surface_id: surface_id} = params)
       when is_binary(surface_id) do
    if String.starts_with?(surface_id, "file://") or
         String.starts_with?(surface_id, "maintenance:/") or
         String.starts_with?(surface_id, "agent:/") do
      UnitaresLeasePlane.acquire_remote_heartbeat(params)
    else
      UnitaresLeasePlane.acquire_local_beam(params)
    end
  end

  defp acquire_for_surface(params), do: UnitaresLeasePlane.acquire_local_beam(params)

  defp extract_create_params(%{"session_id" => sid, "paused_agent_id" => paused} = body)
       when is_binary(sid) and byte_size(sid) > 0 and is_binary(paused) and byte_size(paused) > 0 do
    optional =
      ~w(reviewer_agent_id session_type topic reason discovery_id dispute_type
                  max_synthesis_rounds synthesis_round paused_agent_state trigger_source phase status)

    params =
      Enum.reduce(optional, %{session_id: sid, paused_agent_id: paused}, fn key, acc ->
        case Map.get(body, key) do
          nil -> acc
          val -> Map.put(acc, String.to_atom(key), val)
        end
      end)

    {:ok, params}
  end

  defp extract_create_params(_),
    do: {:error, "session_id and paused_agent_id (non-empty strings) required"}

  defp extract_resolve_params(
         %{
           "session_id" => session_id,
           "paused_agent_id" => paused,
           "reviewer_agent_id" => reviewer,
           "resolution" => resolution
         } = body
       )
       when is_binary(session_id) and byte_size(session_id) > 0 and
              is_binary(paused) and byte_size(paused) > 0 and
              is_binary(reviewer) and byte_size(reviewer) > 0 and is_map(resolution) do
    # status is optional, defaults to "resolved"; only the two terminal states
    # are valid (BEAM owns both the resolved and failed terminal writes).
    case Map.get(body, "status", "resolved") do
      status when status in ["resolved", "failed"] ->
        {:ok,
         %{
           session_id: session_id,
           paused_agent_id: paused,
           reviewer_agent_id: reviewer,
           resolution_payload: resolution,
           status: status
         }}

      _ ->
        {:error, "status must be 'resolved' or 'failed'"}
    end
  end

  defp extract_resolve_params(_),
    do:
      {:error,
       "session_id, paused_agent_id, reviewer_agent_id (non-empty strings) and resolution (object) required"}

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
        {:error, "invalid release_reason"}
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
      earned_status: lease.earned_status,
      # RFC §7.13: include substrate columns so callers reading via /v1/lease/status
      # see what they wrote (PR 1 §7.13.6 touch-list "Elixir router — response shape").
      substrate_state: Map.get(lease, :substrate_state),
      substrate_state_observed_at: iso(Map.get(lease, :substrate_state_observed_at))
    }
  end

  defp iso(nil), do: nil
  defp iso(%DateTime{} = dt), do: DateTime.to_iso8601(dt)

  # Wave 2 §"Lease-integration boundary hardening" — versioned contracts.
  # Every response from this router carries a `protocol_version` field so
  # clients can detect server/client shape skew without having to enumerate
  # every endpoint. Bump when response shapes change in a way that requires
  # a coordinated client/server deploy. The Python client
  # (src/lease_plane/client.py) keeps its own constant and logs a WARNING
  # on mismatch (does NOT fail) during the rollout grace window — see
  # `tests/test_lease_plane_protocol_version.py`. URL versioning (/v1/lease/*)
  # remains the major-version axis; `protocol_version` is the finer-grained
  # shape-version axis within /v1.
  @protocol_version "v1.0"

  @doc """
  Boundary protocol version. Public so tests on the Elixir side can pin
  the constant; Python-side callers don't reach this directly — they
  receive it in every response body as the `protocol_version` field.
  """
  @spec protocol_version() :: String.t()
  def protocol_version, do: @protocol_version

  defp json(conn, status, body) do
    versioned_body = Map.put(body, :protocol_version, @protocol_version)

    conn
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(status, Jason.encode!(versioned_body))
  end

  defp safe_reason(%module{}), do: inspect(module)
  defp safe_reason(reason) when is_atom(reason), do: Atom.to_string(reason)
  defp safe_reason(reason) when is_binary(reason), do: redact_sensitive(reason)

  defp safe_reason(reason) do
    reason
    |> inspect(limit: 5, printable_limit: 120)
    |> redact_sensitive()
  end

  defp redact_sensitive(text) do
    Regex.replace(
      ~r/(password|token|secret|authorization|bearer)(\s*[=:]\s*)[^\s,}\]]+/i,
      text,
      "\\1\\2[REDACTED]"
    )
  end
end
