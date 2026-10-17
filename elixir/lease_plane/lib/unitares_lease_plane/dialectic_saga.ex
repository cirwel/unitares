defmodule UnitaresLeasePlane.DialecticSaga do
  @moduledoc """
  Cross-runtime serialization primitive for the dialectic SYNTHESIS->RESOLVED
  transition (BEAM dialectic-on-BEAM, "Slice 1", council 2026-06-28).

  This is the BEAM-side terminal-resolution state machine over
  `coordination.session_resolution_sagas`. It is the foundation the forthcoming
  `SessionServer` GenServer drives; the saga row is a durable crash-recovery
  log + idempotency gate, NOT a mutex (the GenServer mailbox serializes within
  BEAM; this gate serializes across runtimes and survives restarts).

  Two invariants enforced here:

    * **Phase guard** — a saga is only claimable for a session that exists and is
      not already terminal (`resolved` / `failed`), and a `resolved` transition
      must still be in `synthesis`. This defends against a retried or stale
      resolve minting a stray saga or closing a row that moved backward.

    * **One in-flight saga per session** — the partial unique index
      `idx_saga_one_pending_per_session` makes a second concurrent claim fail;
      we map that to `{:error, :saga_in_flight}`. A claim that repeats the same
      terminal operation — payload, requested terminal status, effect protocol,
      participants, and positive claim generation — is an idempotent replay and
      returns the existing saga.

  `resolve/1` is the only public terminal commit: it changes the session row and
  saga state in one PostgreSQL transaction. The historical split `commit/1`
  entrypoint fails closed so it cannot free a reservation while leaving the
  session active.

  Same `governance` database as the lease plane, so `UnitaresLeasePlane.DB`
  reaches the `coordination` and `core` schemas (cf. `GovernedEffect` writing
  `audit.events`).
  """

  alias UnitaresLeasePlane.DB

  require Logger

  @inflight_states ~w(reserved paused_agent_applied both_agents_applied reverting)
  @reviewed_work_authorship_key "_dialectic_reviewed_work_authorship"
  @reviewed_work_authorship_schema "unitares.dialectic-reviewed-work-authorship.v1"
  @max_reviewed_work_author_agent_ids 32
  @canonical_agent_uuid ~r/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/
  # PostgreSQL's POSIX regex engine does not understand Elixir's \A / \z
  # anchors. Keep the two representations explicit so a valid declaration
  # reaches the guarded UPDATE instead of raising `invalid escape \ sequence`.
  @sql_canonical_agent_uuid "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

  @type claim_ok ::
          {:ok,
           %{
             saga_id: String.t(),
             origin: :new | :idempotent,
             claim_revision: non_neg_integer(),
             claim_updated_at: DateTime.t()
           }}
  @type claim_err ::
          {:error, :session_not_found}
          | {:error, {:session_terminal, String.t()}}
          | {:error, :session_identity_mismatch}
          | {:error, :session_phase_mismatch}
          | {:error, :session_revision_mismatch}
          | {:error, :session_facilitation_mismatch}
          | {:error, :session_activity_mismatch}
          | {:error, :saga_claim_lost}
          | {:error, :self_review_not_authorizing}
          | {:error, :invalid_review_authorship}
          | {:error, :reviewer_recused}
          | {:error, :saga_in_flight}
          | {:error, term()}

  @doc """
  Claim a resolution saga slot for `session_id`.

  `params` requires:
    * `:session_id` (text)
    * `:paused_agent_id` (text)
    * `:reviewer_agent_id` (text — the saga table requires it NOT NULL)
    * `:resolution_payload` (map — the candidate resolution; hashed for dedup)

  Returns:
    * `{:ok, %{saga_id: id, origin: :new}}` — slot freshly reserved
    * `{:ok, %{saga_id: id, origin: :idempotent}}` — same terminal operation already claimed
    * `{:error, :session_not_found}`
    * `{:error, {:session_terminal, status}}` — session already resolved/failed
    * `{:error, :saga_in_flight}` — a different in-flight saga holds the session
  """
  @spec claim(map()) :: claim_ok() | claim_err()
  def claim(
        %{
          session_id: session_id,
          paused_agent_id: paused_agent_id,
          reviewer_agent_id: reviewer_agent_id,
          resolution_payload: payload
        } = params
      )
      when is_binary(session_id) and is_binary(paused_agent_id) and
             is_binary(reviewer_agent_id) and is_map(payload) do
    status = Map.get(params, :status, "resolved")
    effect_protocol = Map.get(params, :effect_protocol)
    hash = operation_hash(payload, status, effect_protocol)
    json = Jason.encode!(payload)

    Postgrex.transaction(DB, fn conn ->
      case guard_session_phase(
             conn,
             session_id,
             paused_agent_id,
             reviewer_agent_id,
             status,
             Map.get(params, :expected_phase),
             Map.get(params, :expected_transcript_revision),
             Map.get(params, :expected_awaiting_facilitation)
           ) do
        {:ok, %{updated_at: locked_updated_at}} ->
          with :ok <- reclaim_stale_reserved(conn, session_id),
               {:ok, result} <-
                 reserve_claim(
                   conn,
                   session_id,
                   paused_agent_id,
                   reviewer_agent_id,
                   json,
                   hash,
                   Map.get(params, :expected_updated_at),
                   locked_updated_at
                 ) do
            result
          else
            {:error, reason} -> Postgrex.rollback(conn, reason)
          end

        {:error, {:session_terminal, terminal_status}} ->
          # The session row is still locked by guard_session_phase/8. Clean
          # reservations before releasing that lock, so a sanctioned reopen
          # and its fresh claim cannot land between the terminal observation
          # and a later broad cleanup that would revoke the new generation.
          case revert_reserved_session_claims(conn, session_id) do
            :ok -> {:session_terminal, terminal_status}
            {:error, reason} -> Postgrex.rollback(conn, reason)
          end

        {:error, reason} ->
          Postgrex.rollback(conn, reason)
      end
    end)
    |> case do
      {:ok, {:session_terminal, terminal_status}} ->
        {:error, {:session_terminal, terminal_status}}

      {:ok, result} ->
        {:ok, result}

      {:error, reason} ->
        {:error, reason}
    end
  end

  def claim(_), do: {:error, :invalid_params}

  # Default: a `reserved` saga older than this with no forward progress is
  # assumed orphaned by a crashed resolver and may be reverted so the session
  # is not permanently wedged. Resolutions complete in well under a second; a
  # 2-minute floor is far above the happy path. Only `reserved` is reclaimable —
  # later states (paused_agent_applied/…) imply real partial work and are left
  # for explicit recovery.
  @stale_reserved_seconds 120

  @doc """
  End-to-end BEAM-owned resolve of the SYNTHESIS->RESOLVED transition.

  BEAM owns two things here: the cross-runtime serialization slot (the saga)
  and the single write of the terminal session row. The resolution payload is
  computed Python-side (synthesis convergence + agent-state mutation stay in
  Python); BEAM is handed the finished payload and is the authority that commits
  it. Steps: claim saga -> guarded write of `core.dialectic_sessions` -> commit
  saga. Idempotent throughout.

    * `{:ok, %{status: "resolved", saga_id: id, origin: :new | :idempotent}}`
    * `{:ok, %{status: terminal_status, saga_id: nil, origin: :already_terminal}}`
    * `{:error, :saga_in_flight}` — a live resolve already holds the session
    * `{:error, :session_not_found}` / other `{:error, term}`
  """
  @terminal_statuses ~w(resolved failed)
  @deterministic_post_claim_refusals [
    :session_not_found,
    :session_identity_mismatch,
    :session_phase_mismatch,
    :session_revision_mismatch,
    :session_facilitation_mismatch,
    :session_activity_mismatch,
    :saga_claim_lost,
    :session_write_refused,
    :self_review_not_authorizing,
    :invalid_review_authorship,
    :reviewer_recused
  ]
  @deterministic_preclaim_refusals [
    :session_identity_mismatch,
    :session_phase_mismatch,
    :session_revision_mismatch,
    :session_facilitation_mismatch,
    :session_activity_mismatch,
    :self_review_not_authorizing,
    :invalid_review_authorship,
    :reviewer_recused
  ]

  @spec resolve(map()) :: {:ok, map()} | {:error, term()}
  def resolve(%{session_id: session_id, resolution_payload: payload} = params)
      when is_binary(session_id) and is_map(payload) do
    status = Map.get(params, :status, "resolved")

    if status in @terminal_statuses do
      do_resolve(params, session_id, payload, status)
    else
      {:error, :invalid_status}
    end
  end

  def resolve(_), do: {:error, :invalid_params}

  @doc """
  BEAM-owned dialectic session creation: guarded INSERT into
  `core.dialectic_sessions` + immediately start a liveness watcher, so a session
  is watched from birth rather than waiting for the 30s reconciler. Idempotent on
  `session_id` (`ON CONFLICT DO NOTHING`): a duplicate returns `{:ok, :exists}`.

  Python still computes the `session_id` and field values (it owns id generation
  + the request semantics); BEAM owns the write + the watcher start. Required:
  `:session_id`, `:paused_agent_id`. Phase/status default to thesis/active (what
  Python sets on create); all other fields are optional.
  """
  @spec create_session(map()) :: {:ok, :created | :exists} | {:error, term()}
  def create_session(%{session_id: sid, paused_agent_id: paused} = p)
      when is_binary(sid) and byte_size(sid) > 0 and is_binary(paused) and byte_size(paused) > 0 do
    sql = """
    INSERT INTO core.dialectic_sessions
      (session_id, paused_agent_id, reviewer_agent_id, phase, status,
       session_type, topic, reason, discovery_id, dispute_type,
       max_synthesis_rounds, synthesis_round, paused_agent_state_json, trigger_source)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,($13::text)::jsonb,$14)
    ON CONFLICT (session_id) DO NOTHING
    RETURNING session_id
    """

    args = [
      sid,
      paused,
      Map.get(p, :reviewer_agent_id),
      Map.get(p, :phase, "thesis"),
      Map.get(p, :status, "active"),
      Map.get(p, :session_type),
      Map.get(p, :topic),
      Map.get(p, :reason),
      Map.get(p, :discovery_id),
      Map.get(p, :dispute_type),
      Map.get(p, :max_synthesis_rounds),
      Map.get(p, :synthesis_round, 0),
      encode_json(Map.get(p, :paused_agent_state)),
      Map.get(p, :trigger_source)
    ]

    with :ok <- validate_reviewer_authorship(p, paused, Map.get(p, :reviewer_agent_id)) do
      case Postgrex.query(DB, sql, args) do
        {:ok, %{rows: [[_sid]]}} ->
          UnitaresLeasePlane.DialecticLivenessSupervisor.ensure_started(sid)
          {:ok, :created}

        {:ok, %{rows: []}} ->
          {:ok, :exists}

        {:error, e} ->
          {:error, e}
      end
    end
  end

  def create_session(_), do: {:error, :invalid_params}

  # Pre-encode to a JSON binary. Every call site MUST bind the result through
  # `($N::text)::jsonb`, never a bare `$N::jsonb`: Postgrex infers a bare jsonb
  # param as jsonb and runs its own Jason encoder over the value, so an
  # already-encoded binary lands as a jsonb *string* rather than an object.
  # That double-encoding silently corrupted every jsonb this module wrote
  # between 2026-06-28 and 2026-08-10 (`resolution_json`, `paused_agent_state_json`,
  # `resolution_payload_json` — 90 rows; `->>` on them returned NULL). The
  # `::text` hop forces the binary in as text so Postgres parses it. Same idiom
  # as `UnitaresLeasePlane.Repo.insert_tool_usage/2`.
  defp encode_json(nil), do: nil
  defp encode_json(v), do: Jason.encode!(v)

  @non_terminal_phases ~w(awaiting_thesis thesis antithesis synthesis quorum_voting)

  @doc """
  Guarded non-terminal phase advance (thesis -> antithesis -> synthesis ...) —
  BEAM as sole writer of `core.dialectic_sessions.phase` for intermediate
  transitions too, not just creation + the terminal write. Refuses to touch an
  already-terminal session (those move only via `resolve/1`) and only accepts a
  non-terminal target phase. Idempotent: a no-op UPDATE still returns `:ok`.
  """
  @spec update_phase(String.t(), String.t()) ::
          :ok | {:error, :invalid_phase | :session_not_found | term()}
  def update_phase(session_id, phase), do: update_phase(session_id, phase, nil)

  @spec update_phase(String.t(), String.t(), non_neg_integer() | nil) ::
          :ok | {:error, :invalid_phase | :session_not_found | term()}

  @doc """
  Advance a non-terminal phase, and the synthesis round when one is supplied.

  `synthesis_round` is COALESCEd, so `nil` leaves the stored value untouched and
  a caller that predates the field behaves exactly as before. Until 2026-08-16
  this statement wrote `phase` only while Python incremented the round in
  memory, so every row read `synthesis_round = 0` no matter how many synthesis
  messages the session carried.
  """
  def update_phase(session_id, phase, synthesis_round)
      when is_binary(session_id) and phase in @non_terminal_phases and
             (is_nil(synthesis_round) or (is_integer(synthesis_round) and synthesis_round >= 0)) do
    sql = """
    UPDATE core.dialectic_sessions
    SET phase = $2,
        synthesis_round = COALESCE($3, synthesis_round),
        updated_at = now()
    WHERE session_id = $1 AND status NOT IN ('resolved', 'failed', 'escalated')
    RETURNING session_id
    """

    case Postgrex.query(DB, sql, [session_id, phase, synthesis_round]) do
      {:ok, %{num_rows: 1}} ->
        :ok

      {:ok, %{num_rows: 0}} ->
        # No row updated: session missing or already terminal. Distinguish so the
        # caller can fall back vs treat as a benign no-op.
        case Postgrex.query(DB, "SELECT 1 FROM core.dialectic_sessions WHERE session_id = $1", [
               session_id
             ]) do
          {:ok, %{num_rows: 1}} -> :ok
          {:ok, %{num_rows: 0}} -> {:error, :session_not_found}
          {:error, e} -> {:error, e}
        end

      {:error, e} ->
        {:error, e}
    end
  end

  def update_phase(_session_id, _phase, _synthesis_round), do: {:error, :invalid_phase}

  @doc """
  Guarded reviewer assignment/reassignment — the last session-row column written
  Python-side. BEAM as sole writer of `reviewer_agent_id` too. Refuses an
  already-terminal session; missing session -> :session_not_found; otherwise :ok.
  """
  @spec update_reviewer(String.t(), String.t()) ::
          :ok
          | {:error,
             :invalid_reviewer
             | :invalid_review_authorship
             | :reviewer_recused
             | :reviewer_write_refused
             | :session_not_found
             | :session_terminal
             | term()}
  def update_reviewer(session_id, reviewer)
      when is_binary(session_id) and is_binary(reviewer) and byte_size(reviewer) > 0,
      do: update_reviewer(session_id, reviewer, false)

  def update_reviewer(_session_id, _reviewer), do: {:error, :invalid_reviewer}

  @spec update_reviewer(String.t(), String.t(), boolean()) ::
          :ok
          | {:error,
             :invalid_reviewer
             | :invalid_review_authorship
             | :reviewer_recused
             | :reviewer_write_refused
             | :session_not_found
             | :session_terminal
             | term()}
  def update_reviewer(session_id, reviewer, claim_if_unassigned)
      when is_binary(session_id) and is_binary(reviewer) and byte_size(reviewer) > 0 and
             is_boolean(claim_if_unassigned) do
    open_slot_guard =
      if claim_if_unassigned, do: "AND reviewer_agent_id IS NULL", else: ""

    sql = """
    UPDATE core.dialectic_sessions
    SET reviewer_agent_id = $2, updated_at = now()
    WHERE session_id = $1 AND status NOT IN ('resolved', 'failed', 'escalated')
      #{open_slot_guard}
      AND (
        paused_agent_state_json IS NULL
        OR NOT (paused_agent_state_json ? '#{@reviewed_work_authorship_key}')
        OR (
          jsonb_typeof(paused_agent_state_json -> '#{@reviewed_work_authorship_key}') = 'object'
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},schema}' = $3
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},status}' = 'declared'
          AND paused_agent_state_json #> '{#{@reviewed_work_authorship_key},authorship_verified}' = 'false'::jsonb
          AND jsonb_typeof(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},declared_by_agent_id}') = 'string'
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},declared_by_agent_id}' ~ $4
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},declared_by_agent_id}' = paused_agent_id
          AND CASE
            WHEN jsonb_typeof(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') = 'array'
            THEN
              jsonb_array_length(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') BETWEEN 1 AND $5
              AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') AS author(value)
                WHERE jsonb_typeof(author.value) <> 'string'
                   OR (author.value #>> '{}') !~ $4
              )
              AND jsonb_array_length(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') = (
                SELECT count(DISTINCT author.value #>> '{}')
                FROM jsonb_array_elements(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') AS author(value)
              )
              AND (
                $2 = paused_agent_id
                OR NOT (
                  paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}' ? $2
                )
              )
            ELSE FALSE
          END
        )
      )
    RETURNING session_id
    """

    case Postgrex.query(DB, sql, [
           session_id,
           reviewer,
           @reviewed_work_authorship_schema,
           @sql_canonical_agent_uuid,
           @max_reviewed_work_author_agent_ids
         ]) do
      {:ok, %{num_rows: 1}} ->
        :ok

      {:ok, %{num_rows: 0}} ->
        # The guarded UPDATE matched nothing. Either the session is gone, or it
        # exists and is terminal — the WHERE clause excludes resolved/failed/
        # escalated rows deliberately (the dual-writer guard).
        #
        # ⛔This branch returned `:ok` for the terminal case until 2026-08-22,
        # which reported a successful reviewer write when NOTHING was written.
        # Python's caller treats 200/ok as persisted and emits
        # `dialectic_reviewer_reassigned` on it, so a terminal row inflated the
        # §11 criterion-10 reassignment count with reassignments that never
        # happened — the same over-count #1804 fixed on the Python write tail,
        # still open here. Existence is not a write.
        case Postgrex.query(
               DB,
               "SELECT status, paused_agent_id, paused_agent_state_json FROM core.dialectic_sessions WHERE session_id = $1",
               [session_id]
             ) do
          {:ok, %{rows: [[status, _paused, _state]]}}
          when status in ~w(resolved failed escalated) ->
            {:error, :session_terminal}

          {:ok, %{rows: [[_status, paused, state]]}} ->
            case validate_reviewer_authorship_state(state, paused, reviewer) do
              :ok -> {:error, :reviewer_write_refused}
              error -> error
            end

          {:ok, %{num_rows: 0}} ->
            {:error, :session_not_found}

          {:error, e} ->
            {:error, e}
        end

      {:error, e} ->
        {:error, e}
    end
  end

  def update_reviewer(_session_id, _reviewer, _claim_if_unassigned),
    do: {:error, :invalid_reviewer}

  defp validate_reviewer_authorship(params, paused, reviewer) do
    validate_reviewer_authorship_state(Map.get(params, :paused_agent_state), paused, reviewer)
  end

  defp validate_reviewer_authorship_state(state, paused, reviewer) when is_map(state) do
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

  defp validate_reviewer_authorship_state(nil, _paused, _reviewer), do: :ok

  defp validate_reviewer_authorship_state(_malformed, _paused, _reviewer),
    do: {:error, :invalid_review_authorship}

  defp canonical_agent_uuid?(value) when is_binary(value),
    do: Regex.match?(@canonical_agent_uuid, value)

  defp canonical_agent_uuid?(_value), do: false

  defp do_resolve(params, session_id, payload, status) do
    case claim(params) do
      {:ok,
       %{
         saga_id: saga_id,
         origin: origin,
         claim_revision: claim_revision,
         claim_updated_at: claim_updated_at
       }} ->
        case commit_session_row(
               session_id,
               payload,
               status,
               Map.get(params, :paused_agent_id),
               Map.get(params, :reviewer_agent_id),
               Map.get(params, :expected_phase),
               Map.get(params, :expected_transcript_revision),
               Map.get(params, :expected_awaiting_facilitation),
               saga_id,
               claim_revision,
               claim_updated_at,
               Map.get(params, :effect_protocol)
             ) do
          :written ->
            {:ok, %{status: status, saga_id: saga_id, origin: origin}}

          {:already_terminal, existing_status} ->
            # A non-saga writer won after claim. Release this reservation and
            # report the actual terminal row; never attribute it to this call.
            with :ok <- revert_reserved_claim(saga_id) do
              {:ok,
               %{
                 status: existing_status,
                 saga_id: nil,
                 origin: :already_terminal
               }}
            end

          {:error, reason} = error when reason in @deterministic_post_claim_refusals ->
            # The claim committed in an earlier transaction, but this refusal
            # proves the terminal statement wrote nothing. Release this exact
            # reservation immediately so a corrected decision does not sit
            # behind the orphan-reclaim timeout. Unknown database/transport
            # errors stay reserved for conservative recovery.
            case revert_reserved_claim(saga_id) do
              :ok -> error
              {:error, cleanup_error} -> {:error, cleanup_error}
            end

          error ->
            error
        end

      {:error, {:session_terminal, existing}} ->
        # Already terminal: nothing to write. claim/1 cleaned stale reserved
        # generations in the same transaction that observed terminal state,
        # while still holding the session-row lock.
        {:ok, %{status: existing, saga_id: nil, origin: :already_terminal}}

      {:error, reason} = error when reason in @deterministic_preclaim_refusals ->
        # No reservation was returned to this invocation, so it owns nothing
        # to clean up. Same-payload generations are intentionally repeatable;
        # selecting by payload/participants here could revoke a different
        # invocation's current generation. Exact post-claim cleanup is keyed
        # by the saga_id returned above.
        error

      {:error, reason} ->
        {:error, reason}
    end
  end

  # Guarded terminal write of the session row — BEAM is the sole writer for both
  # terminal transitions (resolved AND failed). Mirrors the Python B-4 guard
  # (#1171): refuses to overwrite an already-terminal row. phase tracks status.
  # A resolved write and its Python runtime-effect handoff are one PostgreSQL
  # statement on the versioned route. The legacy route remains a genuinely
  # separate query for application rollback compatibility after migration 075:
  # PostgreSQL resolves every relation while parsing a query, so a conditional
  # CTE that merely skips the INSERT is not a compatibility boundary. Migrations
  # 075 (effect outbox), 077 (transcript frontier), and 078 (saga generation)
  # are hard prerequisites for deploying this versioned writer.
  defp commit_session_row(
         session_id,
         payload,
         status,
         paused_agent_id,
         reviewer_agent_id,
         expected_phase,
         expected_transcript_revision,
         expected_awaiting_facilitation,
         saga_id,
         claim_revision,
         claim_updated_at,
         "v1"
       ) do
    sql = """
    WITH terminal AS (
      #{guarded_terminal_update_sql("session_id, paused_agent_id, reviewer_agent_id, paused_agent_state_json, resolution_json")}
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

    commit_claimed_session_row(
      sql,
      session_id,
      payload,
      status,
      paused_agent_id,
      reviewer_agent_id,
      expected_phase,
      expected_transcript_revision,
      expected_awaiting_facilitation,
      saga_id,
      claim_revision,
      claim_updated_at
    )
  end

  defp commit_session_row(
         session_id,
         payload,
         status,
         paused_agent_id,
         reviewer_agent_id,
         expected_phase,
         expected_transcript_revision,
         expected_awaiting_facilitation,
         saga_id,
         claim_revision,
         claim_updated_at,
         _legacy_protocol
       ) do
    commit_claimed_session_row(
      guarded_terminal_update_sql("session_id"),
      session_id,
      payload,
      status,
      paused_agent_id,
      reviewer_agent_id,
      expected_phase,
      expected_transcript_revision,
      expected_awaiting_facilitation,
      saga_id,
      claim_revision,
      claim_updated_at
    )
  end

  defp guarded_terminal_update_sql(returning_columns) do
    """
    UPDATE core.dialectic_sessions
    SET status = $2, phase = $2, resolution_json = ($3::text)::jsonb,
        awaiting_facilitation = false, updated_at = now()
    WHERE session_id = $1 AND status NOT IN ('resolved', 'failed')
      AND ($2 <> 'resolved' OR phase = 'synthesis')
      AND paused_agent_id = $4
      AND reviewer_agent_id = $5
      AND ($9::text IS NULL OR phase IS NOT DISTINCT FROM $9)
      AND ($10::bigint IS NULL OR transcript_revision = $10)
      AND ($11::boolean IS NULL OR awaiting_facilitation IS NOT DISTINCT FROM $11)
      AND resolution_saga_revision = $12
      AND updated_at IS NOT DISTINCT FROM $13
      AND (
        $2 <> 'resolved'
        OR paused_agent_state_json IS NULL
        OR paused_agent_state_json->'pause_effect_required'
             IS DISTINCT FROM 'true'::jsonb
        OR reviewer_agent_id IS DISTINCT FROM paused_agent_id
      )
      AND (
        $2 <> 'resolved'
        OR paused_agent_state_json IS NULL
        OR NOT (paused_agent_state_json ? '#{@reviewed_work_authorship_key}')
        OR (
          jsonb_typeof(paused_agent_state_json -> '#{@reviewed_work_authorship_key}') = 'object'
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},schema}' = $6
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},status}' = 'declared'
          AND paused_agent_state_json #> '{#{@reviewed_work_authorship_key},authorship_verified}' = 'false'::jsonb
          AND jsonb_typeof(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},declared_by_agent_id}') = 'string'
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},declared_by_agent_id}' ~ $7
          AND paused_agent_state_json #>> '{#{@reviewed_work_authorship_key},declared_by_agent_id}' = paused_agent_id
          AND CASE
            WHEN jsonb_typeof(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') = 'array'
            THEN
              jsonb_array_length(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') BETWEEN 1 AND $8
              AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') AS author(value)
                WHERE jsonb_typeof(author.value) <> 'string'
                   OR (author.value #>> '{}') !~ $7
              )
              AND jsonb_array_length(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') = (
                SELECT count(DISTINCT author.value #>> '{}')
                FROM jsonb_array_elements(paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}') AS author(value)
              )
              AND (
                reviewer_agent_id = paused_agent_id
                OR NOT (
                  paused_agent_state_json #> '{#{@reviewed_work_authorship_key},reviewed_work_author_agent_ids}' ? reviewer_agent_id
                )
              )
            ELSE FALSE
          END
        )
      )
    RETURNING #{returning_columns}
    """
  end

  defp commit_claimed_session_row(
         sql,
         session_id,
         payload,
         status,
         paused_agent_id,
         reviewer_agent_id,
         expected_phase,
         expected_transcript_revision,
         expected_awaiting_facilitation,
         saga_id,
         claim_revision,
         claim_updated_at
       ) do
    Postgrex.transaction(DB, fn conn ->
      with :ok <- lock_session_row(conn, session_id),
           :ok <- lock_reserved_claim(conn, saga_id, session_id, claim_revision) do
        case run_terminal_commit_query(
               conn,
               sql,
               session_id,
               payload,
               status,
               paused_agent_id,
               reviewer_agent_id,
               expected_phase,
               expected_transcript_revision,
               expected_awaiting_facilitation,
               claim_revision,
               claim_updated_at
             ) do
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
    end)
    |> case do
      {:ok, result} -> result
      {:error, reason} -> {:error, reason}
    end
  end

  defp lock_session_row(conn, session_id) do
    case Postgrex.query(
           conn,
           "SELECT 1 FROM core.dialectic_sessions WHERE session_id = $1 FOR UPDATE",
           [session_id]
         ) do
      {:ok, %{rows: [[1]]}} -> :ok
      {:ok, %{rows: []}} -> {:error, :session_not_found}
      {:error, error} -> {:error, error}
    end
  end

  defp lock_reserved_claim(conn, saga_id, session_id, claim_revision) do
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

  defp commit_reserved_claim(conn, saga_id, claim_revision) do
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

  defp run_terminal_commit_query(
         conn,
         sql,
         session_id,
         payload,
         status,
         paused_agent_id,
         reviewer_agent_id,
         expected_phase,
         expected_transcript_revision,
         expected_awaiting_facilitation,
         claim_revision,
         claim_updated_at
       ) do
    case Postgrex.query(conn, sql, [
           session_id,
           status,
           Jason.encode!(payload),
           paused_agent_id,
           reviewer_agent_id,
           @reviewed_work_authorship_schema,
           @sql_canonical_agent_uuid,
           @max_reviewed_work_author_agent_ids,
           expected_phase,
           expected_transcript_revision,
           expected_awaiting_facilitation,
           claim_revision,
           claim_updated_at
         ]) do
      {:ok, %{num_rows: 1}} ->
        :written

      {:ok, %{num_rows: 0}} ->
        case Postgrex.query(
               conn,
               "SELECT status, phase, paused_agent_id, reviewer_agent_id, paused_agent_state_json, transcript_revision, awaiting_facilitation, resolution_saga_revision, updated_at FROM core.dialectic_sessions WHERE session_id = $1",
               [session_id]
             ) do
          {:ok,
           %{
             rows: [
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
               ]
             ]
           }}
          when existing in @terminal_statuses ->
            # Already terminal (raced/idempotent). The caller commits the
            # losing saga but must report the live winner, not its request.
            {:already_terminal, existing}

          {:ok,
           %{
             rows: [
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
               ]
             ]
           }} ->
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

          {:ok, %{rows: []}} ->
            {:error, :session_not_found}

          {:error, e} ->
            {:error, e}
        end

      {:error, e} ->
        {:error, e}
    end
  end

  defp reclaim_stale_reserved(conn, session_id) do
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

  defp revert_reserved_claim(saga_id) do
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

  defp revert_reserved_session_claims(conn, session_id) do
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

  @doc """
  Compatibility boundary retained for callers compiled against the former
  split claim/commit API.

  A saga is not terminal merely because its reservation row can be marked
  `pg_committed`: the session row must become terminal in the *same database
  transaction*. Freeing the in-flight slot while the session remains active
  permits a second, contradictory resolution to claim it. Use `resolve/1`,
  which owns both writes atomically.
  """
  @spec commit(String.t()) :: {:error, :terminal_commit_requires_resolve}
  def commit(saga_id) when is_binary(saga_id) do
    {:error, :terminal_commit_requires_resolve}
  end

  @doc """
  Periodic recovery: revert every orphaned (old, still-`reserved`) saga across
  ALL sessions. The on-claim `reclaim_stale_reserved` only frees a session being
  re-claimed; this sweep frees an orphan even if its session is never resolved
  again, so a crashed resolver can never permanently wedge a session. Returns
  `{:ok, count}`. Run by `DialecticSagaReaper` under the PeriodicWorker.
  """
  @spec reclaim_all_stale() :: {:ok, non_neg_integer()} | {:error, term()}
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

  @doc """
  Live (non-terminal) dialectic sessions as a BEAM-served presence read: each
  with phase, age in seconds, and whether a resolution saga is currently in
  flight. Backs `GET /v1/dialectic/presence`.
  """
  @spec live_sessions(pos_integer()) :: {:ok, [map()]} | {:error, term()}
  def live_sessions(limit \\ 100) when is_integer(limit) and limit > 0 do
    sql = """
    SELECT s.session_id, s.phase,
           EXTRACT(EPOCH FROM (now() - s.created_at))::bigint AS age_s,
           EXISTS(
             SELECT 1 FROM coordination.session_resolution_sagas g
             WHERE g.session_id = s.session_id AND g.state = ANY($1)
           ) AS resolving
    FROM core.dialectic_sessions s
    WHERE s.status NOT IN ('resolved', 'failed', 'escalated')
    ORDER BY s.created_at DESC
    LIMIT $2
    """

    case Postgrex.query(DB, sql, [@inflight_states, limit]) do
      {:ok, %{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [sid, phase, age, resolving] ->
           %{session_id: sid, phase: phase, age_seconds: age, resolving: resolving}
         end)}

      {:error, e} ->
        {:error, e}
    end
  end

  @doc """
  Per-session liveness snapshot for the live-timer layer: status, the two agent
  ids (needed to drive a `failed` resolve), and seconds since the last update.
  `{:ok, nil}` if the session is gone.
  """
  @spec get_session_liveness(String.t()) :: {:ok, map() | nil} | {:error, term()}
  def get_session_liveness(session_id) when is_binary(session_id) do
    # The standing-verdict block is READ, never derived. A dialectic outcome is
    # decided in core.dialectic_messages; the sweeper's job is to carry that
    # forward into the row it writes, not to form a judgment of its own. So:
    #
    #   standing_verdict     -- the reviewer's last agrees=false, or none
    #   verdict_message_id   -- which message it was, so the claim is checkable
    #   verdict_acceptance   -- accepted | contested | no_reply, from whether the
    #                           paused agent posted after that verdict, and how
    #
    # `verdict_acceptance` is an OBSERVATION of the transcript, not a protocol
    # transition. Nothing may terminate a session on it. That distinction is the
    # condition this came from: inferring acceptance from silence is unsound
    # even when the record contains no counterexample, and at the time of
    # writing it contains none — 0 of 25 rejected-then-swept sessions had the
    # paused agent still contesting; 20 never replied and 5 replied agreeing.
    sql = """
    WITH v AS (
      SELECT m.message_id, m.timestamp
      FROM core.dialectic_messages m
      JOIN core.dialectic_sessions ds ON ds.session_id = m.session_id
      WHERE m.session_id = $1
        AND m.agent_id = ds.reviewer_agent_id
        AND m.agrees IS FALSE
      ORDER BY m.timestamp DESC
      LIMIT 1
    ),
    reply AS (
      SELECT bool_or(m.agrees IS TRUE) AS agreed, count(*) AS n
      FROM core.dialectic_messages m
      JOIN core.dialectic_sessions ds ON ds.session_id = m.session_id
      CROSS JOIN v
      WHERE m.session_id = $1
        AND m.agent_id = ds.paused_agent_id
        AND m.timestamp > v.timestamp
    )
    SELECT d.status, d.paused_agent_id, d.reviewer_agent_id,
           EXTRACT(EPOCH FROM (now() - d.updated_at))::bigint AS inactive_s,
           d.phase, d.awaiting_facilitation, d.transcript_revision,
           d.updated_at,
           (SELECT message_id FROM v) AS verdict_message_id,
           (SELECT n FROM reply) AS replies_after_verdict,
           (SELECT agreed FROM reply) AS reply_agreed
    FROM core.dialectic_sessions d WHERE d.session_id = $1
    """

    case Postgrex.query(DB, sql, [session_id]) do
      {:ok,
       %{
         rows: [
           [
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
           ]
         ]
       }} ->
        {:ok,
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
         }}

      {:ok, %{rows: []}} ->
        {:ok, nil}

      {:error, e} ->
        {:error, e}
    end
  end

  @doc "Return the in-flight saga_id for a session, or nil. Used by recovery + the Python sweeper guard's BEAM-side mirror."
  @spec get_inflight(String.t()) :: {:ok, String.t() | nil} | {:error, term()}
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

  # ---------- internals ----------

  defp guard_session_phase(
         conn,
         session_id,
         paused_agent_id,
         reviewer_agent_id,
         requested_status,
         expected_phase,
         expected_transcript_revision,
         expected_awaiting_facilitation
       ) do
    sql =
      "SELECT status, phase, paused_agent_id, reviewer_agent_id, paused_agent_state_json, transcript_revision, awaiting_facilitation, updated_at " <>
        "FROM core.dialectic_sessions WHERE session_id = $1 FOR UPDATE"

    case Postgrex.query(conn, sql, [session_id]) do
      {:ok,
       %{
         rows: [
           [status, _phase, _paused, _reviewer, _state, _revision, _awaiting, _updated_at]
         ]
       }}
      when status in ["resolved", "failed"] ->
        {:error, {:session_terminal, status}}

      {:ok,
       %{
         rows: [
           [_status, phase, _paused, _reviewer, _state, _revision, _awaiting, _updated_at]
         ]
       }}
      when not is_nil(expected_phase) and phase != expected_phase ->
        {:error, :session_phase_mismatch}

      {:ok,
       %{
         rows: [
           [_status, _phase, _paused, _reviewer, _state, revision, _awaiting, _updated_at]
         ]
       }}
      when not is_nil(expected_transcript_revision) and
             revision != expected_transcript_revision ->
        {:error, :session_revision_mismatch}

      {:ok,
       %{
         rows: [
           [_status, _phase, _paused, _reviewer, _state, _revision, awaiting, _updated_at]
         ]
       }}
      when not is_nil(expected_awaiting_facilitation) and
             awaiting != expected_awaiting_facilitation ->
        {:error, :session_facilitation_mismatch}

      {:ok,
       %{
         rows: [
           [_status, phase, _paused, _reviewer, _state, _revision, _awaiting, _updated_at]
         ]
       }}
      when requested_status == "resolved" and phase != "synthesis" ->
        {:error, :session_phase_mismatch}

      {:ok,
       %{
         rows: [
           [
             _status,
             phase,
             stored_paused,
             stored_reviewer,
             state,
             _revision,
             _awaiting,
             updated_at
           ]
         ]
       }} ->
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

      {:ok, %{rows: []}} ->
        {:error, :session_not_found}

      {:error, e} ->
        {:error, e}
    end
  end

  defp validate_resolution_authority(
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

  defp reserve_claim(
         conn,
         session_id,
         paused_agent_id,
         reviewer_agent_id,
         json,
         hash,
         expected_updated_at,
         locked_updated_at
       ) do
    # The session row is already locked by guard_session_phase/7.  Read and
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
          hash
        )

      {:error, error} ->
        {:error, error}
    end
  end

  defp insert_new_claim(conn, session_id, paused_agent_id, reviewer_agent_id, json, hash) do
    with {:ok, %{rows: [[revision, updated_at]]}} <-
           Postgrex.query(
             conn,
             """
             UPDATE core.dialectic_sessions
             SET resolution_saga_revision = resolution_saga_revision + 1
             WHERE session_id = $1
             RETURNING resolution_saga_revision, updated_at
             """,
             [session_id]
           ),
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
      {:ok, %{rows: []}} -> {:error, :session_not_found}
      {:error, error} -> {:error, error}
    end
  end

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
  defp operation_hash(payload, status, effect_protocol) do
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
end
