defmodule AgentOrchestrator.AgentRunner.LeaseGovernance do
  @moduledoc """
  Lease acquisition/release, lineage/server-URL/session-anchor environment
  provisioning, Port launch, and process-tree reaping helpers for
  `AgentOrchestrator.AgentRunner`.
  """

  import Bitwise

  require Logger

  alias AgentOrchestrator.AgentRunner
  alias AgentOrchestrator.AgentRunner.OutputBuffer

  # Child stdin disposition. Default `close` — see wire_stdin/3 for why an open
  # stdin is both unusable here and a hang risk for the CLIs we spawn.
  @default_stdin "close"
  @sh "/bin/sh"

  # Default ceiling on a single agent's wall-clock lifetime (30 min). Generous —
  # well past the slowest known consumer (orchestrated reviewer ~70s, council
  # lanes ~120s) — so it only ever fires on a genuinely wedged agent. Per-spawn
  # overridable via the spec's `max_runtime_ms` (nil / <= 0 disables it);
  # config-overridable via :default_max_runtime_ms.
  @default_max_runtime_ms 1_800_000

  # Lineage-provisioning env vars surfaced to the child (candidate
  # declarations — see "Lineage provisioning" in the moduledoc).
  @server_url_var "UNITARES_SERVER_URL"
  @client_session_id_var "UNITARES_CLIENT_SESSION_ID"
  # Fail-closed companion to the session anchor: the child's onboard hook resumes
  # under the anchor ONLY when this orchestration marker is present, so a stray
  # UNITARES_CLIENT_SESSION_ID leaked into a normal interactive session (which the
  # same hook serves) cannot trigger resume-sharing — it mints. See the
  # session-anchor moduledoc section and onboard_helper.run_onboard's `orchestrated`.
  @orchestrated_var "UNITARES_ORCHESTRATED"
  @lineage_parent_var "UNITARES_PARENT_AGENT_ID"
  @lineage_reason_var "UNITARES_SPAWN_REASON"
  @default_spawn_reason "subagent"

  # Shape check only (8-4-4-4-12 hex) — ancestry truth is the server's call.
  @uuid_re ~r/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/

  # Presence is DEFAULT-ON and best-effort: with no `:lease` key, an agent
  # registers an `agent:/<id>` presence row on the plane (migration 042 routes it
  # to the self-healing remote_heartbeat path). `lease: false` opts out entirely.
  # A `:lease` map overrides the defaults (e.g. `required: true` to make it a
  # gating lease, or a different `surface_id`).
  @doc false
  def normalize_lease_cfg(false, _execution_id), do: nil

  def normalize_lease_cfg(nil, execution_id), do: normalize_lease_cfg(%{}, execution_id)

  def normalize_lease_cfg(%{} = cfg, execution_id) do
    %{
      # Best-effort by default: presence should NOT gate spawning. A caller that
      # genuinely needs a gating lease passes `required: true`.
      required: Map.get(cfg, :required, false),
      holder_agent_uuid: Map.get(cfg, :holder_agent_uuid) || uuid4(),
      surface_id: Map.get(cfg, :surface_id) || "agent:/" <> execution_id,
      ttl_s:
        Map.get(cfg, :ttl_s, Application.get_env(:agent_orchestrator, :default_lease_ttl_s, 300))
    }
  end

  # Returns {:ok, lease_id, presence} | {:error, :lease_denied, reason}.
  # presence is :disabled | :registered | :unregistered — a distinguishable
  # signal so a consumer of plane presence knows that an :unregistered agent is
  # live-but-not-on-the-plane (absence from the plane ≠ not running).
  @doc false
  def maybe_acquire_lease(%{lease_cfg: nil}), do: {:ok, nil, :disabled}

  def maybe_acquire_lease(%{
        execution_id: execution_id,
        agent_id: agent_id,
        lease_cfg: cfg,
        lease_client: client
      }) do
    case client.acquire(cfg.surface_id, cfg.holder_agent_uuid, "remote_heartbeat", cfg.ttl_s) do
      {:ok, lease_id} ->
        {:ok, lease_id, :registered}

      {:error, reason} ->
        if cfg.required do
          {:error, :lease_denied, reason}
        else
          # Best-effort: proceed, but emit a DISTINGUISHABLE signal. This agent is
          # running yet has no plane presence row — anything querying the plane
          # for "live agents" must not read this agent's absence as "not running."
          # :no_bearer means presence is simply not configured (dev/test) → quiet;
          # any other reason is a configured-but-unreachable plane → loud.
          level = if reason == :no_bearer, do: :debug, else: :warning

          Logger.log(
            level,
            "execution #{execution_id} agent=#{agent_id} presence UNREGISTERED " <>
              "(#{inspect(reason)}) — running " <>
              "WITHOUT a presence row; plane-absence does NOT imply not-running"
          )

          {:ok, nil, :unregistered}
        end
    end
  end

  @doc false
  def maybe_release_lease(%{lease_id: nil}, _reason), do: :no_lease

  def maybe_release_lease(%{lease_id: lease_id, lease_client: client}, reason) do
    case client.release(lease_id, reason) do
      :ok ->
        :ok

      {:error, r} = err ->
        Logger.warning("agent lease release failed lease=#{lease_id}: #{inspect(r)}")
        err
    end
  end

  @doc false
  def open_port(spec, candidates) do
    cmd = Map.fetch!(spec, :cmd)

    case resolve_executable(cmd) do
      nil ->
        {:error, {:executable_not_found, cmd}}

      path ->
        # `|| []` not just a default: an explicit `env: nil` (spec built from
        # nullable config) must not crash the merge below — that exception
        # escapes open_port's rescue and orphans the just-acquired lease.
        env = Map.get(spec, :env, []) || []

        {exec, exec_args} =
          wire_stdin(Map.get(spec, :stdin) || @default_stdin, path, Map.get(spec, :args, []))

        opts =
          [
            :binary,
            :exit_status,
            # EOF is the output-drain barrier. Its ordering relative to
            # exit_status is unspecified, so AgentRunner waits for both.
            :eof,
            :stderr_to_stdout,
            {:line, AgentRunner.line_max_bytes()},
            {:args, exec_args}
          ]
          |> maybe_opt(
            :env,
            encode_env(child_env(env ++ OutputBuffer.secret_env(spec), candidates))
          )
          |> maybe_opt(:cd, Map.get(spec, :cd))

        try do
          port = Port.open({:spawn_executable, exec}, opts)
          os_pid = port |> Port.info(:os_pid) |> elem(1)
          {:ok, port, os_pid}
        rescue
          e -> {:error, {:port_open_error, Exception.message(e)}}
        end
    end
  end

  defp resolve_executable(cmd) do
    if String.contains?(cmd, "/"), do: cmd, else: System.find_executable(cmd)
  end

  # Close the child's stdin by re-exec'ing it through /bin/sh with stdin
  # redirected from /dev/null. Two properties matter and neither survives if the
  # caller does this wrapping itself:
  #
  #   * the ALLOWLIST and `executable_not_found` still see the real command,
  #     because resolve_executable/1 runs on `spec.cmd` before this. A caller
  #     that passes `/bin/sh` itself defeats both — a missing binary becomes an
  #     opaque exit 127 from the shell instead of a spawn error, so the caller's
  #     fail-open path never fires.
  #   * `"$0"` / `"$@"` keep the command and its arguments as separate argv
  #     words, so an argument containing quotes, `$(...)`, or backticks is never
  #     interpreted by the shell.
  defp wire_stdin("pipe", path, args), do: {path, args}

  defp wire_stdin(_close, path, args),
    do: {@sh, ["-c", ~s(exec "$0" "$@" </dev/null), path | args]}

  # Lineage PROVISIONING (see moduledoc): candidate declarations only — the
  # child makes its own onboard call. Validated up front because a malformed
  # parent UUID does not fail here; it fails later as false ancestry in the
  # governance lineage DAG, attributed to whatever agent declared it.
  @doc false
  def normalize_lineage_cfg(nil), do: {:ok, nil}

  def normalize_lineage_cfg(%{} = cfg) do
    parent = Map.get(cfg, :parent_agent_uuid)
    spawn_reason = Map.get(cfg, :spawn_reason, @default_spawn_reason)

    cond do
      not (is_binary(parent) and Regex.match?(@uuid_re, parent)) ->
        {:error, {:parent_agent_uuid_not_uuid, parent}}

      not (is_binary(spawn_reason) and spawn_reason != "") ->
        {:error, {:spawn_reason_invalid, spawn_reason}}

      true ->
        {:ok, %{parent_agent_uuid: parent, spawn_reason: spawn_reason}}
    end
  end

  def normalize_lineage_cfg(other), do: {:error, {:lineage_not_map, other}}

  # Scheme check only — a value with no http(s):// scheme fails at the child
  # as a confusing OFFLINE far from its cause; refuse it at the boundary.
  @doc false
  def normalize_server_url(nil), do: {:ok, nil}

  def normalize_server_url(url) when is_binary(url) do
    if String.starts_with?(url, ["http://", "https://"]) do
      {:ok, url}
    else
      {:error, {:server_url_not_http, url}}
    end
  end

  def normalize_server_url(other), do: {:error, {:server_url_not_string, other}}

  # Non-empty-string check only — the server canonicalizes/sanitizes the session
  # key itself (see resolve_session_identity). An empty/blank anchor is refused
  # here because it would silently fall back to fresh-mint-per-turn, the exact
  # symptom this provisioning prevents (a confusing "still minting" far from the
  # blank-anchor cause).
  @doc false
  def normalize_client_session_id(nil), do: {:ok, nil}

  def normalize_client_session_id(v) when is_binary(v) do
    if String.trim(v) == "" do
      {:error, {:client_session_id_blank, v}}
    else
      {:ok, v}
    end
  end

  def normalize_client_session_id(other), do: {:error, {:client_session_id_not_string, other}}

  # The full provisioned-candidate list for the child env, built from the
  # already-validated configs.
  @doc false
  def candidate_env(lineage_cfg, server_url, client_session_id) do
    lineage =
      case lineage_cfg do
        nil ->
          []

        cfg ->
          [{@lineage_parent_var, cfg.parent_agent_uuid}, {@lineage_reason_var, cfg.spawn_reason}]
      end

    server = if server_url, do: [{@server_url_var, server_url}], else: []

    # The anchor travels WITH the orchestration marker: the child's hook
    # fail-closes on the marker, so provisioning the anchor without it would
    # make a genuine orchestrated child fail to resume (anchor ignored). They
    # are one unit — set both, or neither.
    session =
      if client_session_id,
        do: [{@client_session_id_var, client_session_id}, {@orchestrated_var, "1"}],
        else: []

    lineage ++ server ++ session
  end

  defp provisioned_env([], _env), do: []

  defp provisioned_env(candidates, env) do
    explicit = for {k, v} <- env, is_binary(v), into: MapSet.new(), do: k

    # Explicit env wins: the caller setting the var directly has made the more
    # specific statement; silently overriding it would be the invasive version.
    # An unset (`false`) is not a statement about the value, so provisioning
    # still applies over it.
    Enum.reject(candidates, fn {k, _v} -> MapSet.member?(explicit, k) end)
  end

  # Child env: explicit entries, minus any unset a provisioned variable
  # replaces, then the provisioned variables themselves.
  defp child_env(env, candidates) do
    provisioned = provisioned_env(candidates, env)
    replaced = MapSet.new(provisioned, fn {k, _v} -> k end)
    Enum.reject(env, fn {k, v} -> v == false and MapSet.member?(replaced, k) end) ++ provisioned
  end

  defp maybe_opt(opts, _key, nil), do: opts
  defp maybe_opt(opts, _key, []), do: opts
  defp maybe_opt(opts, key, val), do: [{key, val} | opts]

  # Arm the self-reap timer unless this spawn opted out. The message lands in our
  # own mailbox after init returns, handled by handle_info(:max_runtime, ...).
  @doc false
  def maybe_arm_max_runtime(spec) do
    case max_runtime_ms(spec) do
      ms when is_integer(ms) and ms > 0 -> Process.send_after(self(), :max_runtime, ms)
      _ -> :ok
    end
  end

  # An explicit `max_runtime_ms` in the spec wins (an integer caps; nil / <= 0
  # disables — the per-spawn escape hatch for a legitimately long agent). Absent,
  # fall back to the configured/default ceiling.
  defp max_runtime_ms(spec) do
    case Map.fetch(spec, :max_runtime_ms) do
      {:ok, v} ->
        v

      :error ->
        Application.get_env(:agent_orchestrator, :default_max_runtime_ms, @default_max_runtime_ms)
    end
  end

  defp encode_env([]), do: []

  defp encode_env(env) do
    Enum.map(env, fn
      {k, false} -> {String.to_charlist(k), false}
      {k, v} -> {String.to_charlist(k), String.to_charlist(v)}
    end)
  end

  @doc false
  def safe_close(port) do
    if Port.info(port) != nil, do: Port.close(port)
  catch
    :error, _ -> :ok
  end

  # Recursively SIGKILL a process and all its descendants, deepest first, so a
  # reaped agent leaves no orphaned subprocesses (e.g. the MCP servers `claude`
  # spawns). Best-effort and side-effecting; an already-dead pid just fails the
  # kill harmlessly. Children are discovered via `pgrep -P` BEFORE the parent is
  # signalled — once the parent dies its children reparent to init and can no
  # longer be found by walking from it.
  @doc false
  def kill_tree(pid) do
    children =
      case System.cmd("pgrep", ["-P", "#{pid}"], stderr_to_stdout: true) do
        {out, 0} -> out |> String.split() |> Enum.map(&String.to_integer/1)
        _ -> []
      end

    Enum.each(children, &kill_tree/1)
    System.cmd("kill", ["-KILL", "#{pid}"], stderr_to_stdout: true)
    :ok
  catch
    # pgrep/kill are external; a malformed pid or a transient System.cmd failure
    # must not crash terminate/2 (which would skip the lease release below it).
    _, _ -> :ok
  end

  # RFC-4122 v4 UUID without a third-party dep. Version nibble forced to 4 and
  # the variant high bits to 10xx per §4.4.
  @doc false
  def uuid4 do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    c = (c &&& 0x0FFF) ||| 0x4000
    d = (d &&& 0x3FFF) ||| 0x8000

    :io_lib.format("~8.16.0b-~4.16.0b-~4.16.0b-~4.16.0b-~12.16.0b", [a, b, c, d, e])
    |> IO.iodata_to_binary()
  end
end
