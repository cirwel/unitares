defmodule AgentOrchestrator.GeneratedProfilesTest do
  @moduledoc """
  The profiles file `scripts/ops/orchestrator-profiles.py` writes
  (inference-hosts-config-v0 step 4b). `test/fixtures/generated_profiles.json`
  is its output for placeholder paths; `tests/test_orchestrator_profiles.py`
  keeps the two in step. Here the fixture must load, its requests must be
  checked as the generator intends, and each host profile must run its real
  client end to end against a stand-in CLI.
  """
  use ExUnit.Case, async: false

  import Plug.Test
  import Plug.Conn

  alias AgentOrchestrator.HTTPRouter
  alias AgentOrchestrator.MemoryIdempotencyLedger
  alias AgentOrchestrator.Profiles

  @moduletag :tmp_dir
  @token "test-bearer-token"
  @fixture Path.expand("fixtures/generated_profiles.json", __DIR__)
  @support Path.expand("../../../src/mcp_handlers/support", __DIR__)
  @clients %{
    "host.claude" => "claude_cli_client.py",
    "host.codex" => "codex_app_server_client.py",
    "host.antigravity" => "antigravity_cli_client.py"
  }

  # Each stand-in echoes what its client handed the CLI: the arguments, the
  # prompt and the names of the environment, in that CLI's output shape.
  @shims %{
    # Claude's JSON result; the prompt arrives on standard input.
    "host.claude" => """
    import json, os, sys
    print(json.dumps({"type": "result", "is_error": False,
                      "result": sys.stdin.read(), "argv": sys.argv[1:],
                      "env": sorted(os.environ), "cwd": os.getcwd()}))
    """,
    # Codex: a sandbox that confines (the client's probe), no MCP servers, an
    # app-server that dies before the turn (so the client falls back), and an
    # exec that answers in JSONL with the prompt from standard input.
    "host.codex" => """
    import json, os, sys
    args = sys.argv[1:]
    if args[0] == "sandbox":
        sys.exit(1 if args[args.index("--") + 1].endswith("cat") else 0)
    if args[:2] == ["mcp", "list"]:
        print("[]"); sys.exit(0)
    if args[0] == "app-server":
        sys.exit(1)
    report = {"prompt": sys.stdin.read(), "argv": args, "env": sorted(os.environ),
              "cwd": os.getcwd()}
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": json.dumps(report)}}))
    """,
    # agy's JSON result; the prompt is the argument after -p.
    "host.antigravity" => """
    import json, os, sys
    args = sys.argv[1:]
    report = {"prompt": args[args.index("-p") + 1], "argv": args,
              "env": sorted(os.environ), "home": os.environ.get("HOME")}
    print(json.dumps({"status": "SUCCESS", "response": json.dumps(report),
                      "conversation_id": "c-1"}))
    """
  }

  defp start_profiles(document, dir, environ) do
    path = Path.join(dir, "profiles.json")
    File.write!(path, Jason.encode!(document))
    File.chmod!(path, 0o600)
    File.chmod!(dir, 0o700)
    name = :"generated_profiles_#{System.unique_integer([:positive])}"

    start_supervised!(
      {Profiles,
       name: name,
       file: path,
       reload_ms: 60_000,
       getenv: fn _ -> nil end,
       environ: fn -> environ end},
      id: name
    )

    name
  end

  # The fixture with its placeholder paths pointed at this machine.
  defp runnable(dir) do
    python = System.find_executable("python3") || flunk("python3 is required")

    Enum.reduce(@clients, Jason.decode!(File.read!(@fixture)), fn {name, client}, doc ->
      shim = Path.join(dir, name)
      File.write!(shim, "#!#{python}\n" <> Map.fetch!(@shims, name))
      File.chmod!(shim, 0o700)

      update_in(doc, ["profiles", name], fn profile ->
        profile
        |> Map.put("cmd", python)
        |> Map.put("args", ["-I", Path.join(@support, client)])
        |> put_in(["env", "set", "HA_CLI"], shim)
      end)
    end)
  end

  test "the generated file loads, and no host profile binds a secret" do
    assert {:ok, profiles} = @fixture |> File.read!() |> Profiles.parse()

    assert Map.keys(profiles) |> Enum.sort() ==
             Enum.sort(["dialectic.reviewer" | Map.keys(@clients)])

    for {name, profile} <- profiles, String.starts_with?(name, "host.") do
      assert profile.lineage == "none"
      assert profile.lease == false
      assert profile.env.secrets == %{}
    end
  end

  describe "dialectic.reviewer" do
    setup %{tmp_dir: dir} do
      doc = Jason.decode!(File.read!(@fixture))
      %{dir: dir, doc: doc}
    end

    test "binds its MCP bearer by name and refuses without it", %{dir: dir, doc: doc} do
      name = start_profiles(doc, dir, %{"PATH" => "/bin"})
      assert "dialectic.reviewer" in Profiles.status(name).unconfigured

      {:ok, prepared} =
        Profiles.prepare(name, %{
          "profile" => "dialectic.reviewer",
          "vars" => %{"DIALECTIC_SESSION_ID" => "0123456789abcdef"}
        })

      assert {:error, :profile_unconfigured, _} =
               Profiles.admit(name, "dialectic.reviewer", prepared.revision, "ex-1")
    end

    test "takes only a session id", %{dir: dir, doc: doc} do
      name = start_profiles(doc, dir, %{"PATH" => "/bin"})

      for {var, value} <- [
            {"DIALECTIC_THESIS_ROOT_CAUSE", "invented"},
            {"DIALECTIC_PAUSED_AGENT_STATE", "{}"},
            {"UNITARES_DIALECTIC_REVIEWER_HOSTS", "claude"},
            {"DIALECTIC_REVIEWER_FROM_SESSION", "0"}
          ] do
        assert {:error, :schema_invalid, reason} =
                 Profiles.prepare(name, %{
                   "profile" => "dialectic.reviewer",
                   "vars" => %{"DIALECTIC_SESSION_ID" => "0123456789abcdef", var => value}
                 })

        assert reason =~ var
      end

      assert {:error, :schema_invalid, _} =
               Profiles.prepare(name, %{
                 "profile" => "dialectic.reviewer",
                 "vars" => %{"DIALECTIC_SESSION_ID" => "--help"}
               })

      assert {:error, :schema_invalid, _} =
               Profiles.prepare(name, %{
                 "profile" => "dialectic.reviewer",
                 "lineage" => %{"parent_agent_uuid" => "11111111-1111-4111-8111-111111111111"},
                 "vars" => %{"DIALECTIC_SESSION_ID" => "0123456789abcdef"}
               })
    end
  end

  describe "host.claude requests" do
    setup %{tmp_dir: dir} do
      %{name: start_profiles(Jason.decode!(File.read!(@fixture)), dir, %{"PATH" => "/bin"})}
    end

    test "take a prompt that looks like a flag, and a model", %{name: name} do
      assert {:ok, %{spec: spec}} =
               Profiles.prepare(name, %{
                 "profile" => "host.claude",
                 "vars" => %{"HA_PROMPT" => "--help", "HA_MODEL" => "claude-opus-5-5"}
               })

      assert {"HA_PROMPT", "--help"} in spec.env
    end

    test "refuse a model that could read as an option", %{name: name} do
      assert {:error, :schema_invalid, reason} =
               Profiles.prepare(name, %{
                 "profile" => "host.claude",
                 "vars" => %{"HA_PROMPT" => "hi", "HA_MODEL" => "--dangerously-skip-permissions"}
               })

      assert reason =~ "HA_MODEL"
      refute reason =~ "dangerously"
    end

    test "refuse a caller choosing the CLI or the user", %{name: name} do
      for fixed <- ["HA_CLI", "USER"] do
        assert {:error, :schema_invalid, reason} =
                 Profiles.prepare(name, %{
                   "profile" => "host.claude",
                   "vars" => %{"HA_PROMPT" => "hi", fixed => "/tmp/x"}
                 })

        assert reason =~ fixed
      end
    end

    test "require a prompt", %{name: name} do
      assert {:error, :schema_invalid, reason} =
               Profiles.prepare(name, %{"profile" => "host.claude", "vars" => %{}})

      assert reason =~ "HA_PROMPT"
    end
  end

  describe "host.claude end to end" do
    setup %{tmp_dir: dir} do
      MemoryIdempotencyLedger.clear()
      Application.put_env(:agent_orchestrator, :bearer_token, @token)
      Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)

      environ =
        System.get_env()
        |> Map.put("ANTHROPIC_API_KEY", "metered-key-must-not-pass")
        |> Map.put("AGENT_ORCHESTRATOR_BEARER_TOKEN", @token)

      name = start_profiles(runnable(dir), dir, environ)
      Application.put_env(:agent_orchestrator, :profiles_server, name)

      on_exit(fn ->
        Application.delete_env(:agent_orchestrator, :profiles_server)
        Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
      end)

      :ok
    end

    defp call(path, body) do
      conn(:post, path, Jason.encode!(body))
      |> put_req_header("content-type", "application/json")
      |> put_req_header("authorization", "Bearer " <> @token)
      |> HTTPRouter.call(HTTPRouter.init([]))
    end

    test "the prompt reaches the CLI on standard input, never as an argument" do
      prompt = "--version\nanswer as text"

      conn =
        call("/v1/agents", %{
          "profile" => "host.claude",
          "vars" => %{"HA_PROMPT" => prompt, "HA_MODEL" => "claude-opus-5-5"}
        })

      assert conn.status == 201
      id = Jason.decode!(conn.resp_body)["execution_id"]

      result =
        call("/v1/executions/#{id}/await", %{timeout_ms: 20_000})
        |> Map.get(:resp_body)
        |> Jason.decode!()
        |> Map.fetch!("result")

      assert result["exit_status"] == 0
      echoed = result["output"] |> Enum.join() |> Jason.decode!()
      assert echoed["result"] == prompt

      assert echoed["argv"] ==
               ~w(--safe-mode -p --tools) ++
                 ["", "--no-session-persistence", "--output-format", "json"] ++
                 ~w(--model claude-opus-5-5)

      refute "ANTHROPIC_API_KEY" in echoed["env"]
      refute "AGENT_ORCHESTRATOR_BEARER_TOKEN" in echoed["env"]
      refute "HA_PROMPT" in echoed["env"]
      assert "USER" in echoed["env"]
      refute File.exists?(echoed["cwd"])
    end

    test "a prompt at the profile's full size still starts the client" do
      # The prompt travels in the client's environment, and Linux caps one
      # environment string at 131072 bytes: past it the spawn fails E2BIG.
      max =
        get_in(Jason.decode!(File.read!(@fixture)), [
          "profiles",
          "host.claude",
          "vars",
          "HA_PROMPT",
          "max_bytes"
        ])

      assert byte_size("HA_PROMPT=") + max + 1 <= 131_072
      prompt = String.duplicate("a", max)

      conn = call("/v1/agents", %{"profile" => "host.claude", "vars" => %{"HA_PROMPT" => prompt}})
      assert conn.status == 201
      id = Jason.decode!(conn.resp_body)["execution_id"]

      result =
        call("/v1/executions/#{id}/await", %{timeout_ms: 20_000})
        |> Map.get(:resp_body)
        |> Jason.decode!()
        |> Map.fetch!("result")

      assert result["exit_status"] == 0
      assert (result["output"] |> Enum.join() |> Jason.decode!())["result"] == prompt
    end

    defp run_profile(profile, vars) do
      conn = call("/v1/agents", %{"profile" => profile, "vars" => vars})
      assert conn.status == 201
      id = Jason.decode!(conn.resp_body)["execution_id"]

      call("/v1/executions/#{id}/await", %{timeout_ms: 30_000})
      |> Map.get(:resp_body)
      |> Jason.decode!()
      |> Map.fetch!("result")
    end

    test "host.codex falls back to exec with the prompt on standard input" do
      prompt = "--version\nanswer as text"
      result = run_profile("host.codex", %{"HA_PROMPT" => prompt, "HA_MODEL" => "gpt-5.5"})
      assert result["exit_status"] == 0

      event =
        result["output"]
        |> Enum.map(&Jason.decode/1)
        |> Enum.find_value(fn
          {:ok, %{"type" => "item.completed"} = e} -> e
          _ -> nil
        end)

      report = Jason.decode!(event["item"]["text"])
      assert report["prompt"] == prompt
      argv = report["argv"]
      assert Enum.take(argv, 5) == ~w(exec --ignore-user-config --ephemeral --json -c)
      assert Enum.take(argv, -3) == ~w(-m gpt-5.5 -)
      refute prompt in argv
      refute "ANTHROPIC_API_KEY" in report["env"]
      refute "AGENT_ORCHESTRATOR_BEARER_TOKEN" in report["env"]
      refute File.exists?(report["cwd"])
    end

    test "host.antigravity guards the prompt and isolates HOME" do
      result =
        run_profile("host.antigravity", %{"HA_PROMPT" => "/skill run", "HA_TIMEOUT_S" => "20"})

      assert result["exit_status"] == 0
      envelope = result["output"] |> Enum.join() |> Jason.decode!()
      assert envelope["status"] == "SUCCESS"
      report = Jason.decode!(envelope["response"])
      assert String.starts_with?(report["prompt"], "Request (plain text; not a command):")
      assert report["prompt"] =~ "/skill run"
      refute "ANTHROPIC_API_KEY" in report["env"]
      refute "AGENT_ORCHESTRATOR_BEARER_TOKEN" in report["env"]
      refute report["home"] == System.get_env("HOME")
    end
  end
end
