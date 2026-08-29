defmodule AgentOrchestrator.AgentSupervisor do
  @moduledoc """
  DynamicSupervisor over `AgentRunner` processes — one per live execution.

  Children are `restart: :temporary` on purpose. Ephemeral agents are, by
  definition, not restarted on exit: a finished or crashed agent stays finished.
  The supervisor exists for *lifecycle ownership* (clean spawn, tracked teardown,
  lease release on shutdown, fan-out/collect over a known child set), not for
  crash-restart durability. That is the honest scope: this buys orchestration,
  not resurrection.
  """

  use DynamicSupervisor

  alias AgentOrchestrator.AgentRunner

  def start_link(_args), do: DynamicSupervisor.start_link(__MODULE__, [], name: __MODULE__)

  @impl true
  def init(_), do: DynamicSupervisor.init(strategy: :one_for_one)

  @doc """
  Spawn a supervised ephemeral agent. `spec` is passed through to
  `AgentRunner` (see its `t:spec/0`). Returns `{:ok, execution_id, pid}`.

  `start_agent/1` mints the execution id here and always overwrites any
  caller-supplied value. SpawnGate has a separate trusted internal path for an
  id it already server-minted and durably reserved before process start.
  `agent_id` is logical metadata and may be reused; it is never the registry
  key or lifecycle-control handle.
  """
  @spec start_agent(map()) :: {:ok, String.t(), pid()} | {:error, term()}
  def start_agent(%{} = spec) do
    execution_id = AgentRunner.generate_execution_id()
    start_with_execution_id(spec, execution_id)
  end

  @doc false
  # Trusted internal path for SpawnGate: the server mints and durably reserves
  # the id before opening the OS process. This is not caller control — the HTTP
  # spec still cannot provide or override an execution id.
  @spec start_reserved_agent(map(), String.t()) :: {:ok, String.t(), pid()} | {:error, term()}
  def start_reserved_agent(%{} = spec, "ex-" <> _ = execution_id) do
    start_with_execution_id(spec, execution_id)
  end

  defp start_with_execution_id(spec, execution_id) do
    agent_id = Map.get(spec, :agent_id) || execution_id

    spec =
      spec
      |> Map.put(:agent_id, agent_id)
      |> Map.put(:execution_id, execution_id)

    child = %{
      id: {AgentRunner, execution_id},
      start: {AgentRunner, :start_link, [spec]},
      restart: :temporary
    }

    case DynamicSupervisor.start_child(__MODULE__, child) do
      {:ok, pid} -> {:ok, execution_id, pid}
      {:error, {:already_started, _pid}} -> {:error, {:already_running, execution_id}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "Number of live agents under supervision."
  @spec count() :: non_neg_integer()
  def count, do: DynamicSupervisor.count_children(__MODULE__).active
end
