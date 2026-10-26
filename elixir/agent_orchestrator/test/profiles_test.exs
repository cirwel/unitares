defmodule AgentOrchestrator.ProfilesTest.FlakyLedger do
  @moduledoc false
  # The memory ledger, with a switch that makes mark_started fail.
  alias AgentOrchestrator.MemoryIdempotencyLedger, as: Memory

  def fail_mark_started(on?), do: :persistent_term.put({__MODULE__, :fail}, on?)
  defp failing?, do: :persistent_term.get({__MODULE__, :fail}, false)

  def reserve(k, d, id, ms), do: Memory.reserve(k, d, id, ms)

  def mark_started(k, d, id),
    do: if(failing?(), do: {:error, :db_down}, else: Memory.mark_started(k, d, id))

  def release_reservation(k, d, id), do: Memory.release_reservation(k, d, id)
  def sweep, do: Memory.sweep()
  def status, do: Memory.status()
end

defmodule AgentOrchestrator.ProfilesTest do
  @moduledoc """
  Named launch profiles (inference-hosts-config-v0 §2.5): loading, the closed
  request schema, the child environment, secret redaction, rate limits, and
  the raw-spawn switch, in-process and over the HTTP surface.
  """
  use ExUnit.Case, async: false

  import Plug.Test
  import Plug.Conn

  alias AgentOrchestrator.HTTPRouter
  alias AgentOrchestrator.MemoryIdempotencyLedger
  alias AgentOrchestrator.Profiles
  alias AgentOrchestrator.ProfilesTest.FlakyLedger

  @moduletag :tmp_dir
  @token "test-bearer-token"
  @secret "s3cret-value-for-redaction"

  defp profile(overrides \\ %{}) do
    Map.merge(
      %{
        "cmd" => "/bin/sh",
        "args" => ["-c", "echo \"$GREETING|$CHILD_KEY|${LEAKY:-unset}|$FIXED\""],
        "env" => %{
          "inherit" => ["PATH"],
          "set" => %{"FIXED" => "fixed-value"},
          "secrets" => %{"CHILD_KEY" => "ORCH_SECRET"}
        },
        "vars" => %{
          "GREETING" => %{"max_bytes" => 32, "pattern" => "[a-z ]+", "required" => true}
        },
        "lease" => false,
        "max_runtime_ms" => 10_000,
        "max_output_lines" => 50,
        "max_concurrent" => 1,
        "max_per_hour" => 3
      },
      overrides
    )
  end

  defp write_file(dir, profiles) do
    path = Path.join(dir, "profiles.json")

    File.write!(
      path,
      Jason.encode!(%{"schema" => "unitares.orchestrator_profiles.v1", "profiles" => profiles})
    )

    File.chmod!(path, 0o600)
    File.chmod!(dir, 0o700)
    path
  end

  defp env_fun(map), do: fn name -> Map.get(map, name) end

  # The revision a request prepared now would carry.
  defp rev(server, profile) do
    :sys.get_state(server).profiles |> Map.fetch!(profile) |> Map.fetch!(:digest)
  end

  defp start_profiles(path, extra \\ []) do
    name = :"profiles_#{System.unique_integer([:positive])}"

    opts =
      [
        name: name,
        file: path,
        raw_spawn: Keyword.get(extra, :raw_spawn),
        reload_ms: Keyword.get(extra, :reload_ms, 60_000),
        getenv:
          Keyword.get_lazy(extra, :getenv, fn ->
            env_fun(Keyword.get(extra, :env, %{"ORCH_SECRET" => @secret}))
          end),
        environ: fn -> Keyword.get(extra, :environ, %{"PATH" => "/bin", "LEAKY" => "leaked"}) end
      ] ++ Keyword.drop(extra, [:raw_spawn, :reload_ms, :env, :environ, :getenv])

    start_supervised!({Profiles, opts}, id: name)
    name
  end

  describe "parse/1" do
    test "accepts a valid file and refuses unknown keys by name" do
      ok =
        Jason.encode!(%{
          "schema" => "unitares.orchestrator_profiles.v1",
          "profiles" => %{"p" => profile()}
        })

      assert {:ok, %{"p" => %{cmd: "/bin/sh", max_concurrent: 1}}} = Profiles.parse(ok)

      bad =
        Jason.encode!(%{
          "schema" => "unitares.orchestrator_profiles.v1",
          "profiles" => %{"p" => profile(%{"stdin" => "pipe"})}
        })

      assert {:error, "profile p: unknown profile key(s): stdin"} = Profiles.parse(bad)
    end

    test "refuses a caller variable that shadows a fixed, inherited or provisioned name" do
      for shadow <- ["PATH", "FIXED", "CHILD_KEY", "UNITARES_SERVER_URL"] do
        p = profile(%{"vars" => %{shadow => %{"max_bytes" => 8}}})

        json =
          Jason.encode!(%{
            "schema" => "unitares.orchestrator_profiles.v1",
            "profiles" => %{"p" => p}
          })

        assert {:error, reason} = Profiles.parse(json)
        assert reason =~ shadow
      end
    end

    test "refuses relative paths, a bad schema and missing limits" do
      base = %{"schema" => "unitares.orchestrator_profiles.v1"}

      assert {:error, _} =
               Profiles.parse(
                 Jason.encode!(Map.put(base, "profiles", %{"p" => profile(%{"cmd" => "sh"})}))
               )

      assert {:error, _} =
               Profiles.parse(Jason.encode!(%{"schema" => "v0", "profiles" => %{}}))

      no_limit = Map.delete(profile(), "max_per_hour")

      assert {:error, reason} =
               Profiles.parse(Jason.encode!(Map.put(base, "profiles", %{"p" => no_limit})))

      assert reason =~ "max_per_hour"
    end
  end

  describe "loading" do
    test "no file configured: profiles off, raw spawns allowed" do
      name = start_profiles(nil)
      assert %{state: "off", raw_spawn: "allow"} = Profiles.status(name)
      assert {:error, :profiles_unavailable, _} = Profiles.prepare(name, %{"profile" => "p"})
    end

    test "a configured file defaults raw spawns to deny, even when broken", %{tmp_dir: dir} do
      path = Path.join(dir, "missing.json")
      name = start_profiles(path)
      assert %{state: "invalid", raw_spawn: "deny", error: error} = Profiles.status(name)
      assert error =~ "cannot stat"
    end

    test "an explicit allow is honoured beside a profiles file", %{tmp_dir: dir} do
      name = start_profiles(write_file(dir, %{"p" => profile()}), raw_spawn: "allow")
      assert %{state: "ok", raw_spawn: "allow", profiles: ["p"]} = Profiles.status(name)
    end

    test "a group-writable file is refused", %{tmp_dir: dir} do
      path = write_file(dir, %{"p" => profile()})
      File.chmod!(path, 0o620)
      name = start_profiles(path)
      assert %{state: "invalid", error: error} = Profiles.status(name)
      assert error =~ "writable by group or others"
    end

    test "a bad replacement keeps the last good set; a fixed one reloads", %{tmp_dir: dir} do
      path = write_file(dir, %{"p" => profile()})
      name = start_profiles(path)
      %{digest: first} = Profiles.status(name)

      File.write!(path, "{not json")
      send(name, :reload)
      assert %{state: "stale", profiles: ["p"], digest: ^first} = Profiles.status(name)

      # Reverting to the loaded file clears the stale state.
      write_file(dir, %{"p" => profile()})
      send(name, :reload)
      assert %{state: "ok", profiles: ["p"], error: nil, digest: ^first} = Profiles.status(name)

      write_file(dir, %{"q" => profile()})
      send(name, :reload)
      assert %{state: "ok", profiles: ["q"], error: nil} = Profiles.status(name)
    end

    test "a missing secret marks only that profile unconfigured", %{tmp_dir: dir} do
      path = write_file(dir, %{"p" => profile(), "plain" => profile(%{"env" => %{}})})
      name = start_profiles(path, env: %{})
      assert %{unconfigured: ["p"]} = Profiles.status(name)

      # Secrets are resolved at admission, not at prepare: a keyed retry of a
      # started request replays without needing its secret.
      assert {:ok, _} =
               Profiles.prepare(name, %{"profile" => "p", "vars" => %{"GREETING" => "hi"}})

      assert {:error, :profile_unconfigured, detail} =
               Profiles.admit(name, "p", rev(name, "p"), "ex-1")

      assert detail =~ "ORCH_SECRET"
      assert {:ok, []} = Profiles.admit(name, "plain", rev(name, "plain"), "ex-2")
    end
  end

  describe "prepare/2" do
    setup %{tmp_dir: dir} do
      %{name: start_profiles(write_file(dir, %{"p" => profile()}))}
    end

    test "builds the env in order and never inherits what the profile omits", %{name: name} do
      assert {:ok, %{spec: spec}} =
               Profiles.prepare(name, %{"profile" => "p", "vars" => %{"GREETING" => "hello"}})

      assert {"LEAKY", false} in spec.env
      refute Enum.any?(spec.env, &match?({"PATH", _}, &1))
      assert {"FIXED", "fixed-value"} in spec.env
      assert {"GREETING", "hello"} in spec.env
      # The secret is not in the prepared spec at all (it is resolved at
      # admission and handed to the runner behind a closure), so nothing that
      # prints the spec shows it, and the child still gets it unset-then-set.
      refute Enum.any?(spec.env, &match?({"CHILD_KEY", _}, &1))
      refute Map.has_key?(spec, :secret_env)
      refute inspect(spec, limit: :infinity, printable_limit: :infinity) =~ @secret
      assert {:ok, [{"CHILD_KEY", @secret}]} = Profiles.admit(name, "p", rev(name, "p"), "ex-env")
      assert spec.cmd == "/bin/sh" and spec.stdin == "close" and spec.lease == false
      assert spec.max_runtime_ms == 10_000
    end

    test "the closed schema refuses fields and variables by name, never by value", %{name: name} do
      assert {:error, :schema_invalid, d1} =
               Profiles.prepare(name, %{
                 "profile" => "p",
                 "cmd" => "/bin/rm",
                 "vars" => %{"GREETING" => "hi"}
               })

      assert d1 == "unknown field(s): cmd"

      assert {:error, :schema_invalid, d2} =
               Profiles.prepare(name, %{
                 "profile" => "p",
                 "vars" => %{"GREETING" => "hi", "HOME" => "/x"}
               })

      assert d2 == "unknown variable(s): HOME"

      assert {:error, :schema_invalid, "missing required variable(s): GREETING"} =
               Profiles.prepare(name, %{"profile" => "p"})

      assert {:error, :schema_invalid, d3} =
               Profiles.prepare(name, %{
                 "profile" => "p",
                 "vars" => %{"GREETING" => "BAD-VALUE-123"}
               })

      refute d3 =~ "BAD-VALUE"
      assert d3 == "vars.GREETING does not match its pattern"

      assert {:error, :schema_invalid, "vars.GREETING is over its 32-byte limit"} =
               Profiles.prepare(name, %{
                 "profile" => "p",
                 "vars" => %{"GREETING" => String.duplicate("a", 33)}
               })
    end

    test "every raw-spawn field is refused by name on a profile request", %{name: name} do
      for {field, value} <- [
            {"cmd", "/bin/rm"},
            {"args", ["-rf", "/"]},
            {"env", %{"HOME" => "/tmp"}},
            {"cd", "/tmp"},
            {"stdin", "pipe"},
            {"lease", %{"required" => false}},
            {"server_url", "http://elsewhere"},
            {"client_session_id", "agent:/x"}
          ] do
        body = %{"profile" => "p", "vars" => %{"GREETING" => "hi"}, field => value}
        assert {:error, :schema_invalid, detail} = Profiles.prepare(name, body)
        assert detail == "unknown field(s): #{field}"
      end
    end

    test "ceilings may be lowered, never raised; lineage only where accepted", %{name: name} do
      base = %{"profile" => "p", "vars" => %{"GREETING" => "hi"}}

      assert {:ok, %{spec: %{max_runtime_ms: 500}}} =
               Profiles.prepare(name, Map.put(base, "max_runtime_ms", 500))

      assert {:error, :schema_invalid, _} =
               Profiles.prepare(name, Map.put(base, "max_runtime_ms", 10_001))

      assert {:error, :schema_invalid, "lineage is not accepted by this profile"} =
               Profiles.prepare(name, Map.put(base, "lineage", %{"parent_agent_uuid" => "x"}))
    end

    test "a malformed lineage parent is a schema refusal that echoes nothing", %{tmp_dir: dir} do
      path =
        write_file(Path.join(dir, "lin") |> tap(&File.mkdir_p!/1), %{
          "l" => profile(%{"lineage" => "caller"})
        })

      name = start_profiles(path)

      body = %{
        "profile" => "l",
        "vars" => %{"GREETING" => "hi"},
        "lineage" => %{"parent_agent_uuid" => "not-a-uuid-value"}
      }

      assert {:error, :schema_invalid, detail} = Profiles.prepare(name, body)
      assert detail == "lineage.parent_agent_uuid must be a UUID"
    end

    test "a server_url without an http scheme is refused at load" do
      json =
        Jason.encode!(%{
          "schema" => "unitares.orchestrator_profiles.v1",
          "profiles" => %{"p" => profile(%{"server_url" => "localhost:8767"})}
        })

      assert {:error, reason} = Profiles.parse(json)
      assert reason =~ "server_url"
      ok = put_in(Jason.decode!(json), ["profiles", "p", "server_url"], "http://127.0.0.1:8767")

      assert {:ok, %{"p" => %{server_url: "http://127.0.0.1:8767"}}} =
               Profiles.parse(Jason.encode!(ok))
    end

    test "unknown profile names are not echoed unless they look like names", %{name: name} do
      assert {:error, :unknown_profile, "unknown profile other"} =
               Profiles.prepare(name, %{"profile" => "other"})

      assert {:error, :unknown_profile, "unknown profile (invalid name)"} =
               Profiles.prepare(name, %{"profile" => "$(rm -rf /)"})
    end

    test "the digest ignores a rotated secret and covers the variables", %{tmp_dir: dir} do
      path = write_file(dir, %{"p" => profile()})
      a = start_profiles(path, env: %{"ORCH_SECRET" => "first-secret"})
      b = start_profiles(path, env: %{"ORCH_SECRET" => "rotated-secret"})
      body = %{"profile" => "p", "vars" => %{"GREETING" => "hi"}}
      {:ok, %{digest: da}} = Profiles.prepare(a, body)
      {:ok, %{digest: db}} = Profiles.prepare(b, body)
      {:ok, %{digest: dc}} = Profiles.prepare(a, put_in(body, ["vars", "GREETING"], "yo"))
      assert da == db
      refute da == dc
    end
  end

  describe "admit/3" do
    defp live_table, do: :ets.new(:live, [:public])
    defp live_fun(table), do: fn id -> :ets.member(table, id) end

    test "limits count admitted ids until they finish; a definite failure refunds",
         %{tmp_dir: dir} do
      live = live_table()
      path = write_file(dir, %{"p" => profile(%{"max_concurrent" => 1, "max_per_hour" => 2})})
      name = start_profiles(path, live?: live_fun(live))

      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-1")
      assert {:error, :rate_limited, d} = Profiles.admit(name, "p", rev(name, "p"), "ex-2")
      assert d =~ "max_concurrent"

      # A start that definitely failed returns its slot and its hourly charge.
      Profiles.refund(name, "ex-1")
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-2")
      Profiles.started(name, "ex-2")
      :ets.insert(live, {"ex-2"})
      assert {:error, :rate_limited, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-3")

      # Once it is no longer live the slot frees; the hour still counts it.
      :ets.delete(live, "ex-2")
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-3")
      Profiles.started(name, "ex-3")
      assert {:error, :rate_limited, d2} = Profiles.admit(name, "p", rev(name, "p"), "ex-4")
      assert d2 =~ "max_per_hour"
    end

    test "a reload between prepare and admit is refused, never mixed", %{tmp_dir: dir} do
      path = write_file(dir, %{"p" => profile()})
      name = start_profiles(path)

      {:ok, prepared} =
        Profiles.prepare(name, %{"profile" => "p", "vars" => %{"GREETING" => "hi"}})

      write_file(dir, %{"p" => profile(%{"args" => ["-c", "echo replaced"]})})
      send(name, :reload)

      assert {:error, :profile_changed, detail} =
               Profiles.admit(name, "p", prepared.revision, "ex-1")

      assert detail =~ "retry"
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-2")
    end

    test "with no reported outcome a slot is held while live, then for the start grace",
         %{tmp_dir: dir} do
      live = live_table()
      clock = :counters.new(1, [])
      path = write_file(dir, %{"p" => profile(%{"max_concurrent" => 1})})

      name =
        start_profiles(path,
          live?: live_fun(live),
          now: fn -> :counters.get(clock, 1) end,
          start_grace_ms: 1_000
        )

      # The admitting caller died mid-start, so no outcome was reported. That
      # proves nothing about the child: the slot stays tied to its id.
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-1")
      :counters.put(clock, 1, 999)
      assert {:error, :rate_limited, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-2")

      # Never seen live past the grace: it never started.
      :counters.put(clock, 1, 1_001)
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-2")

      # Seen live once, it counts as started; past the grace and gone, it frees.
      :ets.insert(live, {"ex-2"})
      :counters.put(clock, 1, 5_000)
      assert {:error, :rate_limited, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-3")
      :ets.delete(live, "ex-2")
      assert {:ok, _} = Profiles.admit(name, "p", rev(name, "p"), "ex-3")
    end
  end

  describe "redaction and admission edge cases" do
    test "a secret holding a line break makes its profile unconfigured", %{tmp_dir: dir} do
      name =
        start_profiles(write_file(dir, %{"p" => profile()}),
          env: %{"ORCH_SECRET" => "abcd\nefgh"}
        )

      assert %{unconfigured: ["p"]} = Profiles.status(name)

      assert {:error, :profile_unconfigured, detail} =
               Profiles.admit(name, "p", rev(name, "p"), "ex-1")

      assert detail =~ "line break"
    end

    test "a secret straddling the oversized-line cut is still redacted" do
      Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)

      {:ok, id, _pid} =
        AgentOrchestrator.run(%{
          cmd: "/bin/sh",
          # 65,530 filler bytes put the 26-byte secret across the 65,536-byte cut.
          args: ["-c", "head -c 65530 /dev/zero | tr '\\0' a; printf '%s' \"$K\"; echo tail"],
          env: [{"K", @secret}],
          lease: false,
          redact: [@secret]
        })

      {:ok, result} = AgentOrchestrator.await(id, 10_000)
      joined = Enum.join(result.output)
      refute joined =~ @secret
      assert joined =~ "[redacted]tail"
    end

    test "the oversized-line cut never splits a UTF-8 character" do
      Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)

      for redact <- [[], ["sixsix"]], prefix <- [0, 1] do
        {:ok, id, _pid} =
          AgentOrchestrator.run(%{
            cmd: "/bin/sh",
            # 32,768 two-byte characters with no newline: 65,536 bytes. With
            # one ASCII byte first, the port's 65,536-byte chunk ends after the
            # first byte of a character.
            args: [
              "-c",
              "i=0; s=é; while [ $i -lt 15 ]; do s=$s$s; i=$((i+1)); done; " <>
                "if [ #{prefix} = 1 ]; then printf a; fi; printf '%s' \"$s\"; echo"
            ],
            env: [{"LANG", "C.UTF-8"}],
            lease: false,
            redact: redact
          })

        {:ok, result} = AgentOrchestrator.await(id, 10_000)
        assert Enum.all?(result.output, &String.valid?/1)
        assert byte_size(Enum.join(result.output)) == 65_536 + prefix
        assert {:ok, _} = Jason.encode(result.output)
      end
    end

    test "SpawnGate admits only a new start, refunds a definite failure, keeps an ambiguous one" do
      gate = :"gate_#{System.unique_integer([:positive])}"
      start_supervised!({AgentOrchestrator.SpawnGate, name: gate, ledger: FlakyLedger}, id: gate)
      parent = self()

      admit = fn id ->
        send(parent, {:admitted, id})

        # A malformed lineage parent makes the runner refuse in init: a
        # definite, synchronous start failure.
        {:ok, %{cmd: "true", lease: false, lineage: %{parent_agent_uuid: "bad"}},
         fn outcome -> send(parent, {outcome, id}) end}
      end

      key = "gate-key-#{System.unique_integer([:positive])}"
      assert {:error, _} = AgentOrchestrator.SpawnGate.start_agent(gate, key, "d1", %{}, admit)
      assert_received {:admitted, id}
      assert_received {:failed, ^id}

      # The process starts but its start cannot be recorded: it may be running.
      ok_admit = fn id ->
        send(parent, {:admitted, id})
        {:ok, %{cmd: "true", lease: false}, fn outcome -> send(parent, {outcome, id}) end}
      end

      FlakyLedger.fail_mark_started(true)
      key2 = "gate-key-#{System.unique_integer([:positive])}"

      assert {:error, {:idempotency_outcome_unknown, id2}} =
               AgentOrchestrator.SpawnGate.start_agent(gate, key2, "d2", %{}, ok_admit)

      assert_received {:admitted, ^id2}
      refute_received {:failed, _}
      refute_received {:started, _}
      FlakyLedger.fail_mark_started(false)

      # A replay of a started key never runs admission.
      key3 = "gate-key-#{System.unique_integer([:positive])}"

      assert {:ok, id3, _, :new} =
               AgentOrchestrator.SpawnGate.start_agent(gate, key3, "d3", %{}, ok_admit)

      assert_received {:admitted, ^id3}
      assert_received {:started, ^id3}

      assert {:ok, ^id3, _, :idempotent} =
               AgentOrchestrator.SpawnGate.start_agent(gate, key3, "d3", %{}, ok_admit)

      refute_received {:admitted, _}
    end
  end

  describe "HTTP surface" do
    setup %{tmp_dir: dir} do
      MemoryIdempotencyLedger.clear()
      Application.put_env(:agent_orchestrator, :bearer_token, @token)
      Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, nil)

      name =
        start_profiles(
          write_file(dir, %{
            "p" => profile(),
            "slow" =>
              profile(%{
                "args" => ["-c", "sleep 5"],
                "vars" => %{},
                "env" => %{"inherit" => ["PATH"]}
              })
          }),
          # The real environment, so the strip is proven on a real child.
          environ: System.get_env() |> Map.put("LEAKY", "leaked")
        )

      System.put_env("LEAKY", "leaked")
      Application.put_env(:agent_orchestrator, :profiles_server, name)

      on_exit(fn ->
        System.delete_env("LEAKY")
        Application.delete_env(:agent_orchestrator, :profiles_server)
        Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
      end)

      %{name: name}
    end

    defp post(body, key \\ nil) do
      conn =
        conn(:post, "/v1/agents", Jason.encode!(body))
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer " <> @token)

      conn = if key, do: put_req_header(conn, "idempotency-key", key), else: conn
      HTTPRouter.call(conn, HTTPRouter.init([]))
    end

    defp await(id) do
      conn =
        conn(:post, "/v1/executions/#{id}/await", Jason.encode!(%{timeout_ms: 5_000}))
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer " <> @token)

      HTTPRouter.call(conn, HTTPRouter.init([])) |> Map.get(:resp_body) |> Jason.decode!()
    end

    test "a profile spawn runs the fixed command, strips the env and redacts the secret" do
      conn = post(%{"profile" => "p", "vars" => %{"GREETING" => "hello"}})
      assert conn.status == 201
      body = Jason.decode!(conn.resp_body)
      assert body["profile"] == "p"

      result = await(body["execution_id"])["result"]
      assert result["exit_status"] == 0
      assert result["output"] == ["hello|[redacted]|unset|fixed-value"]
      refute Enum.any?(result["output"], &String.contains?(&1, @secret))
    end

    test "raw spawns are refused while profiles are configured" do
      conn = post(%{"cmd" => "echo", "args" => ["x"]})
      assert conn.status == 403
      assert Jason.decode!(conn.resp_body)["reason"] =~ "raw spawns are disabled"
    end

    test "over max_concurrent is 429, but a keyed retry replays instead" do
      first = post(%{"profile" => "slow"}, "key-1")
      assert first.status == 201
      id = Jason.decode!(first.resp_body)["execution_id"]

      # The retry of a started key replays without taking a slot.
      replay = post(%{"profile" => "slow"}, "key-1")
      assert replay.status == 200
      assert Jason.decode!(replay.resp_body)["execution_id"] == id

      # A new attempt, keyed or not, is over the limit, and the refused key is
      # left free for a later retry.
      assert post(%{"profile" => "slow"}, "key-2").status == 429
      assert post(%{"profile" => "slow"}).status == 429

      AgentOrchestrator.stop(id, :test_cleanup)
      Process.sleep(50)
      assert post(%{"profile" => "slow"}, "key-2").status == 201
    end

    test "a keyed retry replays even after its secret source is gone", %{tmp_dir: dir} do
      secret = Agent.start_link(fn -> @secret end) |> elem(1)

      name =
        start_profiles(
          write_file(Path.join(dir, "s") |> tap(&File.mkdir_p!/1), %{
            "s" => profile(%{"args" => ["-c", "sleep 5"], "vars" => %{}})
          }),
          getenv: fn "ORCH_SECRET" -> Agent.get(secret, & &1) end,
          live?: fn _ -> false end
        )

      Application.put_env(:agent_orchestrator, :profiles_server, name)
      first = post(%{"profile" => "s"}, "sec-1")
      assert first.status == 201
      id = Jason.decode!(first.resp_body)["execution_id"]

      Agent.update(secret, fn _ -> nil end)
      replay = post(%{"profile" => "s"}, "sec-1")
      assert replay.status == 200
      assert Jason.decode!(replay.resp_body)["execution_id"] == id
      AgentOrchestrator.stop(id, :test_cleanup)

      # A new start does need it.
      fresh = post(%{"profile" => "s"}, "sec-2")
      assert fresh.status == 503
      assert Jason.decode!(fresh.resp_body)["error"] == "profile_unconfigured"
    end

    test "with raw spawns denied, /bin/sh is refused like any other cmd" do
      for cmd <- ["/bin/sh", "sh", "echo"] do
        conn = post(%{"cmd" => cmd, "args" => ["-c", "true"]})
        assert conn.status == 403
      end
    end

    test "the resolved secret reaches no log line" do
      log =
        ExUnit.CaptureLog.capture_log(fn ->
          conn = post(%{"profile" => "p", "vars" => %{"GREETING" => "hello"}})
          await(Jason.decode!(conn.resp_body)["execution_id"])
        end)

      refute log =~ @secret
    end

    test "a keyed retry that differs only in lineage conflicts", %{tmp_dir: dir} do
      lineage_profile =
        profile(%{
          "args" => ["-c", "sleep 5"],
          "vars" => %{},
          "lineage" => "caller",
          "env" => %{}
        })

      name =
        start_profiles(
          write_file(Path.join(dir, "l") |> tap(&File.mkdir_p!/1), %{"l" => lineage_profile})
        )

      Application.put_env(:agent_orchestrator, :profiles_server, name)
      parent = "11111111-2222-4333-8444-555555555555"
      other = "66666666-7777-4888-9999-000000000000"

      first = post(%{"profile" => "l", "lineage" => %{"parent_agent_uuid" => parent}}, "lin-1")
      assert first.status == 201

      conflict = post(%{"profile" => "l", "lineage" => %{"parent_agent_uuid" => other}}, "lin-1")
      assert conflict.status == 409
      assert Jason.decode!(conflict.resp_body)["error"] == "idempotency_conflict"
    end

    test "health reports the profile state" do
      conn =
        conn(:get, "/v1/health")
        |> put_req_header("authorization", "Bearer " <> @token)
        |> HTTPRouter.call(HTTPRouter.init([]))

      assert %{
               "profiles" => %{
                 "state" => "ok",
                 "raw_spawn" => "deny",
                 "profiles" => ["p", "slow"]
               }
             } =
               Jason.decode!(conn.resp_body)
    end

    test "refusals carry typed errors" do
      assert post(%{"profile" => "nope"}).status == 422

      assert Jason.decode!(post(%{"profile" => "p", "env" => %{}}).resp_body)["error"] ==
               "schema_invalid"
    end
  end
end
