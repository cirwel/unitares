defmodule AgentOrchestrator.GeneratedProfilesTest do
  @moduledoc """
  The profiles file `scripts/ops/orchestrator-profiles.py` writes
  (inference-hosts-config-v0 step 4b). `test/fixtures/generated_profiles.json`
  is its output for placeholder paths; `tests/test_orchestrator_profiles.py`
  keeps the two in step. Here the fixture must load, its requests must be
  checked as the generator intends, and the `host.claude` profile must run
  its real client end to end against a stand-in Claude CLI.
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
  @client Path.expand("../../../src/mcp_handlers/support/claude_cli_client.py", __DIR__)

  # Echoes what the client handed the CLI: its arguments, its standard input
  # and the names of its environment, in Claude's JSON result shape.
  @shim """
  import json, os, sys
  print(json.dumps({"type": "result", "is_error": False,
                    "result": sys.stdin.read(), "argv": sys.argv[1:],
                    "env": sorted(os.environ), "cwd": os.getcwd()}))
  """

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
    shim = Path.join(dir, "claude")
    File.write!(shim, "#!#{python}\n" <> @shim)
    File.chmod!(shim, 0o700)

    @fixture
    |> File.read!()
    |> Jason.decode!()
    |> update_in(["profiles", "host.claude"], fn profile ->
      profile
      |> Map.put("cmd", python)
      |> Map.put("args", ["-I", @client])
      |> put_in(["env", "set", "HA_CLI"], shim)
    end)
  end

  test "the generated file loads" do
    assert {:ok, %{"host.claude" => profile}} = @fixture |> File.read!() |> Profiles.parse()
    assert profile.lineage == "none"
    assert profile.lease == false
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
  end
end
