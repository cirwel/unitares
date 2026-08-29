defmodule AgentOrchestrator.MemoryIdempotencyLedger do
  @moduledoc """
  Process-local idempotency ledger used by the isolated test runtime.

  Production uses `PostgresIdempotencyLedger`. Keeping the test ledger in its
  own supervised process exercises SpawnGate restarts without requiring or
  mutating a developer's database.
  """

  use GenServer

  @behaviour AgentOrchestrator.IdempotencyLedger

  @default_max_entries 10_000

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @impl true
  def reserve(key_hash, digest, execution_id, retention_ms) do
    GenServer.call(__MODULE__, {:reserve, key_hash, digest, execution_id, retention_ms})
  end

  @impl true
  def mark_started(key_hash, digest, execution_id) do
    GenServer.call(__MODULE__, {:mark_started, key_hash, digest, execution_id})
  end

  @impl true
  def release_reservation(key_hash, digest, execution_id) do
    GenServer.call(__MODULE__, {:release, key_hash, digest, execution_id})
  end

  @impl true
  def sweep, do: GenServer.call(__MODULE__, :sweep)

  @impl true
  def status,
    do: %{backend: "memory", durable: false, available: Process.whereis(__MODULE__) != nil}

  @doc false
  def clear, do: GenServer.call(__MODULE__, :clear)

  @impl true
  def init(opts) do
    max_entries =
      Keyword.get(
        opts,
        :spawn_idempotency_max,
        Application.get_env(:agent_orchestrator, :spawn_idempotency_max, @default_max_entries)
      )

    {:ok, %{entries: %{}, max_entries: max_entries}}
  end

  @impl true
  def handle_call({:reserve, key, digest, execution_id, retention_ms}, _from, state) do
    now = now_ms()
    entries = expire_key(state.entries, key, now)

    case Map.get(entries, key) do
      %{digest: ^digest, execution_id: stored_id, state: stored_state} ->
        {:reply, {:ok, {:replay, stored_id, stored_state}}, %{state | entries: entries}}

      %{digest: _other_digest} ->
        {:reply, {:error, :idempotency_conflict}, %{state | entries: entries}}

      nil ->
        entry = %{
          digest: digest,
          execution_id: execution_id,
          state: :reserved,
          expires_at: now + retention_ms
        }

        {:reply, {:ok, :reserved}, %{state | entries: put_entry(entries, key, entry, state)}}
    end
  end

  def handle_call({:mark_started, key, digest, execution_id}, _from, state) do
    case Map.get(state.entries, key) do
      %{digest: ^digest, execution_id: ^execution_id} = entry ->
        entries = Map.put(state.entries, key, %{entry | state: :started})
        {:reply, :ok, %{state | entries: entries}}

      _ ->
        {:reply, {:error, :reservation_lost}, state}
    end
  end

  def handle_call({:release, key, digest, execution_id}, _from, state) do
    case Map.get(state.entries, key) do
      %{digest: ^digest, execution_id: ^execution_id, state: :reserved} ->
        {:reply, :ok, %{state | entries: Map.delete(state.entries, key)}}

      _ ->
        {:reply, {:error, :reservation_lost}, state}
    end
  end

  def handle_call(:sweep, _from, state) do
    now = now_ms()
    entries = Map.reject(state.entries, fn {_key, entry} -> entry.expires_at <= now end)
    {:reply, :ok, %{state | entries: entries}}
  end

  def handle_call(:clear, _from, state), do: {:reply, :ok, %{state | entries: %{}}}

  defp expire_key(entries, key, now) do
    case Map.get(entries, key) do
      %{expires_at: expires_at} when expires_at <= now -> Map.delete(entries, key)
      _ -> entries
    end
  end

  defp put_entry(entries, key, entry, state) do
    entries = Map.put(entries, key, entry)

    if map_size(entries) > state.max_entries do
      {oldest_key, _} = Enum.min_by(entries, fn {_key, item} -> item.expires_at end)
      Map.delete(entries, oldest_key)
    else
      entries
    end
  end

  defp now_ms, do: System.system_time(:millisecond)
end
