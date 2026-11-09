defmodule UnitaresLeasePlane.Repo.Messaging do
  @moduledoc """
  Postgrex queries for topic messaging (`lease_plane.topic_messages`,
  migration 069) and audit-outbox forwarding (`lease_plane.lease_plane_events`
  -> `audit.tool_usage`) backing `UnitaresLeasePlane.Repo`.
  """

  alias UnitaresLeasePlane.DB
  alias UnitaresLeasePlane.Repo.EffectsAndHandoffs

  @typep event :: map()

  @select_message_columns """
  message_id::text AS message_id,
  topic,
  sender_agent_uuid::text AS sender_agent_uuid,
  recipient_agent_uuid::text AS recipient_agent_uuid,
  envelope,
  response_to_id::text AS response_to_id,
  reply_depth, delivery_state, created_at, expires_at, delivered_at
  """

  # ---------- topic messages (migration 069) ----------

  @doc """
  Insert one addressed message.

  `reply_depth` is derived server-side from the parent row rather than trusted
  from the caller, so a harness cannot reset its own depth to escape the loop
  bound. A `response_to_id` naming a row that does not exist inserts nothing
  and returns `:parent_not_found` — silently dropping the thread pointer would
  turn a reply into a new conversation.
  """
  @spec send_message(map()) :: {:ok, map()} | {:error, term()}
  def send_message(%{} = p) do
    # The parent must match on topic as well as exist. Without that a third
    # agent can reply into an A<->B thread and silently relocate the whole
    # conversation onto a topic its participants are not reading.
    sql = """
    WITH parent AS (
      SELECT reply_depth, topic FROM lease_plane.topic_messages WHERE message_id = $5
    )
    INSERT INTO lease_plane.topic_messages
      (topic, sender_agent_uuid, recipient_agent_uuid, envelope,
       response_to_id, reply_depth, expires_at)
    SELECT $1, $2, $3, $4, $5,
           COALESCE((SELECT reply_depth + 1 FROM parent), 0),
           now() + make_interval(secs => $6)
    WHERE $5::uuid IS NULL OR EXISTS (SELECT 1 FROM parent WHERE topic = $1)
    RETURNING #{@select_message_columns}
    """

    params = [
      p.topic,
      EffectsAndHandoffs.uuid_to_binary(p.sender_agent_uuid),
      EffectsAndHandoffs.uuid_to_binary(p.recipient_agent_uuid),
      p.envelope,
      p.response_to_id && EffectsAndHandoffs.uuid_to_binary(p.response_to_id),
      p.ttl_s
    ]

    case Postgrex.query(DB, sql, params) do
      {:ok, %{rows: [row], columns: cols}} ->
        {:ok, row_to_message(cols, row)}

      # Zero rows means the WHERE guard failed, which now has two causes. One
      # extra query on the error path only, to tell the caller which.
      {:ok, %{rows: []}} ->
        classify_missing_parent(p.response_to_id, p.topic)

      {:error, %Postgrex.Error{postgres: %{constraint: constraint}}} ->
        {:error, message_constraint_reason(constraint)}

      {:error, e} ->
        {:error, e}
    end
  end

  @doc """
  Atomically claim and return one recipient's pending, unexpired mail.

  The claim and the read are ONE statement with `FOR UPDATE SKIP LOCKED`: two
  concurrent pollers for the same agent get disjoint sets, never the same
  message twice. The gate doc names this missing primitive explicitly ("Spawn
  needs an ATOMIC claim so two pollers cannot launch duplicate responders") —
  it is a property of the transport, so it holds here whether or not anything
  is ever built on top.

  Expired mail is never delivered and never counted; it is filtered here and
  removed later by `purge_expired_messages/1`.
  """
  @spec inbox(String.t(), pos_integer()) :: {:ok, [map()]} | {:error, term()}
  def inbox(recipient_agent_uuid, limit)
      when is_binary(recipient_agent_uuid) and is_integer(limit) and limit > 0 do
    sql = """
    WITH claimed AS (
      -- Aliased to claim_id: an unqualified `message_id` in RETURNING is
      -- ambiguous once the CTE joins the target table.
      SELECT message_id AS claim_id
      FROM lease_plane.topic_messages
      WHERE recipient_agent_uuid = $1
        AND delivery_state = 'pending'
        AND expires_at > now()
      ORDER BY created_at
      LIMIT $2
      FOR UPDATE SKIP LOCKED
    ),
    updated AS (
      UPDATE lease_plane.topic_messages m
      SET delivery_state = 'delivered', delivered_at = now()
      FROM claimed c
      WHERE m.message_id = c.claim_id
      RETURNING #{@select_message_columns}
    )
    -- UPDATE ... RETURNING has NO defined row order, so the ORDER BY inside
    -- `claimed` only decides WHICH rows are taken, not the order they come
    -- back in. Ordering the outer SELECT is what actually makes the inbox
    -- oldest-first; without it a caller reconstructing a thread from a batch
    -- can see a reply before the message it answers.
    SELECT * FROM updated ORDER BY created_at
    """

    case Postgrex.query(DB, sql, [EffectsAndHandoffs.uuid_to_binary(recipient_agent_uuid), limit]) do
      {:ok, %{rows: rows, columns: cols}} ->
        {:ok, Enum.map(rows, &row_to_message(cols, &1))}

      {:error, e} ->
        {:error, e}
    end
  end

  @doc """
  Delete expired messages, delivered or not.

  Expiry is the property that keeps this table from becoming what it replaced:
  45 of 59 governance-KG channel notes were still `status='open'` because a
  note has no natural end. A message that outlived its TTL is gone.
  """
  @spec purge_expired_messages(pos_integer()) :: {:ok, non_neg_integer()} | {:error, term()}
  def purge_expired_messages(limit \\ 1_000) when is_integer(limit) and limit > 0 do
    sql = """
    WITH doomed AS (
      SELECT message_id FROM lease_plane.topic_messages
      WHERE expires_at <= now()
      ORDER BY expires_at
      LIMIT $1
    )
    DELETE FROM lease_plane.topic_messages m
    USING doomed d
    WHERE m.message_id = d.message_id
    """

    case Postgrex.query(DB, sql, [limit]) do
      {:ok, %{num_rows: n}} -> {:ok, n}
      {:error, e} -> {:error, e}
    end
  end

  defp classify_missing_parent(response_to_id, topic) do
    sql = "SELECT topic FROM lease_plane.topic_messages WHERE message_id = $1"

    case Postgrex.query(DB, sql, [EffectsAndHandoffs.uuid_to_binary(response_to_id)]) do
      {:ok, %{rows: [[parent_topic]]}} when parent_topic != topic -> {:error, :topic_mismatch}
      {:ok, %{rows: []}} -> {:error, :parent_not_found}
      # The parent exists on this topic yet the insert matched nothing: it was
      # deleted or its topic changed between the two statements. Report the
      # honest race rather than guessing which.
      {:ok, _} -> {:error, :parent_not_found}
      {:error, e} -> {:error, e}
    end
  end

  defp row_to_message(columns, row) do
    columns
    |> Enum.zip(row)
    |> Enum.into(%{}, fn
      {"envelope", val} -> {:envelope, EffectsAndHandoffs.decode_payload(val)}
      {col, val} -> {String.to_atom(col), val}
    end)
  end

  # Constraint names are the typed vocabulary here: a CHECK violation is a
  # caller error (422), never a transient outage (503), so it must not fall
  # through to the generic error arm.
  defp message_constraint_reason("topic_messages_reply_depth_bounded"), do: :reply_depth_exceeded
  defp message_constraint_reason("topic_messages_ttl_bounded"), do: :ttl_out_of_range
  defp message_constraint_reason("topic_messages_topic_grammar"), do: :invalid_topic
  defp message_constraint_reason("topic_messages_not_self_addressed"), do: :self_addressed
  defp message_constraint_reason("topic_messages_envelope_is_object"), do: :envelope_not_object
  defp message_constraint_reason(other), do: {:constraint_violation, other}

  # ---------- audit outbox ----------

  @spec unforwarded_events(pos_integer(), keyword()) :: {:ok, [event]} | {:error, term()}
  def unforwarded_events(limit, opts \\ []) when is_integer(limit) and limit > 0 do
    {where, args} =
      case Keyword.fetch(opts, :surface_id) do
        {:ok, surface_id} -> {"forwarded_at IS NULL AND surface_id = $2", [limit, surface_id]}
        :error -> {"forwarded_at IS NULL", [limit]}
      end

    # forward_attempts ASC before ts ASC: rows that keep failing sink behind
    # fresh work instead of head-of-line blocking the batch, but are still
    # retried once fresher rows drain — so a repaired downstream (e.g. the
    # 2026-06 audit partition gap, migration 045) self-heals with no operator
    # action and no parked/dead-letter state. The wedge this fixes: 2,199
    # rows stuck on a partition hole monopolized every ORDER BY ts ASC batch
    # for 11 days — zero forwards, 31k attempts per row.
    sql = """
    SELECT
      event_id::text AS event_id,
      ts, event_type, lease_id::text AS lease_id,
      surface_id, surface_kind,
      holder_agent_uuid::text AS holder_agent_uuid,
      holder_class, advisory_mode, payload, earned_status
    FROM lease_plane.lease_plane_events
    WHERE #{where}
    ORDER BY forward_attempts ASC, ts ASC
    LIMIT $1
    """

    case Postgrex.query(DB, sql, args) do
      {:ok, %{rows: rows, columns: cols}} ->
        {:ok, Enum.map(rows, &EffectsAndHandoffs.row_to_map(cols, &1))}

      {:error, e} ->
        {:error, e}
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

    case Postgrex.query(conn, sql, [EffectsAndHandoffs.uuid_to_binary(event_id)]) do
      {:ok, %{rows: [row], columns: cols}} -> {:ok, EffectsAndHandoffs.row_to_map(cols, row)}
      {:ok, %{rows: []}} -> {:error, :not_found}
      {:error, e} -> {:error, e}
    end
  end

  # Pre-encoded JSON binary bound through `($7::text)::jsonb` (never bare
  # `$7::jsonb`) so Postgrex does not double-encode the binary as a jsonb
  # string.
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
      UnitaresLeasePlane.SessionRef.reference(Map.get(payload, "audit_session")),
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

    case Postgrex.query(conn, sql, [EffectsAndHandoffs.uuid_to_binary(event_id)]) do
      {:ok, _} -> :ok
      {:error, e} -> {:error, e}
    end
  end

  defp bump_forward_attempts(event_id) do
    Postgrex.query(
      DB,
      "UPDATE lease_plane.lease_plane_events SET forward_attempts = forward_attempts + 1 WHERE event_id = $1",
      [EffectsAndHandoffs.uuid_to_binary(event_id)]
    )
  end

  defp tool_usage_payload(event) do
    # A lease recorded before acquire ingest hashed keyed ids may still hold
    # one raw; audit.tool_usage is readable by other callers (SessionRef).
    event_payload =
      case EffectsAndHandoffs.decode_payload(event.payload) do
        %{"audit_session" => session} = decoded ->
          Map.put(decoded, "audit_session", UnitaresLeasePlane.SessionRef.reference(session))

        decoded ->
          decoded
      end

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
end
