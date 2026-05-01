defmodule UnitaresLeasePlane.Repo do
  @moduledoc """
  Postgrex-backed durable mirror for `lease_plane.surface_leases` and
  `lease_plane.lease_plane_events`. Intentionally not Ecto — keeping the
  surface area minimal until v1.

  Returns shapes mirror RFC §4.5 typed-absence:
    {:ok, lease_map, :new | :idempotent}     # acquire
    {:ok, lease_map | nil}                   # status
    :ok | {:error, reason}                   # renew/release/heartbeat

  All writes also append a row to `lease_plane.lease_plane_events`
  (audit-outbox) inside the same transaction so durable truth and audit
  trail cannot diverge. Forwarding to Python-side `audit.tool_usage`
  happens in a separate Oban job (Phase A — not yet wired).
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

  defp acquire_step(_conn, p, %{holder_agent_uuid: held} = existing)
       when is_binary(held) do
    if held == p.holder_agent_uuid do
      {:ok, {:ok, existing, :idempotent}}
    else
      # PR 5 council BLOCK fix: include surface_id + blocking_lease_id so the
      # 409 response carries every field the v0.7 §7.3.2 AcquireHeldByOther shape
      # requires. Without these, Pydantic validation degrades to AcquireSchemaInvalid.
      {:ok,
       {:error, :held_by_other,
        %{
          held_by_uuid: held,
          expires_at: existing.expires_at,
          surface_id: existing.surface_id,
          blocking_lease_id: existing.lease_id
        }}}
    end
  end

  defp acquire_step(conn, p, nil) do
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

    with {:ok, %{rows: [row], columns: cols}} <- Postgrex.query(conn, insert_lease_sql, args),
         lease = row_to_map(cols, row),
         :ok <- log_event(conn, "acquire", lease) do
      {:ok, {:ok, lease, :new}}
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
                :ok <- log_event(conn, "release", lease) do
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

  # ---------- helpers ----------

  defp log_event(conn, event_type, lease) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (event_type, lease_id, surface_id, surface_kind,
       holder_agent_uuid, holder_class, advisory_mode,
       payload, earned_status)
    VALUES ($1, $2, $3, $4, $5, $6, true, $7::jsonb, $8)
    """

    args = [
      event_type,
      uuid_to_binary(lease.lease_id),
      lease.surface_id,
      lease.surface_kind,
      uuid_to_binary(lease.holder_agent_uuid),
      lease.holder_class,
      Jason.encode!(%{}),
      lease.earned_status
    ]

    case Postgrex.query(conn, sql, args) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

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
