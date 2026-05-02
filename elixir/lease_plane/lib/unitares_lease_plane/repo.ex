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
  audit_session, original_ttl_s, earned_status
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
    |> normalize_tx_result()
  end

  defp maybe_existing_active(conn, surface_id) do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE surface_id = $1 AND released_at IS NULL"

    case Postgrex.query(conn, sql, [surface_id]) do
      {:ok, %{rows: []}} -> {:ok, nil}
      {:ok, %{rows: [row], columns: cols}} -> {:ok, row_to_map(cols, row)}
      {:error, e} -> {:error, e}
    end
  end

  defp acquire_step(conn, p, %{holder_agent_uuid: held} = existing)
       when is_binary(held) do
    if held == p.holder_agent_uuid do
      {:ok, {:ok, existing, :idempotent}}
    else
      # PR 5 council BLOCK fix: include surface_id + blocking_lease_id so the
      # 409 response carries every field the v0.7 §7.3.2 AcquireHeldByOther shape
      # requires. Without these, Pydantic validation degrades to AcquireSchemaInvalid.
      # Preserved log_conflict from upstream — emits the audit event for the
      # conflict-detected path.
      case log_conflict(conn, p, existing) do
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
    insert_lease_sql = """
    INSERT INTO lease_plane.surface_leases
      (surface_id, holder_agent_uuid, holder_class,
       holder_kind, holder_pid, heartbeat_required, intent,
       expires_at, original_ttl_s, audit_session, earned_status)
    VALUES
      ($1, $2, $3, $4, $5, $6, $7,
       now() + make_interval(secs => $8), $8, $9, 'provisional')
    RETURNING #{@select_lease_columns}
    """

    args = [
      p.surface_id,
      uuid_to_binary(p.holder_agent_uuid),
      Map.get(p, :holder_class, "process_instance"),
      p.holder_kind,
      Map.get(p, :holder_pid),
      p.holder_kind == "remote_heartbeat",
      Map.get(p, :intent),
      p.ttl_s,
      Map.get(p, :audit_session)
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
        lease = row_to_map(cols, row)

        case log_event(conn, "acquire", lease) do
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

  # ---------- status ----------

  @spec status(String.t()) :: {:ok, lease | nil} | {:error, term()}
  def status(surface_id) when is_binary(surface_id) do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE surface_id = $1 AND released_at IS NULL"

    case Postgrex.query(DB, sql, [surface_id]) do
      {:ok, %{rows: []}} -> {:ok, nil}
      {:ok, %{rows: [row], columns: cols}} -> {:ok, row_to_map(cols, row)}
      {:error, e} -> {:error, e}
    end
  end

  # ---------- renew / heartbeat ----------

  @doc """
  Aliases renew + heartbeat per RFC §4.4.2. Always extends by the
  immutable `original_ttl_s`; never accepts caller-supplied ttl.
  """
  @spec renew(binary()) :: :ok | {:error, term()}
  def renew(lease_id) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET expires_at = now() + make_interval(secs => original_ttl_s),
        last_heartbeat_at = CASE WHEN heartbeat_required THEN now()
                                 ELSE last_heartbeat_at END
    WHERE lease_id = $1 AND released_at IS NULL
    RETURNING #{@select_lease_columns}
    """

    case Postgrex.transaction(DB, fn conn ->
           with {:ok, %{rows: [row], columns: cols}} <-
                  Postgrex.query(conn, sql, [uuid_to_binary(lease_id)]),
                lease = row_to_map(cols, row),
                :ok <- log_event(conn, "renew", lease) do
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
                  Postgrex.query(conn, sql, [uuid_to_binary(lease_id), release_reason]),
                lease = row_to_map(cols, row),
                :ok <- log_event(conn, release_event_type(release_reason), lease) do
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

  # ---------- handoff ----------

  @spec active_lease(binary()) :: {:ok, lease} | {:error, :not_found | term()}
  def active_lease(lease_id) when is_binary(lease_id) do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE lease_id = $1 AND released_at IS NULL"

    case Postgrex.query(DB, sql, [uuid_to_binary(lease_id)]) do
      {:ok, %{rows: [row], columns: cols}} -> {:ok, row_to_map(cols, row)}
      {:ok, %{rows: []}} -> {:error, :not_found}
      {:error, e} -> {:error, e}
    end
  end

  @spec log_handoff_offer(lease, map()) :: :ok | {:error, term()}
  def log_handoff_offer(%{} = lease, %{} = handoff) do
    payload = %{
      handoff_id: handoff.handoff_id,
      from_lease_id: lease.lease_id,
      from_holder_agent_uuid: lease.holder_agent_uuid,
      to_holder_agent_uuid: handoff.to_holder_agent_uuid,
      new_ttl_s: handoff.ttl_s,
      offer_expires_at: iso(handoff.expires_at),
      audit_session: lease.audit_session
    }

    log_event(DB, "handoff_offer", lease, payload)
  end

  @spec accept_handoff(map()) :: {:ok, lease} | {:error, term()}
  def accept_handoff(%{} = handoff) do
    Postgrex.transaction(DB, fn conn ->
      with {:ok, old_lease} <- lock_active_lease(conn, handoff.lease_id),
           {:ok, new_lease} <- transition_handoff(conn, old_lease, handoff) do
        new_lease
      else
        {:error, reason} -> Postgrex.rollback(conn, reason)
      end
    end)
    |> case do
      {:ok, lease} -> {:ok, lease}
      {:error, reason} -> {:error, reason}
    end
  end

  defp lock_active_lease(conn, lease_id) do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE lease_id = $1 AND released_at IS NULL FOR UPDATE"

    case Postgrex.query(conn, sql, [uuid_to_binary(lease_id)]) do
      {:ok, %{rows: [row], columns: cols}} -> {:ok, row_to_map(cols, row)}
      {:ok, %{rows: []}} -> {:error, :not_found}
      {:error, e} -> {:error, e}
    end
  end

  # surface_kind dropped from reacquire INSERT per RFC v0.8 §7.2.3 — generated
  # from surface_id post-026. PR 1 fixed this for acquire; the handoff path was
  # missed and surfaced when 026-029 were applied to the live DB on 2026-05-02.
  defp transition_handoff(conn, old_lease, handoff) do
    release_sql = """
    UPDATE lease_plane.surface_leases
    SET released_at = now(), release_reason = 'handoff'
    WHERE lease_id = $1 AND released_at IS NULL
    """

    insert_sql = """
    INSERT INTO lease_plane.surface_leases
      (surface_id, holder_agent_uuid, holder_class,
       holder_kind, holder_pid, heartbeat_required, intent,
       expires_at, original_ttl_s, audit_session, earned_status)
    VALUES
      ($1, $2, $3, 'remote_heartbeat', NULL, true, $4,
       now() + make_interval(secs => $5), $5, $6, 'provisional')
    RETURNING #{@select_lease_columns}
    """

    intent = old_lease.intent || "handoff from #{old_lease.lease_id}"
    audit_session = old_lease.audit_session

    args = [
      old_lease.surface_id,
      uuid_to_binary(handoff.to_holder_agent_uuid),
      old_lease.holder_class,
      intent,
      handoff.ttl_s,
      audit_session
    ]

    with {:ok, _} <- Postgrex.query(conn, release_sql, [uuid_to_binary(old_lease.lease_id)]),
         {:ok, %{rows: [row], columns: cols}} <- Postgrex.query(conn, insert_sql, args),
         new_lease = row_to_map(cols, row),
         payload = %{
           handoff_id: handoff.handoff_id,
           from_lease_id: old_lease.lease_id,
           from_holder_agent_uuid: old_lease.holder_agent_uuid,
           to_holder_agent_uuid: handoff.to_holder_agent_uuid,
           audit_session: audit_session
         },
         :ok <- log_event(conn, "handoff_accept", new_lease, payload) do
      {:ok, new_lease}
    end
  end

  # ---------- reaper ----------

  @spec expired_active_leases(pos_integer()) :: {:ok, [lease]} | {:error, term()}
  def expired_active_leases(limit) when is_integer(limit) and limit > 0 do
    sql =
      "SELECT #{@select_lease_columns} FROM lease_plane.surface_leases " <>
        "WHERE released_at IS NULL AND expires_at < now() " <>
        "ORDER BY expires_at ASC LIMIT $1"

    case Postgrex.query(DB, sql, [limit]) do
      {:ok, %{rows: rows, columns: cols}} -> {:ok, Enum.map(rows, &row_to_map(cols, &1))}
      {:error, e} -> {:error, e}
    end
  end

  @spec release_if_expired(binary(), String.t()) :: :ok | {:error, term()}
  def release_if_expired(lease_id, release_reason) when is_binary(release_reason) do
    sql = """
    UPDATE lease_plane.surface_leases
    SET released_at = now(), release_reason = $2
    WHERE lease_id = $1 AND released_at IS NULL AND expires_at < now()
    RETURNING #{@select_lease_columns}
    """

    case Postgrex.transaction(DB, fn conn ->
           with {:ok, %{rows: [row], columns: cols}} <-
                  Postgrex.query(conn, sql, [uuid_to_binary(lease_id), release_reason]),
                lease = row_to_map(cols, row),
                :ok <- log_event(conn, release_event_type(release_reason), lease) do
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

  # ---------- audit outbox ----------

  @spec unforwarded_events(pos_integer(), keyword()) :: {:ok, [event]} | {:error, term()}
  def unforwarded_events(limit, opts \\ []) when is_integer(limit) and limit > 0 do
    {where, args} =
      case Keyword.fetch(opts, :surface_id) do
        {:ok, surface_id} -> {"forwarded_at IS NULL AND surface_id = $2", [limit, surface_id]}
        :error -> {"forwarded_at IS NULL", [limit]}
      end

    sql = """
    SELECT
      event_id::text AS event_id,
      ts, event_type, lease_id::text AS lease_id,
      surface_id, surface_kind,
      holder_agent_uuid::text AS holder_agent_uuid,
      holder_class, advisory_mode, payload, earned_status
    FROM lease_plane.lease_plane_events
    WHERE #{where}
    ORDER BY ts ASC
    LIMIT $1
    """

    case Postgrex.query(DB, sql, args) do
      {:ok, %{rows: rows, columns: cols}} -> {:ok, Enum.map(rows, &row_to_map(cols, &1))}
      {:error, e} -> {:error, e}
    end
  end

  @spec forward_outbox_event(binary()) :: :ok | {:error, term()}
  def forward_outbox_event(event_id) when is_binary(event_id) do
    result =
      Postgrex.transaction(DB, fn conn ->
        with {:ok, event} <- lock_unforwarded_event(conn, event_id),
             :ok <- insert_tool_usage(conn, event),
             :ok <- mark_forwarded(conn, event.event_id) do
          :ok
        else
          {:error, reason} -> Postgrex.rollback(conn, reason)
        end
      end)

    case result do
      {:ok, :ok} ->
        :ok

      {:error, reason} ->
        _ = bump_forward_attempts(event_id)
        {:error, reason}
    end
  end

  defp lock_unforwarded_event(conn, event_id) do
    sql = """
    SELECT
      event_id::text AS event_id,
      ts, event_type, lease_id::text AS lease_id,
      surface_id, surface_kind,
      holder_agent_uuid::text AS holder_agent_uuid,
      holder_class, advisory_mode, payload, earned_status
    FROM lease_plane.lease_plane_events
    WHERE event_id = $1 AND forwarded_at IS NULL
    FOR UPDATE
    """

    case Postgrex.query(conn, sql, [uuid_to_binary(event_id)]) do
      {:ok, %{rows: [row], columns: cols}} -> {:ok, row_to_map(cols, row)}
      {:ok, %{rows: []}} -> {:error, :not_found}
      {:error, e} -> {:error, e}
    end
  end

  defp insert_tool_usage(conn, event) do
    sql = """
    INSERT INTO audit.tool_usage
      (ts, agent_id, session_id, tool_name, latency_ms, success, error_type, payload)
    VALUES ($1, $2, $3, $4, NULL, $5, $6, ($7::text)::jsonb)
    """

    success = event.event_type != "service_unavailable"
    error_type = if success, do: nil, else: "service_unavailable"
    payload = tool_usage_payload(event)

    args = [
      event.ts,
      event.holder_agent_uuid,
      Map.get(payload, "audit_session"),
      "lease.#{event.event_type}",
      success,
      error_type,
      Jason.encode!(payload)
    ]

    case Postgrex.query(conn, sql, args) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  defp mark_forwarded(conn, event_id) do
    sql = """
    UPDATE lease_plane.lease_plane_events
    SET forwarded_at = now(), forward_attempts = forward_attempts + 1
    WHERE event_id = $1
    """

    case Postgrex.query(conn, sql, [uuid_to_binary(event_id)]) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  defp bump_forward_attempts(event_id) do
    Postgrex.query(
      DB,
      "UPDATE lease_plane.lease_plane_events SET forward_attempts = forward_attempts + 1 WHERE event_id = $1",
      [uuid_to_binary(event_id)]
    )
  end

  # ---------- helpers ----------

  defp log_event(conn, event_type, lease, payload \\ nil) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (event_type, lease_id, surface_id, surface_kind,
       holder_agent_uuid, holder_class, advisory_mode,
       payload, earned_status)
    VALUES ($1, $2, $3, $4, $5, $6, true, ($7::text)::jsonb, $8)
    """

    args = [
      event_type,
      uuid_to_binary(lease.lease_id),
      lease.surface_id,
      lease.surface_kind,
      uuid_to_binary(lease.holder_agent_uuid),
      lease.holder_class,
      Jason.encode!(payload || lease_payload(lease)),
      lease.earned_status
    ]

    case Postgrex.query(conn, sql, args) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  defp log_conflict(conn, p, existing) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (event_type, lease_id, surface_id, surface_kind,
       holder_agent_uuid, holder_class, advisory_mode,
       payload, earned_status)
    VALUES ($1, $2, $3, $4, $5, $6, true, ($7::text)::jsonb, $8)
    """

    payload = %{
      requested_holder_agent_uuid: p.holder_agent_uuid,
      held_by_uuid: existing.holder_agent_uuid,
      expires_at: iso(existing.expires_at),
      audit_session: Map.get(p, :audit_session)
    }

    args = [
      "conflict_held_by_other",
      uuid_to_binary(existing.lease_id),
      existing.surface_id,
      existing.surface_kind,
      uuid_to_binary(p.holder_agent_uuid),
      Map.get(p, :holder_class, "process_instance"),
      Jason.encode!(payload),
      existing.earned_status
    ]

    case Postgrex.query(conn, sql, args) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  defp lease_payload(lease) do
    %{
      "lease_id" => lease.lease_id,
      "surface_id" => lease.surface_id,
      "surface_kind" => lease.surface_kind,
      "holder_agent_uuid" => lease.holder_agent_uuid,
      "holder_class" => lease.holder_class,
      "holder_kind" => lease.holder_kind,
      "holder_pid" => lease.holder_pid,
      "heartbeat_required" => lease.heartbeat_required,
      "intent" => lease.intent,
      "acquired_at" => iso(lease.acquired_at),
      "expires_at" => iso(lease.expires_at),
      "last_heartbeat_at" => iso(lease.last_heartbeat_at),
      "released_at" => iso(lease.released_at),
      "release_reason" => lease.release_reason,
      "audit_session" => lease.audit_session,
      "original_ttl_s" => lease.original_ttl_s,
      "earned_status" => lease.earned_status
    }
  end

  defp tool_usage_payload(event) do
    event_payload = decode_payload(event.payload)

    %{
      "lease_event_id" => event.event_id,
      "lease_id" => event.lease_id,
      "surface_id" => event.surface_id,
      "surface_kind" => event.surface_kind,
      "holder_agent_uuid" => event.holder_agent_uuid,
      "holder_class" => event.holder_class,
      "advisory_mode" => event.advisory_mode,
      "earned_status" => event.earned_status,
      "audit_session" => Map.get(event_payload, "audit_session"),
      "lease_payload" => event_payload
    }
  end

  defp decode_payload(%{} = payload), do: stringify_keys(payload)

  defp decode_payload(payload) when is_binary(payload) do
    case Jason.decode(payload) do
      {:ok, %{} = decoded} -> stringify_keys(decoded)
      _ -> %{"raw_payload" => payload}
    end
  end

  defp decode_payload(_), do: %{}

  defp stringify_keys(map) do
    Map.new(map, fn
      {key, value} when is_atom(key) -> {Atom.to_string(key), value}
      {key, value} -> {key, value}
    end)
  end

  defp release_event_type(reason)
       when reason in ["reaped_remote_ttl", "reaped_local_ttl", "down_local", "forced"],
       do: reason

  defp release_event_type(_reason), do: "release"

  defp iso(nil), do: nil
  defp iso(%DateTime{} = dt), do: DateTime.to_iso8601(dt)

  defp row_to_map(columns, row) do
    columns
    |> Enum.zip(row)
    |> Enum.into(%{}, fn {col, val} -> {String.to_atom(col), val} end)
  end

  # SELECT casts uuid columns to text so the boundary always sees 36-char strings.
  # For parameter binding (INSERT/UPDATE/WHERE), Postgrex requires the canonical
  # 16-byte binary form — uuid_to_binary/1 handles the conversion.

  defp uuid_to_binary(uuid) when is_binary(uuid) and byte_size(uuid) == 16, do: uuid

  defp uuid_to_binary(
         <<a::binary-size(8), "-", b::binary-size(4), "-", c::binary-size(4), "-",
           d::binary-size(4), "-", e::binary-size(12)>>
       ) do
    Base.decode16!(a <> b <> c <> d <> e, case: :mixed)
  end

  defp normalize_tx_result({:ok, {:ok, lease, kind}}), do: {:ok, lease, kind}
  defp normalize_tx_result({:ok, {:error, code, detail}}), do: {:error, code, detail}
  defp normalize_tx_result({:error, reason}), do: {:error, reason}
end
