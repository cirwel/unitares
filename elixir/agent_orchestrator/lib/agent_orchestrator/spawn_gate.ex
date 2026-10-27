defmodule AgentOrchestrator.SpawnGate do
  @moduledoc """
  Serializes explicitly idempotent HTTP spawns through a durable reservation.

  A direct `POST /v1/agents` can succeed while its response is lost. Retrying
  without a stable key would launch a second process. For keyed calls the gate:

    1. hashes the raw key in memory;
    2. durably reserves a server-minted execution id for that key + spec hash;
    3. opens the supervised OS process with exactly that id; and
    4. marks the reservation started.

  Same key + same digest replays the original execution id across orchestrator
  restarts. Same key + a different digest fails closed. A crash between the
  database reservation and OS spawn is fundamentally ambiguous because those
  effects cannot share one transaction; a still-reserved replay therefore
  returns `idempotency_outcome_unknown` and never starts another process.

  The production ledger is PostgreSQL and stores only hashes, execution ids,
  state, and timestamps — never the raw key, command, args, environment,
  secrets, or output. Callers that omit a key retain the original at-least-once
  spawn behavior.
  """

  use GenServer

  require Logger

  alias AgentOrchestrator.AgentRunner
  alias AgentOrchestrator.AgentSupervisor

  @default_retention_ms 86_400_000
  @default_sweep_interval_ms 60_000

  @type disposition :: :new | :idempotent
  @type start_result ::
          {:ok, String.t(), pid() | nil, disposition()}
          | {:error,
             :idempotency_conflict
             | :idempotency_unavailable
             | {:idempotency_outcome_unknown, String.t()}
             | term()}

  def start_link(opts \\ []) do
    name = Keyword.get(opts, :name, __MODULE__)

    if name do
      GenServer.start_link(__MODULE__, opts, name: name)
    else
      GenServer.start_link(__MODULE__, opts)
    end
  end

  @doc """
  Start a keyed spawn once, replaying its durable execution id on retry.

  `admit`, when given, runs only for a new reservation, never for a replay.
  It receives the reserved execution id and returns `{:ok, spec, on_outcome}`
  to start `spec` (the admitted spec replaces the one given here), or
  `{:error, reason}` to refuse, which releases the reservation so a later
  retry is a fresh attempt. `on_outcome` is called with `:started` or, when
  the start definitely failed, `:failed`; an ambiguous start
  (`idempotency_outcome_unknown`) may be running, so it reports neither.
  Profile limits and secrets use it, so a retry of an already-started key
  replays without either.
  """
  @spec start_agent(
          String.t(),
          String.t(),
          map(),
          (String.t() -> {:ok, map(), (:started | :failed -> any())} | {:error, term()}) | nil
        ) :: start_result()
  def start_agent(idempotency_key, digest, spec, admit \\ nil)

  def start_agent(idempotency_key, digest, spec, admit)
      when is_binary(idempotency_key) and is_binary(digest) and is_map(spec) do
    start_agent(__MODULE__, idempotency_key, digest, spec, admit)
  end

  # start_agent(server, key, digest, spec): the explicit-server form.
  def start_agent(server, idempotency_key, digest, spec)
      when is_binary(idempotency_key) and is_binary(digest) and is_map(spec) do
    start_agent(server, idempotency_key, digest, spec, nil)
  end

  @doc false
  def start_agent(server, idempotency_key, digest, spec, admit)
      when is_binary(idempotency_key) and is_binary(digest) and is_map(spec) do
    GenServer.call(server, {:start_agent, idempotency_key, digest, spec, admit}, 30_000)
  end

  @doc "Report whether keyed-spawn durability is configured and reachable."
  @spec status() :: map()
  def status, do: GenServer.call(__MODULE__, :status)

  @impl true
  def init(opts) do
    retention_ms = opt(opts, :spawn_idempotency_retention_ms, @default_retention_ms)
    sweep_ms = opt(opts, :spawn_idempotency_sweep_interval_ms, @default_sweep_interval_ms)
    result_retention_ms = opt(opts, :result_retention_ms, 300_000)
    default_runtime_ms = opt(opts, :default_max_runtime_ms, 1_800_000)

    ledger =
      Keyword.get(
        opts,
        :ledger,
        Application.get_env(
          :agent_orchestrator,
          :idempotency_ledger,
          AgentOrchestrator.PostgresIdempotencyLedger
        )
      )

    schedule_sweep(sweep_ms)

    {:ok,
     %{
       ledger: ledger,
       retention_ms: retention_ms,
       result_retention_ms: result_retention_ms,
       default_runtime_ms: default_runtime_ms,
       sweep_ms: sweep_ms
     }}
  end

  @impl true
  def handle_call({:start_agent, key, digest, spec}, from, state),
    do: handle_call({:start_agent, key, digest, spec, nil}, from, state)

  def handle_call({:start_agent, key, digest, spec, admit}, _from, state) do
    key_hash = hash_key(key)
    execution_id = AgentRunner.generate_execution_id()
    retention_ms = retention_for(spec, state)

    reply =
      case ledger_call(state.ledger, :reserve, [
             key_hash,
             digest,
             execution_id,
             retention_ms
           ]) do
        {:ok, :reserved} ->
          admit_and_start(state.ledger, key_hash, digest, execution_id, spec, admit)

        {:ok, {:replay, stored_id, :started}} ->
          {:ok, stored_id, nil, :idempotent}

        {:ok, {:replay, stored_id, :reserved}} ->
          {:error, {:idempotency_outcome_unknown, stored_id}}

        {:error, :idempotency_conflict} ->
          {:error, :idempotency_conflict}

        {:error, _reason} ->
          {:error, :idempotency_unavailable}
      end

    {:reply, reply, state}
  end

  def handle_call(:status, _from, state) do
    status =
      case ledger_call(state.ledger, :status, []) do
        %{} = result -> result
        _ -> %{backend: "unknown", durable: false, available: false}
      end

    {:reply, status, state}
  end

  @impl true
  def handle_info(:sweep, state) do
    case ledger_call(state.ledger, :sweep, []) do
      :ok ->
        :ok

      {:error, reason} ->
        Logger.warning("orchestrator idempotency sweep failed: #{inspect(reason)}")

      _ ->
        Logger.warning("orchestrator idempotency sweep returned an unexpected response")
    end

    schedule_sweep(state.sweep_ms)
    {:noreply, state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  @doc false
  def hash_key(key) when is_binary(key) do
    key
    |> then(&:crypto.hash(:sha256, &1))
    |> Base.encode16(case: :lower)
  end

  @doc false
  def retention_for(spec, state) when is_map(spec) and is_map(state) do
    runtime_ms = Map.get(spec, :max_runtime_ms, state.default_runtime_ms)

    if is_integer(runtime_ms) and runtime_ms > 0 do
      max(state.retention_ms, runtime_ms + state.result_retention_ms)
    else
      state.retention_ms
    end
  end

  defp admit_and_start(ledger, key_hash, digest, execution_id, spec, nil),
    do: start_reserved(ledger, key_hash, digest, execution_id, spec)

  defp admit_and_start(ledger, key_hash, digest, execution_id, _spec, admit) do
    case admit.(execution_id) do
      {:ok, admitted_spec, on_outcome} ->
        result = start_reserved(ledger, key_hash, digest, execution_id, admitted_spec)

        case result do
          {:error, {:idempotency_outcome_unknown, _}} -> :ok
          {:error, _definite} -> on_outcome.(:failed)
          {:ok, _, _, _} -> on_outcome.(:started)
        end

        result

      {:error, reason} ->
        case ledger_call(ledger, :release_reservation, [key_hash, digest, execution_id]) do
          :ok -> {:error, reason}
          {:error, _ledger_reason} -> {:error, :idempotency_unavailable}
        end
    end
  end

  defp start_reserved(ledger, key_hash, digest, execution_id, spec) do
    case AgentSupervisor.start_reserved_agent(spec, execution_id) do
      {:ok, ^execution_id, pid} ->
        case ledger_call(ledger, :mark_started, [key_hash, digest, execution_id]) do
          :ok -> {:ok, execution_id, pid, :new}
          {:error, _reason} -> {:error, {:idempotency_outcome_unknown, execution_id}}
        end

      {:error, reason} ->
        case ledger_call(ledger, :release_reservation, [key_hash, digest, execution_id]) do
          :ok -> {:error, reason}
          {:error, _ledger_reason} -> {:error, :idempotency_unavailable}
        end
    end
  end

  defp ledger_call(ledger, function, args) do
    try do
      apply(ledger, function, args)
    rescue
      error ->
        Logger.error(
          "orchestrator idempotency ledger #{function} raised: #{Exception.message(error)}"
        )

        {:error, :idempotency_unavailable}
    catch
      :exit, reason ->
        Logger.error("orchestrator idempotency ledger #{function} exited: #{inspect(reason)}")
        {:error, :idempotency_unavailable}
    end
  end

  defp opt(opts, key, default) do
    Keyword.get(opts, key, Application.get_env(:agent_orchestrator, key, default))
  end

  defp schedule_sweep(sweep_ms), do: Process.send_after(self(), :sweep, sweep_ms)
end
