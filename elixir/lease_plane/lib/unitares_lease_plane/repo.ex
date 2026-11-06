defmodule UnitaresLeasePlane.Repo do
  @moduledoc """
  Postgrex-backed durable mirror for `lease_plane.surface_leases` and
  `lease_plane.lease_plane_events`. Intentionally not Ecto — keeping the
  surface area minimal until v1.

  Returns shapes mirror RFC §4.5 typed-absence:
    {:ok, lease_map, :new | :idempotent}     # acquire
    {:ok, lease_map | nil}                   # status
    :ok | {:error, reason}                   # renew/release/heartbeat

  All lease writes also append a row to `lease_plane.lease_plane_events`
  (audit-outbox) inside the same transaction so durable truth and audit
  trail cannot diverge. The forwarder projects those rows into
  `audit.tool_usage` after the fact.
  """

  alias UnitaresLeasePlane.DB
  alias UnitaresLeasePlane.Repo.EffectsAndHandoffs
  alias UnitaresLeasePlane.Repo.Messaging

  @typep lease :: map()
  @typep event :: map()

  # Cast uuid columns to text on the boundary so callers don't have to
  # know whether Postgrex returned 16-byte raw binaries or 36-char strings.
  @select_lease_columns """
  lease_id::text AS lease_id,
  surface_id, surface_kind,
  holder_agent_uuid::text AS holder_agent_uuid,
  holder_class, holder_kind, holder_pid, heartbeat_required, intent,
  acquired_at, expires_at, last_heartbeat_at, released_at, release_reason,
  audit_session, original_ttl_s, earned_status,
  substrate_state, substrate_state_observed_at, effect_fence
  """

  # ---------- acquire ----------

  @doc """
  Idempotent acquire on `(surface_id, holder_agent_uuid)`. Returns the
  existing active row with `:idempotent` if the requester already holds
  a non-released lease for this surface; only when a different holder
  is active do we return `{:error, :held_by_other, ...}`.
  """
  @spec acquire(map()) ::
          {:ok, lease, :new | :idempotent}
          | {:error, :held_by_other,
             %{
               held_by_uuid: binary(),
               expires_at: DateTime.t(),
               surface_id: binary(),
               blocking_lease_id: binary()
             }}
          | {:error, term()}
  def acquire(%{} = p) do
    Postgrex.transaction(DB, fn conn ->
      with {:ok, existing} <- maybe_existing_active(conn, p.surface_id),
           {:ok, result} <- acquire_step(conn, p, existing) do
        result
      else
        {:error, reason} -> Postgrex.rollback(conn, reason)
      end
    end)
    |> EffectsAndHandoffs.normalize_tx_result()
  end

  defp maybe_existing_active(conn, surface_id) do
    with :ok <- release_expired_remote_for_acquire(conn, surface_id) do
      sql =
        "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
          "WHERE surface_id = $1 AND released_at IS NULL"

      case Postgrex.query(conn, sql, [surface_id]) do
        {:ok, %{rows: []}} -> {:ok, nil}
        {:ok, %{rows: [row], columns: cols}} -> {:ok, EffectsAndHandoffs.row_to_map(cols, row)}
        {:error, e} -> {:error, e}
      end
    end
  end

  # Remote-heartbeat leases have no local process that can still own the
  # surface after their deadline. Close an already-expired row in the same
  # transaction as the replacement acquire so callers do not wait for the
  # periodic reaper. The conditional UPDATE also serializes acquire,
  # heartbeat, and reaper races; whichever changes the row first determines
  # what the following active-row read observes. Local-BEAM rows remain under
  # LeaseHolder/Reaper lifecycle control even if their persisted TTL elapsed.
  defp release_expired_remote_for_acquire(conn, surface_id) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET released_at = now(), release_reason = 'reaped_remote_ttl'
    WHERE surface_id = $1
      AND released_at IS NULL
      AND effect_fence IS NULL
      AND holder_kind = 'remote_heartbeat'
      AND expires_at < now()
    RETURNING #{@select_lease_columns}
    """

    case Postgrex.query(conn, sql, [surface_id]) do
      {:ok, %{rows: []}} ->
        :ok

      {:ok, %{rows: [row], columns: cols}} ->
        lease = EffectsAndHandoffs.row_to_map(cols, row)
        EffectsAndHandoffs.log_event(conn, "reaped_remote_ttl", lease)

      {:error, e} ->
        {:error, e}
    end
  end

  defp acquire_step(_conn, _p, %{effect_fence: fence}) when not is_nil(fence),
    do: {:error, :effect_in_progress}

  defp acquire_step(conn, p, %{holder_agent_uuid: held} = existing)
       when is_binary(held) do
    if held == p.holder_agent_uuid do
      # RFC §7.13: idempotent re-acquire from the same holder MUST update
      # substrate_state when caller provided new substrate fields. Without
      # this, SDK-based residents (Vigil/Sentinel/Watcher/Chronicler) using
      # run_once never see substrate updates: each cycle creates a fresh
      # GovernanceClient → fresh _LeaseCache → always calls acquire (not
      # renew) → idempotent-acquire returned the existing row unchanged.
      # Only Steward avoided this because PR #4's lease_substrate.py uses
      # a module-global cache that persists across cycles. Caught
      # 2026-05-04 multi-resident canary debug: Sentinel had a substrate
      # row from cycle 1 stuck at 23:52:05 even though subsequent cycles
      # ran successfully. Substrate-less acquires (legacy callers + tests)
      # are unchanged: skip the UPDATE and return existing as before.
      substrate_state = Map.get(p, :substrate_state)
      substrate_observed_at = Map.get(p, :substrate_state_observed_at)

      if not is_nil(substrate_state) or not is_nil(substrate_observed_at) do
        update_substrate_on_idempotent(
          conn,
          existing,
          substrate_state,
          substrate_observed_at
        )
      else
        {:ok, {:ok, existing, :idempotent}}
      end
    else
      # PR 5 council BLOCK fix: include surface_id + blocking_lease_id so the
      # 409 response carries every field the v0.7 §7.3.2 AcquireHeldByOther shape
      # requires. Without these, Pydantic validation degrades to AcquireSchemaInvalid.
      # Preserved log_conflict from upstream — emits the audit event for the
      # conflict-detected path.
      case EffectsAndHandoffs.log_conflict(conn, p, existing) do
        :ok ->
          {:ok,
           {:error, :held_by_other,
            %{
              held_by_uuid: held,
              expires_at: existing.expires_at,
              surface_id: existing.surface_id,
              blocking_lease_id: existing.lease_id
            }}}

        {:error, reason} ->
          {:error, reason}
      end
    end
  end

  defp acquire_step(conn, p, nil), do: acquire_step_nil(conn, p, 0)

  # Bounded recursion depth for the {:ok, nil} race-loop (council BLOCK C1).
  # 3 iterations is well above the practical maximum for the triple-race
  # (winner inserted, winner released, loser re-reads) — anything more
  # is a flapping pathology and deserves a typed error rather than infinite
  # recursion.
  defp acquire_step_nil(_conn, _p, depth) when depth > 3 do
    {:error, :race_storm}
  end

  defp acquire_step_nil(conn, p, depth) do
    # surface_kind dropped from INSERT column list per RFC v0.8 §7.2.3:
    # post-migration-026 it is a generated column derived from
    # split_part(surface_id, ':', 1). Including it here would raise
    # `ERROR: column "surface_kind" is a generated column`.
    # RFC §7.13: substrate_state and substrate_state_observed_at are optional
    # nullable columns. Both default to NULL via Map.get/3; the migration-034
    # pair-coherence CHECK enforces both-set-or-both-null at storage layer.
    substrate_state = Map.get(p, :substrate_state)
    substrate_observed_at = Map.get(p, :substrate_state_observed_at)

    insert_lease_sql = """
    INSERT INTO lease_plane.surface_leases
      (surface_id, holder_agent_uuid, holder_class,
       holder_kind, holder_pid, heartbeat_required, intent,
       expires_at, original_ttl_s, audit_session, earned_status,
       substrate_state, substrate_state_observed_at)
    VALUES
      ($1, $2, $3, $4, $5, $6, $7,
       now() + make_interval(secs => $8), $8, $9, 'provisional',
       $10::jsonb, $11::timestamptz)
    RETURNING #{@select_lease_columns}
    """

    args = [
      p.surface_id,
      EffectsAndHandoffs.uuid_to_binary(p.holder_agent_uuid),
      Map.get(p, :holder_class, "process_instance"),
      p.holder_kind,
      Map.get(p, :holder_pid),
      p.holder_kind == "remote_heartbeat",
      Map.get(p, :intent),
      p.ttl_s,
      Map.get(p, :audit_session),
      substrate_state,
      substrate_observed_at
    ]

    # Unique savepoint name per call — defends against future refactors that
    # could cause two acquire_step_nil calls on the same connection (which
    # would silently rewind state on a shared savepoint name). Council BLOCK 1.
    savepoint = "acquire_insert_#{:erlang.unique_integer([:positive])}"

    # Savepoint isolates a unique_violation to the INSERT (not the whole tx),
    # allowing race-recovery via re-read + re-dispatch. Without this,
    # concurrent acquires on the same surface_id leak raw Postgrex.Error
    # tuples instead of typed-absence responses — surfaced by
    # lease_acquire_concurrency_test.exs.
    Postgrex.query!(conn, "SAVEPOINT #{savepoint}", [])

    case Postgrex.query(conn, insert_lease_sql, args) do
      {:ok, %{rows: [row], columns: cols}} ->
        Postgrex.query!(conn, "RELEASE SAVEPOINT #{savepoint}", [])
        lease = EffectsAndHandoffs.row_to_map(cols, row)

        case EffectsAndHandoffs.log_event(conn, "acquire", lease) do
          :ok -> {:ok, {:ok, lease, :new}}
          {:error, e} -> {:error, e}
        end

      # Match on :unique_violation alone (not constraint name) so a future
      # rename of `surface_leases_active_unique` doesn't silently leak raw
      # errors. The `surface_leases` table has only one unique index (the
      # active-unique partial index from migration 024), so any
      # unique_violation here is the same race. Council BLOCK 2.
      {:error, %Postgrex.Error{postgres: %{code: :unique_violation}}} ->
        Postgrex.query!(conn, "ROLLBACK TO SAVEPOINT #{savepoint}", [])
        recover_race(conn, p, depth)

      {:error, e} ->
        Postgrex.query(conn, "ROLLBACK TO SAVEPOINT #{savepoint}", [])
        {:error, e}
    end
  end

  # Race recovery: re-read the winning row and re-dispatch through
  # acquire_step/3 so the existing-row branch handles same-holder
  # (idempotent) vs different-holder (held_by_other) uniformly.
  # If the winner was released between the unique_violation and our
  # re-read (extreme triple-race), loop the INSERT instead of bailing —
  # the surface is now free, retry should succeed (council BLOCK C1).
  defp recover_race(conn, p, depth) do
    case maybe_existing_active(conn, p.surface_id) do
      {:ok, %{} = winner} -> acquire_step(conn, p, winner)
      {:ok, nil} -> acquire_step_nil(conn, p, depth + 1)
      {:error, e} -> {:error, e}
    end
  end

  # Idempotent re-acquire from same holder + caller-provided substrate fields:
  # COALESCE-update substrate columns on the existing row. Mirrors the
  # COALESCE pattern in renew/3 — caller-omitted fields preserve existing
  # values; caller-provided fields overwrite. Returns the refreshed lease
  # with `:idempotent` so the caller-side response shape stays the same.
  defp update_substrate_on_idempotent(conn, existing, substrate_state, substrate_observed_at) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET substrate_state =
          COALESCE($2::jsonb, substrate_state),
        substrate_state_observed_at =
          COALESCE($3::timestamptz, substrate_state_observed_at)
    WHERE lease_id = $1 AND released_at IS NULL
    RETURNING #{@select_lease_columns}
    """

    args = [
      EffectsAndHandoffs.uuid_to_binary(existing.lease_id),
      substrate_state,
      substrate_observed_at
    ]

    case Postgrex.query(conn, sql, args) do
      {:ok, %{rows: [row], columns: cols}} ->
        refreshed = EffectsAndHandoffs.row_to_map(cols, row)
        {:ok, {:ok, refreshed, :idempotent}}

      # Lease was released between the maybe_existing_active read and our UPDATE
      # (race against the reaper). Fall through to acquire_step_nil so the
      # caller still gets a fresh acquire instead of a confusing :not_found.
      {:ok, %{rows: []}} ->
        acquire_step_nil(
          conn,
          p_from_existing(existing, substrate_state, substrate_observed_at),
          0
        )

      {:error, e} ->
        {:error, e}
    end
  end

  # Reconstruct the acquire params map from an existing-lease snapshot when
  # the idempotent UPDATE races against a release. The original acquire `p`
  # isn't in scope here, so synthesize from the existing row + caller fields.
  # Only used in the rare race-recovery path above.
  defp p_from_existing(existing, substrate_state, substrate_observed_at) do
    %{
      surface_id: existing.surface_id,
      holder_agent_uuid: existing.holder_agent_uuid,
      holder_class: existing.holder_class,
      holder_kind: existing.holder_kind,
      holder_pid: existing.holder_pid,
      ttl_s: existing.original_ttl_s,
      intent: existing.intent,
      audit_session: existing.audit_session,
      substrate_state: substrate_state,
      substrate_state_observed_at: substrate_observed_at
    }
  end

  # ---------- status ----------

  @spec status(String.t()) :: {:ok, lease | nil} | {:error, term()}
  def status(surface_id) when is_binary(surface_id) do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE surface_id = $1 AND released_at IS NULL"

    case Postgrex.query(DB, sql, [surface_id]) do
      {:ok, %{rows: []}} -> {:ok, nil}
      {:ok, %{rows: [row], columns: cols}} -> {:ok, EffectsAndHandoffs.row_to_map(cols, row)}
      {:error, e} -> {:error, e}
    end
  end

  # ---------- effect custody freshness ----------

  @doc """
  Re-read the exact leases acquired for an effect before dispatching it.
  """
  @spec check_effect_custody([map()]) :: :ok | {:error, atom()}
  defdelegate check_effect_custody(leases), to: EffectsAndHandoffs

  @doc """
  Commit a durable, exclusive reservation before pre-image capture or mutation.
  """
  def begin_effect_custody(effect_id, leases, prepare \\ fn _conn -> :ok end),
    do: EffectsAndHandoffs.begin_effect_custody(effect_id, leases, prepare)

  @doc """
  Retire only this execution's reservation after its last possible filesystem
  call.
  """
  defdelegate finish_effect_custody(effect_id, leases), to: EffectsAndHandoffs

  # ---------- identity attestation replay ledger ----------

  @doc """
  Atomically consume one operator-issued lease attestation.

  The `(issuer, jti)` primary key is the serialization point: one inserted row
  authorizes the mutation, while zero rows means the credential was already
  presented.  Store failures remain distinct so the identity gate can fail
  closed as unavailable rather than mislabeling them as invalid credentials.
  """
  @spec consume_identity_attestation(String.t(), String.t(), integer()) ::
          :ok | {:error, :replayed | term()}
  def consume_identity_attestation(issuer, jti, exp)
      when is_binary(issuer) and is_binary(jti) and is_integer(exp) do
    sql = """
    INSERT INTO lease_plane.consumed_identity_attestations (issuer, jti, expires_at)
    VALUES ($1, $2, to_timestamp($3))
    ON CONFLICT (issuer, jti) DO NOTHING
    """

    case Postgrex.query(DB, sql, [issuer, jti, exp]) do
      {:ok, %Postgrex.Result{num_rows: 1}} -> :ok
      {:ok, %Postgrex.Result{num_rows: 0}} -> {:error, :replayed}
      {:error, reason} -> {:error, reason}
    end
  end

  @spec purge_expired_identity_attestations(pos_integer()) ::
          {:ok, non_neg_integer()} | {:error, term()}
  def purge_expired_identity_attestations(limit \\ 1_000)
      when is_integer(limit) and limit > 0 and limit <= 10_000 do
    sql = """
    WITH expired AS (
      SELECT issuer, jti
      FROM lease_plane.consumed_identity_attestations
      -- Keep consumed nonces beyond token expiry so integer-second verifier
      -- time and modest app/database clock skew cannot reopen replay.
      WHERE expires_at < now() - interval '60 seconds'
      ORDER BY expires_at
      LIMIT $1
    )
    DELETE FROM lease_plane.consumed_identity_attestations AS consumed
    USING expired
    WHERE consumed.issuer = expired.issuer AND consumed.jti = expired.jti
    """

    case Postgrex.query(DB, sql, [limit]) do
      {:ok, %Postgrex.Result{num_rows: count}} -> {:ok, count}
      {:error, reason} -> {:error, reason}
    end
  end

  # ---------- renew / heartbeat ----------

  @doc """
  Aliases renew + heartbeat per RFC §4.4.2. Always extends by the
  immutable `original_ttl_s`; never accepts caller-supplied ttl.

  RFC §7.13: optional `substrate_state` (jsonb) and `substrate_state_observed_at`
  (DateTime) update the substrate columns when both are provided. Both NULL
  (default) leaves the columns at their current value (COALESCE pattern).
  Pair-coherence is enforced server-side by `substrate_state_observed_pair_coherent`
  CHECK and client-side by Pydantic; passing exactly one is undefined behavior
  and surfaces as a CHECK violation if the existing column is NULL.
  """
  @spec renew(binary()) :: :ok | {:error, term()}
  @spec renew(binary(), map() | nil, DateTime.t() | nil) :: :ok | {:error, term()}
  def renew(lease_id, substrate_state \\ nil, substrate_observed_at \\ nil) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET expires_at = now() + make_interval(secs => original_ttl_s),
        last_heartbeat_at = CASE WHEN heartbeat_required THEN now()
                                 ELSE last_heartbeat_at END,
        substrate_state =
          COALESCE($2::jsonb, substrate_state),
        substrate_state_observed_at =
          COALESCE($3::timestamptz, substrate_state_observed_at)
    WHERE lease_id = $1 AND released_at IS NULL
    RETURNING #{@select_lease_columns}
    """

    case Postgrex.transaction(DB, fn conn ->
           with {:ok, %{rows: [row], columns: cols}} <-
                  Postgrex.query(conn, sql, [
                    EffectsAndHandoffs.uuid_to_binary(lease_id),
                    substrate_state,
                    substrate_observed_at
                  ]),
                lease = EffectsAndHandoffs.row_to_map(cols, row),
                :ok <- EffectsAndHandoffs.log_event(conn, "renew", lease) do
             :ok
           else
             {:ok, %{rows: []}} -> Postgrex.rollback(conn, :not_found)
             {:error, e} -> Postgrex.rollback(conn, e)
           end
         end) do
      {:ok, :ok} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  # ---------- release ----------

  @spec release(binary(), String.t()) :: :ok | {:error, term()}
  def release(lease_id, release_reason) when is_binary(release_reason) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET released_at = now(), release_reason = $2
    WHERE lease_id = $1 AND released_at IS NULL
    RETURNING #{@select_lease_columns}
    """

    case Postgrex.transaction(DB, fn conn ->
           with {:ok, %{rows: [row], columns: cols}} <-
                  Postgrex.query(conn, sql, [
                    EffectsAndHandoffs.uuid_to_binary(lease_id),
                    release_reason
                  ]),
                lease = EffectsAndHandoffs.row_to_map(cols, row),
                :ok <-
                  EffectsAndHandoffs.log_event(
                    conn,
                    EffectsAndHandoffs.release_event_type(release_reason),
                    lease
                  ) do
             :ok
           else
             {:ok, %{rows: []}} -> Postgrex.rollback(conn, :not_found)
             {:error, e} -> Postgrex.rollback(conn, e)
           end
         end) do
      {:ok, :ok} -> :ok
      {:error, reason} -> {:error, EffectsAndHandoffs.custody_error(reason)}
    end
  end

  # ---------- handoff ----------

  @spec active_lease(binary()) :: {:ok, lease} | {:error, :not_found | term()}
  defdelegate active_lease(lease_id), to: EffectsAndHandoffs

  @spec log_handoff_offer(lease, map()) :: :ok | {:error, term()}
  defdelegate log_handoff_offer(lease, handoff), to: EffectsAndHandoffs

  @spec accept_handoff(map()) :: {:ok, lease} | {:error, term()}
  defdelegate accept_handoff(handoff), to: EffectsAndHandoffs

  # ---------- topic messages (migration 069) ----------

  @doc """
  Insert one addressed message.
  """
  @spec send_message(map()) :: {:ok, map()} | {:error, term()}
  defdelegate send_message(params), to: Messaging

  @doc """
  Atomically claim and return one recipient's pending, unexpired mail.
  """
  @spec inbox(String.t(), pos_integer()) :: {:ok, [map()]} | {:error, term()}
  defdelegate inbox(recipient_agent_uuid, limit), to: Messaging

  @doc """
  Delete expired messages, delivered or not.
  """
  @spec purge_expired_messages(pos_integer()) :: {:ok, non_neg_integer()} | {:error, term()}
  def purge_expired_messages(limit \\ 1_000), do: Messaging.purge_expired_messages(limit)

  # ---------- reaper ----------

  @spec expired_active_leases(pos_integer()) :: {:ok, [lease]} | {:error, term()}
  defdelegate expired_active_leases(limit), to: EffectsAndHandoffs

  @spec release_if_expired(binary(), String.t()) :: :ok | {:error, term()}
  defdelegate release_if_expired(lease_id, release_reason), to: EffectsAndHandoffs

  # ---------- audit outbox ----------

  @spec unforwarded_events(pos_integer(), keyword()) :: {:ok, [event]} | {:error, term()}
  def unforwarded_events(limit, opts \\ []), do: Messaging.unforwarded_events(limit, opts)

  @spec forward_outbox_event(binary()) :: :ok | {:error, term()}
  defdelegate forward_outbox_event(event_id), to: Messaging

  # ---------- governed-effect durable recording (contract §8) ----------

  @doc """
  Durably record a `record_only` governed-effect proposal to `audit.events`
  with its `effect_lane` discriminator in the payload. `audit.events` is the
  forensic sink outside the EISV predictive slice (Invariant 5); the dedicated
  `governed_effect_events` table is deferred to the Phase 4 execute-promotion
  migration. `agent_id`/`session_id` are non-secret attribution and may be nil.
  """
  @spec insert_governed_effect_event(map()) :: :ok | {:error, term()}
  def insert_governed_effect_event(%{event_type: event_type, payload: payload} = rec)
      when is_binary(event_type) and is_map(payload) do
    # audit.events is range-partitioned by `ts` (no column default); supply
    # now() explicitly or the row finds no partition (Partition key (ts) = null).
    sql = """
    INSERT INTO audit.events (ts, event_type, agent_id, session_id, payload)
    VALUES (now(), $1, $2, $3, ($4::text)::jsonb)
    """

    args = [
      event_type,
      Map.get(rec, :agent_id),
      UnitaresLeasePlane.SessionRef.reference(Map.get(rec, :session_id)),
      Jason.encode!(payload)
    ]

    case Postgrex.query(DB, sql, args) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  @doc """
  Look up the most recent governed-effect record for an `idempotency_key` within
  one `event_type` channel (defaults to the `record_only` shadow; `execute`
  passes its own channel so an irreversible spawn can never double-fire on a
  retry — the channels share no idempotency space). Returns
  `{:ok, %{idempotency_digest, payload}}` (string-keyed payload), `{:ok, nil}`
  when none exists, or `{:error, reason}`.

  The key is unindexed in `audit.events` — acceptable at shadow volume; a
  constraint-backed unique key arrives with the Phase 4 table.
  """
  @spec governed_effect_by_idempotency_key(String.t(), String.t()) ::
          {:ok, %{idempotency_digest: String.t() | nil, payload: map()}}
          | {:ok, nil}
          | {:error, term()}
  def governed_effect_by_idempotency_key(
        idempotency_key,
        event_type \\ "governed_effect.record_only"
      )
      when is_binary(idempotency_key) and is_binary(event_type) do
    sql = """
    SELECT payload->>'idempotency_digest' AS idempotency_digest, payload::text AS payload
    FROM audit.events
    WHERE event_type = $1 AND payload->>'idempotency_key' = $2
    ORDER BY ts DESC
    LIMIT 1
    """

    case Postgrex.query(DB, sql, [event_type, idempotency_key]) do
      {:ok, %{rows: []}} ->
        {:ok, nil}

      # A row whose payload has no `idempotency_digest` (manual insert, migration
      # replay, or a future code branch) is unusable for the digest comparison —
      # treat it as not-found so the caller persists fresh, rather than returning
      # a `digest: nil` map that matches none of the caller's case clauses
      # (`^digest` / `is_binary(other)` / `nil` / `{:error, _}`) and raises
      # CaseClauseError on the retry. The current writer always sets the digest;
      # this guards the corrupt/legacy-row edge.
      {:ok, %{rows: [[nil, _payload_text]]}} ->
        {:ok, nil}

      {:ok, %{rows: [[digest, payload_text]]}} ->
        {:ok,
         %{idempotency_digest: digest, payload: EffectsAndHandoffs.decode_payload(payload_text)}}

      {:error, e} ->
        {:error, e}
    end
  end

  @doc """
  Load one `record_only` governed-effect payload by its durable `effect_id`.

  This is the shadow-promotion lookup. It intentionally reads only the
  `governed_effect.record_only` channel, so an execute/audit identifier cannot
  be laundered into a promotion predecessor. Returns the string-keyed payload,
  `nil` when absent, or an error.
  """
  @spec governed_effect_by_effect_id(String.t()) ::
          {:ok, map() | nil} | {:error, term()}
  def governed_effect_by_effect_id(effect_id) when is_binary(effect_id) do
    sql = """
    SELECT payload::text
    FROM audit.events
    WHERE event_type = 'governed_effect.record_only'
      AND payload->>'effect_id' = $1
    ORDER BY ts DESC
    LIMIT 1
    """

    case Postgrex.query(DB, sql, [effect_id]) do
      {:ok, %{rows: []}} -> {:ok, nil}
      {:ok, %{rows: [[payload_text]]}} -> {:ok, EffectsAndHandoffs.decode_payload(payload_text)}
      {:error, reason} -> {:error, reason}
    end
  end
end
