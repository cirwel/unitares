defmodule AgentOrchestrator.SpawnGateTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.SpawnGate
  alias AgentOrchestrator.MemoryIdempotencyLedger

  defmodule UnavailableLedger do
    def reserve(_key_hash, _digest, _execution_id, _retention_ms),
      do: {:error, :idempotency_unavailable}

    def mark_started(_key_hash, _digest, _execution_id), do: {:error, :not_called}
    def release_reservation(_key_hash, _digest, _execution_id), do: {:error, :not_called}
    def sweep, do: :ok
    def status, do: %{backend: "test-unavailable", durable: true, available: false}
  end

  setup do
    MemoryIdempotencyLedger.clear()

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

  test "replays the same execution id after SpawnGate restarts" do
    key = unique_key()
    spec = %{cmd: "sh", args: ["-c", "sleep 5"], lease: false}

    assert {:ok, execution_id, _pid, :new} = SpawnGate.start_agent(key, "digest-a", spec)

    old_gate = Process.whereis(SpawnGate)
    Process.exit(old_gate, :kill)
    new_gate = await_restarted_gate(old_gate)
    assert is_pid(new_gate)

    assert {:ok, ^execution_id, nil, :idempotent} =
             SpawnGate.start_agent(key, "digest-a", spec)
  end

  test "a replay of a crash-ambiguous reservation never spawns" do
    key = unique_key()
    execution_id = "ex-11111111-1111-4111-8111-111111111111"
    digest = String.duplicate("a", 64)

    assert {:ok, :reserved} =
             MemoryIdempotencyLedger.reserve(
               SpawnGate.hash_key(key),
               digest,
               execution_id,
               60_000
             )

    before_count = AgentOrchestrator.count()

    assert {:error, {:idempotency_outcome_unknown, ^execution_id}} =
             SpawnGate.start_agent(key, digest, %{cmd: "true", lease: false})

    assert AgentOrchestrator.count() == before_count
  end

  test "raw idempotency keys are reduced to lowercase SHA-256" do
    hash = SpawnGate.hash_key("do-not-persist-me")
    assert byte_size(hash) == 64
    assert hash =~ ~r/^[0-9a-f]{64}$/
    refute hash =~ "persist"
  end

  test "long-running spawns retain their key through the result window" do
    state = %{retention_ms: 60_000, result_retention_ms: 5_000, default_runtime_ms: 30_000}

    assert SpawnGate.retention_for(%{max_runtime_ms: 120_000}, state) == 125_000
    assert SpawnGate.retention_for(%{}, state) == 60_000
  end

  test "an unavailable durable ledger never falls back to an unkeyed spawn" do
    {:ok, gate} = start_supervised({SpawnGate, name: nil, ledger: UnavailableLedger})
    before_count = AgentOrchestrator.count()

    assert {:error, :idempotency_unavailable} =
             SpawnGate.start_agent(gate, unique_key(), "digest-a", %{
               cmd: "true",
               lease: false
             })

    assert AgentOrchestrator.count() == before_count
  end

  defp await_restarted_gate(old_gate, attempts \\ 100)

  defp await_restarted_gate(_old_gate, 0), do: flunk("SpawnGate did not restart")

  defp await_restarted_gate(old_gate, attempts) do
    case Process.whereis(SpawnGate) do
      pid when is_pid(pid) and pid != old_gate ->
        pid

      _ ->
        Process.sleep(10)
        await_restarted_gate(old_gate, attempts - 1)
    end
  end
end
