defmodule UnitaresLeasePlane.DialecticSaga.Claims do
  @moduledoc """
  Saga reservation claims (`coordination.session_resolution_sagas`), resolution
  effect outbox CTE construction (`coordination.dialectic_resolution_effects`),
  reviewed-work authorship validation, and canonical operation hashing for
  `UnitaresLeasePlane.DialecticSaga`.
  """

  alias UnitaresLeasePlane.DB

  require Logger

  @inflight_states ~w(reserved paused_agent_applied both_agents_applied reverting)
  @terminal_statuses ~w(resolved failed)
  @reviewed_work_authorship_key "_dialectic_reviewed_work_authorship"
  @reviewed_work_authorship_schema "unitares.dialectic-reviewed-work-authorship.v1"
  @max_reviewed_work_author_agent_ids 32
  @canonical_agent_uuid ~r/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/
  # PostgreSQL's POSIX regex engine does not understand Elixir's \A / \z
  # anchors. Keep the two representations explicit so a valid declaration
  # reaches the guarded UPDATE instead of raising `invalid escape \ sequence`.
  @sql_canonical_agent_uuid "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

  # Default: a `reserved` saga older than this with no forward progress is
  # assumed orphaned by a crashed resolver and may be reverted so the session
  # is not permanently wedged. Resolutions complete in well under a second; a
  # 2-minute floor is far above the happy path. Only `reserved` is reclaimable —
  # later states (paused_agent_applied/…) imply real partial work and are left
  # for explicit recovery.
  @stale_reserved_seconds 120

  @doc false
  def inflight_states, do: @inflight_states

  @doc false
  def reviewed_work_authorship_schema, do: @reviewed_work_authorship_schema

  @doc false
  def sql_canonical_agent_uuid, do: @sql_canonical_agent_uuid

  @doc false
  def max_reviewed_work_author_agent_ids, do: @max_reviewed_work_author_agent_ids

  # Pre-encode to a JSON binary. Every call site MUST bind the result through
  # `($N::text)::jsonb`, never a bare `$N::jsonb`: Postgrex infers a bare jsonb
  # param as jsonb and runs its own Jason encoder over the value, so an
  # already-encoded binary lands as a jsonb *string* rather than an object.
  @doc false
  def encode_json(nil), do: nil
  def encode_json(v), do: Jason.encode!(v)

  @doc false
  def validate_reviewer_authorship(params, paused, reviewer) do
    validate_reviewer_authorship_state(Map.get(params, :paused_agent_state), paused, reviewer)
  end

  @doc false
  def validate_reviewer_authorship_state(state, paused, reviewer) when is_map(state) do
    case Map.fetch(state, @reviewed_work_authorship_key) do
      :error ->
        :ok

      {:ok,
       %{
         "schema" => @reviewed_work_authorship_schema,
         "status" => "declared",
         "authorship_verified" => false,
         "reviewed_work_author_agent_ids" => author_ids,
         "declared_by_agent_id" => declared_by
       }}
      when is_list(author_ids) and author_ids != [] and is_binary(declared_by) ->
        valid_authors =
          length(author_ids) <= @max_reviewed_work_author_agent_ids and
            Enum.all?(author_ids, &canonical_agent_uuid?/1) and
            Enum.uniq(author_ids) == author_ids

        if valid_authors and canonical_agent_uuid?(declared_by) and declared_by == paused do
          if is_nil(reviewer) or reviewer == paused or reviewer not in author_ids,
            do: :ok,
            else: {:error, :reviewer_recused}
        else
          {:error, :invalid_review_authorship}
        end

      {:ok, _malformed} ->
        {:error, :invalid_review_authorship}
    end
  end

  def validate_reviewer_authorship_state(nil, _paused, _reviewer), do: :ok

  def validate_reviewer_authorship_state(_malformed, _paused, _reviewer),
    do: {:error, :invalid_review_authorship}

  defp canonical_agent_uuid?(value) when is_binary(value),
    do: Regex.match?(@canonical_agent_uuid, value)

  defp canonical_agent_uuid?(_value), do: false

  @doc false
  def validate_resolution_authority(
        requested_status,
        stored_paused,
        stored_reviewer,
        state,
        paused_agent_id,
        reviewer_agent_id
      ) do
    cond do
      stored_paused != paused_agent_id or stored_reviewer != reviewer_agent_id ->
        {:error, :session_identity_mismatch}

      requested_status == "resolved" and stored_paused == stored_reviewer and
        is_map(state) and Map.get(state, "pause_effect_required") == true ->
        {:error, :self_review_not_authorizing}

      requested_status == "resolved" ->
        validate_reviewer_authorship_state(state, stored_paused, stored_reviewer)

      true ->
        :ok
    end
  end

  @doc false
  def classify_guard_session_phase(
        [status, _phase, _paused, _reviewer, _state, _revision, _awaiting, _updated_at],
        _expectations
      )
      when status in ["resolved", "failed"] do
    {:error, {:session_terminal, status}}
  end

  def classify_guard_session_phase(
        [
          _status,
          phase,
          stored_paused,
          stored_reviewer,
          state,
          revision,
          awaiting,
          updated_at
        ],
        %{
          paused_agent_id: paused_agent_id,
          reviewer_agent_id: reviewer_agent_id,
          requested_status: requested_status,
          expected_phase: expected_phase,
          expected_transcript_revision: expected_transcript_revision,
          expected_awaiting_facilitation: expected_awaiting_facilitation
        }
      ) do
    cond do
      not is_nil(expected_phase) and phase != expected_phase ->
        {:error, :session_phase_mismatch}

      not is_nil(expected_transcript_revision) and revision != expected_transcript_revision ->
        {:error, :session_revision_mismatch}

      not is_nil(expected_awaiting_facilitation) and awaiting != expected_awaiting_facilitation ->
        {:error, :session_facilitation_mismatch}

      requested_status == "resolved" and phase != "synthesis" ->
        {:error, :session_phase_mismatch}

      true ->
        case validate_resolution_authority(
               requested_status,
               stored_paused,
               stored_reviewer,
               state,
               paused_agent_id,
               reviewer_agent_id
             ) do
          :ok -> {:ok, %{phase: phase, updated_at: updated_at}}
          error -> error
        end
    end
  end

  @doc false
  def classify_terminal_commit_miss(
        [
          existing,
          _phase,
          _paused,
          _reviewer,
          _state,
          _revision,
          _awaiting,
          _saga_revision,
          _updated_at
        ],
        _expectations
      )
      when existing in @terminal_statuses do
    # Already terminal (raced/idempotent). The caller commits the
    # losing saga but must report the live winner, not its request.
    {:already_terminal, existing}
  end

  def classify_terminal_commit_miss(
        [
          _existing,
          stored_phase,
          stored_paused,
          stored_reviewer,
          state,
          stored_revision,
          stored_awaiting,
          stored_saga_revision,
          stored_updated_at
        ],
        %{
          status: status,
          paused_agent_id: paused_agent_id,
          reviewer_agent_id: reviewer_agent_id,
          expected_phase: expected_phase,
          expected_transcript_revision: expected_transcript_revision,
          expected_awaiting_facilitation: expected_awaiting_facilitation,
          claim_revision: claim_revision,
          claim_updated_at: claim_updated_at
        }
      ) do
    case validate_resolution_authority(
           status,
           stored_paused,
           stored_reviewer,
           state,
           paused_agent_id,
           reviewer_agent_id
         ) do
      :ok
      when not is_nil(expected_phase) and stored_phase != expected_phase ->
        {:error, :session_phase_mismatch}

      :ok
      when not is_nil(expected_transcript_revision) and
             stored_revision != expected_transcript_revision ->
        {:error, :session_revision_mismatch}

      :ok
      when not is_nil(expected_awaiting_facilitation) and
             stored_awaiting != expected_awaiting_facilitation ->
        {:error, :session_facilitation_mismatch}

      :ok when stored_saga_revision != claim_revision ->
        {:error, :saga_claim_lost}

      :ok when stored_updated_at != claim_updated_at ->
        {:error, :session_activity_mismatch}

      :ok when status == "resolved" and stored_phase != "synthesis" ->
        {:error, :session_phase_mismatch}

      :ok ->
        {:error, :session_write_refused}

      error ->
        error
    end
  end

  @doc false
  def v1_terminal_commit_sql(guarded_update_sql) when is_binary(guarded_update_sql) do
    """
    WITH terminal AS (
      #{guarded_update_sql}
    ), prepared AS (
      SELECT terminal.*,
             CASE
               WHEN COALESCE(
                 paused_agent_state_json ? 'pause_effect_required',
                 false
               )
               THEN paused_agent_state_json->'pause_effect_required' = 'true'::jsonb
               ELSE false
             END AS effect_required,
             NOT COALESCE(
               paused_agent_state_json ? 'pause_effect_required',
               false
             ) AS legacy_unbound
      FROM terminal
    ), enqueued AS (
      INSERT INTO coordination.dialectic_resolution_effects
        (session_id, paused_agent_id, reviewer_agent_id, effect_required,
         pause_token, resolution_payload_json, state,
         execution_result_json, applied_at)
      SELECT session_id, paused_agent_id, reviewer_agent_id,
             effect_required, paused_agent_state_json->>'pause_token',
             resolution_json,
             CASE WHEN effect_required THEN 'pending' ELSE 'applied' END,
             CASE WHEN effect_required THEN NULL ELSE
               jsonb_build_object(
                 'success', true,
                 'no_op', true,
                 'reason', CASE
                   WHEN legacy_unbound THEN 'legacy_unbound_session'
                   ELSE 'active_at_session_open'
                 END
               )
             END,
             CASE WHEN effect_required THEN NULL ELSE clock_timestamp() END
      FROM prepared
      WHERE $2 = 'resolved'
      RETURNING session_id
    )
    SELECT terminal.session_id
    FROM terminal
    LEFT JOIN enqueued USING (session_id)
    """
  end

  @doc false
  def commit_claimed_session(
        conn,
        saga_id,
        session_id,
        claim_revision,
        lock_session_fn,
        commit_fn
      )
      when is_function(lock_session_fn, 0) and is_function(commit_fn, 0) do
    with :ok <- lock_session_fn.(),
         :ok <- lock_reserved_claim(conn, saga_id, session_id, claim_revision) do
      case commit_fn.() do
        :written ->
          case commit_reserved_claim(conn, saga_id, claim_revision) do
            :ok -> :written
            {:error, reason} -> Postgrex.rollback(conn, reason)
          end

        {:error, reason} ->
          Postgrex.rollback(conn, reason)

        other ->
          other
      end
    else
      {:error, reason} -> {:error, reason}
    end
  end

  @doc false
  def lock_reserved_claim(conn, saga_id, session_id, claim_revision) do
    case Postgrex.query(
           conn,
           """
           SELECT 1
           FROM coordination.session_resolution_sagas
           WHERE saga_id::text = $1 AND session_id = $2
             AND claim_revision = $3 AND state = 'reserved'
           FOR UPDATE
           """,
           [saga_id, session_id, claim_revision]
         ) do
      {:ok, %{rows: [[1]]}} -> :ok
      {:ok, %{rows: []}} -> {:error, :saga_claim_lost}
      {:error, error} -> {:error, error}
    end
  end

  @doc false
  def commit_reserved_claim(conn, saga_id, claim_revision) do
    case Postgrex.query(
           conn,
           """
           UPDATE coordination.session_resolution_sagas
           SET state = 'pg_committed', pg_committed_at = now(), updated_at = now()
           WHERE saga_id::text = $1 AND claim_revision = $2 AND state = 'reserved'
           RETURNING saga_id
           """,
           [saga_id, claim_revision]
         ) do
      {:ok, %{num_rows: 1}} -> :ok
      {:ok, %{num_rows: 0}} -> {:error, :saga_claim_lost}
      {:error, error} -> {:error, error}
    end
  end

  @doc false
  def reclaim_stale_reserved(conn, session_id) do
    sql = """
    UPDATE coordination.session_resolution_sagas
    SET state = 'reverted', reverted_at = now(), updated_at = now()
    WHERE session_id = $1 AND state = 'reserved'
      AND last_attempt_at < now() - ($2 || ' seconds')::interval
    """

    case Postgrex.query(conn, sql, [session_id, Integer.to_string(@stale_reserved_seconds)]) do
      {:ok, %{num_rows: n}} when n > 0 ->
        Logger.warning(
          "dialectic_saga: reclaimed #{n} stale reserved saga(s) for session #{String.slice(session_id, 0, 16)} (assumed orphaned)"
        )

        :ok

      {:ok, _} ->
        :ok

      {:error, e} ->
        {:error, e}
    end
  end

  @doc false
  def revert_reserved_claim(saga_id) do
    sql = """
    UPDATE coordination.session_resolution_sagas
    SET state = 'reverted', reverted_at = now(), updated_at = now()
    WHERE saga_id::text = $1 AND state = 'reserved'
    """

    case Postgrex.query(DB, sql, [saga_id]) do
      {:ok, %{num_rows: 1}} ->
        :ok

      {:ok, %{num_rows: 0}} ->
        # Cleanup is idempotent when a reaper or another deterministic refusal
        # already reverted this generation. Forward states still refuse so a
        # committed decision can never be unwound.
        case Postgrex.query(
               DB,
               "SELECT state FROM coordination.session_resolution_sagas WHERE saga_id::text = $1",
               [saga_id]
             ) do
          {:ok, %{rows: [["reverted"]]}} -> :ok
          {:ok, _} -> {:error, :saga_cleanup_refused}
          {:error, error} -> {:error, error}
        end

      {:error, error} ->
        {:error, error}
    end
  end

  @doc false
  def revert_reserved_session_claims(conn, session_id) do
    case Postgrex.query(
           conn,
           """
           UPDATE coordination.session_resolution_sagas
           SET state = 'reverted', reverted_at = now(), updated_at = now()
           WHERE session_id = $1 AND state = 'reserved'
           """,
           [session_id]
         ) do
      {:ok, _} -> :ok
      {:error, error} -> {:error, error}
    end
  end

  @doc false
  def reclaim_all_stale do
    sql = """
    UPDATE coordination.session_resolution_sagas
    SET state = 'reverted', reverted_at = now(), updated_at = now()
    WHERE state = 'reserved'
      AND last_attempt_at < now() - ($1 || ' seconds')::interval
    """

    case Postgrex.query(DB, sql, [Integer.to_string(@stale_reserved_seconds)]) do
      {:ok, %{num_rows: n}} -> {:ok, n}
      {:error, e} -> {:error, e}
    end
  end

  @doc false
  def get_inflight(session_id) when is_binary(session_id) do
    sql = """
    SELECT saga_id::text FROM coordination.session_resolution_sagas
    WHERE session_id = $1 AND state = ANY($2)
    LIMIT 1
    """

    case Postgrex.query(DB, sql, [session_id, @inflight_states]) do
      {:ok, %{rows: [[saga_id]]}} -> {:ok, saga_id}
      {:ok, %{rows: []}} -> {:ok, nil}
      {:error, e} -> {:error, e}
    end
  end

  @doc false
  def reserve_claim(
        conn,
        session_id,
        paused_agent_id,
        reviewer_agent_id,
        json,
        hash,
        expected_updated_at,
        locked_updated_at,
        bump_revision_fn
      )
      when is_function(bump_revision_fn, 0) do
    # The session row is already locked by guard_session_phase/8. Read and
    # lock any live claim before allocating a new generation, preserving the
    # global session -> saga lock order used by terminal commit.
    inflight_sql = """
    SELECT saga_id::text, resolution_payload_hash, paused_agent_id,
           reviewer_agent_id, claim_revision, claim_updated_at
    FROM coordination.session_resolution_sagas
    WHERE session_id = $1
      AND state IN ('reserved', 'paused_agent_applied',
                    'both_agents_applied', 'reverting')
    LIMIT 1
    FOR UPDATE
    """

    case Postgrex.query(conn, inflight_sql, [session_id]) do
      {:ok,
       %{
         rows: [
           [
             saga_id,
             ^hash,
             ^paused_agent_id,
             ^reviewer_agent_id,
             revision,
             claim_updated_at
           ]
         ]
       }}
      when is_integer(revision) and revision > 0 and not is_nil(claim_updated_at) ->
        {:ok,
         %{
           saga_id: saga_id,
           origin: :idempotent,
           claim_revision: revision,
           claim_updated_at: claim_updated_at
         }}

      {:ok, %{rows: [_]}} ->
        {:error, :saga_in_flight}

      {:ok, %{rows: []}}
      when not is_nil(expected_updated_at) and
             locked_updated_at != expected_updated_at ->
        # Check the caller's activity frontier only when allocating a new
        # generation. An exact live claim is an idempotent replay even though
        # that claim itself advanced updated_at; terminal commit still binds
        # claim_updated_at and refuses any activity after the reservation.
        {:error, :session_activity_mismatch}

      {:ok, %{rows: []}} ->
        insert_new_claim(
          conn,
          session_id,
          paused_agent_id,
          reviewer_agent_id,
          json,
          hash,
          bump_revision_fn
        )

      {:error, error} ->
        {:error, error}
    end
  end

  defp insert_new_claim(
         conn,
         session_id,
         paused_agent_id,
         reviewer_agent_id,
         json,
         hash,
         bump_revision_fn
       ) do
    with {:ok, {revision, updated_at}} <- bump_revision_fn.(),
         {:ok, %{rows: [[saga_id]]}} <-
           Postgrex.query(
             conn,
             """
             INSERT INTO coordination.session_resolution_sagas
               (saga_id, session_id, paused_agent_id, reviewer_agent_id, state,
                resolution_payload_json, resolution_payload_hash,
                claim_revision, claim_updated_at, last_attempt_at, attempt_count)
             VALUES (gen_random_uuid(), $1, $2, $3, 'reserved',
                     ($4::text)::jsonb, $5, $6, $7, now(), 1)
             RETURNING saga_id::text
             """,
             [
               session_id,
               paused_agent_id,
               reviewer_agent_id,
               json,
               hash,
               revision,
               updated_at
             ]
           ) do
      {:ok,
       %{
         saga_id: saga_id,
         origin: :new,
         claim_revision: revision,
         claim_updated_at: updated_at
       }}
    else
      {:error, error} -> {:error, error}
    end
  end

  @doc false
  def build_liveness_snapshot([
        status,
        paused,
        reviewer,
        inactive_s,
        phase,
        awaiting,
        revision,
        updated_at,
        verdict_msg,
        replies,
        agreed
      ]) do
    %{
      status: status,
      paused_agent_id: paused,
      reviewer_agent_id: reviewer,
      inactive_seconds: inactive_s,
      # Carried so a reap can say WHICH kind of stall it ended. Without
      # these the sweeper writes a verdict-free row and every reader
      # reconstructs "the agent walked away" — see fail_stuck/2.
      phase: phase,
      awaiting_facilitation: awaiting == true,
      transcript_revision: revision,
      updated_at: updated_at,
      standing_verdict: if(is_nil(verdict_msg), do: "none", else: "reject"),
      verdict_message_id: verdict_msg,
      verdict_acceptance: verdict_acceptance(verdict_msg, replies, agreed)
    }
  end

  # accepted   -- the paused agent posted after the verdict and agreed
  # contested  -- it posted after the verdict and did not agree
  # no_reply   -- it never posted after the verdict
  # not_applicable -- no standing rejection to accept or contest
  #
  # Descriptive only. `no_reply` in particular must NOT be read as acceptance:
  # the common cause is that the agent's session had already ended, not that it
  # assented. Nothing may terminate on this field.
  defp verdict_acceptance(nil, _replies, _agreed), do: "not_applicable"
  defp verdict_acceptance(_msg, replies, _agreed) when replies in [nil, 0], do: "no_reply"
  defp verdict_acceptance(_msg, _replies, true), do: "accepted"
  defp verdict_acceptance(_msg, _replies, _agreed), do: "contested"

  # Deterministic payload hash: recursively sort map keys, encoding as a stable
  # array-of-pairs structure so the same logical resolution always hashes
  # identically (the dedup key). We hash a canonical *representation*, not a
  # round-trippable object, so no Jason.OrderedObject dependency is needed.
  # NOTE: BEAM-internal idempotency only; byte-parity with Python's HMAC
  # canonical_payload is a separate, later concern (architect M2).
  @doc false
  def payload_hash(payload) when is_map(payload) do
    payload
    |> canonical()
    |> Jason.encode!()
    |> then(&:crypto.hash(:sha256, &1))
    |> Base.encode16(case: :lower)
  end

  # Saga idempotency is identity of the whole terminal operation, not merely
  # the JSON stored in resolution_json. The same payload sent as `failed`
  # versus `resolved`, or through the effect-owning v2 route versus legacy v1,
  # must contend rather than join one another's reservation.
  @doc false
  def operation_hash(payload, status, effect_protocol) do
    payload_hash(%{
      "resolution_payload" => payload,
      "status" => status,
      "effect_protocol" => effect_protocol
    })
  end

  defp canonical(m) when is_map(m) do
    m
    |> Enum.map(fn {k, v} -> [to_string(k), canonical(v)] end)
    |> Enum.sort_by(fn [k, _] -> k end)
  end

  defp canonical(list) when is_list(list), do: Enum.map(list, &canonical/1)
  defp canonical(other), do: other
end
