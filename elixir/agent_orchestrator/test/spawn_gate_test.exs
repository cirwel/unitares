defmodule AgentOrchestrator.SpawnGateTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.SpawnGate

  setup do
    on_exit(fn ->
      Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
    end)

    :ok
  end

  defp unique_key do
    "spawn-gate-test-" <> Integer.to_string(System.unique_integer([:positive]))
  end

  test "concurrent same-key spawns produce one execution and replay its id" do
    key = unique_key()
    digest = "digest-a"
    spec = %{cmd: "sh", args: ["-c", "sleep 5"], lease: false}

    results =
      1..8
      |> Enum.map(fn _index ->
        Task.async(fn -> SpawnGate.start_agent(key, digest, spec) end)
      end)
      |> Task.await_many(10_000)

    execution_ids = Enum.map(results, fn {:ok, execution_id, _pid, _kind} -> execution_id end)
    dispositions = Enum.map(results, fn {:ok, _id, _pid, kind} -> kind end)

    assert length(Enum.uniq(execution_ids)) == 1
    assert Enum.count(dispositions, &(&1 == :new)) == 1
    assert Enum.count(dispositions, &(&1 == :idempotent)) == 7
  end

  test "same key with a different digest fails closed" do
    key = unique_key()
    spec = %{cmd: "sh", args: ["-c", "sleep 5"], lease: false}

    assert {:ok, _execution_id, _pid, :new} = SpawnGate.start_agent(key, "digest-a", spec)

    assert {:error, :idempotency_conflict} =
             SpawnGate.start_agent(key, "digest-b", %{spec | args: ["-c", "sleep 6"]})
  end
end
