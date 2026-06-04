defmodule AgentOrchestrator.ResultStoreTest do
  # async: false — exercises the singleton ResultStore + its shared ETS table,
  # and mutates :result_retention_ms app env for the expiry case.
  use ExUnit.Case, async: false

  alias AgentOrchestrator.ResultStore

  # The orchestrator app (and thus the ResultStore GenServer + its named table)
  # is already started for the test run; these exercise the public API directly.

  defp unique_id, do: "ag-test-" <> Integer.to_string(System.unique_integer([:positive]))

  defp result_for(id) do
    %{
      agent_id: id,
      os_pid: 1234,
      lease_id: nil,
      presence: :disabled,
      exit_status: 0,
      running: false,
      lease_released: false,
      output: ["done"]
    }
  end

  test "fetch returns :error for an id that was never stored" do
    assert :error = ResultStore.fetch(unique_id())
  end

  test "put then fetch returns the retained result" do
    id = unique_id()
    result = result_for(id)

    assert :ok = ResultStore.put(id, result)
    assert {:ok, ^result} = ResultStore.fetch(id)
  end

  test "put overwrites a prior entry for the same id (newest wins)" do
    id = unique_id()
    assert :ok = ResultStore.put(id, %{result_for(id) | exit_status: 1})
    assert :ok = ResultStore.put(id, %{result_for(id) | exit_status: 0})

    assert {:ok, %{exit_status: 0}} = ResultStore.fetch(id)
  end

  test "fetch lazily expires an entry past its TTL" do
    prior = Application.get_env(:agent_orchestrator, :result_retention_ms)
    on_exit(fn -> restore_env(:result_retention_ms, prior) end)

    # Tighten the TTL so the entry ages out within the test window. Lazy expiry
    # reads retention from app env on each fetch, so no GenServer restart needed.
    Application.put_env(:agent_orchestrator, :result_retention_ms, 1)

    id = unique_id()
    assert :ok = ResultStore.put(id, result_for(id))
    Process.sleep(10)

    assert :error = ResultStore.fetch(id)
    # And the stale row is dropped, not just hidden.
    assert :error = ResultStore.fetch(id)
  end

  defp restore_env(_key, nil), do: Application.delete_env(:agent_orchestrator, :result_retention_ms)
  defp restore_env(key, val), do: Application.put_env(:agent_orchestrator, key, val)
end
