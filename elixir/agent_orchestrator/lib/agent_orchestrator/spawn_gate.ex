defmodule AgentOrchestrator.SpawnGate do
  @moduledoc """
  Serializes explicitly idempotent HTTP spawns and retains their execution ids.

  A direct `POST /v1/agents` can succeed while its response is lost. Retrying
  that request without a stable key would launch a second process. This
  GenServer closes that live-orchestrator ambiguity: one idempotency key plus
  one canonical spawn digest maps to exactly one server-minted execution id.
  Concurrent retries queue behind the first spawn and replay its handle.

  The mapping is intentionally bounded and process-local. It survives a lost
  HTTP response, not an orchestrator restart; persisting arbitrary spawn specs
  or captured model output needs a separately reviewed durable-storage and
  retention contract. Callers that omit an idempotency key keep the original
  at-least-once spawn behavior.
  """

  use GenServer

  alias AgentOrchestrator.AgentSupervisor

  @default_retention_ms 300_000
  @default_sweep_interval_ms 60_000
  @default_max_entries 10_000

  @type disposition :: :new | :idempotent
  @type start_result ::
          {:ok, String.t(), pid() | nil, disposition()}
          | {:error, :idempotency_conflict | term()}

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc """
  Start `spec` once for `idempotency_key` and `digest`.

  Same key + same digest replays the original execution id. Same key + a
  different digest fails closed with `:idempotency_conflict`.
  """
  @spec start_agent(String.t(), String.t(), map()) :: start_result()
  def start_agent(idempotency_key, digest, spec)
      when is_binary(idempotency_key) and is_binary(digest) and is_map(spec) do
    GenServer.call(__MODULE__, {:start_agent, idempotency_key, digest, spec}, 30_000)
  end

  @impl true
  def init(opts) do
    retention_ms =
      opt(
        opts,
        :spawn_idempotency_retention_ms,
        Application.get_env(:agent_orchestrator, :result_retention_ms, @default_retention_ms)
      )

    sweep_ms = opt(opts, :spawn_idempotency_sweep_interval_ms, @default_sweep_interval_ms)
    max_entries = opt(opts, :spawn_idempotency_max, @default_max_entries)
    schedule_sweep(sweep_ms)

    {:ok,
     %{
       entries: %{},
       retention_ms: retention_ms,
       sweep_ms: sweep_ms,
       max_entries: max_entries
     }}
  end

  @impl true
  def handle_call({:start_agent, key, digest, spec}, _from, state) do
    # Expire only the addressed key on the hot path. The periodic sweep handles
    # the full map; scanning all retained keys for every spawn would turn a
    # retry-safety feature into O(n) work per request at fleet scale.
    state = expire_key(state, key)

    case Map.get(state.entries, key) do
      %{digest: ^digest, execution_id: execution_id} ->
        {:reply, {:ok, execution_id, nil, :idempotent}, state}

      %{digest: _other_digest} ->
        {:reply, {:error, :idempotency_conflict}, state}

      nil ->
        case AgentSupervisor.start_agent(spec) do
          {:ok, execution_id, pid} ->
            entry = %{
              digest: digest,
              execution_id: execution_id,
              inserted_at: now_ms()
            }

            state = put_entry(state, key, entry)
            {:reply, {:ok, execution_id, pid, :new}, state}

          {:error, reason} ->
            # A failed spawn never poisons the key; the caller may correct the
            # transient condition and safely retry the same material request.
            {:reply, {:error, reason}, state}
        end
    end
  end

  @impl true
  def handle_info(:sweep, state) do
    state = sweep(state)
    schedule_sweep(state.sweep_ms)
    {:noreply, state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  defp opt(opts, key, default) do
    Keyword.get(opts, key, Application.get_env(:agent_orchestrator, key, default))
  end

  defp put_entry(state, key, entry) do
    entries = Map.put(state.entries, key, entry)

    entries =
      if map_size(entries) > state.max_entries do
        {oldest_key, _entry} = Enum.min_by(entries, fn {_key, item} -> item.inserted_at end)
        Map.delete(entries, oldest_key)
      else
        entries
      end

    %{state | entries: entries}
  end

  defp sweep(state) do
    cutoff = now_ms() - state.retention_ms

    entries =
      Map.reject(state.entries, fn {_key, entry} -> entry.inserted_at <= cutoff end)

    %{state | entries: entries}
  end

  defp expire_key(state, key) do
    cutoff = now_ms() - state.retention_ms

    case Map.get(state.entries, key) do
      %{inserted_at: inserted_at} when inserted_at <= cutoff ->
        %{state | entries: Map.delete(state.entries, key)}

      _entry_or_nil ->
        state
    end
  end

  defp schedule_sweep(sweep_ms), do: Process.send_after(self(), :sweep, sweep_ms)
  defp now_ms, do: System.monotonic_time(:millisecond)
end
