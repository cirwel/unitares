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

    test "stop/2 does not crash when the agent died between lookup and the call" do
      {:ok, id, pid} = AgentOrchestrator.run(%{cmd: "sleep", args: ["30"], lease: false})
      # Brutal kill: terminate/2 does NOT run and the Registry :DOWN is still in
      # flight, so stop/2's whereis/0 may still return this now-dead pid and reach
      # GenServer.stop on a dead process. It must NOT exit/crash the caller —
      # :ok (stop of an already-gone agent) or :not_found (whereis lost the race),
      # never an uncaught :noproc exit. (Regression for the on_exit cleanup flake.)
      Process.exit(pid, :kill)
      assert AgentOrchestrator.stop(id) in [:ok, {:error, :not_found}]
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

  describe "lineage provisioning" do
    @parent_uuid "33333333-3333-4333-8333-333333333333"

    test "provisions UNITARES_PARENT_AGENT_ID and UNITARES_SPAWN_REASON into the child env" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "$UNITARES_PARENT_AGENT_ID|$UNITARES_SPAWN_REASON")],
          lineage: %{parent_agent_uuid: @parent_uuid, spawn_reason: "explicit"}
        })

      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.output == ["#{@parent_uuid}|explicit"]
      assert result.lineage == :provisioned
    end

    test "spawn_reason defaults to subagent" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_SPAWN_REASON"],
          lineage: %{parent_agent_uuid: @parent_uuid}
        })

      assert {:ok, %{output: ["subagent"]}} = AgentOrchestrator.await(id)
    end

    test "an explicit env entry wins over the provisioned value" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "$UNITARES_PARENT_AGENT_ID|$UNITARES_SPAWN_REASON")],
          env: [{"UNITARES_PARENT_AGENT_ID", "caller-override"}],
          lineage: %{parent_agent_uuid: @parent_uuid}
        })

      # Parent var overridden by the explicit entry; spawn_reason still provisioned.
      assert {:ok, %{output: ["caller-override|subagent"]}} = AgentOrchestrator.await(id)
    end

    test "without :lineage nothing is provisioned and the result says :none" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          # ${VAR-unset}: distinguish absent from empty.
          args: ["-c", ~s(echo "${UNITARES_PARENT_AGENT_ID-unset}")]
        })

      assert {:ok, result} = AgentOrchestrator.await(id)
      assert result.output == ["unset"]
      assert result.lineage == :none
    end

    test "refuses to spawn on a malformed parent UUID (no acquire, no child)" do
      assert {:error, {:invalid_lineage, {:parent_agent_uuid_not_uuid, "not-a-uuid"}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["x"],
                 lineage: %{parent_agent_uuid: "not-a-uuid"},
                 lease_client: StubLeaseClient
               })

      # Validation runs before the lease path — no plane churn for a config error.
      refute_receive {:lease_event, _}, 100
    end

    test "refuses a lineage map with no parent_agent_uuid" do
      assert {:error, {:invalid_lineage, {:parent_agent_uuid_not_uuid, nil}}} =
               AgentOrchestrator.run(%{cmd: "echo", args: ["x"], lineage: %{}})
    end

    test "refuses a non-map lineage value" do
      assert {:error, {:invalid_lineage, {:lineage_not_map, "bad"}}} =
               AgentOrchestrator.run(%{cmd: "echo", args: ["x"], lineage: "bad"})
    end

    test "refuses an empty spawn_reason" do
      assert {:error, {:invalid_lineage, {:spawn_reason_invalid, ""}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["x"],
                 lineage: %{parent_agent_uuid: @parent_uuid, spawn_reason: ""}
               })
    end

    test "the inverse partial override: explicit spawn_reason, provisioned parent" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "$UNITARES_PARENT_AGENT_ID|$UNITARES_SPAWN_REASON")],
          env: [{"UNITARES_SPAWN_REASON", "caller-reason"}],
          lineage: %{parent_agent_uuid: @parent_uuid}
        })

      assert {:ok, %{output: ["#{@parent_uuid}|caller-reason"]}} = AgentOrchestrator.await(id)
    end

    test "env: nil with lineage provisions without crashing (lease-orphan regression)" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_SPAWN_REASON"],
          env: nil,
          lineage: %{parent_agent_uuid: @parent_uuid}
        })

      assert {:ok, %{output: ["subagent"], exit_status: 0}} = AgentOrchestrator.await(id)
    end
  end


  describe "server-url provisioning" do
    test "provisions UNITARES_SERVER_URL into the child env" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_SERVER_URL"],
          server_url: "http://localhost:9999"
        })

      assert {:ok, %{output: ["http://localhost:9999"], exit_status: 0}} =
               AgentOrchestrator.await(id)
    end

    test "an explicit env entry wins over the provisioned URL" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_SERVER_URL"],
          env: [{"UNITARES_SERVER_URL", "http://caller:1"}],
          server_url: "http://provisioned:2"
        })

      assert {:ok, %{output: ["http://caller:1"]}} = AgentOrchestrator.await(id)
    end

    test "refuses a URL without an http(s) scheme (no acquire, no child)" do
      assert {:error, {:invalid_server_url, {:server_url_not_http, "localhost:8767"}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["x"],
                 server_url: "localhost:8767",
                 lease_client: StubLeaseClient
               })

      refute_receive {:lease_event, _}, 100
    end

    test "refuses a non-string server_url" do
      assert {:error, {:invalid_server_url, {:server_url_not_string, 8767}}} =
               AgentOrchestrator.run(%{cmd: "echo", args: ["x"], server_url: 8767})
    end

    test "env: nil with server_url provisions without crashing" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_SERVER_URL"],
          env: nil,
          server_url: "http://localhost:9999"
        })

      assert {:ok, %{output: ["http://localhost:9999"], exit_status: 0}} =
               AgentOrchestrator.await(id)
    end

    test "composes with lineage provisioning" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "$UNITARES_PARENT_AGENT_ID|$UNITARES_SERVER_URL")],
          lineage: %{parent_agent_uuid: "44444444-4444-4444-8444-444444444444"},
          server_url: "https://gov.example:8767"
        })

      assert {:ok, %{output: ["44444444-4444-4444-8444-444444444444|https://gov.example:8767"]}} =
               AgentOrchestrator.await(id)
    end
  end

  describe "session-anchor provisioning (thread-stable identity)" do
    test "provisions UNITARES_CLIENT_SESSION_ID into the child env" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_CLIENT_SESSION_ID"],
          client_session_id: "agent:/thread-discord-42"
        })

      assert {:ok, %{output: ["agent:/thread-discord-42"], exit_status: 0}} =
               AgentOrchestrator.await(id)
    end

    test "provisions UNITARES_ORCHESTRATED=1 alongside the anchor (fail-closed marker)" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "$UNITARES_CLIENT_SESSION_ID|$UNITARES_ORCHESTRATED")],
          client_session_id: "agent:/thread-1"
        })

      assert {:ok, %{output: ["agent:/thread-1|1"], exit_status: 0}} =
               AgentOrchestrator.await(id)
    end

    test "no anchor => no orchestration marker (the marker never travels alone)" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "${UNITARES_ORCHESTRATED-unset}")]
        })

      assert {:ok, %{output: ["unset"], exit_status: 0}} = AgentOrchestrator.await(id)
    end

    test "an explicit env entry wins over the provisioned anchor" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_CLIENT_SESSION_ID"],
          env: [{"UNITARES_CLIENT_SESSION_ID", "agent:/caller-anchor"}],
          client_session_id: "agent:/provisioned-anchor"
        })

      assert {:ok, %{output: ["agent:/caller-anchor"]}} = AgentOrchestrator.await(id)
    end

    test "refuses a blank anchor (no acquire, no child)" do
      assert {:error, {:invalid_client_session_id, {:client_session_id_blank, "  "}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["x"],
                 client_session_id: "  ",
                 lease_client: StubLeaseClient
               })

      refute_receive {:lease_event, _}, 100
    end

    test "refuses a non-string anchor" do
      assert {:error, {:invalid_client_session_id, {:client_session_id_not_string, 123}}} =
               AgentOrchestrator.run(%{cmd: "echo", args: ["x"], client_session_id: 123})
    end

    test "without the key nothing is provisioned" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", ~s(echo "${UNITARES_CLIENT_SESSION_ID-unset}")]
        })

      assert {:ok, %{output: ["unset"], exit_status: 0}} = AgentOrchestrator.await(id)
    end

    test "env: nil with client_session_id provisions without crashing" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: ["-c", "echo $UNITARES_CLIENT_SESSION_ID"],
          env: nil,
          client_session_id: "agent:/thread-7"
        })

      assert {:ok, %{output: ["agent:/thread-7"], exit_status: 0}} =
               AgentOrchestrator.await(id)
    end

    test "composes with lineage and server-url provisioning" do
      {:ok, id, _} =
        AgentOrchestrator.run(%{
          cmd: "sh",
          args: [
            "-c",
            ~s(echo "$UNITARES_PARENT_AGENT_ID|$UNITARES_SERVER_URL|$UNITARES_CLIENT_SESSION_ID")
          ],
          lineage: %{parent_agent_uuid: "55555555-5555-4555-8555-555555555555"},
          server_url: "https://gov.example:8767",
          client_session_id: "agent:/thread-9"
        })

      assert {:ok,
              %{
                output: [
                  "55555555-5555-4555-8555-555555555555|https://gov.example:8767|agent:/thread-9"
                ]
              }} = AgentOrchestrator.await(id)
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
