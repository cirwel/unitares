defmodule UnitaresLeasePlane.EffectRepo do
  @moduledoc """
  Postgrex-backed durable store for the governed-effect EXECUTE half
  (`effects.payloads`, migration 052). Not Ecto — same minimal-surface posture
  as `Repo`. Scoped to execute custody: pre-image capture, crash-recovery
  reconciliation, and tombstone/quarantine state. The `record_only` path does
  NOT use this module (it rides `audit.events`).

  All UPDATEs are written so a restarted custodian or the boot recovery scanner
  can replay them safely (guarded `WHERE` clauses, idempotent marks).
  """

  alias UnitaresLeasePlane.DB

  require Logger

  @doc """
  Insert the fresh effect-payload row (rollback_state NULL). Called at propose
  time, before the custodian starts. ON CONFLICT DO NOTHING so a same-key retry
  does not error.
  """
  @spec insert_effect_payload(map()) :: :ok | {:error, term()}
  def insert_effect_payload(p) do
    sql = """
    INSERT INTO effects.payloads
      (effect_id, effect_type, payload_bytes, payload_sha256, required_leases,
       proposer_agent_uuid, idempotency_key, idempotency_digest)
    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
    ON CONFLICT (effect_id) DO NOTHING
    """

    params = [
      p.effect_id,
      p.effect_type,
      p.payload_bytes,
      p.payload_sha256,
      Jason.encode!(Map.get(p, :required_leases, [])),
      Map.get(p, :proposer_agent_uuid),
      p.idempotency_key,
      p.idempotency_digest
    ]

    case Postgrex.query(DB, sql, params) do
      {:ok, _} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Record the rollback pre-image and flip rollback_state -> 'pending'. The
  `WHERE rollback_state IS NULL` guard makes a double-call (e.g. a restarted
  custodian re-running apply) a no-op rather than clobbering a captured
  pre-image. Returns `:ok` on the first capture, `:already` if the row had
  already moved past NULL, `{:error, _}` on DB failure.
  """
  @spec record_pre_image(String.t(), String.t() | nil, binary() | nil, boolean()) ::
          :ok | :already | {:error, term()}
  def record_pre_image(effect_id, pre_image_sha256, pre_image_bytes, existed?) do
    sql = """
    UPDATE effects.payloads
       SET rollback_state = 'pending',
           pre_image_sha256 = $2,
           pre_image_bytes = $3,
           pre_image_existed = $4
     WHERE effect_id = $1 AND rollback_state IS NULL
    RETURNING effect_id
    """

    case Postgrex.query(DB, sql, [effect_id, pre_image_sha256, pre_image_bytes, existed?]) do
      {:ok, %{num_rows: 1}} -> :ok
      {:ok, %{num_rows: 0}} -> :already
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Mark the effect committed (clears rollback_state, sets committed_at).
  Idempotent via `WHERE committed_at IS NULL` — safe to call from both the
  happy path and a crash-recovery commit-forward.
  """
  @spec mark_committed(String.t()) :: :ok | {:error, term()}
  def mark_committed(effect_id) do
    sql = """
    UPDATE effects.payloads
       SET committed_at = now(), rollback_state = NULL
     WHERE effect_id = $1 AND committed_at IS NULL
    """

    case Postgrex.query(DB, sql, [effect_id]) do
      {:ok, _} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "Tombstone a rolled-back effect so a same-key retry RE-EXECUTES (§4/§5b)."
  @spec tombstone(String.t()) :: :ok | {:error, term()}
  def tombstone(effect_id), do: set_state(effect_id, "tombstoned")

  @doc "Quarantine an effect whose compensation failed — operator-first, retry unsafe."
  @spec quarantine(String.t()) :: :ok | {:error, term()}
  def quarantine(effect_id), do: set_state(effect_id, "quarantined")

  defp set_state(effect_id, state) do
    sql = "UPDATE effects.payloads SET rollback_state = $2 WHERE effect_id = $1"

    case Postgrex.query(DB, sql, [effect_id, state]) do
      {:ok, _} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "Full row fetch for a single effect, or nil when absent."
  @spec get_payload(String.t()) :: {:ok, map() | nil} | {:error, term()}
  def get_payload(effect_id) do
    sql = """
    SELECT effect_id, effect_type, payload_sha256, pre_image_sha256,
           pre_image_existed, rollback_state, committed_at, required_leases
      FROM effects.payloads
     WHERE effect_id = $1
    """

    case Postgrex.query(DB, sql, [effect_id]) do
      {:ok, %{rows: []}} -> {:ok, nil}
      {:ok, %{columns: cols, rows: [row]}} -> {:ok, row_to_map(cols, row)}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Orphans for the boot recovery scanner: pre-image captured, never committed.
  Backed by the partial index in migration 052.
  """
  @spec orphaned_payloads() :: {:ok, [map()]} | {:error, term()}
  def orphaned_payloads do
    sql = """
    SELECT effect_id, effect_type, payload_sha256, pre_image_sha256,
           pre_image_existed, rollback_state, committed_at, required_leases
      FROM effects.payloads
     WHERE rollback_state = 'pending' AND committed_at IS NULL
    """

    case Postgrex.query(DB, sql, []) do
      {:ok, %{columns: cols, rows: rows}} -> {:ok, Enum.map(rows, &row_to_map(cols, &1))}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_map(cols, row) do
    cols
    |> Enum.map(&String.to_atom/1)
    |> Enum.zip(row)
    |> Map.new()
  end
end
