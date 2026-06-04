defmodule AgentOrchestratorTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.StubLeaseClient

  setup do
    Application.put_env(:agent_orchestrator, :test_pid, self())
    Application.put_env(:agent_orchestrator, :stub_acquire_result, {:ok, "stub-lease"})
    # Presence is default-on, so a spec WITHOUT an injected lease_client uses the
    # real LeasePlaneClient. Null the bearer so that path is a deterministic
    # no-network :no_bearer fast-fail (→ presence :unregistered) regardless of
    # the shell env — tests must never hit the live plane.
    Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)

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

  describe "await/snapshot after exit (retained result, #581 race)" do
    test "await after a fast agent has already exited returns the retained result" do
      {:ok, id, pid} = AgentOrchestrator.run(%{cmd: "echo", args: ["fast"]})

      # Drive the race deterministically: wait until the runner is fully gone
      # before awaiting, so whereis/0 is nil (or the call exits :noproc). Before
      # the ResultStore this returned {:error, :not_found} and lost the result.
      assert eventually(fn -> id not in AgentOrchestrator.list() end)
      refute Process.alive?(pid)

      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.exit_status == 0
      assert result.output == ["fast"]
      assert result.running == false
    end

    test "snapshot after exit returns the retained result instead of :not_found" do
      {:ok, id, _} = AgentOrchestrator.run(%{cmd: "echo", args: ["snap"]})

      assert eventually(fn -> id not in AgentOrchestrator.list() end)

      assert {:ok, %{exit_status: 0, output: ["snap"], running: false}} =
               AgentOrchestrator.snapshot(id)
    end

    test "a non-zero fast exit is still retained for a late await" do
      {:ok, id, _} = AgentOrchestrator.run(%{cmd: "sh", args: ["-c", "echo boom 1>&2; exit 3"]})

      assert eventually(fn -> id not in AgentOrchestrator.list() end)

      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.exit_status == 3
      assert "boom" in result.output
    end

    test "await/snapshot for an agent id that never ran returns :not_found" do
      assert {:error, :not_found} = AgentOrchestrator.await("ag-never-existed")
      assert {:error, :not_found} = AgentOrchestrator.snapshot("ag-never-existed")
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
          # Brief sleep (not echo) so await/0 registers as a waiter before the
          # process exits — avoids the await-after-fast-exit race under load.
          cmd: "sh",
          args: ["-c", "sleep 0.15"],
          lease: %{holder_agent_uuid: "11111111-1111-4111-8111-111111111111"},
          lease_client: StubLeaseClient
        })

      assert_receive {:lease_event,
                      {:acquire, surface_id, "11111111-1111-4111-8111-111111111111",
                       "remote_heartbeat", 300}},
                     500

      assert surface_id == "agent:/" <> id

      assert {:ok, %{lease_id: "stub-lease", exit_status: 0, lease_released: true, presence: :registered}} =
               AgentOrchestrator.await(id)

      assert_receive {:lease_event, {:release, "stub-lease", "normal"}}, 500
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

    test "refuses to start only when a lease is explicitly required and denied" do
      Application.put_env(:agent_orchestrator, :stub_acquire_result, {:error, {:held_by_other, "other"}})

      assert {:error, {:lease_denied, {:held_by_other, "other"}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["x"],
                 lease: %{required: true},
                 lease_client: StubLeaseClient
               })
    end

    test "best-effort presence proceeds and reports :unregistered when acquire fails" do
      Application.put_env(:agent_orchestrator, :stub_acquire_result, {:error, :plane_down})

      {:ok, id, _} =
        AgentOrchestrator.run(%{cmd: "sleep", args: ["5"], lease_client: StubLeaseClient})

      # snapshot while alive — the agent proceeded despite the acquire failure.
      assert {:ok, %{lease_id: nil, presence: :unregistered, running: true}} =
               AgentOrchestrator.snapshot(id)

      assert :ok = AgentOrchestrator.stop(id)
    end
  end

  describe "presence (default-on, best-effort)" do
    # Long-lived agents + snapshot/0 (read while alive) — avoids the await race
    # where a fast command exits and unregisters before the assertion runs.
    test "registers agent:/<id> presence by default with no :lease key" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{cmd: "sleep", args: ["5"], lease_client: StubLeaseClient})

      # required:false (best-effort), surface is the agent:/ presence surface.
      assert_receive {:lease_event, {:acquire, surface_id, _uuid, "remote_heartbeat", 300}}, 500
      assert surface_id == "agent:/" <> id
      assert {:ok, %{presence: :registered, lease_id: "stub-lease", running: true}} =
               AgentOrchestrator.snapshot(id)

      assert :ok = AgentOrchestrator.stop(id)
    end

    test "lease: false disables presence (no acquire, :disabled)" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sleep",
          args: ["5"],
          lease: false,
          lease_client: StubLeaseClient
        })

      refute_receive {:lease_event, {:acquire, _, _, _, _}}, 100
      assert {:ok, %{presence: :disabled, lease_id: nil, running: true}} =
               AgentOrchestrator.snapshot(id)

      assert :ok = AgentOrchestrator.stop(id)
    end
  end
end
