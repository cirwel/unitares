defmodule AgentOrchestrator.Profiles do
  @moduledoc """
  Named launch profiles: the only processes a profile request may start.

  Design: `docs/proposals/active/inference-hosts-config-v0.md` §2.5 (operator
  decision 2026-10-15, KG `2026-10-15T10:29:57.051370+00:00`). A command
  allowlist leaves a holder of the bearer token able to run code through an
  allowed command (`env`, `cd`, an appended flag) and to route any secret in
  the orchestrator's environment to a host of its choosing. A profile fixes
  everything that changes what runs; a caller picks a profile and fills only
  the variables it declares.

  ## The file

  `AGENT_ORCHESTRATOR_PROFILES_FILE` names a JSON file:

      {"schema": "unitares.orchestrator_profiles.v1",
       "profiles": {"<name>": {
          "cmd": "/abs/path", "args": ["..."], "cd": "/abs/dir",
          "env": {"inherit": ["HOME", "PATH"], "set": {"K": "v"},
                  "secrets": {"CHILD_VAR": "ORCHESTRATOR_VAR"}},
          "vars": {"PROMPT": {"max_bytes": 65536, "pattern": "...", "required": true}},
          "lineage": "none" | "caller", "server_url": "http://...", "lease": false,
          "max_runtime_ms": 600000, "max_output_lines": 1000,
          "max_concurrent": 2, "max_per_hour": 30}}}

  Unknown keys are refused. The file and its directory must belong to this
  user and be writable by no one else. Without the variable, profile spawns
  are off. A file that fails to load or validate at boot leaves profile spawns
  off with the reason in `status/0`; a replacement that fails while running
  leaves the last good set in force and reports the error. The file is
  re-read when its digest changes, so regenerating it needs no restart.

  ## The child environment

  Built in one order: every orchestrator variable not inherited is unset
  (default inherit: `HOME`, `PATH`, `LANG`, `TMPDIR`), then fixed values,
  then secrets resolved from the orchestrator's environment at spawn, then
  the caller's variables; the runner adds its provisioned variables last. A
  caller variable may never name an inherited, fixed, secret or provisioned
  variable, and that is checked when the file loads. Resolved secret values
  are redacted from the output the orchestrator captures and returns. A
  profile whose secret source is unset or shorter than four bytes is
  `unconfigured` and refuses its own spawns; the others are unaffected.

  ## Requests

  A closed schema: `profile`, `vars`, `max_runtime_ms`, `max_output_lines`
  (each at most the profile's ceiling), and `lineage` only where the profile
  accepts it. A refusal names the field or variable, never a value. The
  concurrency and per-hour limits are checked inside the idempotent spawn, after
  a retry of an already-started key has replayed, so a lost response is never
  turned into a refusal.

  ## Raw spawns

  `AGENT_ORCHESTRATOR_RAW_SPAWN` is `allow` or `deny`. It defaults to `deny`
  whenever the profiles file variable is set, whether or not the file loads,
  and to `allow` only when it is unset; any other value is `deny`. A broken
  profiles file therefore never reopens raw spawning.
  """

  use GenServer

  require Logger

  @schema "unitares.orchestrator_profiles.v1"
  @default_inherit ~w(HOME PATH LANG TMPDIR)
  # Variables the runner provisions (agent_runner.ex). No profile may let a
  # caller set them.
  @provisioned ~w(UNITARES_SERVER_URL UNITARES_CLIENT_SESSION_ID UNITARES_ORCHESTRATED
                  UNITARES_PARENT_AGENT_ID UNITARES_SPAWN_REASON)
  @profile_keys ~w(cmd args cd env vars lineage server_url lease max_runtime_ms
                   max_output_lines max_concurrent max_per_hour)
  @env_keys ~w(inherit set secrets)
  @var_keys ~w(max_bytes pattern required)
  @request_keys ~w(profile vars max_runtime_ms max_output_lines lineage)
  @name_re ~r/\A[a-z0-9][a-z0-9_.-]{0,63}\z/
  @env_name_re ~r/\A[A-Za-z_][A-Za-z0-9_]{0,127}\z/
  @max_var_bytes 262_144
  @min_secret_bytes 4
  @hour_ms 3_600_000
  @default_reload_ms 5_000

  # ---------- public API ----------

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc "The server the HTTP surface uses (overridable for tests)."
  def server, do: Application.get_env(:agent_orchestrator, :profiles_server, __MODULE__)

  @doc "Operator-facing state: mode, digest, profile names, unconfigured, last error."
  def status(server \\ server()), do: GenServer.call(server, :status)

  @doc "`:allow` or `:deny`."
  def raw_spawn_mode(server \\ server()), do: GenServer.call(server, :raw_spawn_mode)

  @doc """
  Validate a request body against the loaded profiles and build its runner
  spec. Returns `{:ok, %{profile: name, spec: spec, digest: digest}}` or
  `{:error, code, detail}`, where `code` is `:schema_invalid`,
  `:unknown_profile`, `:profiles_unavailable` or `:profile_unconfigured`.
  """
  def prepare(server \\ server(), body), do: GenServer.call(server, {:prepare, body})

  @doc """
  Take one concurrency and per-hour slot for `profile`. Returns
  `{:ok, token}` or `{:error, :rate_limited, detail}`; `finish/3` attaches the
  started execution or refunds the slot.
  """
  def admit(server \\ server(), profile), do: GenServer.call(server, {:admit, profile})

  def finish(server \\ server(), token, result),
    do: GenServer.cast(server, {:finish, token, result})

  # ---------- pure parsing (exposed for tests) ----------

  @doc false
  def parse(json) when is_binary(json) do
    with {:ok, decoded} <- decode(json),
         :ok <- expect_keys(decoded, ~w(schema profiles), "file"),
         :ok <- expect(decoded["schema"] == @schema, "schema must be #{@schema}"),
         {:ok, raw_profiles} <- expect_map(decoded["profiles"], "profiles") do
      Enum.reduce_while(raw_profiles, {:ok, %{}}, fn {name, raw}, {:ok, acc} ->
        case parse_profile(name, raw) do
          {:ok, profile} -> {:cont, {:ok, Map.put(acc, name, profile)}}
          {:error, reason} -> {:halt, {:error, "profile #{safe_name(name)}: #{reason}"}}
        end
      end)
    end
  end

  # ---------- GenServer ----------

  @impl true
  def init(opts) do
    file = blank_to_nil(setting(opts, :file, :profiles_file, "AGENT_ORCHESTRATOR_PROFILES_FILE"))
    raw = setting(opts, :raw_spawn, :raw_spawn, "AGENT_ORCHESTRATOR_RAW_SPAWN")

    state = %{
      file: file,
      raw_spawn: raw_mode(raw, file),
      profiles: nil,
      digest: nil,
      error: nil,
      reload_ms: Keyword.get(opts, :reload_ms, @default_reload_ms),
      getenv: Keyword.get(opts, :getenv, &System.get_env/1),
      environ: Keyword.get(opts, :environ, &System.get_env/0),
      live_ids: Keyword.get(opts, :live_ids, &AgentOrchestrator.list/0),
      uid: Keyword.get_lazy(opts, :uid, &current_uid/0),
      now: Keyword.get(opts, :now, fn -> System.monotonic_time(:millisecond) end),
      starts: %{},
      slots: %{}
    }

    state = if file, do: reload(state), else: state
    if file, do: schedule_reload(state.reload_ms)
    {:ok, state}
  end

  @impl true
  def handle_call(:status, _from, state), do: {:reply, present_status(state), state}

  def handle_call(:raw_spawn_mode, _from, state), do: {:reply, state.raw_spawn, state}

  def handle_call({:prepare, body}, _from, state) do
    {:reply, do_prepare(body, state), state}
  end

  def handle_call({:admit, name}, _from, state) do
    case state.profiles && Map.get(state.profiles, name) do
      nil ->
        {:reply, {:error, :rate_limited, "profile #{name} is not loaded"}, state}

      profile ->
        state = prune(state)
        now = state.now.()
        running = Enum.count(state.slots, fn {_t, {n, _id}} -> n == name end)
        recent = Map.get(state.starts, name, [])

        cond do
          running >= profile.max_concurrent ->
            {:reply, {:error, :rate_limited, "profile #{name} is at max_concurrent"}, state}

          length(recent) >= profile.max_per_hour ->
            {:reply, {:error, :rate_limited, "profile #{name} is at max_per_hour"}, state}

          true ->
            token = make_ref()

            state = %{
              state
              | slots: Map.put(state.slots, token, {name, :pending}),
                starts: Map.put(state.starts, name, [{now, token} | recent])
            }

            {:reply, {:ok, token}, state}
        end
    end
  end

  @impl true
  def handle_cast({:finish, token, result}, state) do
    state =
      case {Map.get(state.slots, token), result} do
        {nil, _} ->
          state

        {{name, :pending}, {:ok, execution_id}} ->
          %{state | slots: Map.put(state.slots, token, {name, execution_id})}

        {{name, :pending}, _refused_or_replayed} ->
          %{
            state
            | slots: Map.delete(state.slots, token),
              starts:
                Map.update(state.starts, name, [], fn l ->
                  Enum.reject(l, &match?({_, ^token}, &1))
                end)
          }

        _ ->
          state
      end

    {:noreply, state}
  end

  @impl true
  def handle_info(:reload, state) do
    schedule_reload(state.reload_ms)
    {:noreply, reload(state)}
  end

  def handle_info(_message, state), do: {:noreply, state}

  # ---------- loading ----------

  defp reload(state) do
    with :ok <- check_file_safety(state.file, state.uid),
         {:ok, body} <- read(state.file) do
      digest = sha256(body)

      if digest == state.digest do
        state
      else
        case parse(body) do
          {:ok, profiles} ->
            if state.profiles, do: Logger.info("orchestrator profiles reloaded (#{digest})")
            %{state | profiles: profiles, digest: digest, error: nil}

          {:error, reason} ->
            keep_last_good(state, reason)
        end
      end
    else
      {:error, reason} -> keep_last_good(state, reason)
    end
  end

  defp keep_last_good(state, reason) do
    if reason != state.error do
      Logger.error("orchestrator profiles file not loaded: #{reason}")
    end

    %{state | error: reason}
  end

  defp read(path) do
    case File.read(path) do
      {:ok, body} -> {:ok, body}
      {:error, reason} -> {:error, "cannot read #{path}: #{reason}"}
    end
  end

  defp check_file_safety(path, uid) do
    with :ok <- owned_and_private(path, uid, "file"),
         :ok <- owned_and_private(Path.dirname(path), uid, "directory") do
      :ok
    end
  end

  defp owned_and_private(path, uid, what) do
    case File.stat(path) do
      {:ok, %File.Stat{uid: ^uid, mode: mode}} ->
        if Bitwise.band(mode, 0o022) == 0,
          do: :ok,
          else: {:error, "#{what} #{path} is writable by group or others"}

      {:ok, %File.Stat{}} ->
        {:error, "#{what} #{path} is owned by another user"}

      {:error, reason} ->
        {:error, "cannot stat #{what} #{path}: #{reason}"}
    end
  end

  defp current_uid do
    case System.cmd("id", ["-u"], stderr_to_stdout: true) do
      {out, 0} -> out |> String.trim() |> String.to_integer()
      _ -> -1
    end
  rescue
    _ -> -1
  end

  defp schedule_reload(ms), do: Process.send_after(self(), :reload, ms)

  # ---------- profile validation ----------

  defp parse_profile(name, raw) do
    with :ok <- expect(is_binary(name) and Regex.match?(@name_re, name), "invalid profile name"),
         {:ok, raw} <- expect_map(raw, "profile"),
         :ok <- expect_keys(raw, @profile_keys, "profile"),
         {:ok, cmd} <- abs_path(raw["cmd"], "cmd"),
         {:ok, args} <- string_list(Map.get(raw, "args", []), "args"),
         {:ok, cd} <- optional_abs_path(raw["cd"], "cd"),
         {:ok, env} <- parse_env(Map.get(raw, "env", %{})),
         {:ok, vars} <- parse_vars(Map.get(raw, "vars", %{})),
         :ok <- disjoint_names(env, vars),
         {:ok, lineage} <- one_of(Map.get(raw, "lineage", "none"), ~w(none caller), "lineage"),
         {:ok, server_url} <- optional_string(raw["server_url"], "server_url"),
         {:ok, lease} <- parse_lease(raw["lease"]),
         {:ok, max_runtime} <- positive(raw["max_runtime_ms"], "max_runtime_ms"),
         {:ok, max_lines} <- positive(Map.get(raw, "max_output_lines", 1000), "max_output_lines"),
         {:ok, max_concurrent} <- positive(raw["max_concurrent"], "max_concurrent"),
         {:ok, max_per_hour} <- positive(raw["max_per_hour"], "max_per_hour") do
      profile = %{
        name: name,
        cmd: cmd,
        args: args,
        cd: cd,
        env: env,
        vars: vars,
        lineage: lineage,
        server_url: server_url,
        lease: lease,
        max_runtime_ms: max_runtime,
        max_output_lines: max_lines,
        max_concurrent: max_concurrent,
        max_per_hour: max_per_hour
      }

      {:ok, Map.put(profile, :digest, profile_digest(profile))}
    end
  end

  defp parse_env(raw) do
    with {:ok, raw} <- expect_map(raw, "env"),
         :ok <- expect_keys(raw, @env_keys, "env"),
         {:ok, inherit} <- string_list(Map.get(raw, "inherit", @default_inherit), "env.inherit"),
         :ok <- env_names(inherit, "env.inherit"),
         {:ok, set} <- string_map(Map.get(raw, "set", %{}), "env.set"),
         :ok <- env_names(Map.keys(set), "env.set"),
         {:ok, secrets} <- string_map(Map.get(raw, "secrets", %{}), "env.secrets"),
         :ok <- env_names(Map.keys(secrets) ++ Map.values(secrets), "env.secrets") do
      fixed = Map.keys(set) ++ Map.keys(secrets)

      cond do
        Enum.any?(fixed, &(&1 in inherit)) ->
          {:error, "env.set and env.secrets may not name an inherited variable"}

        not MapSet.disjoint?(MapSet.new(Map.keys(set)), MapSet.new(Map.keys(secrets))) ->
          {:error, "env.set and env.secrets name the same variable"}

        Enum.any?(fixed ++ inherit, &(&1 in @provisioned)) ->
          {:error, "env may not name a variable the orchestrator provisions"}

        true ->
          {:ok, %{inherit: Enum.uniq(inherit), set: set, secrets: secrets}}
      end
    end
  end

  defp parse_vars(raw) do
    with {:ok, raw} <- expect_map(raw, "vars"),
         :ok <- env_names(Map.keys(raw), "vars") do
      Enum.reduce_while(raw, {:ok, %{}}, fn {name, spec}, {:ok, acc} ->
        case parse_var(spec) do
          {:ok, var} -> {:cont, {:ok, Map.put(acc, name, var)}}
          {:error, reason} -> {:halt, {:error, "vars.#{name}: #{reason}"}}
        end
      end)
    end
  end

  defp parse_var(spec) do
    with {:ok, spec} <- expect_map(spec, "variable"),
         :ok <- expect_keys(spec, @var_keys, "variable"),
         {:ok, max_bytes} <- positive(spec["max_bytes"], "max_bytes"),
         :ok <- expect(max_bytes <= @max_var_bytes, "max_bytes is over #{@max_var_bytes}"),
         {:ok, required} <- boolean(Map.get(spec, "required", false), "required"),
         {:ok, pattern} <- compile_pattern(spec["pattern"]) do
      {:ok,
       %{max_bytes: max_bytes, required: required, pattern: pattern, source: spec["pattern"]}}
    end
  end

  defp compile_pattern(nil), do: {:ok, nil}

  defp compile_pattern(source) when is_binary(source) do
    case Regex.compile("\\A(?:" <> source <> ")\\z", "u") do
      {:ok, re} -> {:ok, re}
      {:error, _} -> {:error, "pattern does not compile"}
    end
  end

  defp compile_pattern(_), do: {:error, "pattern must be a string"}

  defp disjoint_names(env, vars) do
    taken = env.inherit ++ Map.keys(env.set) ++ Map.keys(env.secrets) ++ @provisioned

    case Enum.find(Map.keys(vars), &(&1 in taken)) do
      nil -> :ok
      name -> {:error, "vars.#{name} names an inherited, fixed, secret or provisioned variable"}
    end
  end

  defp parse_lease(nil), do: {:ok, :absent}
  defp parse_lease(false), do: {:ok, false}
  defp parse_lease(_), do: {:error, "lease must be false or absent"}

  # Everything that changes what runs; secrets are named by source variable, so
  # rotating a secret's value leaves the digest (and a keyed retry) unchanged.
  defp profile_digest(profile) do
    profile
    |> Map.update!(:vars, fn vars ->
      Map.new(vars, fn {k, v} -> {k, Map.delete(v, :pattern)} end)
    end)
    |> :erlang.term_to_binary([:deterministic])
    |> sha256()
  end

  # ---------- request preparation ----------

  defp do_prepare(body, state) when is_map(body) do
    with :ok <- known_request_keys(body),
         {:ok, profiles} <- loaded(state),
         {:ok, name} <- request_profile(body),
         {:ok, profile} <- find_profile(profiles, name),
         {:ok, secret_env} <- resolve_secrets(profile, state.getenv),
         {:ok, vars} <- request_vars(body["vars"], profile.vars),
         {:ok, max_runtime} <-
           ceiling(body["max_runtime_ms"], profile.max_runtime_ms, "max_runtime_ms"),
         {:ok, max_lines} <-
           ceiling(body["max_output_lines"], profile.max_output_lines, "max_output_lines"),
         {:ok, lineage} <- request_lineage(body["lineage"], profile.lineage) do
      env = child_env(profile, secret_env, vars, state.environ.())

      spec =
        %{
          cmd: profile.cmd,
          args: profile.args,
          env: env,
          stdin: "close",
          max_runtime_ms: max_runtime,
          max_output_lines: max_lines,
          redact: Enum.map(secret_env, fn {_k, v} -> v end)
        }
        |> put_opt(:cd, profile.cd)
        |> put_opt(:server_url, profile.server_url)
        |> put_opt(:lineage, lineage)
        |> put_lease(profile.lease)

      digest =
        sha256(
          :erlang.term_to_binary(
            {:profile, name, Enum.sort(vars), max_runtime, max_lines, lineage, profile.digest},
            [:deterministic]
          )
        )

      {:ok, %{profile: name, spec: spec, digest: digest}}
    end
  end

  defp do_prepare(_body, _state), do: {:error, :schema_invalid, "body must be a JSON object"}

  defp known_request_keys(body) do
    case Map.keys(body) -- @request_keys do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown field(s): #{names(extra)}"}
    end
  end

  defp loaded(%{profiles: nil, file: nil}),
    do: {:error, :profiles_unavailable, "no profiles file is configured"}

  defp loaded(%{profiles: nil, error: error}),
    do: {:error, :profiles_unavailable, "profiles file not loaded: #{error}"}

  defp loaded(%{profiles: profiles}), do: {:ok, profiles}

  defp request_profile(body) do
    case body["profile"] do
      name when is_binary(name) and name != "" -> {:ok, name}
      _ -> {:error, :schema_invalid, "profile must be a non-empty string"}
    end
  end

  defp find_profile(profiles, name) do
    case Map.fetch(profiles, name) do
      {:ok, profile} -> {:ok, profile}
      :error -> {:error, :unknown_profile, "unknown profile #{safe_name(name)}"}
    end
  end

  defp resolve_secrets(profile, getenv) do
    Enum.reduce_while(profile.env.secrets, {:ok, []}, fn {child, source}, {:ok, acc} ->
      case getenv.(source) do
        value when is_binary(value) and byte_size(value) >= @min_secret_bytes ->
          {:cont, {:ok, [{child, value} | acc]}}

        _ ->
          {:halt,
           {:error, :profile_unconfigured,
            "profile #{profile.name} is unconfigured: secret source #{source} is unset"}}
      end
    end)
  end

  defp request_vars(nil, declared), do: request_vars(%{}, declared)

  defp request_vars(given, declared) when is_map(given) do
    with :ok <- known_vars(given, declared),
         :ok <- required_vars(given, declared) do
      Enum.reduce_while(given, {:ok, []}, fn {name, value}, {:ok, acc} ->
        spec = Map.fetch!(declared, name)

        cond do
          not is_binary(value) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} must be a string"}}

          byte_size(value) > spec.max_bytes ->
            {:halt,
             {:error, :schema_invalid, "vars.#{name} is over its #{spec.max_bytes}-byte limit"}}

          not String.valid?(value) or String.contains?(value, <<0>>) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} must be text without NUL"}}

          spec.pattern && not Regex.match?(spec.pattern, value) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} does not match its pattern"}}

          true ->
            {:cont, {:ok, [{name, value} | acc]}}
        end
      end)
    end
  end

  defp request_vars(_given, _declared),
    do: {:error, :schema_invalid, "vars must be a JSON object"}

  defp known_vars(given, declared) do
    case Map.keys(given) -- Map.keys(declared) do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown variable(s): #{names(extra)}"}
    end
  end

  defp required_vars(given, declared) do
    missing = for {name, %{required: true}} <- declared, not Map.has_key?(given, name), do: name

    case missing do
      [] ->
        :ok

      _ ->
        {:error, :schema_invalid,
         "missing required variable(s): #{Enum.join(Enum.sort(missing), ", ")}"}
    end
  end

  defp ceiling(nil, max, _field), do: {:ok, max}

  defp ceiling(n, max, _field) when is_integer(n) and n > 0 and n <= max, do: {:ok, n}

  defp ceiling(n, max, field) when is_integer(n) and n > max,
    do: {:error, :schema_invalid, "#{field} is over the profile's ceiling of #{max}"}

  defp ceiling(_n, _max, field),
    do: {:error, :schema_invalid, "#{field} must be a positive integer"}

  defp request_lineage(nil, _mode), do: {:ok, nil}

  defp request_lineage(_given, "none"),
    do: {:error, :schema_invalid, "lineage is not accepted by this profile"}

  defp request_lineage(%{} = given, "caller") do
    with :ok <- known_lineage_keys(given),
         {:ok, parent} <- required_string(given["parent_agent_uuid"], "lineage.parent_agent_uuid"),
         {:ok, reason} <- optional_string_req(given["spawn_reason"], "lineage.spawn_reason") do
      base = %{parent_agent_uuid: parent}
      {:ok, if(reason, do: Map.put(base, :spawn_reason, reason), else: base)}
    end
  end

  defp request_lineage(_given, "caller"),
    do: {:error, :schema_invalid, "lineage must be a JSON object"}

  defp known_lineage_keys(given) do
    case Map.keys(given) -- ~w(parent_agent_uuid spawn_reason) do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown lineage field(s): #{names(extra)}"}
    end
  end

  defp required_string(v, _f) when is_binary(v) and v != "", do: {:ok, v}
  defp required_string(_v, f), do: {:error, :schema_invalid, "#{f} must be a non-empty string"}

  defp optional_string_req(nil, _f), do: {:ok, nil}
  defp optional_string_req(v, _f) when is_binary(v) and v != "", do: {:ok, v}

  defp optional_string_req(_v, f),
    do: {:error, :schema_invalid, "#{f} must be a non-empty string"}

  # Unset everything not inherited (or set below), then fixed values, secrets
  # and the caller's variables. The runner appends its provisioned variables.
  defp child_env(profile, secret_env, vars, environ) do
    keep = MapSet.new(profile.env.inherit)

    unset =
      for {name, _v} <- environ, not MapSet.member?(keep, name), do: {name, false}

    set = Enum.sort(profile.env.set)
    written = MapSet.new(set ++ secret_env ++ vars, fn {k, _} -> k end)

    Enum.reject(unset, fn {k, _} -> MapSet.member?(written, k) end) ++
      set ++ Enum.sort(secret_env) ++ Enum.sort(vars)
  end

  # ---------- rate state ----------

  defp prune(state) do
    cutoff = state.now.() - @hour_ms
    live = MapSet.new(state.live_ids.())

    slots =
      state.slots
      |> Enum.filter(fn
        {_t, {_n, :pending}} -> true
        {_t, {_n, id}} -> MapSet.member?(live, id)
      end)
      |> Map.new()

    starts =
      state.starts
      |> Enum.map(fn {name, list} -> {name, Enum.filter(list, fn {at, _} -> at > cutoff end)} end)
      |> Map.new()

    %{state | slots: slots, starts: starts}
  end

  # ---------- status ----------

  defp present_status(state) do
    profiles = state.profiles || %{}

    unconfigured =
      profiles
      |> Enum.filter(fn {_n, p} -> match?({:error, _, _}, resolve_secrets(p, state.getenv)) end)
      |> Enum.map(fn {n, _} -> n end)
      |> Enum.sort()

    %{
      state:
        cond do
          is_nil(state.file) -> "off"
          is_nil(state.profiles) -> "invalid"
          state.error -> "stale"
          true -> "ok"
        end,
      raw_spawn: Atom.to_string(state.raw_spawn),
      digest: state.digest,
      profiles: profiles |> Map.keys() |> Enum.sort(),
      unconfigured: unconfigured,
      error: state.error
    }
  end

  # ---------- helpers ----------

  defp raw_mode("allow", _file), do: :allow
  defp raw_mode("deny", _file), do: :deny
  defp raw_mode(nil, nil), do: :allow
  defp raw_mode("", nil), do: :allow
  defp raw_mode(_other, _file), do: :deny

  defp setting(opts, key, app_key, env_var) do
    case Keyword.fetch(opts, key) do
      {:ok, value} ->
        value

      :error ->
        case Application.fetch_env(:agent_orchestrator, app_key) do
          {:ok, value} -> value
          :error -> System.get_env(env_var)
        end
    end
  end

  defp blank_to_nil(nil), do: nil
  defp blank_to_nil(""), do: nil
  defp blank_to_nil(v), do: v

  defp decode(json) do
    case Jason.decode(json) do
      {:ok, %{} = map} -> {:ok, map}
      {:ok, _} -> {:error, "file must hold a JSON object"}
      {:error, _} -> {:error, "file is not valid JSON"}
    end
  end

  defp expect(true, _reason), do: :ok
  defp expect(_false, reason), do: {:error, reason}

  defp expect_map(%{} = m, _what), do: {:ok, m}
  defp expect_map(_v, what), do: {:error, "#{what} must be a JSON object"}

  defp expect_keys(map, allowed, what) do
    case Map.keys(map) -- allowed do
      [] -> :ok
      extra -> {:error, "unknown #{what} key(s): #{names(extra)}"}
    end
  end

  defp abs_path(v, field) when is_binary(v) do
    if Path.type(v) == :absolute and not String.contains?(v, <<0>>),
      do: {:ok, v},
      else: {:error, "#{field} must be an absolute path"}
  end

  defp abs_path(_v, field), do: {:error, "#{field} must be an absolute path"}

  defp optional_abs_path(nil, _field), do: {:ok, nil}
  defp optional_abs_path(v, field), do: abs_path(v, field)

  defp string_list(list, field) when is_list(list) do
    if Enum.all?(list, &is_binary/1),
      do: {:ok, list},
      else: {:error, "#{field} must be a list of strings"}
  end

  defp string_list(_v, field), do: {:error, "#{field} must be a list of strings"}

  defp string_map(%{} = m, field) do
    if Enum.all?(m, fn {k, v} -> is_binary(k) and is_binary(v) end),
      do: {:ok, m},
      else: {:error, "#{field} must map strings to strings"}
  end

  defp string_map(_v, field), do: {:error, "#{field} must map strings to strings"}

  defp env_names(list, field) do
    if Enum.all?(list, &(is_binary(&1) and Regex.match?(@env_name_re, &1))),
      do: :ok,
      else: {:error, "#{field} holds an invalid variable name"}
  end

  defp one_of(v, allowed, field) do
    if v in allowed,
      do: {:ok, v},
      else: {:error, "#{field} must be one of #{Enum.join(allowed, ", ")}"}
  end

  defp optional_string(nil, _field), do: {:ok, nil}
  defp optional_string(v, _field) when is_binary(v) and v != "", do: {:ok, v}
  defp optional_string(_v, field), do: {:error, "#{field} must be a non-empty string"}

  defp positive(n, _field) when is_integer(n) and n > 0, do: {:ok, n}
  defp positive(_n, field), do: {:error, "#{field} must be a positive integer"}

  defp boolean(b, _field) when is_boolean(b), do: {:ok, b}
  defp boolean(_b, field), do: {:error, "#{field} must be true or false"}

  defp put_opt(spec, _key, nil), do: spec
  defp put_opt(spec, key, value), do: Map.put(spec, key, value)

  defp put_lease(spec, :absent), do: spec
  defp put_lease(spec, false), do: Map.put(spec, :lease, false)

  # Field and variable names a caller sent are echoed only when they look like
  # names; anything else is summarised, so a refusal never carries a value.
  defp names(list) do
    list
    |> Enum.map(fn n ->
      if is_binary(n) and byte_size(n) <= 64 and n =~ ~r/\A[\w.-]+\z/,
        do: n,
        else: "(invalid name)"
    end)
    |> Enum.sort()
    |> Enum.join(", ")
  end

  defp safe_name(n) when is_binary(n) do
    if Regex.match?(@name_re, n), do: n, else: "(invalid name)"
  end

  defp safe_name(_n), do: "(invalid name)"

  defp sha256(bin), do: :crypto.hash(:sha256, bin) |> Base.encode16(case: :lower)
end
