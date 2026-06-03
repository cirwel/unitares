defmodule AgentOrchestrator do
  @moduledoc """
  Public facade for the BEAM-native ephemeral-agent orchestrator.

  Spawn an ephemeral agent as an OTP-supervised external process, optionally
  bound to a lease on the plane; await it, snapshot it, or fan out a fleet.

      {:ok, id, _pid} = AgentOrchestrator.run(%{cmd: "echo", args: ["hello"]})
      {:ok, %{exit_status: 0, output: ["hello"]}} = AgentOrchestrator.await(id)

  Lease-bound (requires the lease plane up + `LEASE_PLANE_BEARER_TOKEN`):

      {:ok, id, _} = AgentOrchestrator.run(%{cmd: "claude", args: ["-p", task], lease: %{}})

  See `AgentOrchestrator.AgentRunner` for the full spec.
  """

  alias AgentOrchestrator.{AgentRunner, AgentSupervisor}

  @doc "Spawn a supervised ephemeral agent. Returns `{:ok, agent_id, pid}`."
  @spec run(map()) :: {:ok, String.t(), pid()} | {:error, term()}
  defdelegate run(spec), to: AgentSupervisor, as: :start_agent

  @doc "Spawn a fleet; returns the list of `{:ok, agent_id, pid}` / `{:error, _}` results in order."
  @spec run_fleet([map()]) :: [{:ok, String.t(), pid()} | {:error, term()}]
  def run_fleet(specs) when is_list(specs), do: Enum.map(specs, &run/1)

  defdelegate await(agent_id, timeout \\ 30_000), to: AgentRunner
  defdelegate snapshot(agent_id), to: AgentRunner
  defdelegate stop(agent_id, reason \\ :operator_stop), to: AgentRunner
  defdelegate list(), to: AgentRunner

  @doc "Count of live supervised agents."
  @spec count() :: non_neg_integer()
  defdelegate count(), to: AgentSupervisor
end
