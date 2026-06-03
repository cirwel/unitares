defmodule AgentOrchestratorTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.StubLeaseClient

  setup do
    Application.put_env(:agent_orchestrator, :test_pid, self())
    Application.put_env(:agent_orchestrator, :stub_acquire_result, {:ok, "stub-lease"})

    on_exit(fn ->
      Application.delete_env(:agent_orchestrator, :test_pid)
      Application.delete_env(:agent_orchestrator, :stub_acquire_result)
      # Tear down any agents a test left running.
      Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
    end)

    :ok
  end

  # Poll a condition for up to ~250ms (async Registry/process teardown).
  defp eventually(fun, retries \\ 50) do
    cond do
      fun.() -> true
      retries <= 0 -> false
      true -> Process.sleep(5) && eventually(fun, retries - 1)
    end
  end

  describe "supervised lifecycle (no lease)" do
    test "runs an ephemeral agent and captures its output" do
      {:ok, id, pid} = AgentOrchestrator.run(%{cmd: "echo", args: ["hello world"]})
      assert is_pid(pid)
      assert {:ok, result} = AgentOrchestrator.await(id, 5_000)
      assert result.exit_status == 0
      assert result.output == ["hello world"]
      assert result.running == false
      refute Process.alive?(pid)
    end

    test "captures multi-line stdout in order" do
      {:ok, id, _} = AgentOrchestrator.run(%{cmd: "sh", args: ["-c", "printf 'a\\nb\\nc\\n'"]})
      assert {:ok, %{output: ["a", "b", "c"], exit_status: 0}} = AgentOrchestrator.await(id)
    end

    test "captures stderr (merged) and a non-zero exit status" do
      {:ok, id, _} = AgentOrchestrator.run(%{cmd: "sh", args: ["-c", "echo oops 1>&2; exit 7"]})
      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.exit_status == 7
      assert "oops" in result.output
    end

    test "passes env through to the child" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $FLEET_TASK"],
          env: [{"FLEET_TASK", "build-widget"}]
        })

      assert {:ok, %{output: ["build-widget"]}} = AgentOrchestrator.await(id)
    end

    test "refuses to start when the executable does not exist" do
      assert {:error, {:executable_not_found, "definitely-not-a-real-binary-xyz"}} =
               AgentOrchestrator.run(%{cmd: "definitely-not-a-real-binary-xyz"})
    end

    test "bounds an over-long unterminated line but still captures all of it" do
      big = String.duplicate("x", 70_000)

      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(printf '%s\\n' "$BIG")],
          env: [{"BIG", big}]
        })

      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.exit_status == 0
      # Flushed across one or more bounded fragments; no byte lost.
      assert result.output |> Enum.join() |> byte_size() == 70_000
    end
  end

  describe "fleet + registry" do
    test "list/count track live agents and stop tears one down" do
      {:ok, id, pid} = AgentOrchestrator.run(%{cmd: "sleep", args: ["30"]})
      assert id in AgentOrchestrator.list()
      assert AgentOrchestrator.count() >= 1

      assert :ok = AgentOrchestrator.stop(id)
      refute Process.alive?(pid)
      # Registry unregisters a dead process via its own async :DOWN handler, so
      # the id can linger in list/0 for a beat after the process is gone.
      assert eventually(fn -> id not in AgentOrchestrator.list() end)
    end

    test "run_fleet spawns each spec" do
      results =
        AgentOrchestrator.run_fleet([
          %{cmd: "echo", args: ["one"]},
          %{cmd: "echo", args: ["two"]}
        ])

      assert [{:ok, _, _}, {:ok, _, _}] = results
      for {:ok, id, _} <- results, do: assert({:ok, %{exit_status: 0}} = AgentOrchestrator.await(id))
    end
  end

  describe "lease binding" do
    test "acquires on spawn and releases on exit" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "echo",
          args: ["done"],
          lease: %{holder_agent_uuid: "11111111-1111-4111-8111-111111111111"},
          lease_client: StubLeaseClient
        })

      assert_receive {:lease_event,
                      {:acquire, surface_id, "11111111-1111-4111-8111-111111111111",
                       "remote_heartbeat", 300}}

      assert surface_id == "agent:" <> id

      assert {:ok, %{lease_id: "stub-lease", exit_status: 0, lease_released: true}} =
               AgentOrchestrator.await(id)

      assert_receive {:lease_event, {:release, "stub-lease", "normal"}}
    end

    test "releases the lease when the port fails to open after acquire (no orphan)" do
      assert {:error, {:executable_not_found, _}} =
               AgentOrchestrator.run(%{
                 cmd: "definitely-not-a-real-binary-xyz",
                 lease: %{holder_agent_uuid: "22222222-2222-4222-8222-222222222222"},
                 lease_client: StubLeaseClient
               })

      assert_receive {:lease_event, {:acquire, _surface, _uuid, "remote_heartbeat", 300}}
      assert_receive {:lease_event, {:release, "stub-lease", "normal"}}
    end

    test "refuses to start when a required lease is denied" do
      Application.put_env(:agent_orchestrator, :stub_acquire_result, {:error, {:held_by_other, "other"}})

      assert {:error, {:lease_denied, {:held_by_other, "other"}}} =
               AgentOrchestrator.run(%{cmd: "echo", args: ["x"], lease: %{}, lease_client: StubLeaseClient})
    end

    test "best-effort lease proceeds even when acquire fails" do
      Application.put_env(:agent_orchestrator, :stub_acquire_result, {:error, :no_bearer})

      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "echo",
          args: ["x"],
          lease: %{required: false},
          lease_client: StubLeaseClient
        })

      assert {:ok, %{lease_id: nil, exit_status: 0}} = AgentOrchestrator.await(id)
    end
  end
end
