defmodule AgentOrchestrator.StubLeaseClient do
  @moduledoc """
  Test double for `AgentOrchestrator.LeasePlaneClient.Behaviour`.

  Reports every acquire/release to the pid in `:agent_orchestrator, :test_pid`
  and returns the canned result in `:agent_orchestrator, :stub_acquire_result`
  (default `{:ok, "stub-lease"}`), so tests can assert lease lifecycle without a
  live plane.
  """

  @behaviour AgentOrchestrator.LeasePlaneClient.Behaviour

  @impl true
  def acquire(surface_id, holder_agent_uuid, holder_kind, ttl_s) do
    notify({:acquire, surface_id, holder_agent_uuid, holder_kind, ttl_s})
    Application.get_env(:agent_orchestrator, :stub_acquire_result, {:ok, "stub-lease"})
  end

  @impl true
  def release(lease_id, reason) do
    notify({:release, lease_id, reason})
    :ok
  end

  defp notify(event) do
    case Application.get_env(:agent_orchestrator, :test_pid) do
      pid when is_pid(pid) -> send(pid, {:lease_event, event})
      _ -> :ok
    end
  end
end
