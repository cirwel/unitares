defmodule UnitaresLeasePlane.GovernedEffect.Execution do
  @moduledoc """
  Execute-mode custody, governance veto, lease acquisition, file-write
  execution, and agent-spawn delegation for `UnitaresLeasePlane.GovernedEffect`.
  """

  require Logger

  alias UnitaresLeasePlane.Canonicalize
  alias UnitaresLeasePlane.EffectRepo
  alias UnitaresLeasePlane.FileWriteExecutor
  alias UnitaresLeasePlane.GovernedEffect
  alias UnitaresLeasePlane.OrchestratorClient
  alias UnitaresLeasePlane.Repo

  @effect_lane "governed_effect"
  @execute_event_type "governed_effect.execute"

  # Restart + compensation budget: a too-short lease cannot survive a crash and
  # recovery, so reject before any custody starts.
  @min_execute_ttl_s 120

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
  @doc false
  def execute(%{effect_type: "agent_spawn"} = env) do
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
  # EffectRecovery (boot) + the executor's in-process compensation. A durable
  # reservation covers filesystem I/O; TTL alone cannot protect a slow writer.
  def execute(%{effect_type: "file_write"} = env) do
    if execute_file_write_enabled?() do
      execute_file_write(env)
    else
      {:error, :execute_not_implemented}
    end
  end

  # Every other effect_type is still gated.
  def execute(_env), do: {:error, :execute_not_implemented}

  defp execute_file_write_enabled? do
    Application.get_env(:lease_plane, :execute_file_write_enabled, false) == true
  end

  # The executable target is one invariant, not three caller-controlled labels.
  # For file_write execute, canonical envelope surface == canonical payload.path
  # == the sole required lease surface. Bind this before lease acquisition,
  # governance, custody, or mutation so the audit row describes the surface
  # that was actually touched.
  defp bind_execution_target(%{custody_mode: "execute", effect_type: "file_write"} = env) do
    with {:ok, envelope_surface} <- Canonicalize.canonicalize(env.surface),
         {:ok, leases} <- canonical_file_write_leases(env.required_leases),
         {:ok, target_surface} <-
           FileWriteExecutor.resolved_target_surface(env.payload, leases),
         true <- envelope_surface == target_surface,
         [%{"surface" => ^target_surface}] <- leases do
      {:ok, %{env | surface: target_surface, required_leases: leases}}
    else
      _ -> {:error, :surface_path_mismatch}
    end
  end

  defp bind_execution_target(env), do: {:ok, env}

  defp canonical_file_write_leases(leases) when is_list(leases) do
    Enum.reduce_while(leases, {:ok, []}, fn lease, {:ok, acc} ->
      surface = Map.get(lease, "surface") || Map.get(lease, :surface)

      case Canonicalize.canonicalize(surface) do
        {:ok, canonical} ->
          normalized = %{"surface" => canonical, "ttl_s" => lease_ttl(lease)}
          {:cont, {:ok, [normalized | acc]}}

        {:error, _} ->
          {:halt, {:error, :surface_path_mismatch}}
      end
    end)
    |> case do
      {:ok, normalized} -> {:ok, Enum.reverse(normalized)}
      {:error, _} = error -> error
    end
  end

  defp canonical_file_write_leases(_), do: {:error, :surface_path_mismatch}

  defp execute_file_write(env) do
    with {:ok, env} <- bind_execution_target(env) do
      cond do
        # Validate the proposer BEFORE acquiring any lease — otherwise a nil or
        # malformed uuid crashes uuid_to_binary inside Repo.acquire and surfaces as
        # an opaque 500 instead of a clean client error.
        not GovernedEffect.valid_proposer?(env) ->
          {:error, :proposer_invalid}

        not min_ttl_ok?(env) ->
          {:error, :lease_ttl_too_short}

        true ->
          digest = GovernedEffect.idempotency_digest(env)

          case Repo.governed_effect_by_idempotency_key(
                 env.idempotency_key,
                 @execute_event_type
               ) do
            # These custody refusals happen before any filesystem mutation.
            # A retry must acquire new custody, never replay the refusal as 202.
            {:ok, %{idempotency_digest: ^digest, payload: %{"status" => status}}}
            when status in [
                   "revoked",
                   "custody_expired",
                   "custody_unavailable",
                   "effect_in_progress"
                 ] ->
              file_write_under_custody(env, digest)

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
  end

  defp min_ttl_ok?(env) do
    Enum.all?(env.required_leases, fn l -> (lease_ttl(l) || 0) >= @min_execute_ttl_s end)
  end

  defp lease_ttl(%{"ttl_s" => t}), do: t
  defp lease_ttl(%{ttl_s: t}), do: t
  defp lease_ttl(_), do: nil

  defp file_write_under_custody(env, digest) do
    effect_id = effect_id_for(env)

    with {:ok, env} <- GovernedEffect.attach_fermata_receipt(env, effect_id) do
      # Canonicalize lease surfaces ONCE so the acquired surface_id and the
      # executor's canonical(path) match (the path-canonicalization seam).
      canon_leases = canonicalize_leases(env.required_leases)

      case acquire_all(canon_leases, env.proposer_agent_uuid) do
        {:ok, acquired} ->
          # Admitted writes reserve durable custody via migration 076's
          # effect_fence trigger. If the request process crashes or raises
          # before admission commits a payload row (or after a clean rollback
          # tombstones it), release_uncommitted_custody returns the unfenced
          # lease immediately so a same-key retry does not hit lease_held (#2620).
          watcher = start_custody_watcher(effect_id, acquired)

          try do
            {reply, custody} = under_custody(env, effect_id, digest, acquired)
            stop_custody_watcher(watcher)
            if custody == :release, do: release_all(acquired)
            reply
          catch
            kind, reason ->
              stop_custody_watcher(watcher)
              release_uncommitted_custody(effect_id, acquired)
              :erlang.raise(kind, reason, __STACKTRACE__)
          end

        {:error, :held_by_other} ->
          {:error, :lease_held}

        {:error, :effect_in_progress} ->
          {:error, :effect_in_progress}

        {:error, reason} ->
          Logger.warning("governed_effect file_write lease acquire failed: #{inspect(reason)}")
          {:error, :lease_acquire_failed}
      end
    end
  end

  # Entered after acquisition; custody may change while the veto is pending.
  # Returns the reply plus what to do with the acquired leases afterwards.
  defp under_custody(env, effect_id, digest, acquired) do
    # §6 veto re-checked HERE, followed by a fresh custody read.
    case GovernedEffect.governance_veto_client().check(env) do
      :allow ->
        case Repo.check_effect_custody(acquired) do
          :ok ->
            apply_with_checked_custody(env, effect_id, digest, acquired)

          {:error, reason} ->
            recorded =
              persist_execute(
                env,
                execute_audit_payload(effect_id, env, digest, Atom.to_string(reason), %{
                  "lease_ids" => Enum.map(acquired, & &1.lease_id),
                  "custody_check" => "after_veto"
                })
              )

            case recorded do
              :ok -> {{:error, reason}, :release}
              {:error, _} -> {{:error, :persist_failed}, :release}
            end
        end

      blocked ->
        payload =
          execute_audit_payload(effect_id, env, digest, "governance_blocked", %{
            "veto_reason" => veto_reason(blocked)
          })

        _ = persist_execute(env, payload)
        {{:error, :governance_blocked}, :release}
    end
  end

  # The veto is an external call. Never treat a lease acquired before that
  # call as still current without checking its exact durable row afterwards.
  # The executor then atomically reserves this exact lease before filesystem I/O.
  defp apply_with_checked_custody(env, effect_id, digest, acquired) do
    # The payload (including a single-consumer promotion claim) commits in the
    # SAME transaction as custody admission. A refusal cannot strand a consumed
    # promotion, and a successful admission always has its durable payload row.
    result =
      FileWriteExecutor.apply_effect(effect_id, env.payload, acquired, fn conn ->
        case ensure_payload_row(effect_id, env, digest, conn) do
          :ok -> :ok
          {:error, :promotion_already_consumed} = error -> error
          {:error, _} -> {:error, :persist_failed}
        end
      end)

    case result do
      {:rejected, :promotion_already_consumed} ->
        # Preserve the prior terminal record for this deterministic effect ID.
        # The failed admission transaction has already rolled back its fence.
        {{:error, :promotion_already_consumed}, :release}

      _ ->
        {status, extra} = result_audit(result)
        extra = Map.put(extra, "lease_ids", Enum.map(acquired, & &1.lease_id))
        _ = persist_execute(env, execute_audit_payload(effect_id, env, digest, status, extra))
        {result_to_reply(result, effect_id, env), custody_after(result)}
    end
  end

  # Unknown/dirty outcomes retain the durable reservation, not just the TTL.
  # Recovery cannot clear it while an old writer may still resume.
  defp custody_after({:rejected, :rollback_failed}), do: :hold
  defp custody_after({:committed, %{custody_retained: true}}), do: :hold
  defp custody_after({:committed, %{mark_deferred: true}}), do: :hold
  defp custody_after(_), do: :release

  # Monitor the caller process across under_custody so an untrappable exit
  # (e.g. Bandit request death or Process.exit(pid, :kill) during the veto HTTP
  # call) still releases pre-admission leases without adopting :idempotent rows.
  defp start_custody_watcher(effect_id, acquired) do
    caller = self()

    spawn_monitor(fn ->
      ref = Process.monitor(caller)

      receive do
        {:disarm, ^caller} ->
          Process.demonitor(ref, [:flush])
          send(caller, {:disarmed, self()})

        {:DOWN, ^ref, :process, ^caller, _reason} ->
          release_uncommitted_custody(effect_id, acquired)
      end
    end)
  end

  defp stop_custody_watcher({pid, ref}) do
    send(pid, {:disarm, self()})

    receive do
      {:disarmed, ^pid} ->
        Process.demonitor(ref, [:flush])
        :ok

      {:DOWN, ^ref, :process, ^pid, _reason} ->
        :ok
    after
      5_000 ->
        Process.demonitor(ref, [:flush])
        Process.exit(pid, :kill)
        :ok
    end
  end

  # Release acquired leases on abnormal exit ONLY when the effect never left an
  # active or committed payload row (#2620). If Repo.begin_effect_custody
  # admitted the write, migration 076's effect_fence trigger already blocks
  # release while filesystem I/O is in flight or dirty; checking get_payload
  # also keeps custody if the process crashed after finish_effect_custody
  # retired the fence on a committed write but before persist_execute wrote
  # the audit.events idempotency record.
  defp release_uncommitted_custody(effect_id, acquired) do
    case EffectRepo.get_payload(effect_id) do
      {:ok, nil} ->
        release_all(acquired)

      {:ok, %{rollback_state: "tombstoned", committed_at: nil}} ->
        release_all(acquired)

      _ ->
        :ok
    end
  rescue
    _ -> :ok
  end

  # Durable row for the commit path only — a dry-run writes nothing and needs no
  # rollback row, so it would otherwise leave an orphan for recovery to reconcile.
  defp ensure_payload_row(effect_id, env, digest, conn) do
    if file_write_commit_enabled?() do
      case FileWriteExecutor.resolved_payload(env.payload) do
        {:ok, bytes, sha} ->
          params = %{
            effect_id: effect_id,
            effect_type: env.effect_type,
            payload_bytes: bytes,
            payload_sha256: sha,
            required_leases: env.required_leases,
            proposer_agent_uuid: env.proposer_agent_uuid,
            idempotency_key: env.idempotency_key,
            idempotency_digest: digest
          }

          case Map.get(env, :promotion_receipt) do
            %{} ->
              case EffectRepo.claim_promotion_payload(params, conn) do
                :inserted -> :ok
                :already -> {:error, :promotion_already_consumed}
                {:error, _} = error -> error
              end

            _ ->
              EffectRepo.insert_effect_payload(params, conn)
          end

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
        {:ok, lease, :new} ->
          {:cont, {:ok, [lease | acc]}}

        # The proposer already holds this surface, through another in-flight
        # effect or a lease it took directly. Custody must belong to this effect
        # alone (a different key on the same exclusive surface is lease_held),
        # and release_all must never close a lease this call did not open.
        {:ok, _lease, :idempotent} ->
          release_all(acc)
          {:halt, {:error, :held_by_other}}

        {:error, :held_by_other, _} ->
          release_all(acc)
          {:halt, {:error, :held_by_other}}

        {:error, reason} ->
          release_all(acc)
          {:halt, {:error, reason}}
      end
    end)
  end

  # Only leases acquire_all opened fresh reach here. 'normal' is the holder
  # returning its own in-hand lease (migration 056), which this is on every
  # path: done, blocked, failed, or rolling back a partial acquire. Any other
  # string fails surface_leases_release_reason_check, the lease stays active,
  # and the TTL reaper later mislabels it reaped_remote_ttl.
  defp release_all(leases) do
    Enum.each(leases, fn lease ->
      lease_id = Map.get(lease, :lease_id) || Map.get(lease, "lease_id")

      if is_binary(lease_id) do
        case Repo.release(lease_id, "normal") do
          :ok ->
            :ok

          {:error, :not_found} ->
            # Revocation/reaping already returned this exact lease. Never
            # release a replacement lease merely because its surface matches.
            :ok

          {:error, reason} ->
            Logger.warning(
              "governed_effect lease release failed lease_id=#{lease_id}: #{inspect(reason)}"
            )
        end
      end
    end)
  end

  defp result_audit({:committed, meta}),
    do: {if(meta[:dry_run], do: "dry_run", else: "committed"), %{"result" => stringify(meta)}}

  defp result_audit({:rejected, reason})
       when reason in [:revoked, :custody_expired, :custody_unavailable, :effect_in_progress],
       do: {Atom.to_string(reason), %{"custody_check" => "durable_admission"}}

  defp result_audit({:rejected, :persist_failed}), do: {"persist_failed", %{}}

  defp result_audit({:rejected, reason}),
    do: {"rejected", %{"error" => inspect(reason)}}

  defp result_to_reply({:committed, meta}, effect_id, env),
    do:
      {:ok,
       %{
         ok: true,
         effect_id: effect_id,
         custody_mode: "execute",
         result: meta,
         fermata: env.fermata_receipt,
         promotion: Map.get(env, :promotion_receipt)
       }}

  defp result_to_reply({:rejected, reason}, _effect_id, _env), do: {:error, reason}

  defp stringify(map), do: Map.new(map, fn {k, v} -> {to_string(k), v} end)

  defp veto_reason({:blocked, r}), do: r
  defp veto_reason({:error, r}), do: "veto_unavailable:#{inspect(r)}"
  defp veto_reason(_), do: "vetoed"

  defp execute_agent_spawn_enabled? do
    Application.get_env(:lease_plane, :execute_agent_spawn_enabled, false) == true and
      is_binary(Application.get_env(:lease_plane, :agent_orchestrator_bearer_token))
  end

  defp execute_agent_spawn(env) do
    digest = GovernedEffect.idempotency_digest(env)

    case Repo.governed_effect_by_idempotency_key(env.idempotency_key, @execute_event_type) do
      # Idempotent replay — a previously COMMITTED spawn. Return the original
      # effect_id + execution_id; DO NOT spawn again (an agent_spawn is irreversible).
      # Only committed rows replay: a blocked/rejected row must not permanently
      # poison its idempotency key — replaying a refusal would answer 202 with
      # a nil execution_id forever and the veto would be evaluated exactly once per
      # key. Non-committed prior rows fall through to a fresh veto + spawn.
      {:ok, %{idempotency_digest: ^digest, payload: %{"status" => "committed"} = stored}} ->
        {:ok, execute_idempotent_body(stored)}

      # Digest conflict is only meaningful against a committed spawn (the
      # irreversible thing that must not double-fire). A non-committed row with
      # a different digest is just an earlier refusal of a different spec.
      {:ok, %{idempotency_digest: other, payload: %{"status" => "committed"}}}
      when is_binary(other) ->
        {:error, :idempotency_conflict}

      {:ok, %{payload: %{}}} ->
        spawn_and_record(env, digest)

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
    effect_id = GovernedEffect.gen_effect_id()

    with {:ok, env} <- GovernedEffect.attach_fermata_receipt(env, effect_id) do
      # §6 governance veto — BEFORE the spawn commits. The effect is committed only
      # if governance affirmatively clears it (`:allow`). A block, a missing
      # proposer, or an unreachable/erroring governance MCP all fail CLOSED: we do
      # not spawn, and persist a `governance_blocked` record.
      case GovernedEffect.governance_veto_client().check(env) do
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
  end

  defp spawn_after_veto(env, digest, effect_id) do
    case OrchestratorClient.spawn_agent(orchestrator_spec(env)) do
      {:ok, execution_id} ->
        Logger.info(
          "governed_effect execute agent_spawn effect_id=#{effect_id} " <>
            "execution_id=#{execution_id} " <>
            "surface=#{env.surface} digest=#{binary_part(digest, 0, 12)}"
        )

        payload =
          execute_audit_payload(effect_id, env, digest, "committed", %{
            "execution_id" => execution_id,
            "agent_id" => execution_id
          })

        # The spawn already happened; record best-effort. A persist failure must
        # NOT re-spawn, so we still return committed with the execution_id (the audit
        # gap is logged), never an error that invites a retry.
        _ = persist_execute(env, payload)
        {:ok, execute_body(payload, execution_id)}

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
  # Public for tests only (same precedent as FileWriteExecutor.resolved_payload/1).
  @doc false
  def orchestrator_spec(%{payload: %{"profile" => profile} = p}) do
    # A launch profile fixes the command, the environment, the working
    # directory and the lineage mode, so only its name and variables go on.
    # No lineage is provisioned: the profile decides it (the reviewer reads
    # its parent from the session record), and a lineage:none profile refuses
    # a request that names one.
    %{"profile" => profile, "vars" => Map.get(p, "vars", %{})}
  end

  def orchestrator_spec(env) do
    p = env.payload || %{}

    base = %{
      "cmd" => Map.get(p, "cmd"),
      "args" => Map.get(p, "args", []),
      "env" => Map.get(p, "env", %{})
    }

    # Working directory matters independently of PYTHONPATH: `python -m`
    # prepends the child's cwd to sys.path, and without `cd` that is the
    # PLANE's cwd — an unrelated directory whose contents could shadow repo
    # modules on name collision. Forward it when the payload names one.
    base =
      case Map.get(p, "cd") do
        cd when is_binary(cd) and byte_size(cd) > 0 -> Map.put(base, "cd", cd)
        _ -> base
      end

    # The reviewer may remain alive through a full synthesis-response window.
    # Forward its positive per-spawn cap so the governed path does not silently
    # fall back to the orchestrator's shorter global default.
    base =
      case Map.get(p, "max_runtime_ms") do
        ms when is_integer(ms) and ms > 0 -> Map.put(base, "max_runtime_ms", ms)
        _ -> base
      end

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
      "proposer_agent_uuid" => env.proposer_agent_uuid,
      "fermata" => env.fermata_receipt
    }
    |> Map.merge(extra)
    |> maybe_put_promotion_receipt(env)
  end

  defp maybe_put_promotion_receipt(payload, env) do
    case Map.get(env, :promotion_receipt) do
      %{} = receipt -> Map.put(payload, "promotion", receipt)
      _ -> payload
    end
  end

  defp execute_body(payload, execution_id) do
    %{
      ok: true,
      effect_id: payload["effect_id"],
      custody_mode: "execute",
      status: "committed",
      effect_lane: @effect_lane,
      idempotency_digest: payload["idempotency_digest"],
      fermata: payload["fermata"],
      promotion: payload["promotion"],
      execution_id: execution_id,
      agent_id: execution_id,
      idempotent: false
    }
  end

  defp execute_idempotent_body(stored) when is_map(stored) do
    execution_id = stored["execution_id"] || stored["agent_id"]

    %{
      ok: true,
      effect_id: stored["effect_id"],
      custody_mode: "execute",
      status: stored["status"] || "committed",
      effect_lane: @effect_lane,
      idempotency_digest: stored["idempotency_digest"],
      fermata: stored["fermata"],
      promotion: stored["promotion"],
      execution_id: execution_id,
      agent_id: execution_id,
      idempotent: true
    }
  end

  defp effect_id_for(%{promotion_receipt: %{"record_only_effect_id" => predecessor}})
       when is_binary(predecessor) do
    # One predecessor maps to one execute identity, independent of the caller's
    # idempotency key. effects.payloads(effect_id PK) then atomically admits a
    # single commit-bearing promotion across BEAM processes.
    hex =
      :crypto.hash(:sha256, "unitares:promotion:" <> predecessor)
      |> Base.encode16(case: :lower)

    [
      binary_part(hex, 0, 8),
      binary_part(hex, 8, 4),
      binary_part(hex, 12, 4),
      binary_part(hex, 16, 4),
      binary_part(hex, 20, 12)
    ]
    |> Enum.join("-")
  end

  defp effect_id_for(_env), do: GovernedEffect.gen_effect_id()
end
