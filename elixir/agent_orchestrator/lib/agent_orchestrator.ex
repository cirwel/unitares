defmodule AgentOrchestrator do
  @moduledoc """
  Public facade for the BEAM-native ephemeral-agent orchestrator.

  Spawn an ephemeral agent as an OTP-supervised external process, optionally
  bound to a lease on the plane; await it, snapshot it, or fan out a fleet.

      {:ok, execution_id, _pid} = AgentOrchestrator.run(%{cmd: "echo", args: ["hello"]})
      {:ok, %{exit_status: 0, output: ["hello"]}} = AgentOrchestrator.await(execution_id)

  Lease-bound (requires the lease plane up + `LEASE_PLANE_BEARER_TOKEN`):

      {:ok, execution_id, _} =
        AgentOrchestrator.run(%{cmd: "claude", args: ["-p", task], lease: %{}})

  Lineage-provisioned — the child env gains `UNITARES_PARENT_AGENT_ID` /
  `UNITARES_SPAWN_REASON` as CANDIDATE declarations the child declares (or
  declines) in its own onboard call; the orchestrator never onboards on the
  child's behalf:

      {:ok, execution_id, _} =
        AgentOrchestrator.run(%{
          cmd: "claude",
          args: ["-p", task],
          lineage: %{parent_agent_uuid: spawner_governance_uuid}
        })

  See `AgentOrchestrator.AgentRunner` for the full spec.
  """

  alias AgentOrchestrator.{AgentRunner, AgentSupervisor}

  @doc "Spawn a supervised ephemeral agent. Returns `{:ok, execution_id, pid}`."
  @spec run(map()) :: {:ok, String.t(), pid()} | {:error, term()}
  defdelegate run(spec), to: AgentSupervisor, as: :start_agent

  @doc "Spawn a fleet; returns `{:ok, execution_id, pid}` / `{:error, _}` results in order."
  @spec run_fleet([map()]) :: [{:ok, String.t(), pid()} | {:error, term()}]
  def run_fleet(specs) when is_list(specs), do: Enum.map(specs, &run/1)

  defdelegate await(execution_id, timeout \\ 30_000), to: AgentRunner
  defdelegate snapshot(execution_id), to: AgentRunner
  defdelegate stop(execution_id, reason \\ :operator_stop), to: AgentRunner
  defdelegate list(), to: AgentRunner

  @doc "Count of live supervised agents."
  @spec count() :: non_neg_integer()
  defdelegate count(), to: AgentSupervisor
end
