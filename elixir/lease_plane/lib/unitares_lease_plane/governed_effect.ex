defmodule UnitaresLeasePlane.GovernedEffect do
  @moduledoc """
  Record-only governed-effect shadow — Phase 3 thin slice of
  `docs/proposals/governed-effect-plane-v0.md`.

  An agent PROPOSES an effect (an intent to mutate a surface). In
  `record_only` mode the plane:

    * validates the envelope (required `idempotency_key`, explicit
      `custody_mode`, `effect_type`, `surface`);
    * OBSERVES — never acquires — the declared `required_leases`, recording
      what an `execute` would have hit (`would_acquire: "ok" | "would_block"`).
      Lease-blindness would corrupt the dry-run signal the shadow exists to
      produce, and *acquiring* would block real work; so it peeks, never holds;
    * assigns a durable `effect_id` and returns the observation inline.

  It claims NOTHING about the side effect — the proposer still executes. This
  is shadow custody / proposal logging, not a commit (contract §2 rhetoric
  discipline).

  `execute` mode is intentionally NOT implemented here — it is gated (RCE
  surface + the 2026-06-24 Wave-3 read). A proposal in `execute` mode returns
  `{:error, :execute_not_implemented}` so the endpoint is honestly record-only.

  ## Durable recording (contract §8)

  A `record_only` proposal is durably recorded to `audit.events` with a
  mandatory `effect_lane: "governed_effect"` tag — NOT `outcome_event` (that
  feeds the EISV predictive slice; Invariant 5 forbids effect telemetry there)
  and NOT a dedicated `governed_effect_events` table (that is promoted as part
  of the execute-promotion migration, Phase 4, when commit-bearing columns earn
  their own constraints). `audit.events` is a forensic sink outside the
  predictive slice, and `WHERE payload->>'effect_lane' IS NOT NULL` gives the
  same partition a dedicated table would, at zero migration cost.

  The stored payload carries the `idempotency_digest`, never the raw effect
  `payload` bytes (Invariant 7) and never the proposer's `client_session_id`
  (a credential). `proposer.agent_uuid` is recorded as attribution only — this
  slice does not re-verify the proposer's identity tier (§2 tier-stamping is a
  later increment), so the record makes no tier claim.

  ## Idempotency (contract §4)

  Before recording, the same `idempotency_key` is looked up. Same key + same
  digest replays the existing `effect_id` (idempotent). Same key + a different
  digest is an `idempotency_conflict`. Dedup is best-effort at the shadow stage
  — `audit.events` has no unique constraint on the key, so a true concurrent
  double-propose can still produce two rows; constraint-backed uniqueness
  arrives with the Phase 4 table. A persist failure surfaces honestly rather
  than returning a 202 that recorded nothing.
  """

  require Logger

  alias UnitaresLeasePlane.Canonicalize
  alias UnitaresLeasePlane.EffectRepo
  alias UnitaresLeasePlane.FileWriteExecutor
  alias UnitaresLeasePlane.GovernanceVetoClient
  alias UnitaresLeasePlane.OrchestratorClient
  alias UnitaresLeasePlane.Repo

  @custody_modes ~w(record_only execute)
  @effect_lane "governed_effect"
  @record_only_event_type "governed_effect.record_only"
  @execute_event_type "governed_effect.execute"

  # Invariant 7 (no secret leakage): payload key substrings that must never be
  # stored or logged. A credential-shaped payload is rejected, not scrubbed —
  # the proposer must not put secrets in an effect payload.
  @credential_key_substrings ~w(client_session_id continuity_token authorization bearer token api_key secret password)

  @doc """
  Handle a governed-effect proposal envelope. Returns:

    * `{:ok, body_map}` — a 202 body (record_only recorded, or execute committed);
    * `{:error, :execute_not_implemented}` — execute mode disabled / unsupported type;
    * `{:error, :idempotency_conflict}` — same key, different digest;
    * `{:error, :governance_blocked}` — governance vetoed (or could not clear) the effect;
    * `{:error, :persist_failed}` / `{:error, :spawn_failed}`;
    * `{:error, detail}` — `schema_invalid` detail string.
  """
  @spec handle(map()) ::
          {:ok, map()}
          | {:error, :execute_not_implemented}
          | {:error, :idempotency_conflict}
          | {:error, :governance_blocked}
          | {:error, :persist_failed}
          | {:error, :spawn_failed}
          | {:error, String.t()}
  def handle(%{} = body) do
    with {:ok, env} <- validate(body) do
      case env.custody_mode do
        "record_only" -> record_only(env)
        "execute" -> execute(env)
      end
    end
  end

  def handle(_), do: {:error, "body must be a JSON object"}

  @doc """
  Canonical idempotency digest: `sha256(effect_type ‖ surface ‖ custody_mode ‖
  payload_hash)`, hex. Excludes `provenance`/`proposer` so a retry from a new
  session is not treated as "materially different" (contract §4).
  """
  @spec idempotency_digest(map()) :: String.t()
  def idempotency_digest(%{} = env) do
    payload_hash =
      :crypto.hash(:sha256, Jason.encode!(Map.get(env, :payload, %{})))
      |> Base.encode16(case: :lower)

    [env.effect_type, env.surface, env.custody_mode, payload_hash]
    |> Enum.join(" ")
    |> then(&:crypto.hash(:sha256, &1))
    |> Base.encode16(case: :lower)
  end

  # ---- validation ----

  defp validate(body) do
    idem = Map.get(body, "idempotency_key")
    mode = Map.get(body, "custody_mode")
    type = Map.get(body, "effect_type")
    surface = Map.get(body, "surface")
    leases = Map.get(body, "required_leases", [])
    payload = Map.get(body, "payload", %{})

    # Attribution only — non-secret fields. The proposer's `client_session_id`
    # is a credential (Invariant 7) and is deliberately NOT extracted or stored.
    proposer_agent_uuid = nested_string(body, "proposer", "agent_uuid")
    provenance_session_id = nested_string(body, "provenance", "session_id")

    # §7 strong-tier re-cert proof — the proposer's continuity_token, carried in
    # the `proposer` object (NOT `payload`, which `credential_shaped?` would
    # reject). CREDENTIAL: forwarded transiently to the governance veto for
    # re-verification, then dropped. It is NEVER written to any audit_payload,
    # response body, or log line (Invariant 1/7); keep it out of every
    # `inspect(env)`. Used only by the execute path (`GovernanceVetoClient`).
    proposer_continuity_token = nested_string(body, "proposer", "continuity_token")

    # §8 effect-binding proof (#1075) — the proposer's single-use, content-bound
    # grant, carried in the `proposer` object alongside the token. CREDENTIAL:
    # forwarded transiently to the governance veto for §8 verification, then
    # dropped. NEVER written to any audit_payload, response body, or log line
    # (Invariant 1/7); keep it out of every `inspect(env)`. Optional — absent
    # today (no proposer mints grants until the binding flag flips).
    proposer_effect_grant = nested_string(body, "proposer", "effect_grant")

    cond do
      not (is_binary(idem) and byte_size(idem) > 0) ->
        {:error, "idempotency_key required (non-empty string)"}

      mode not in @custody_modes ->
        {:error, "custody_mode required, one of: record_only, execute"}

      not (is_binary(type) and byte_size(type) > 0) ->
        {:error, "effect_type required (non-empty string)"}

      not (is_binary(surface) and byte_size(surface) > 0) ->
        {:error, "surface required (non-empty string)"}

      not valid_leases?(leases) ->
        {:error, "required_leases must be a list of objects with a string surface"}

      not (is_nil(payload) or is_map(payload)) ->
        {:error, "payload must be a JSON object"}

      credential_shaped?(payload) ->
        {:error, "payload must not contain credential-shaped keys (Invariant 7)"}

      true ->
        {:ok,
         %{
           idempotency_key: idem,
           custody_mode: mode,
           effect_type: type,
           surface: surface,
           required_leases: leases,
           payload: payload || %{},
           proposer_agent_uuid: proposer_agent_uuid,
           provenance_session_id: provenance_session_id,
           # CREDENTIAL — transient, never persisted/logged (see comment above).
           proposer_continuity_token: proposer_continuity_token,
           # CREDENTIAL — transient §8 effect-binding proof (see comment above).
           proposer_effect_grant: proposer_effect_grant
         }}
    end
  end

  # Pull a nested string field (`body[outer][inner]`) when present and non-empty;
  # nil otherwise. Tolerates a missing or non-map outer object.
  defp nested_string(body, outer, inner) do
    case Map.get(body, outer) do
      %{} = m ->
        case Map.get(m, inner) do
          v when is_binary(v) and byte_size(v) > 0 -> v
          _ -> nil
        end

      _ ->
        nil
    end
  end

  defp valid_leases?(leases) when is_list(leases) do
    Enum.all?(leases, fn
      %{"surface" => s} when is_binary(s) and byte_size(s) > 0 -> true
      _ -> false
    end)
  end

  defp valid_leases?(_), do: false

  defp credential_shaped?(payload) when is_map(payload) do
    Enum.any?(Map.keys(payload), fn k ->
      ks = k |> to_string() |> String.downcase()
      Enum.any?(@credential_key_substrings, &String.contains?(ks, &1))
    end)
  end

  defp credential_shaped?(_), do: false

  # ---- record_only ----

  defp record_only(env) do
    digest = idempotency_digest(env)

    case Repo.governed_effect_by_idempotency_key(env.idempotency_key) do
      # Same key + same digest → idempotent replay of the existing record
      # (contract §4). Reconstruct the response from the durable row so a retry
      # gets the original effect_id and observations, not a fresh shadow.
      {:ok, %{idempotency_digest: ^digest, payload: stored}} ->
        {:ok, idempotent_body(stored)}

      # Same key + a different digest → the proposer reused a key for a
      # materially different effect. Refuse rather than silently fork.
      {:ok, %{idempotency_digest: other}} when is_binary(other) ->
        {:error, :idempotency_conflict}

      {:ok, nil} ->
        persist_new(env, digest)

      {:error, reason} ->
        Logger.warning(
          "governed_effect idempotency lookup failed key=#{env.idempotency_key}: " <>
            inspect(reason)
        )

        {:error, :persist_failed}
    end
  end

  defp persist_new(env, digest) do
    effect_id = gen_effect_id()
    observations = Enum.map(env.required_leases, &observe_lease/1)
    audit_payload = audit_payload(effect_id, env, digest, observations)

    case Repo.insert_governed_effect_event(%{
           event_type: @record_only_event_type,
           agent_id: env.proposer_agent_uuid,
           session_id: env.provenance_session_id,
           payload: audit_payload
         }) do
      :ok ->
        Logger.info(
          "governed_effect record_only effect_id=#{effect_id} surface=#{env.surface} " <>
            "type=#{env.effect_type} digest=#{binary_part(digest, 0, 12)} " <>
            "observations=#{inspect(observations)}"
        )

        {:ok, response_body(audit_payload, observations, false)}

      {:error, reason} ->
        Logger.warning(
          "governed_effect record_only persist failed effect_id=#{effect_id} " <>
            "surface=#{env.surface}: #{inspect(reason)}"
        )

        {:error, :persist_failed}
    end
  end

  # The durable payload stored in `audit.events.payload`. Carries the digest and
  # observations, never the raw effect payload bytes (Invariant 7) nor any
  # credential. `effect_lane` is the mandatory discriminator (contract §8).
  defp audit_payload(effect_id, env, digest, observations) do
    %{
      "effect_lane" => @effect_lane,
      "effect_id" => effect_id,
      "custody_mode" => "record_only",
      "status" => "recorded",
      "effect_type" => env.effect_type,
      "surface" => env.surface,
      "idempotency_key" => env.idempotency_key,
      "idempotency_digest" => digest,
      "required_leases" => env.required_leases,
      "observations" => observations,
      "proposer_agent_uuid" => env.proposer_agent_uuid
    }
  end

  # The 202 body. `observations` is passed separately so a fresh record returns
  # its live atom-keyed observation maps unchanged, while an idempotent replay
  # rebuilds them from the stored (string-keyed JSON) payload.
  defp response_body(audit_payload, observations, idempotent?) do
    %{
      ok: true,
      effect_id: audit_payload["effect_id"],
      custody_mode: "record_only",
      status: "recorded",
      effect_lane: @effect_lane,
      idempotency_digest: audit_payload["idempotency_digest"],
      custody_expires_at: nil,
      observations: observations,
      idempotent: idempotent?
    }
  end

  defp idempotent_body(stored) when is_map(stored) do
    response_body(stored, Map.get(stored, "observations", []), true)
  end

  # ---- execute (agent_spawn → live orchestrator) ----
  #
  # First execute slice. ONLY `agent_spawn` is wired, and only when the
  # per-type flag is on AND the orchestrator bearer is configured — otherwise
  # `execute` stays `execute_not_implemented` exactly as before. The spawn is
  # delegated to the already-live agent orchestrator (`:8789`), which owns the
  # OS-process spawn, OTP supervision, lease-binding and lineage.
  #
  # Gates before commit (all fail-closed): the per-type flag + the
  # orchestrator's own bearer + `check_allowed` cmd allowlist, the §6 governance
  # veto (verdict/action), AND the §7 strong-tier re-certification — the veto
  # endpoint re-verifies the proposer's forwarded continuity_token to the
  # `strong` tier; a proposer that does not re-certify strong is blocked the
  # same as a flagged one (`GovernanceVetoClient.check/1`). `agent_spawn` is
  # irreversible (§5b), so there is no rollback to prove; idempotency is the
  # load-bearing safety property here — a retry must never spawn twice.
  defp execute(%{effect_type: "agent_spawn"} = env) do
    if execute_agent_spawn_enabled?() do
      execute_agent_spawn(env)
    else
      {:error, :execute_not_implemented}
    end
  end

  # file_write — the first REVERSIBLE execute surface. Synchronous: acquire the
  # lease, re-check the §6 veto on the commit path, hand to FileWriteExecutor
  # (which captures the pre-image, then dry-runs or commits per
  # :execute_file_write_commit_enabled), release the lease. Crash recovery is
  # EffectRecovery (boot) + the executor's in-process compensation; a fast write
  # is covered by the min-TTL lease floor. (A supervised EffectCustodian with a
  # lease heartbeat + immediate :transient recovery is a robustness follow-up.)
  defp execute(%{effect_type: "file_write"} = env) do
    if execute_file_write_enabled?() do
      execute_file_write(env)
    else
      {:error, :execute_not_implemented}
    end
  end

  # Every other effect_type is still gated.
  defp execute(_env), do: {:error, :execute_not_implemented}

  defp execute_file_write_enabled? do
    Application.get_env(:lease_plane, :execute_file_write_enabled, false) == true
  end

  # Restart + compensation budget: a too-short lease cannot survive a crash and
  # recovery, so reject before any custody starts.
  @min_execute_ttl_s 120

  defp execute_file_write(env) do
    cond do
      # Validate the proposer BEFORE acquiring any lease — otherwise a nil or
      # malformed uuid crashes uuid_to_binary inside Repo.acquire and surfaces as
      # an opaque 500 instead of a clean client error.
      not valid_proposer?(env) ->
        {:error, :proposer_invalid}

      not min_ttl_ok?(env) ->
        {:error, :lease_ttl_too_short}

      true ->
        digest = idempotency_digest(env)

        case Repo.governed_effect_by_idempotency_key(env.idempotency_key, @execute_event_type) do
          {:ok, %{idempotency_digest: ^digest, payload: stored}} ->
            {:ok, execute_idempotent_body(stored)}

          {:ok, %{idempotency_digest: other}} when is_binary(other) ->
            {:error, :idempotency_conflict}

          {:ok, nil} ->
            file_write_under_custody(env, digest)

          {:error, reason} ->
            Logger.warning(
              "governed_effect file_write idempotency lookup failed: #{inspect(reason)}"
            )

            {:error, :idempotency_lookup_failed}
        end
    end
  end

  @proposer_uuid_re ~r/\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\z/
  defp valid_proposer?(%{proposer_agent_uuid: u}) when is_binary(u),
    do: Regex.match?(@proposer_uuid_re, u)

  defp valid_proposer?(_), do: false

  defp min_ttl_ok?(env) do
    Enum.all?(env.required_leases, fn l -> (lease_ttl(l) || 0) >= @min_execute_ttl_s end)
  end

  defp lease_ttl(%{"ttl_s" => t}), do: t
  defp lease_ttl(%{ttl_s: t}), do: t
  defp lease_ttl(_), do: nil

  defp file_write_under_custody(env, digest) do
    effect_id = gen_effect_id()
    # Canonicalize lease surfaces ONCE so the acquired surface_id and the
    # executor's canonical(path) match (the path-canonicalization seam).
    canon_leases = canonicalize_leases(env.required_leases)

    case acquire_all(canon_leases, env.proposer_agent_uuid) do
      {:ok, acquired} ->
        try do
          # §6 veto re-checked HERE, on the commit path, with the lease held.
          case GovernanceVetoClient.check(env) do
            :allow ->
              # Insert the durable effects.payloads row BEFORE the commit so the
              # executor's record_pre_image UPDATE (and crash recovery) have a row
              # to act on. record_pre_image is UPDATE-only; without this the file
              # would commit with no rollback/pre-image record.
              case ensure_payload_row(effect_id, env, digest) do
                :ok ->
                  result =
                    FileWriteExecutor.apply_effect(effect_id, env.payload, canon_leases)

                  {status, extra} = result_audit(result)

                  _ =
                    persist_execute(
                      env,
                      execute_audit_payload(effect_id, env, digest, status, extra)
                    )

                  result_to_reply(result, effect_id)

                {:error, reason} ->
                  _ =
                    persist_execute(
                      env,
                      execute_audit_payload(effect_id, env, digest, "persist_failed", %{
                        "error" => inspect(reason)
                      })
                    )

                  {:error, :persist_failed}
              end

            blocked ->
              payload =
                execute_audit_payload(effect_id, env, digest, "governance_blocked", %{
                  "veto_reason" => veto_reason(blocked)
                })

              _ = persist_execute(env, payload)
              {:error, :governance_blocked}
          end
        after
          release_all(acquired)
        end

      {:error, :held_by_other} ->
        {:error, :lease_held}

      {:error, reason} ->
        Logger.warning("governed_effect file_write lease acquire failed: #{inspect(reason)}")
        {:error, :lease_acquire_failed}
    end
  end

  # Durable row for the commit path only — a dry-run writes nothing and needs no
  # rollback row, so it would otherwise leave an orphan for recovery to reconcile.
  defp ensure_payload_row(effect_id, env, digest) do
    if file_write_commit_enabled?() do
      case FileWriteExecutor.resolved_payload(env.payload) do
        {:ok, bytes, sha} ->
          EffectRepo.insert_effect_payload(%{
            effect_id: effect_id,
            effect_type: env.effect_type,
            payload_bytes: bytes,
            payload_sha256: sha,
            required_leases: env.required_leases,
            proposer_agent_uuid: env.proposer_agent_uuid,
            idempotency_key: env.idempotency_key,
            idempotency_digest: digest
          })

        {:error, reason} ->
          {:error, reason}
      end
    else
      :ok
    end
  end

  defp file_write_commit_enabled? do
    Application.get_env(:lease_plane, :execute_file_write_commit_enabled, false) == true
  end

  defp canonicalize_leases(leases) do
    Enum.map(leases, fn l ->
      surface = Map.get(l, "surface") || Map.get(l, :surface)

      case Canonicalize.canonicalize(surface) do
        {:ok, canon} -> %{"surface" => canon, "ttl_s" => lease_ttl(l)}
        _ -> %{"surface" => surface, "ttl_s" => lease_ttl(l)}
      end
    end)
  end

  # Acquire every required lease; on the first conflict, release what we hold and
  # bail (atomic-ish: no partial custody escapes).
  defp acquire_all(leases, proposer) do
    Enum.reduce_while(leases, {:ok, []}, fn l, {:ok, acc} ->
      params = %{
        surface_id: Map.get(l, "surface"),
        holder_agent_uuid: proposer,
        holder_kind: "remote_heartbeat",
        ttl_s: lease_ttl(l)
      }

      case Repo.acquire(params) do
        {:ok, lease, _} ->
          {:cont, {:ok, [lease | acc]}}

        {:error, :held_by_other, _} ->
          release_all(acc)
          {:halt, {:error, :held_by_other}}

        {:error, reason} ->
          release_all(acc)
          {:halt, {:error, reason}}
      end
    end)
  end

  defp release_all(leases) do
    Enum.each(leases, fn lease ->
      lease_id = Map.get(lease, :lease_id) || Map.get(lease, "lease_id")
      if is_binary(lease_id), do: Repo.release(lease_id, "governed_effect_file_write_complete")
    end)
  end

  defp result_audit({:committed, meta}),
    do: {if(meta[:dry_run], do: "dry_run", else: "committed"), %{"result" => stringify(meta)}}

  defp result_audit({:rejected, reason}),
    do: {"rejected", %{"error" => inspect(reason)}}

  defp result_to_reply({:committed, meta}, effect_id),
    do: {:ok, %{ok: true, effect_id: effect_id, custody_mode: "execute", result: meta}}

  defp result_to_reply({:rejected, reason}, _effect_id), do: {:error, reason}

  defp stringify(map), do: Map.new(map, fn {k, v} -> {to_string(k), v} end)

  defp veto_reason({:blocked, r}), do: r
  defp veto_reason({:error, r}), do: "veto_unavailable:#{inspect(r)}"
  defp veto_reason(_), do: "vetoed"

  defp execute_agent_spawn_enabled? do
    Application.get_env(:lease_plane, :execute_agent_spawn_enabled, false) == true and
      is_binary(Application.get_env(:lease_plane, :agent_orchestrator_bearer_token))
  end

  defp execute_agent_spawn(env) do
    digest = idempotency_digest(env)

    case Repo.governed_effect_by_idempotency_key(env.idempotency_key, @execute_event_type) do
      # Idempotent replay — a previously committed spawn. Return the original
      # effect_id + agent_id; DO NOT spawn again (an agent_spawn is irreversible).
      {:ok, %{idempotency_digest: ^digest, payload: stored}} ->
        {:ok, execute_idempotent_body(stored)}

      {:ok, %{idempotency_digest: other}} when is_binary(other) ->
        {:error, :idempotency_conflict}

      {:ok, nil} ->
        spawn_and_record(env, digest)

      {:error, reason} ->
        Logger.warning(
          "governed_effect execute idempotency lookup failed key=#{env.idempotency_key}: " <>
            inspect(reason)
        )

        {:error, :persist_failed}
    end
  end

  defp spawn_and_record(env, digest) do
    effect_id = gen_effect_id()

    # §6 governance veto — BEFORE the spawn commits. The effect is committed only
    # if governance affirmatively clears it (`:allow`). A block, a missing
    # proposer, or an unreachable/erroring governance MCP all fail CLOSED: we do
    # not spawn, and persist a `governance_blocked` record.
    case GovernanceVetoClient.check(env) do
      :allow ->
        spawn_after_veto(env, digest, effect_id)

      {:blocked, reason} ->
        Logger.info(
          "governed_effect execute agent_spawn VETOED effect_id=#{effect_id} " <>
            "surface=#{env.surface} reason=#{reason}"
        )

        payload =
          execute_audit_payload(effect_id, env, digest, "governance_blocked", %{
            "veto_reason" => reason
          })

        _ = persist_execute(env, payload)
        {:error, :governance_blocked}

      {:error, reason} ->
        # Fail closed: could not confirm governance clearance → do not spawn.
        Logger.warning(
          "governed_effect execute agent_spawn veto-unavailable effect_id=#{effect_id} " <>
            "surface=#{env.surface}: #{inspect(reason)} — failing closed"
        )

        payload =
          execute_audit_payload(effect_id, env, digest, "governance_blocked", %{
            "veto_reason" => "veto_unavailable:#{inspect(reason)}"
          })

        _ = persist_execute(env, payload)
        {:error, :governance_blocked}
    end
  end

  defp spawn_after_veto(env, digest, effect_id) do
    case OrchestratorClient.spawn_agent(orchestrator_spec(env)) do
      {:ok, agent_id} ->
        Logger.info(
          "governed_effect execute agent_spawn effect_id=#{effect_id} agent_id=#{agent_id} " <>
            "surface=#{env.surface} digest=#{binary_part(digest, 0, 12)}"
        )

        payload =
          execute_audit_payload(effect_id, env, digest, "committed", %{"agent_id" => agent_id})

        # The spawn already happened; record best-effort. A persist failure must
        # NOT re-spawn, so we still return committed with the agent_id (the audit
        # gap is logged), never an error that invites a retry.
        _ = persist_execute(env, payload)
        {:ok, execute_body(payload, agent_id)}

      {:error, reason} ->
        Logger.warning(
          "governed_effect execute agent_spawn FAILED effect_id=#{effect_id} " <>
            "surface=#{env.surface}: #{inspect(reason)}"
        )

        payload =
          execute_audit_payload(effect_id, env, digest, "rejected", %{"error" => inspect(reason)})

        _ = persist_execute(env, payload)
        {:error, :spawn_failed}
    end
  end

  # Build the orchestrator spawn spec from the effect payload. The payload
  # carries the command (`cmd`/`args`/`env`); lineage is provisioned from the
  # proposer so the spawned agent's parentage is correct by construction. The
  # proposer's `client_session_id` is NEVER forwarded (Invariant 1/7 — BEAM
  # consumes proof, the child mints its own identity under provisioned lineage).
  defp orchestrator_spec(env) do
    p = env.payload || %{}

    base = %{
      "cmd" => Map.get(p, "cmd"),
      "args" => Map.get(p, "args", []),
      "env" => Map.get(p, "env", %{})
    }

    case env.proposer_agent_uuid do
      uuid when is_binary(uuid) ->
        # Orchestrator lineage contract: `parent_agent_uuid` (+ optional
        # `spawn_reason`) — verified live, a `parent_agent_id` key 422s.
        Map.put(base, "lineage", %{
          "parent_agent_uuid" => uuid,
          "spawn_reason" => "governed_effect"
        })

      _ ->
        base
    end
  end

  defp persist_execute(env, payload) do
    Repo.insert_governed_effect_event(%{
      event_type: @execute_event_type,
      agent_id: env.proposer_agent_uuid,
      session_id: env.provenance_session_id,
      payload: payload
    })
  end

  defp execute_audit_payload(effect_id, env, digest, status, extra) do
    %{
      "effect_lane" => @effect_lane,
      "effect_id" => effect_id,
      "custody_mode" => "execute",
      "status" => status,
      "effect_type" => env.effect_type,
      "surface" => env.surface,
      "idempotency_key" => env.idempotency_key,
      "idempotency_digest" => digest,
      "proposer_agent_uuid" => env.proposer_agent_uuid
    }
    |> Map.merge(extra)
  end

  defp execute_body(payload, agent_id) do
    %{
      ok: true,
      effect_id: payload["effect_id"],
      custody_mode: "execute",
      status: "committed",
      effect_lane: @effect_lane,
      idempotency_digest: payload["idempotency_digest"],
      agent_id: agent_id,
      idempotent: false
    }
  end

  defp execute_idempotent_body(stored) when is_map(stored) do
    %{
      ok: true,
      effect_id: stored["effect_id"],
      custody_mode: "execute",
      status: stored["status"] || "committed",
      effect_lane: @effect_lane,
      idempotency_digest: stored["idempotency_digest"],
      agent_id: stored["agent_id"],
      idempotent: true
    }
  end

  # Observe-not-acquire: peek the lease state, NEVER acquire (acquiring would
  # block the genuine holder, violating "shadow claims nothing"). A present,
  # un-released lease is what an `execute` acquire would collide with → record
  # `would_block` with the blocking holder; an absent lease → `ok`.
  #
  # Canonicalize first — the same as `acquire`/`status` (RFC §7.12.1) — so a
  # raw, non-canonical surface in the envelope cannot split-brain past a held
  # lease and falsely read `ok`.
  defp observe_lease(%{"surface" => raw_surface}) do
    case UnitaresLeasePlane.Canonicalize.canonicalize(raw_surface) do
      {:error, reason} ->
        %{surface: raw_surface, would_acquire: "invalid", reason: to_string(reason)}

      {:ok, surface} ->
        case UnitaresLeasePlane.status(surface) do
          {:ok, nil} ->
            %{surface: surface, would_acquire: "ok"}

          {:ok, lease} when is_map(lease) ->
            %{
              surface: surface,
              would_acquire: "would_block",
              held_by_uuid: Map.get(lease, :holder_agent_uuid),
              expires_at: present_dt(Map.get(lease, :expires_at))
            }

          {:error, reason} ->
            %{surface: surface, would_acquire: "unknown", reason: inspect(reason)}
        end
    end
  end

  defp present_dt(%DateTime{} = dt), do: DateTime.to_iso8601(dt)
  defp present_dt(%NaiveDateTime{} = dt), do: NaiveDateTime.to_iso8601(dt)
  defp present_dt(other) when is_binary(other) or is_nil(other), do: other
  defp present_dt(other), do: inspect(other)

  defp gen_effect_id do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    parts = [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    Enum.map_join(parts, "-", &Base.encode16(&1, case: :lower))
  end
end
