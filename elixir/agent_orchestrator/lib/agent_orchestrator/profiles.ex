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
  profile whose secret source is unset, shorter than four bytes or holds a
  line break is `unconfigured` and refuses its own spawns; the others are
  unaffected.

  ## Requests

  A closed schema: `profile`, `vars`, `max_runtime_ms`, `max_output_lines`
  (each at most the profile's ceiling), and `lineage` only where the profile
  accepts it. A refusal names the field or variable, never a value. The
  concurrency and per-hour limits, and the profile's secrets, are checked and
  resolved at admission (`admit/3`), which runs inside the idempotent spawn
  after a retry of an already-started key has replayed: a lost response is
  never turned into a refusal, and a replay needs no secret. Concurrency is
  recounted from live executions tagged with their profile, so a restart of
  this server forgets no running child; the per-hour count after a restart
  charges live executions but not ones that finished before it. Resolved secrets
  reach the runner only behind a closure, so no timeout message, crash report
  or supervisor report can print them.

  ## Freshness, when the generator owns the file

  With `AGENT_ORCHESTRATOR_PROFILES_GENERATE=1` (the operator handing the
  file to `scripts/ops/orchestrator-profiles.py`, which `scripts/start.sh` runs
  at boot), this server reruns the generator when it starts and every minute
  after, from the inputs recorded beside the file (`<file>.inputs.json`,
  `--replay`), and reloads what it writes. A stale profile is a stale egress
  policy, so profile spawns are refused, at preparation and again at
  admission, until the first run here succeeds, while the last run failed or
  ran too long, or while the file on disk does not load (operator decision 2026-10-27, KG
  `2026-10-27T08:11:41.632304+00:00`). `status/0` reports the generation
  state. A file the operator maintains by hand is not regenerated, and a bad
  replacement of it leaves the last good set in force, as above.

  ## Raw spawns

  `AGENT_ORCHESTRATOR_RAW_SPAWN` is `allow` or `deny`. It defaults to `deny`
  whenever the profiles file variable is set, whether or not the file loads,
  and to `allow` only when it is unset; any other value is `deny`. A broken
  profiles file therefore never reopens raw spawning.
  """

  use GenServer

  alias AgentOrchestrator.Profiles.Schema

  require Logger

  @hour_ms 3_600_000
  @default_reload_ms 5_000
  @default_regenerate_ms 60_000
  @inputs_suffix ".inputs.json"
  # How long an admitted id may stay unseen before it counts as never started.
  @start_grace_ms 60_000

  # ---------- public API ----------

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc "The server the HTTP surface uses (overridable for tests)."
  def server, do: Application.get_env(:agent_orchestrator, :profiles_server, __MODULE__)

  @doc "Operator-facing state: mode, file, digest, profile names, unconfigured, last error."
  def status(server \\ server()), do: GenServer.call(server, :status)

  @doc "`:allow` or `:deny`."
  def raw_spawn_mode(server \\ server()), do: GenServer.call(server, :raw_spawn_mode)

  @doc """
  Validate a request body against the loaded profiles and build its runner
  spec, without its secrets. Returns `{:ok, %{profile: name, spec: spec,
  digest: digest, revision: profile_digest}}` or `{:error, code, detail}`,
  where `code` is `:schema_invalid`, `:unknown_profile` or
  `:profiles_unavailable`. Admission must be given the same `revision`.
  """
  def prepare(server \\ server(), body), do: GenServer.call(server, {:prepare, body})

  @doc """
  Admit one new start of `profile` as `execution_id`, for the profile
  `revision` the request was prepared against: a reload in between is
  refused (`:profile_changed`), so a secret is never attached to a command
  from another revision. Then check its concurrency
  and per-hour limits and resolve its secrets. Called only once a start is
  known to be new (after a keyed retry has replayed), so a replay needs
  neither a free slot nor its secret. Returns `{:ok, secret_env}` or
  `{:error, :profile_changed | :rate_limited | :profile_unconfigured, detail}`.

  The slot is tied to `execution_id` at once. `started/2` or `refund/2`
  reports the start's outcome; a started execution holds its slot while it
  is live, and a refunded one gives back its slot and its hourly charge. An
  outcome that never arrives (the caller died mid-start, or the start was
  ambiguous) proves nothing about the child, so the slot is held while the id
  is live, and otherwise for a grace period before it counts as never
  started.
  """
  def admit(server \\ server(), profile, revision, execution_id),
    do: GenServer.call(server, {:admit, profile, revision, execution_id})

  @doc "Return the slot and hourly charge of a start that definitely failed."
  def refund(server \\ server(), execution_id),
    do: GenServer.cast(server, {:refund, execution_id})

  @doc "Record that `execution_id` started: once it is no longer live, its slot frees."
  def started(server \\ server(), execution_id),
    do: GenServer.cast(server, {:started, execution_id})

  # ---------- pure parsing (exposed for tests) ----------

  @doc false
  defdelegate parse(json), to: Schema

  # ---------- GenServer ----------

  @impl true
  def init(opts) do
    # Resolved once, so every reload reads the same file and health names it
    # absolutely, whatever the working directory later becomes.
    file =
      case blank_to_nil(setting(opts, :file, :profiles_file, "AGENT_ORCHESTRATOR_PROFILES_FILE")) do
        nil -> nil
        path -> Path.expand(path)
      end

    raw = setting(opts, :raw_spawn, :raw_spawn, "AGENT_ORCHESTRATOR_RAW_SPAWN")

    generate? =
      not is_nil(file) and
        setting(opts, :generate, :profiles_generate, "AGENT_ORCHESTRATOR_PROFILES_GENERATE") in [
          "1",
          true
        ]

    state = %{
      file: file,
      raw_spawn: raw_mode(raw, file),
      profiles: nil,
      digest: nil,
      error: nil,
      reload_ms: Keyword.get(opts, :reload_ms, @default_reload_ms),
      generate?: generate?,
      regenerate_ms: Keyword.get(opts, :regenerate_ms, @default_regenerate_ms),
      regenerate: Keyword.get(opts, :regenerate, &regenerate_from_record/1),
      # Unverified until the first run here succeeds: the file a restart
      # finds may be one start.sh failed to regenerate, and a restart must
      # not clear a refusal the last run had earned.
      generation: %{
        error: if(generate?, do: "not yet regenerated since this server started"),
        task: nil,
        started_at: nil,
        last_ok_at: nil
      },
      getenv: Keyword.get(opts, :getenv, &System.get_env/1),
      environ: Keyword.get(opts, :environ, &System.get_env/0),
      live?: Keyword.get(opts, :live?, &live?/1),
      live_executions: Keyword.get(opts, :live_executions, &live_executions/0),
      start_grace_ms: Keyword.get(opts, :start_grace_ms, @start_grace_ms),
      uid: Keyword.get_lazy(opts, :uid, &current_uid/0),
      now: Keyword.get(opts, :now, fn -> System.monotonic_time(:millisecond) end),
      starts: %{},
      slots: %{}
    }

    state = if file, do: reload(state), else: state
    if file, do: schedule_reload(state.reload_ms)
    # The first run now, then every interval.
    if generate?, do: send(self(), :regenerate)
    {:ok, recount_hour(state)}
  end

  @impl true
  def handle_call(:status, _from, state), do: {:reply, present_status(state), state}

  def handle_call(:raw_spawn_mode, _from, state), do: {:reply, state.raw_spawn, state}

  def handle_call({:prepare, body}, _from, state) do
    {:reply, do_prepare(body, state), state}
  end

  def handle_call({:admit, name, revision, execution_id}, _from, state) do
    # Checked again here: a request prepared while fresh may reach admission
    # (after a keyed reservation, say) once the profiles are not.
    case stale_reason(state) do
      nil ->
        admit_current(name, revision, execution_id, state)

      reason ->
        {:reply, {:error, :profiles_unavailable, "profiles are stale: #{reason}"}, state}
    end
  end

  defp admit_current(name, revision, execution_id, state) do
    case state.profiles && Map.get(state.profiles, name) do
      %{digest: ^revision} = profile ->
        admit_profile(profile, name, execution_id, state)

      _changed_or_gone ->
        {:reply,
         {:error, :profile_changed,
          "profile #{Schema.safe_name(name)} changed while the request was waiting; retry"},
         state}
    end
  end

  defp admit_profile(profile, name, execution_id, state) do
    state = prune(state)
    now = state.now.()
    # Live executions tagged with this profile count as well as admitted
    # slots, so a restart of this server cannot forget what is running.
    running =
      state.slots
      |> Enum.flat_map(fn {id, slot} -> if slot.name == name, do: [id], else: [] end)
      |> Enum.concat(for {id, ^name} <- state.live_executions.(), do: id)
      |> Enum.uniq()
      |> length()

    recent = Map.get(state.starts, name, [])

    cond do
      running >= profile.max_concurrent ->
        {:reply, {:error, :rate_limited, "profile #{name} is at max_concurrent"}, state}

      length(recent) >= profile.max_per_hour ->
        {:reply, {:error, :rate_limited, "profile #{name} is at max_per_hour"}, state}

      true ->
        case Schema.resolve_secrets(profile, state.getenv) do
          {:ok, secret_env} ->
            slot = %{name: name, admitted_at: now, started: false}

            state = %{
              state
              | slots: Map.put(state.slots, execution_id, slot),
                starts: Map.put(state.starts, name, [{now, execution_id} | recent])
            }

            {:reply, {:ok, secret_env}, state}

          {:error, code, detail} ->
            {:reply, {:error, code, detail}, state}
        end
    end
  end

  @impl true
  def handle_cast({:started, execution_id}, state) do
    case Map.fetch(state.slots, execution_id) do
      {:ok, slot} ->
        {:noreply, %{state | slots: Map.put(state.slots, execution_id, %{slot | started: true})}}

      :error ->
        {:noreply, state}
    end
  end

  def handle_cast({:refund, execution_id}, state) do
    case Map.pop(state.slots, execution_id) do
      {nil, _} ->
        {:noreply, state}

      {%{name: name}, slots} ->
        starts =
          Map.update(state.starts, name, [], fn l ->
            Enum.reject(l, &match?({_, ^execution_id}, &1))
          end)

        {:noreply, %{state | slots: slots, starts: starts}}
    end
  end

  @impl true
  def handle_info(:reload, state) do
    schedule_reload(state.reload_ms)
    {:noreply, reload(state)}
  end

  def handle_info(:regenerate, state) do
    schedule_regenerate(state.regenerate_ms)
    {:noreply, start_regeneration(state)}
  end

  def handle_info({ref, result}, %{generation: %{task: %Task{ref: ref}}} = state) do
    Process.demonitor(ref, [:flush])
    now = state.now.()

    generation =
      case result do
        :ok ->
          %{state.generation | error: nil, task: nil, started_at: nil, last_ok_at: now}

        {:error, reason} ->
          if reason != state.generation.error,
            do: Logger.error("orchestrator profiles not regenerated: #{reason}")

          %{state.generation | error: reason, task: nil, started_at: nil}
      end

    # Load what the generator wrote now, not at the next reload tick.
    {:noreply, reload(%{state | generation: generation})}
  end

  def handle_info(
        {:DOWN, ref, :process, _pid, reason},
        %{generation: %{task: %Task{ref: ref}}} = state
      ) do
    error = "the regeneration task exited (#{inspect(reason)})"
    Logger.error("orchestrator profiles not regenerated: #{error}")
    {:noreply, %{state | generation: %{state.generation | error: error, task: nil}}}
  end

  def handle_info(_message, state), do: {:noreply, state}

  # ---------- regeneration ----------

  # One run at a time. A run still going when the next is due has taken a
  # whole interval, and is reported (and refuses spawns) until it ends.
  defp start_regeneration(%{generation: %{task: %Task{}}} = state) do
    elapsed = state.now.() - state.generation.started_at

    if elapsed >= state.regenerate_ms do
      error = "regeneration has been running for #{div(elapsed, 1000)} s"
      %{state | generation: %{state.generation | error: error}}
    else
      state
    end
  end

  defp start_regeneration(state) do
    file = state.file
    regenerate = state.regenerate
    # The task is linked, so nothing it raises may escape it: a failed run
    # is a result, never a crash of this server.
    task =
      Task.async(fn ->
        try do
          regenerate.(file)
        rescue
          e -> {:error, "the generator could not run: #{Exception.message(e)}"}
        catch
          kind, reason -> {:error, "the generator could not run: #{inspect({kind, reason})}"}
        end
      end)

    %{state | generation: %{state.generation | task: task, started_at: state.now.()}}
  end

  # The generator, run from the inputs its last write recorded, in an empty
  # environment but PATH, as start.sh runs it. The record is held to the same
  # ownership rule as the file it describes, and must name that file.
  defp regenerate_from_record(file) do
    inputs = file <> @inputs_suffix

    with :ok <- owned_and_private(inputs, current_uid(), "input record"),
         {:ok, body} <- read(inputs),
         {:ok, record} <- decode_record(body, inputs),
         :ok <- record_names(record, file, inputs) do
      generator = Path.join([record["repo_root"], "scripts", "ops", "orchestrator-profiles.py"])
      path = System.get_env("PATH") || "/usr/bin:/bin"

      case System.cmd(
             "/usr/bin/env",
             ["-i", "PATH=" <> path, record["python"], "-I", generator, "--replay", inputs],
             stderr_to_stdout: true
           ) do
        {_out, 0} ->
          :ok

        {out, status} ->
          last = out |> String.split("\n", trim: true) |> List.last() || "no output"
          {:error, "the generator exited #{status}: #{String.slice(last, 0, 300)}"}
      end
    end
  end

  defp decode_record(body, inputs) do
    case Jason.decode(body) do
      {:ok, %{"python" => python, "repo_root" => root} = record}
      when is_binary(python) and is_binary(root) ->
        {:ok, record}

      _ ->
        {:error, "#{inputs} is not a generator input record"}
    end
  end

  defp record_names(%{"out" => out}, file, _inputs) when is_binary(out) do
    if Path.expand(out) == file,
      do: :ok,
      else: {:error, "the input record names #{out}, not #{file}"}
  end

  defp record_names(_record, _file, inputs), do: {:error, "#{inputs} names no output file"}

  defp schedule_regenerate(ms), do: Process.send_after(self(), :regenerate, ms)

  # Why profile spawns are refused as stale, or nil. Only a file the generator
  # owns is held to it.
  defp stale_reason(%{generate?: false}), do: nil
  defp stale_reason(%{generation: %{error: error}}) when is_binary(error), do: error

  defp stale_reason(%{error: error}) when is_binary(error),
    do: "the profiles file does not load: #{error}"

  defp stale_reason(_state), do: nil

  # ---------- loading ----------

  defp reload(state) do
    with :ok <- check_file_safety(state.file, state.uid),
         {:ok, body} <- read(state.file) do
      digest = Schema.sha256(body)

      if digest == state.digest do
        # The loaded set is back on disk (say, after a bad replacement was
        # reverted), so any stale error is over.
        %{state | error: nil}
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

  # ---------- request preparation ----------

  defp do_prepare(body, state) do
    Schema.prepare(body, loaded(state), state.environ)
  end

  defp loaded(%{profiles: nil, file: nil}),
    do: {:error, :profiles_unavailable, "no profiles file is configured"}

  defp loaded(%{profiles: nil, error: error}),
    do: {:error, :profiles_unavailable, "profiles file not loaded: #{error}"}

  defp loaded(%{profiles: profiles} = state) do
    case stale_reason(state) do
      nil -> {:ok, profiles}
      reason -> {:error, :profiles_unavailable, "profiles are stale: #{reason}"}
    end
  end

  # ---------- rate state ----------

  defp prune(state) do
    now = state.now.()
    cutoff = now - @hour_ms

    slots =
      for {id, slot} <- state.slots,
          slot = %{slot | started: slot.started or state.live?.(id)},
          keep?(slot, state.live?.(id), now, state.start_grace_ms),
          into: %{},
          do: {id, slot}

    starts =
      state.starts
      |> Enum.map(fn {name, list} -> {name, Enum.filter(list, fn {at, _} -> at > cutoff end)} end)
      |> Map.new()

    %{state | slots: slots, starts: starts}
  end

  # Live: held. Started and gone: finished, freed. Never seen live with no
  # reported outcome: held for the start grace, then counted as never started.
  defp keep?(_slot, true, _now, _grace), do: true
  defp keep?(%{started: true}, false, _now, _grace), do: false
  defp keep?(slot, false, now, grace), do: now - slot.admitted_at < grace

  defp live?(execution_id), do: execution_id in AgentOrchestrator.list()

  # Live executions and the profile each was started from (nil for raw ones).
  defp live_executions do
    Enum.map(AgentOrchestrator.list_details(), &{&1.execution_id, Map.get(&1, :profile)})
  catch
    :exit, _ -> []
  end

  # After a restart this server has no record of the past hour. Executions
  # still live count as started now, so they are charged; finished ones from
  # before the restart are not known and are not charged.
  defp recount_hour(state) do
    now = state.now.()

    starts =
      for {id, name} <- state.live_executions.(), is_binary(name), reduce: state.starts do
        acc -> Map.update(acc, name, [{now, id}], &[{now, id} | &1])
      end

    %{state | starts: starts}
  end

  # ---------- status ----------

  defp present_status(state) do
    profiles = state.profiles || %{}

    unconfigured =
      profiles
      |> Enum.filter(fn {_n, p} ->
        match?({:error, _, _}, Schema.resolve_secrets(p, state.getenv))
      end)
      |> Enum.map(fn {n, _} -> n end)
      |> Enum.sort()

    %{
      state:
        cond do
          is_nil(state.file) -> "off"
          is_nil(state.profiles) -> "invalid"
          state.error || stale_reason(state) -> "stale"
          true -> "ok"
        end,
      # Present only when the generator owns the file: whether profile spawns
      # are refused as stale, and why.
      generation:
        if state.generate? do
          %{
            managed: true,
            enforced: not is_nil(stale_reason(state)),
            error: state.generation.error,
            running: not is_nil(state.generation.task)
          }
        end,
      raw_spawn: Atom.to_string(state.raw_spawn),
      # The file the digest is of, absolute (resolved in init/1), so the
      # doctor can compare it with a fresh generation from the inputs
      # recorded beside it.
      file: state.file,
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
end
