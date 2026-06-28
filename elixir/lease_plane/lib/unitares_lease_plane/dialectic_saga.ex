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
