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

  `execute` mode is available only for explicitly enabled effect types. A
  direct execute remains valid for compatibility. An execute that declares a
  `promotion` is stricter: before leases, veto, or mutation, the plane loads
  the named `record_only` predecessor and verifies effect type, surface, and
  intended-payload SHA-256. The durable execute receipt links the exact shadow,
  decision-standard reference, approval reference, and evidence references.

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
  (a credential). Each row also carries a non-secret receipt for the production
  mapping to fermata's Governed Effect IR; the full execute intent remains
  transient because it contains the effect input. A legacy payload outside the
  cross-language canonical subset keeps direct execution compatible but gets
  an explicit unavailable marker, never a false Fermata digest; such a shadow
  is not promotable.

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
  alias UnitaresLeasePlane.GovernedEffectIR
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
    * `{:error, :promotion_*}` — a declared shadow promotion could not be proven;
    * `{:error, :governance_blocked}` — governance vetoed (or could not clear) the effect;
    * `{:error, :persist_failed}` / `{:error, :spawn_failed}`;
    * `{:error, detail}` — `schema_invalid` detail string.
  """
  @spec handle(map()) ::
          {:ok, map()}
          | {:error, :execute_not_implemented}
          | {:error, :idempotency_conflict}
          | {:error, :promotion_predecessor_not_found}
          | {:error, :promotion_predecessor_unverifiable}
          | {:error, :promotion_mismatch}
          | {:error, :promotion_already_consumed}
          | {:error, :promotion_lookup_failed}
          | {:error, :governance_blocked}
          | {:error, :persist_failed}
          | {:error, :spawn_failed}
          | {:error, String.t()}
  def handle(%{} = body) do
    with {:ok, env} <- validate(body) do
      case env.custody_mode do
        "record_only" ->
          record_only(env)

        "execute" ->
          with {:ok, verified_env} <- verify_promotion(env) do
            execute(verified_env)
          end
      end
    end
  end

  def handle(_), do: {:error, "body must be a JSON object"}

  @doc """
  Canonical idempotency digest: direct requests retain
  `sha256(effect_type ‖ surface ‖ custody_mode ‖ payload_hash)`, hex. A declared
  promotion additionally binds its verified receipt, so a direct execute or a
  different predecessor can never replay as the requested promotion. Excludes
  `provenance`/`proposer` so a retry from a new session is not treated as
  "materially different" (contract §4).
  """
  @spec idempotency_digest(map()) :: String.t()
  def idempotency_digest(%{} = env) do
    payload_hash =
      :crypto.hash(:sha256, Jason.encode!(Map.get(env, :payload, %{})))
      |> Base.encode16(case: :lower)

    base_digest =
      [env.effect_type, env.surface, env.custody_mode, payload_hash]
      |> Enum.join(" ")
      |> then(&:crypto.hash(:sha256, &1))
      |> Base.encode16(case: :lower)

    case promotion_identity_digest(env) do
      nil ->
        # Preserve byte-for-byte compatibility with pre-promotion direct
        # execute and record_only idempotency rows.
        base_digest

      promotion_digest ->
        :crypto.hash(:sha256, base_digest <> " promotion " <> promotion_digest)
        |> Base.encode16(case: :lower)
    end
  end

  # ---- validation ----

  defp validate(body) do
    idem = Map.get(body, "idempotency_key")
    mode = Map.get(body, "custody_mode")
    type = Map.get(body, "effect_type")
    surface = Map.get(body, "surface")
    leases = Map.get(body, "required_leases", [])
    payload = Map.get(body, "payload", %{})
    promotion = Map.get(body, "promotion")
    promotion_error = promotion_shape_error(promotion, mode)

    # Attribution only — non-secret fields. The proposer's `client_session_id`
    # is a credential (Invariant 7) and is deliberately NOT extracted or stored.
    proposer_agent_uuid = nested_string(body, "proposer", "agent_uuid")
    provenance_session_id = nested_string(body, "provenance", "session_id")
    provenance = sanitize_provenance(Map.get(body, "provenance"))

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

      is_binary(promotion_error) ->
        {:error, promotion_error}

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
           required_leases: sanitize_required_leases(leases),
           payload: payload || %{},
           proposer_agent_uuid: proposer_agent_uuid,
           provenance_session_id: provenance_session_id,
           provenance: provenance,
           promotion: normalize_promotion(promotion),
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

  @promotion_required_fields ~w(record_only_effect_id decision_standard_ref approval_ref evidence_refs)
  @promotion_ref_max_bytes 512
  @promotion_max_evidence_refs 16

  defp promotion_shape_error(nil, _mode), do: nil

  defp promotion_shape_error(_promotion, mode) when mode != "execute",
    do: "promotion is valid only for custody_mode=execute"

  defp promotion_shape_error(%{} = promotion, "execute") do
    missing =
      Enum.filter(@promotion_required_fields, fn field ->
        case Map.get(promotion, field) do
          value when field == "evidence_refs" ->
            not (is_list(value) and value != [] and
                   length(value) <= @promotion_max_evidence_refs and
                   Enum.all?(value, &valid_promotion_ref?/1))

          value ->
            not valid_promotion_ref?(value)
        end
      end)

    if missing == [],
      do: nil,
      else: "promotion requires bounded non-empty reference fields: #{Enum.join(missing, ", ")}"
  end

  defp promotion_shape_error(_promotion, "execute"),
    do: "promotion must be an object"

  defp valid_promotion_ref?(value) when is_binary(value) do
    byte_size(value) > 0 and byte_size(value) <= @promotion_ref_max_bytes and
      String.valid?(value) and not Regex.match?(~r/[\x00-\x20\x7F]/, value) and
      not credential_like_reference?(value)
  end

  defp valid_promotion_ref?(_), do: false

  defp credential_like_reference?(value) do
    Regex.match?(
      ~r/\A(?:bearer|basic)\s|\A(?:v1\.|sk-|ghp_|github_pat_)|(?:authorization|api[_-]?key|continuity[_-]?token|access[_-]?token)=/i,
      value
    )
  end

  defp normalize_promotion(nil), do: nil

  defp normalize_promotion(%{} = promotion),
    do: Map.take(promotion, @promotion_required_fields)

  defp promotion_identity_digest(env) do
    case Map.get(env, :promotion_receipt) || Map.get(env, :promotion) do
      %{} = promotion ->
        # A fixed-position array avoids map-order dependence while binding both
        # the caller-declared references and the server-derived continuity
        # hashes/tier. Validation constrains these values to JSON primitives.
        fields =
          @promotion_required_fields ++
            ~w(payload_sha256 predecessor_fermata_intent_sha256 predecessor_reverified_tier predecessor_proposer_agent_uuid continuity_verified)

        fields
        |> Enum.map(&Map.get(promotion, &1))
        |> Jason.encode!()
        |> then(&:crypto.hash(:sha256, &1))
        |> Base.encode16(case: :lower)

      _ ->
        nil
    end
  end

  defp sanitize_provenance(%{} = provenance) do
    Map.take(provenance, ~w(harness session_id verification_source))
  end

  defp sanitize_provenance(_), do: %{}

  defp valid_leases?(leases) when is_list(leases) do
    Enum.all?(leases, fn
      %{"surface" => s} = lease when is_binary(s) and byte_size(s) > 0 ->
        case Map.get(lease, "ttl_s") do
          nil -> true
          ttl when is_integer(ttl) and ttl > 0 -> true
          _ -> false
        end

      _ ->
        false
    end)
  end

  defp valid_leases?(_), do: false

  defp sanitize_required_leases(leases) do
    Enum.map(leases, fn lease ->
      %{"surface" => Map.get(lease, "surface")}
      |> maybe_put_lease_ttl(Map.get(lease, "ttl_s"))
    end)
  end

  defp maybe_put_lease_ttl(lease, nil), do: lease
  defp maybe_put_lease_ttl(lease, ttl), do: Map.put(lease, "ttl_s", ttl)

  defp credential_shaped?(payload) when is_map(payload) do
    Enum.any?(Map.keys(payload), fn k ->
      ks = k |> to_string() |> String.downcase()
      Enum.any?(@credential_key_substrings, &String.contains?(ks, &1))
    end)
  end

  defp credential_shaped?(_), do: false

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

  # ---- explicit record_only -> execute promotion continuity ----

  # Direct execute remains a distinct, backwards-compatible path. Once a
  # caller declares promotion, however, every link is mandatory and verified
  # before the plane acquires a lease, calls governance, or touches a surface.
  defp verify_promotion(%{promotion: nil} = env), do: {:ok, env}

  defp verify_promotion(%{promotion: promotion} = env) do
    cond do
      not promotion_enabled?() ->
        {:error, :promotion_not_enabled}

      env.effect_type != "file_write" ->
        # Only the reversible, exact-target file_write path has the continuity
        # and compensation proof required for promotion today.
        {:error, :promotion_effect_type_not_supported}

      true ->
        case intended_payload_sha(env) do
          {:ok, intended_sha} ->
            verify_promotion_predecessor(env, promotion, intended_sha)

          {:error, reason} ->
            {:error, reason}
        end
    end
  end

  defp promotion_enabled? do
    Application.get_env(:lease_plane, :governed_effect_promotion_enabled, false) == true
  end

  defp verify_promotion_predecessor(env, promotion, intended_sha) do
    predecessor_id = promotion["record_only_effect_id"]

    case Repo.governed_effect_by_effect_id(predecessor_id) do
      {:ok, nil} ->
        {:error, :promotion_predecessor_not_found}

      {:ok, predecessor} ->
        case verify_predecessor(predecessor, env, intended_sha) do
          :ok ->
            predecessor_fermata = get_in(predecessor, ["fermata", "intent_sha256"])

            receipt =
              promotion
              |> Map.put("payload_sha256", intended_sha)
              |> Map.put("predecessor_fermata_intent_sha256", predecessor_fermata)
              |> Map.put("predecessor_reverified_tier", predecessor["reverified_tier"])
              |> Map.put(
                "predecessor_proposer_agent_uuid",
                predecessor["proposer_agent_uuid"]
              )
              |> Map.put("continuity_verified", true)

            {:ok, Map.put(env, :promotion_receipt, receipt)}

          {:error, reason} ->
            {:error, reason}
        end

      {:error, _reason} ->
        {:error, :promotion_lookup_failed}
    end
  end

  defp intended_payload_sha(%{effect_type: "file_write", payload: payload}) do
    case FileWriteExecutor.resolved_payload(payload) do
      {:ok, _bytes, sha} -> {:ok, sha}
      {:error, reason} -> {:error, reason}
    end
  end

  defp intended_payload_sha(%{payload: payload}) do
    case UnitaresLeasePlane.CanonicalPayload.sha256(payload) do
      {:ok, sha} -> {:ok, sha}
      {:error, _reason} -> {:error, :promotion_predecessor_unverifiable}
    end
  end

  defp verify_predecessor(nil, _env, _intended_sha),
    do: {:error, :promotion_predecessor_not_found}

  defp verify_predecessor(predecessor, env, intended_sha) do
    predecessor_sha = Map.get(predecessor, "payload_sha256")
    predecessor_tier = Map.get(predecessor, "reverified_tier")

    cond do
      predecessor["custody_mode"] != "record_only" or predecessor["status"] != "recorded" ->
        {:error, :promotion_mismatch}

      predecessor["effect_type"] != env.effect_type or
          not same_canonical_surface?(predecessor["surface"], env.surface) ->
        {:error, :promotion_mismatch}

      predecessor_tier not in ~w(medium strong) or
          not valid_proposer?(%{proposer_agent_uuid: predecessor["proposer_agent_uuid"]}) ->
        {:error, :promotion_predecessor_unverifiable}

      not valid_sha256?(predecessor_sha) or not valid_fermata_predecess?(predecessor) ->
        {:error, :promotion_predecessor_unverifiable}

      String.downcase(predecessor_sha) != String.downcase(intended_sha) ->
        {:error, :promotion_mismatch}

      true ->
        :ok
    end
  end

  defp valid_sha256?(value) when is_binary(value),
    do: Regex.match?(~r/\A[0-9a-fA-F]{64}\z/, value)

  defp valid_sha256?(_), do: false

  defp same_canonical_surface?(left, right) when is_binary(left) and is_binary(right) do
    case {Canonicalize.canonicalize(left), Canonicalize.canonicalize(right)} do
      {{:ok, canonical}, {:ok, canonical}} -> true
      _ -> left == right
    end
  end

  defp same_canonical_surface?(_, _), do: false

  defp valid_fermata_predecess?(predecessor) do
    receipt = Map.get(predecessor, "fermata")

    expected_keys =
      ~w(adapter intent_id intent_sha256 operation profile proposal_id required_capability schema)

    is_map(receipt) and
      Enum.sort(Map.keys(receipt)) == expected_keys and
      receipt["schema"] == "fermata.governed-effect-ir.v0" and
      receipt["profile"] == "unitares" and
      receipt["intent_id"] == predecessor["effect_id"] and
      receipt["proposal_id"] == predecessor["effect_id"] and
      valid_sha256?(receipt["intent_sha256"]) and
      valid_fermata_effect_mapping?(predecessor["effect_type"], receipt)
  end

  defp valid_fermata_effect_mapping?("file_write", receipt) do
    receipt["adapter"] == "file" and receipt["operation"] == "write" and
      receipt["required_capability"] == "file.write"
  end

  defp valid_fermata_effect_mapping?(_, _), do: false

  defp attach_fermata_receipt(env, effect_id) do
    case GovernedEffectIR.receipt(env, effect_id) do
      {:ok, receipt} ->
        {:ok, Map.put(env, :fermata_receipt, receipt)}

      {:error, reason} when is_nil(env.promotion) ->
        # Preserve pre-Fermata direct/record-only payload compatibility without
        # inventing a runtime-local digest. The marker is non-secret and cannot
        # pass valid_fermata_predecess?/1, so it never becomes promotion proof.
        unavailable = %{
          "schema" => "unitares.fermata-receipt-unavailable.v1",
          "profile" => "unitares",
          "intent_id" => effect_id,
          "reason" => to_string(reason)
        }

        {:ok, Map.put(env, :fermata_receipt, unavailable)}

      {:error, reason} ->
        {:error, "fermata intent is not canonical: #{inspect(reason)}"}
    end
  end

  # ---- record_only ----

  defp record_only(env) do
    env = Map.put(env, :reverified_tier, reverify_record_only_tier(env))
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

    with {:ok, env} <- attach_fermata_receipt(env, effect_id) do
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
      "proposer_agent_uuid" => env.proposer_agent_uuid,
      "reverified_tier" => env.reverified_tier,
      "fermata" => env.fermata_receipt
    }
    |> maybe_put_payload_sha256(env)
  end

  defp maybe_put_payload_sha256(payload, env) do
    case Map.get(env.payload, "sha256") do
      sha when is_binary(sha) -> Map.put(payload, "payload_sha256", String.downcase(sha))
      _ -> payload
    end
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
      fermata: audit_payload["fermata"],
      identity_assurance_tier: audit_payload["reverified_tier"],
      custody_expires_at: nil,
      observations: observations,
      idempotent: idempotent?
    }
  end

  defp idempotent_body(stored) when is_map(stored) do
    response_body(stored, Map.get(stored, "observations", []), true)
  end

  defp reverify_record_only_tier(env) do
    with true <- valid_proposer?(env),
         token when is_binary(token) and token != "" <- env.proposer_continuity_token,
         client <- governance_veto_client(),
         true <- function_exported?(client, :verify_identity_tier, 1),
         {:ok, tier} when tier in ~w(medium strong) <- client.verify_identity_tier(env) do
      tier
    else
      _ -> "unverified"
    end
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
    with {:ok, env} <- bind_execution_target(env) do
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

          case Repo.governed_effect_by_idempotency_key(
                 env.idempotency_key,
                 @execute_event_type
               ) do
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
    effect_id = effect_id_for(env)

    with {:ok, env} <- attach_fermata_receipt(env, effect_id) do
      # Canonicalize lease surfaces ONCE so the acquired surface_id and the
      # executor's canonical(path) match (the path-canonicalization seam).
      canon_leases = canonicalize_leases(env.required_leases)

      case acquire_all(canon_leases, env.proposer_agent_uuid) do
        {:ok, acquired} ->
          try do
            # §6 veto re-checked HERE, on the commit path, with the lease held.
            case governance_veto_client().check(env) do
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

                    result_to_reply(result, effect_id, env)

                  {:error, :promotion_already_consumed} ->
                    # The deterministic predecessor-derived effect id already
                    # owns a payload row. This caller lost the atomic claim;
                    # refuse before FileWriteExecutor can observe or mutate.
                    {:error, :promotion_already_consumed}

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
  end

  # Durable row for the commit path only — a dry-run writes nothing and needs no
  # rollback row, so it would otherwise leave an orphan for recovery to reconcile.
  defp ensure_payload_row(effect_id, env, digest) do
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
              case EffectRepo.claim_promotion_payload(params) do
                :inserted -> :ok
                :already -> {:error, :promotion_already_consumed}
                {:error, _} = error -> error
              end

            _ ->
              EffectRepo.insert_effect_payload(params)
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

  # Injectable only for a full commit-path test; production always resolves to
  # the real fail-closed HTTP client unless the test environment overrides it.
  defp governance_veto_client do
    Application.get_env(:lease_plane, :governance_veto_client, GovernanceVetoClient)
  end

  defp execute_agent_spawn_enabled? do
    Application.get_env(:lease_plane, :execute_agent_spawn_enabled, false) == true and
      is_binary(Application.get_env(:lease_plane, :agent_orchestrator_bearer_token))
  end

  defp execute_agent_spawn(env) do
    digest = idempotency_digest(env)

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
    effect_id = gen_effect_id()

    with {:ok, env} <- attach_fermata_receipt(env, effect_id) do
      # §6 governance veto — BEFORE the spawn commits. The effect is committed only
      # if governance affirmatively clears it (`:allow`). A block, a missing
      # proposer, or an unreachable/erroring governance MCP all fail CLOSED: we do
      # not spawn, and persist a `governance_blocked` record.
      case governance_veto_client().check(env) do
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

  defp effect_id_for(_env), do: gen_effect_id()
end
