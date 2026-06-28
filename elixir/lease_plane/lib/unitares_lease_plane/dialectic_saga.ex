defmodule UnitaresLeasePlane.DialecticSaga do
  @moduledoc """
  Cross-runtime serialization primitive for the dialectic SYNTHESIS->RESOLVED
  transition (BEAM dialectic-on-BEAM, "Slice 1", council 2026-06-28).

  This is the BEAM-side claim/commit state machine over
  `coordination.session_resolution_sagas`. It is the foundation the forthcoming
  `SessionServer` GenServer drives; the saga row is a durable crash-recovery
  log + idempotency gate, NOT a mutex (the GenServer mailbox serializes within
  BEAM; this gate serializes across runtimes and survives restarts).

  Two invariants enforced here:

    * **Phase guard** — a saga is only claimable for a session that exists and is
      not already terminal (`resolved` / `failed`). This defends against a
      retried resolve on an already-finished session minting a stray saga.

    * **One in-flight saga per session** — the partial unique index
      `idx_saga_one_pending_per_session` makes a second concurrent claim fail;
      we map that to `{:error, :saga_in_flight}`. A claim that repeats the *same*
      resolution payload (same `(session_id, resolution_payload_hash)`) is an
      idempotent replay and returns the existing saga.

  Slice-1 scope: `claim/1`, `commit/1`, `get_inflight/1`. The HTTP endpoint,
  the Python `execute_resolution` boundary call, and the GenServer wiring land
  in the next increment; this module is exercised directly by `mix test`.

  Same `governance` database as the lease plane, so `UnitaresLeasePlane.DB`
  reaches the `coordination` and `core` schemas (cf. `GovernedEffect` writing
  `audit.events`).
  """

  alias UnitaresLeasePlane.DB

  require Logger

  @inflight_states ~w(reserved paused_agent_applied both_agents_applied reverting)

  @type claim_ok :: {:ok, %{saga_id: String.t(), origin: :new | :idempotent}}
  @type claim_err ::
          {:error, :session_not_found}
          | {:error, {:session_terminal, String.t()}}
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
    * `{:ok, %{saga_id: id, origin: :idempotent}}` — same payload already claimed
    * `{:error, :session_not_found}`
    * `{:error, {:session_terminal, status}}` — session already resolved/failed
    * `{:error, :saga_in_flight}` — a different in-flight saga holds the session
  """
  @spec claim(map()) :: claim_ok() | claim_err()
  def claim(%{
        session_id: session_id,
        paused_agent_id: paused_agent_id,
        reviewer_agent_id: reviewer_agent_id,
        resolution_payload: payload
      })
      when is_binary(session_id) and is_binary(paused_agent_id) and
             is_binary(reviewer_agent_id) and is_map(payload) do
    hash = payload_hash(payload)
    json = Jason.encode!(payload)

    Postgrex.transaction(DB, fn conn ->
      with {:ok, _phase} <- guard_session_phase(conn, session_id),
           :ok <- reclaim_stale_reserved(conn, session_id),
           {:ok, result} <-
             insert_reserved(conn, session_id, paused_agent_id, reviewer_agent_id, json, hash) do
        result
      else
        {:error, reason} -> Postgrex.rollback(conn, reason)
      end
    end)
    |> case do
      {:ok, result} -> {:ok, result}
      {:error, reason} -> {:error, reason}
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

    * `{:ok, %{status: "resolved", saga_id: id, origin: :new | :idempotent | :already_terminal}}`
    * `{:error, :saga_in_flight}` — a live resolve already holds the session
    * `{:error, :session_not_found}` / other `{:error, term}`
  """
  @terminal_statuses ~w(resolved failed)

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

  defp do_resolve(params, session_id, payload, status) do
    case claim(params) do
      {:ok, %{saga_id: saga_id, origin: origin}} ->
        with :ok <- commit_session_row(session_id, payload, status),
             :ok <- commit(saga_id) do
          {:ok, %{status: status, saga_id: saga_id, origin: origin}}
        end

      {:error, {:session_terminal, existing}} ->
        # Already terminal: nothing to write, treat as idempotent success.
        {:ok, %{status: existing, saga_id: nil, origin: :already_terminal}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # Guarded terminal write of the session row — BEAM is the sole writer for both
  # terminal transitions (resolved AND failed). Mirrors the Python B-4 guard
  # (#1171): refuses to overwrite an already-terminal row. phase tracks status.
  defp commit_session_row(session_id, payload, status) do
    sql = """
    UPDATE core.dialectic_sessions
    SET status = $2, phase = $2, resolution_json = $3::jsonb, updated_at = now()
    WHERE session_id = $1 AND status NOT IN ('resolved', 'failed')
    RETURNING session_id
    """

    case Postgrex.query(DB, sql, [session_id, status, Jason.encode!(payload)]) do
      {:ok, %{num_rows: 1}} -> :ok
      # Already terminal (raced/idempotent) — saga still commits; not an error.
      {:ok, %{num_rows: 0}} -> :ok
      {:error, e} -> {:error, e}
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

  @doc """
  Mark a claimed saga committed (terminal success). Idempotent: a saga already
  `pg_committed` returns `:ok` without a second write.
  """
  @spec commit(String.t()) :: :ok | {:error, :saga_not_found} | {:error, term()}
  def commit(saga_id) when is_binary(saga_id) do
    # Compare on saga_id::text so the param is a plain string (avoids Postgrex
    # trying to encode a 36-char string into the 16-byte uuid wire format).
    sql = """
    UPDATE coordination.session_resolution_sagas
    SET state = 'pg_committed', pg_committed_at = now(), updated_at = now()
    WHERE saga_id::text = $1 AND state <> 'pg_committed'
    RETURNING saga_id
    """

    case Postgrex.query(DB, sql, [saga_id]) do
      {:ok, %{num_rows: 1}} ->
        :ok

      {:ok, %{num_rows: 0}} ->
        # Either already committed (idempotent) or no such saga — disambiguate.
        case Postgrex.query(
               DB,
               "SELECT 1 FROM coordination.session_resolution_sagas WHERE saga_id::text = $1",
               [saga_id]
             ) do
          {:ok, %{num_rows: 1}} -> :ok
          {:ok, %{num_rows: 0}} -> {:error, :saga_not_found}
          {:error, e} -> {:error, e}
        end

      {:error, e} ->
        {:error, e}
    end
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

  defp guard_session_phase(conn, session_id) do
    sql = "SELECT status FROM core.dialectic_sessions WHERE session_id = $1"

    case Postgrex.query(conn, sql, [session_id]) do
      {:ok, %{rows: [[status]]}} when status in ["resolved", "failed"] ->
        {:error, {:session_terminal, status}}

      {:ok, %{rows: [[_status]]}} ->
        {:ok, :claimable}

      {:ok, %{rows: []}} ->
        {:error, :session_not_found}

      {:error, e} ->
        {:error, e}
    end
  end

  defp insert_reserved(conn, session_id, paused_agent_id, reviewer_agent_id, json, hash) do
    # ON CONFLICT DO NOTHING so a uniqueness clash does NOT raise — raising would
    # abort the surrounding transaction and make the disambiguation SELECT below
    # fail with "current transaction is aborted". An empty RETURNING means a
    # conflict (either the one-pending partial index or the (session_id, hash)
    # constraint); we then read which case it is.
    sql = """
    INSERT INTO coordination.session_resolution_sagas
      (saga_id, session_id, paused_agent_id, reviewer_agent_id, state,
       resolution_payload_json, resolution_payload_hash, last_attempt_at, attempt_count)
    VALUES (gen_random_uuid(), $1, $2, $3, 'reserved', $4::jsonb, $5, now(), 1)
    ON CONFLICT DO NOTHING
    RETURNING saga_id::text
    """

    case Postgrex.query(conn, sql, [session_id, paused_agent_id, reviewer_agent_id, json, hash]) do
      {:ok, %{rows: [[saga_id]]}} ->
        {:ok, %{saga_id: saga_id, origin: :new}}

      {:ok, %{rows: []}} ->
        resolve_conflict(conn, session_id, hash)

      {:error, e} ->
        {:error, e}
    end
  end

  # A conflict is either: the same resolution payload already has a saga (any
  # state -> idempotent replay of that saga), or a *different* in-flight saga
  # holds the one-pending slot for this session.
  defp resolve_conflict(conn, session_id, hash) do
    same_payload =
      Postgrex.query(
        conn,
        "SELECT saga_id::text FROM coordination.session_resolution_sagas WHERE session_id = $1 AND resolution_payload_hash = $2 LIMIT 1",
        [session_id, hash]
      )

    case same_payload do
      {:ok, %{rows: [[saga_id]]}} ->
        {:ok, %{saga_id: saga_id, origin: :idempotent}}

      {:ok, %{rows: []}} ->
        {:error, :saga_in_flight}

      {:error, e} ->
        {:error, e}
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

  defp canonical(m) when is_map(m) do
    m
    |> Enum.map(fn {k, v} -> [to_string(k), canonical(v)] end)
    |> Enum.sort_by(fn [k, _] -> k end)
  end

  defp canonical(list) when is_list(list), do: Enum.map(list, &canonical/1)
  defp canonical(other), do: other
end
