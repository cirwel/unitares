defmodule AgentOrchestrator.PostgresIdempotencyLedger do
  @moduledoc """
  PostgreSQL-backed spawn idempotency ledger.

  Only hashes, the server-minted execution id, state, and timestamps cross the
  database boundary. Raw keys and spawn material never do. All keyed spawns
  fail closed when this ledger is unavailable; there is no silent in-memory
  downgrade that would reopen duplicate execution after a restart.
  """

  @behaviour AgentOrchestrator.IdempotencyLedger

  require Logger

  @db AgentOrchestrator.IdempotencyDB

  @reserve_sql """
  INSERT INTO orchestration.spawn_idempotency AS existing
    (key_hash, spec_digest, execution_id, state, reserved_at, started_at, expires_at)
  VALUES
    ($1, $2, $3, 'reserved', now(), NULL,
     now() + ($4::bigint * interval '1 millisecond'))
  ON CONFLICT (key_hash) DO UPDATE SET
    spec_digest = CASE WHEN existing.expires_at <= now()
                       THEN EXCLUDED.spec_digest ELSE existing.spec_digest END,
    execution_id = CASE WHEN existing.expires_at <= now()
                        THEN EXCLUDED.execution_id ELSE existing.execution_id END,
    state = CASE WHEN existing.expires_at <= now()
                 THEN EXCLUDED.state ELSE existing.state END,
    reserved_at = CASE WHEN existing.expires_at <= now()
                       THEN EXCLUDED.reserved_at ELSE existing.reserved_at END,
    started_at = CASE WHEN existing.expires_at <= now()
                      THEN EXCLUDED.started_at ELSE existing.started_at END,
    expires_at = CASE WHEN existing.expires_at <= now()
                      THEN EXCLUDED.expires_at ELSE existing.expires_at END
  RETURNING spec_digest, execution_id, state
  """

  @mark_started_sql """
  UPDATE orchestration.spawn_idempotency
     SET state = 'started', started_at = COALESCE(started_at, now())
   WHERE key_hash = $1
     AND spec_digest = $2
     AND execution_id = $3
     AND state IN ('reserved', 'started')
     AND expires_at > now()
  RETURNING execution_id
  """

  @release_sql """
  DELETE FROM orchestration.spawn_idempotency
   WHERE key_hash = $1
     AND spec_digest = $2
     AND execution_id = $3
     AND state = 'reserved'
  RETURNING execution_id
  """

  @sweep_sql "DELETE FROM orchestration.spawn_idempotency WHERE expires_at <= now()"

  @impl true
  def reserve(key_hash, digest, execution_id, retention_ms) do
    case query(@reserve_sql, [key_hash, digest, execution_id, retention_ms]) do
      {:ok, %{rows: [[stored_digest, stored_id, state]]}} ->
        classify_reservation(stored_digest, stored_id, state, digest, execution_id)

      {:ok, result} ->
        Logger.error(
          "orchestrator idempotency reserve returned an unexpected shape: #{inspect(result)}"
        )

        {:error, :idempotency_unavailable}

      {:error, reason} ->
        log_unavailable("reserve", reason)
        {:error, :idempotency_unavailable}
    end
  end

  @impl true
  def mark_started(key_hash, digest, execution_id) do
    mutation(@mark_started_sql, [key_hash, digest, execution_id], "mark_started")
  end

  @impl true
  def release_reservation(key_hash, digest, execution_id) do
    mutation(@release_sql, [key_hash, digest, execution_id], "release_reservation")
  end

  @impl true
  def sweep do
    case query(@sweep_sql, []) do
      {:ok, _result} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  @impl true
  def status do
    available =
      case query(
             "SELECT to_regclass('orchestration.spawn_idempotency') IS NOT NULL",
             [],
             timeout: 250,
             pool_timeout: 250
           ) do
        {:ok, %{rows: [[true]]}} -> true
        {:error, _reason} -> false
        _other -> false
      end

    %{backend: "postgres", durable: true, available: available}
  end

  @doc false
  def reserve_sql, do: @reserve_sql

  @doc false
  def classify_reservation(stored_digest, stored_id, state, digest, candidate_id) do
    cond do
      stored_digest != digest ->
        {:error, :idempotency_conflict}

      stored_id == candidate_id and state == "reserved" ->
        {:ok, :reserved}

      state == "reserved" ->
        {:ok, {:replay, stored_id, :reserved}}

      state == "started" ->
        {:ok, {:replay, stored_id, :started}}

      true ->
        {:error, :idempotency_unavailable}
    end
  end

  defp mutation(sql, params, operation) do
    case query(sql, params) do
      {:ok, %{num_rows: 1}} ->
        :ok

      {:ok, _result} ->
        {:error, :reservation_lost}

      {:error, reason} ->
        log_unavailable(operation, reason)
        {:error, :idempotency_unavailable}
    end
  end

  defp query(sql, params, opts \\ []) do
    if Process.whereis(@db) do
      try do
        Postgrex.query(@db, sql, params, Keyword.merge([timeout: 5_000], opts))
      catch
        :exit, reason -> {:error, reason}
      end
    else
      {:error, :database_not_started}
    end
  end

  defp log_unavailable(operation, reason) do
    Logger.error("orchestrator idempotency #{operation} unavailable: #{inspect(reason)}")
  end
end
