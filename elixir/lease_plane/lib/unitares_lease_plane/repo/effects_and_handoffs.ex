defmodule UnitaresLeasePlane.Repo.EffectsAndHandoffs do
  @moduledoc """
  Governed-effect custody freshness & admission, lease handoff offer/accept,
  expired-lease reaping queries, and shared Postgrex row/event helpers for
  `UnitaresLeasePlane.Repo`.
  """

  alias UnitaresLeasePlane.DB

  @typep lease :: map()

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

  @doc false
  def select_lease_columns, do: @select_lease_columns

  # ---------- effect custody freshness ----------

  @doc """
  Re-read the exact leases acquired for an effect before dispatching it.

  A replacement lease, even for the same surface and holder, does not restore
  an earlier effect's custody. Expiry uses the database wall clock (including
  the boundary itself), independently of whether the reaper has run.

  This is a freshness check, NOT a lock held across a filesystem operation.
  Revocation after this read still requires executor-side fencing to exclude.
  Experiment and limits: `docs/operations/effect-custody-recheck.md`.
  """
  @spec check_effect_custody([map()]) :: :ok | {:error, atom()}
  def check_effect_custody([_ | _] = leases) do
    Enum.reduce_while(leases, :ok, fn lease, :ok ->
      case check_effect_lease(lease) do
        :ok -> {:cont, :ok}
        {:error, _} = error -> {:halt, error}
      end
    end)
  end

  def check_effect_custody(_), do: {:error, :custody_unavailable}

  defp check_effect_lease(%{
         lease_id: id,
         surface_id: surface,
         holder_agent_uuid: holder
       }) do
    sql = """
    SELECT released_at IS NULL, expires_at > clock_timestamp()
    FROM lease_plane.surface_leases
    WHERE lease_id = $1 AND surface_id = $2 AND holder_agent_uuid = $3
    """

    case Postgrex.query(DB, sql, [uuid_to_binary(id), surface, uuid_to_binary(holder)]) do
      {:ok, %{rows: [[true, true]]}} -> :ok
      {:ok, %{rows: [[true, false]]}} -> {:error, :custody_expired}
      {:ok, %{rows: [[false, _]]}} -> {:error, :revoked}
      {:ok, %{rows: []}} -> {:error, :revoked}
      _ -> {:error, :custody_unavailable}
    end
  end

  defp check_effect_lease(_), do: {:error, :custody_unavailable}

  @doc """
  Commit a durable, exclusive reservation before pre-image capture or mutation.
  An optional prepare callback runs on the same transaction connection after
  custody validation. Its payload/promotion claim commits with the reservation,
  or both roll back. It must not perform filesystem I/O.
  This transaction is the admission point: it serializes with release on
  the exact lease row. Expiry is checked here; admitted operations retain
  custody until all their filesystem calls have returned, even after expiry.
  A connection/process loss does NOT release this reservation.
  """
  def begin_effect_custody(effect_id, leases, prepare \\ fn _conn -> :ok end)

  def begin_effect_custody(
        effect_id,
        [
          %{lease_id: id, surface_id: surface, holder_agent_uuid: holder}
        ],
        prepare
      )
      when is_binary(effect_id) and is_function(prepare, 1) do
    sql = """
    UPDATE lease_plane.surface_leases SET effect_fence = $4
    WHERE lease_id = $1 AND surface_id = $2 AND holder_agent_uuid = $3
      AND released_at IS NULL AND expires_at > clock_timestamp()
      AND effect_fence IS NULL
    RETURNING lease_id
    """

    # Take the lock before evaluating the clock. UPDATE's original WHERE may
    # have been evaluated before waiting behind an unchanged row lock.
    admission =
      Postgrex.transaction(DB, fn conn ->
        with {:ok, _lease} <- lock_active_lease(conn, id),
             {:ok, %{num_rows: 1}} <-
               Postgrex.query(conn, sql, [
                 uuid_to_binary(id),
                 surface,
                 uuid_to_binary(holder),
                 effect_id
               ]) do
          case prepare.(conn) do
            :ok -> :ok
            {:error, reason} -> Postgrex.rollback(conn, {:prepare_failed, reason})
          end
        else
          {:ok, %{num_rows: 0}} -> Postgrex.rollback(conn, :not_admitted)
          {:error, reason} -> Postgrex.rollback(conn, reason)
        end
      end)

    case admission do
      {:ok, :ok} ->
        :ok

      {:error, :not_found} ->
        {:error, :revoked}

      {:error, :not_admitted} ->
        case check_effect_custody([
               %{lease_id: id, surface_id: surface, holder_agent_uuid: holder}
             ]) do
          :ok -> {:error, :effect_in_progress}
          error -> error
        end

      {:error, {:prepare_failed, reason}} ->
        {:error, reason}

      {:error, _} ->
        {:error, :custody_unavailable}
    end
  end

  def begin_effect_custody(_, _, _), do: {:error, :custody_unavailable}

  @doc """
  Retire only this execution's reservation after its last possible filesystem
  call. Never call from an `after` clause or a timeout/recovery sweep: those do
  not establish that an outstanding filesystem operation has finished.
  """
  def finish_effect_custody(effect_id, [%{lease_id: id}]) do
    case Postgrex.query(
           DB,
           """
           UPDATE lease_plane.surface_leases SET effect_fence = NULL
           WHERE lease_id = $1 AND effect_fence = $2 AND released_at IS NULL
           """,
           [uuid_to_binary(id), effect_id]
         ) do
      {:ok, %{num_rows: 1}} -> :ok
      _ -> {:error, :custody_unavailable}
    end
  end

  def finish_effect_custody(_, _), do: {:error, :custody_unavailable}

  @doc false
  def custody_error(%Postgrex.Error{postgres: %{constraint: "effect_custody_reserved"}}),
    do: :effect_in_progress

  def custody_error(reason), do: reason

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
      {:error, reason} -> {:error, custody_error(reason)}
    end
  end

  @doc false
  def lock_active_lease(conn, lease_id) do
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
        "WHERE released_at IS NULL AND effect_fence IS NULL AND expires_at < now() " <>
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
      {:error, reason} -> {:error, custody_error(reason)}
    end
  end

  # ---------- shared helpers ----------

  @doc false
  def log_event(conn, event_type, lease, payload \\ nil) do
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

  @doc false
  def log_conflict(conn, p, existing) do
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

  @doc false
  def decode_payload(%{} = payload), do: stringify_keys(payload)

  def decode_payload(payload) when is_binary(payload) do
    case Jason.decode(payload) do
      {:ok, %{} = decoded} -> stringify_keys(decoded)
      _ -> %{"raw_payload" => payload}
    end
  end

  def decode_payload(_), do: %{}

  defp stringify_keys(map) do
    Map.new(map, fn
      {key, value} when is_atom(key) -> {Atom.to_string(key), value}
      {key, value} -> {key, value}
    end)
  end

  @doc false
  def release_event_type(reason)
      when reason in ["reaped_remote_ttl", "reaped_local_ttl", "down_local", "forced"],
      do: reason

  def release_event_type(_reason), do: "release"

  @doc false
  def iso(nil), do: nil
  def iso(%DateTime{} = dt), do: DateTime.to_iso8601(dt)

  @doc false
  def row_to_map(columns, row) do
    columns
    |> Enum.zip(row)
    |> Enum.into(%{}, fn {col, val} -> {String.to_atom(col), val} end)
  end

  # SELECT casts uuid columns to text so the boundary always sees 36-char strings.
  # For parameter binding (INSERT/UPDATE/WHERE), Postgrex requires the canonical
  # 16-byte binary form — uuid_to_binary/1 handles the conversion.
  @doc false
  def uuid_to_binary(uuid) when is_binary(uuid) and byte_size(uuid) == 16, do: uuid

  def uuid_to_binary(
        <<a::binary-size(8), "-", b::binary-size(4), "-", c::binary-size(4), "-",
          d::binary-size(4), "-", e::binary-size(12)>>
      ) do
    Base.decode16!(a <> b <> c <> d <> e, case: :mixed)
  end

  @doc false
  def normalize_tx_result({:ok, {:ok, lease, kind}}), do: {:ok, lease, kind}
  def normalize_tx_result({:ok, {:error, code, detail}}), do: {:error, code, detail}
  def normalize_tx_result({:error, reason}), do: {:error, custody_error(reason)}
end
