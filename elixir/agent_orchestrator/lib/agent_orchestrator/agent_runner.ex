defmodule AgentOrchestrator.AgentRunner do
  @moduledoc """
  One GenServer per ephemeral agent, wrapping a `Port` to an external runtime.

  The Port is the BEAM↔runtime boundary: the agent's actual model-calling work
  runs in a separate OS process (a Claude SDK script, `claude -p`, a tool
  worker), and BEAM owns only its lifecycle. We deliberately do NOT reimplement
  the agent loop in Elixir — the harness/SDK is Anthropic-maintained; rebuilding
  it here would be owning a moving target to gain less.

  ## Lifecycle

    1. `init/1` optionally acquires a `remote_heartbeat` lease for the agent's
       `agent:<id>` surface. If a lease is configured (`:required`) and the
       acquire fails, the agent refuses to start (admission control, fail closed).
    2. The Port is opened with `:exit_status` and line framing; stdout+stderr are
       captured into a bounded buffer.
    3. On `{:exit_status, status}` the lease is released and the runner stops
       `:normal` (status 0) or `{:shutdown, {:exit_status, n}}` (non-zero). Being
       `restart: :temporary`, the supervisor does not resurrect it.
    4. `terminate/2` releases the lease on any shutdown path (kill, app stop), so
       the lease is freed promptly rather than waiting out its TTL — the TTL is
       the backstop for a crash that skips `terminate/2`, not the normal path.

  ## Spec

      %{
        agent_id: String.t() | nil,        # generated if absent
        cmd: String.t(),                   # executable name or absolute path (required)
        args: [String.t()],                # argv (default [])
        env: [{String.t(), String.t()}],   # extra env for the child (default [])
        cd: String.t() | nil,              # working dir
        max_output_lines: pos_integer(),   # buffer cap (default 1000)
        max_runtime_ms: pos_integer(),     # wall-clock lifetime cap; self-reaps on expiry
                                           # (default :default_max_runtime_ms; nil/<=0 disables)
        lease: nil | false | lease_cfg,    # default (absent/nil) = best-effort agent:/ presence;
                                           # false = no presence; map = override
        lease_client: module(),            # injectable, default LeasePlaneClient
        lineage: nil | lineage_cfg,        # default nil = no lineage provisioning
        server_url: String.t() | nil,      # default nil = no server-URL provisioning
        client_session_id: String.t() | nil # default nil = no session-anchor provisioning
      }

      lease_cfg :: %{
        required: boolean(),               # default FALSE — presence is best-effort, not gating
        surface_id: String.t(),            # default "agent:/<id>" (presence surface)
        holder_agent_uuid: String.t(),     # the agent's governance UUID (generated if absent)
        ttl_s: pos_integer()               # default from config :default_lease_ttl_s
      }

      lineage_cfg :: %{
        parent_agent_uuid: String.t(),     # spawner's governance UUID (required, UUID-shaped)
        spawn_reason: String.t()           # default "subagent" (server vocabulary also has
                                           # compaction | new_session | explicit)
      }

  ## Lineage provisioning (not injection)

  A spawner that knows its own governance UUID can pass `lineage:`, and the
  child env gains `UNITARES_PARENT_AGENT_ID` / `UNITARES_SPAWN_REASON`. These
  are CANDIDATE declarations, consistent with the declaration-based identity
  ontology (identity.md v2): the child declares lineage in its own
  onboard/start_session call — or declines to. The orchestrator cannot and
  does not make a governance call on the child's behalf; it only puts the
  ground-truth parent UUID where the child (or its session-start hook) can
  find it, so lineage correctness comes from the spawn context instead of a
  prompt convention someone has to remember.

  Explicit `env:` entries win over provisioned ones — a caller that sets
  `UNITARES_PARENT_AGENT_ID` itself has made the more specific statement. A
  malformed `parent_agent_uuid` refuses the spawn (`{:error,
  {:invalid_lineage, _}}`): a garbage candidate at the provisioning boundary
  surfaces downstream as false ancestry in the lineage DAG. The result's
  `:lineage` field reports `:provisioned` or `:none`.

  ## Server-URL provisioning

  `server_url:` provisions `UNITARES_SERVER_URL` into the child env under
  the same explicit-wins rule. Without it, a child whose governance hooks
  default to `http://localhost:8767` silently talks to the wrong server
  whenever the spawner targets a non-default one. This is transport config,
  not lineage, hence a top-level key rather than a `lineage_cfg` field. A
  value without an `http(s)://` scheme refuses the spawn (`{:error,
  {:invalid_server_url, _}}`) — a malformed URL fails at the child as a
  confusing OFFLINE, far from its cause.

  ## Session-anchor provisioning (thread-stable identity)

  `client_session_id:` provisions `UNITARES_CLIENT_SESSION_ID` into the child
  env under the same explicit-wins rule. It is the **continuity anchor** a
  spawner passes when many short-lived children are turns of one logical
  conversation (the canonical case: the Discord BEAM bridge, where each user
  turn is a fresh `claude -p` process). Each fresh process otherwise opens a new
  governance session and onboards a fresh identity — a new UUID *every turn*.
  When the spawner passes a **stable** per-conversation anchor (e.g.
  `"agent:/thread-<discord-thread-id>"`), every turn's child onboards under the
  same session key and the server resolves it to the **same** governance UUID
  across turns (resume), instead of minting a disconnected identity each time.

  This is transport/continuity config, not lineage (the turns are the *same*
  agent resumed, not a parent/child chain), hence a top-level key rather than a
  `lineage_cfg` field. The orchestrator does NOT onboard on the child's behalf;
  it only places the anchor where the child's onboard hook can find it. An empty
  or non-string value refuses the spawn (`{:error, {:invalid_client_session_id,
  _}}`) — a blank anchor would silently degrade back to fresh-mint-per-turn, the
  exact failure this provisioning exists to prevent.

  ### Fail-closed marker (`UNITARES_ORCHESTRATED`)

  The anchor is provisioned WITH a companion `UNITARES_ORCHESTRATED=1` marker.
  The same onboard hook serves *normal interactive* sessions, so it must not
  treat a bare `UNITARES_CLIENT_SESSION_ID` as sufficient to resume — a stray
  global export of that var leaking into an interactive session would otherwise
  flip it into resume mode, and two sessions sharing the leaked value would
  resume onto ONE governance UUID (the ghost-siphon the v2 identity ontology
  removed name-claim to prevent). So the hook fail-closes: it resumes only when
  the marker explicitly declares an orchestrated headless turn-child. The
  orchestrator KNOWS its children are exactly that, so it sets the marker; an
  interactive session never carries it and therefore can never resume-share. The
  two vars are one unit — set together or not at all.

  ## Presence

  By default an agent registers an `agent:/<id>` PRESENCE row on the lease plane
  (migration 042 routes it to the self-healing remote_heartbeat path). It is
  best-effort: a plane failure does NOT block the spawn. The result's `:presence`
  field is the distinguishable signal:

    * `:registered`   — a plane presence row exists for this agent.
    * `:unregistered` — best-effort acquire failed; the agent is running but has
      NO plane row (so plane-absence ≠ not-running).
    * `:disabled`     — presence was turned off (`lease: false`).
  """

  use GenServer

  import Bitwise

  require Logger

  alias AgentOrchestrator.LeasePlaneClient
  alias AgentOrchestrator.ResultStore

  @default_max_lines 1000
  @line_max_bytes 65_536

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

  defstruct [
    :agent_id,
    :port,
    :os_pid,
    :lease_id,
    :lease_client,
    :lease_cfg,
    :presence,
    # :provisioned | :none — provisioning status only; the lineage_cfg map
    # itself is consumed in init/1 and never stored.
    :lineage,
    :exit_status,
    :release_status,
    output: [],
    output_count: 0,
    max_output_lines: @default_max_lines,
    partial: "",
    waiters: []
  ]

  # Orchestrator-initiated lease release reason. The plane validates
  # release_reason against an allowlist (normal | down_local | reaped_* |
  # handoff); "normal" is the orderly-release member.
  @release_reason "normal"

  # ---------- public API ----------

  def start_link(%{} = spec) do
    agent_id = Map.fetch!(spec, :agent_id)
    GenServer.start_link(__MODULE__, spec, name: via(agent_id))
  end

  @doc "Block until the agent exits (or `timeout` ms). Returns `{:ok, result}` or `{:error, :timeout}`."
  @spec await(String.t(), timeout()) :: {:ok, map()} | {:error, :timeout | :not_found}
  def await(agent_id, timeout \\ 30_000) do
    case call(agent_id, :await, timeout) do
      # whereis/0 already saw the runner gone — fall back to the retained result.
      {:error, :not_found} -> retained_or_not_found(agent_id)
      reply -> reply
    end
  catch
    :exit, {:timeout, _} ->
      {:error, :timeout}

    # The agent can exit between whereis/0 and the call landing in its mailbox
    # (it stops itself on exit). GenServer.call then exits :noproc/:normal —
    # catch it instead of crashing the caller. The runner writes its final
    # result to ResultStore before it dies (see finalize/2), so a fast agent's
    # result survives this race rather than being lost to :not_found (#581).
    :exit, _ ->
      retained_or_not_found(agent_id)
  end

  @doc "Current captured output and status without blocking."
  @spec snapshot(String.t()) :: {:ok, map()} | {:error, :not_found}
  def snapshot(agent_id) do
    case call(agent_id, :snapshot) do
      {:error, :not_found} -> retained_or_not_found(agent_id)
      reply -> reply
    end
  catch
    # Same dead-mid-call race as await/2: a live whereis/0 followed by the
    # process exiting before the snapshot lands. Fall back to the retained
    # terminal result instead of crashing the caller with the GenServer exit.
    :exit, _ ->
      retained_or_not_found(agent_id)
  end

  # On a dead runner, the terminal result may have been retained by finalize/2.
  defp retained_or_not_found(agent_id) do
    case ResultStore.fetch(agent_id) do
      {:ok, result} -> {:ok, result}
      :error -> {:error, :not_found}
    end
  end

  @doc "Stop the agent: close the Port (terminating the child) and release its lease."
  @spec stop(String.t(), term()) :: :ok | {:error, :not_found}
  def stop(agent_id, reason \\ :operator_stop) do
    case whereis(agent_id) do
      nil ->
        {:error, :not_found}

      pid ->
        # The agent can die between whereis/0 and this call (it is
        # restart: :temporary and stops itself on exit; the Registry unregisters
        # only on the async :DOWN). GenServer.stop then exits :noproc and would
        # crash the caller — e.g. on_exit cleanup sweeping a just-exited agent.
        # Stopping something that's already gone IS success, so treat it as :ok.
        try do
          GenServer.stop(pid, {:shutdown, reason})
        catch
          :exit, _ -> :ok
        end
    end
  end

  @doc "List live agent ids."
  @spec list() :: [String.t()]
  def list do
    Registry.select(AgentOrchestrator.Registry, [{{:"$1", :_, :_}, [], [:"$1"]}])
  end

  @doc "Generate a short, collision-resistant ephemeral agent id."
  @spec generate_agent_id() :: String.t()
  def generate_agent_id do
    "ag-" <> (:crypto.strong_rand_bytes(6) |> Base.url_encode64(padding: false))
  end

  # ---------- GenServer ----------

  @impl true
  def init(spec) do
    Process.flag(:trap_exit, true)
    agent_id = Map.fetch!(spec, :agent_id)
    lease_client = Map.get(spec, :lease_client, LeasePlaneClient)
    lease_cfg = normalize_lease_cfg(Map.get(spec, :lease), agent_id)

    # Validate lineage BEFORE the lease acquire: a refused spawn must not have
    # touched the plane (no acquire-then-release churn for a config error).
    case normalize_lineage_cfg(Map.get(spec, :lineage)) do
      {:error, reason} ->
        {:stop, {:invalid_lineage, reason}}

      {:ok, lineage_cfg} ->
        case normalize_server_url(Map.get(spec, :server_url)) do
          {:error, reason} ->
            {:stop, {:invalid_server_url, reason}}

          {:ok, server_url} ->
            case normalize_client_session_id(Map.get(spec, :client_session_id)) do
              {:error, reason} ->
                {:stop, {:invalid_client_session_id, reason}}

              {:ok, client_session_id} ->
                state = %__MODULE__{
                  agent_id: agent_id,
                  lease_client: lease_client,
                  lease_cfg: lease_cfg,
                  lineage: if(lineage_cfg, do: :provisioned, else: :none),
                  max_output_lines: Map.get(spec, :max_output_lines, @default_max_lines)
                }

                init_with_lease(
                  state,
                  spec,
                  candidate_env(lineage_cfg, server_url, client_session_id)
                )
            end
        end
    end
  end

  defp init_with_lease(state, spec, candidates) do
    # Not a `with`: the acquired lease_id must be in scope on the port-open
    # failure path so we can release it. A `with/else` only sees the failing
    # clause's value, so the acquired lease_id would be invisible there and the
    # lease would orphan (and a non-file lease does NOT self-heal — see release
    # discipline in terminate/2).
    case maybe_acquire_lease(state) do
      {:error, :lease_denied, reason} ->
        {:stop, {:lease_denied, reason}}

      {:ok, lease_id, presence} ->
        state = %{state | lease_id: lease_id, presence: presence}

        case open_port(spec, candidates) do
          {:ok, port, os_pid} ->
            Logger.info(
              "agent #{state.agent_id} started os_pid=#{os_pid} lease=#{lease_id || "none"} cmd=#{Map.get(spec, :cmd)}"
            )

            # Arm the max-runtime backstop. No caller is obligated to DELETE a
            # wedged agent (the live dialectic reviewer explicitly "leaves it
            # async" on a 504), so without this an agent that never exits leaks
            # its OS process + subprocess tree until the orchestrator restarts.
            # The timer fires :max_runtime into our own mailbox; we self-reap.
            maybe_arm_max_runtime(spec)

            {:ok, %{state | port: port, os_pid: os_pid}}

          {:error, reason} ->
            # Port failed to open after a lease was taken — release so we don't
            # leak the surface (state now carries lease_id).
            _ = maybe_release_lease(state, @release_reason)
            {:stop, reason}
        end
    end
  end

  @impl true
  def handle_call(:snapshot, _from, state), do: {:reply, {:ok, result(state)}, state}

  def handle_call(:await, from, %{exit_status: nil} = state) do
    {:noreply, %{state | waiters: [from | state.waiters]}}
  end

  def handle_call(:await, _from, state), do: {:reply, {:ok, result(state)}, state}

  @impl true
  # Line-framed stdout/stderr from the child.
  def handle_info({port, {:data, {:eol, line}}}, %{port: port} = state) do
    {:noreply, push_line(state, state.partial <> line)}
  end

  def handle_info({port, {:data, {:noeol, chunk}}}, %{port: port} = state) do
    partial = state.partial <> chunk

    # Bound the partial buffer: a child that emits a line longer than
    # @line_max_bytes with no newline would otherwise grow `partial` without
    # limit. Flush the oversized fragment as a synthetic line instead.
    if byte_size(partial) >= @line_max_bytes do
      {:noreply, push_line(%{state | partial: ""}, partial)}
    else
      {:noreply, %{state | partial: partial}}
    end
  end

  # exit_status is the authoritative terminal signal. Match on any port and
  # not-yet-finalized rather than requiring state.port to still equal the
  # reference — the linked-port {:EXIT} and {:exit_status} can arrive in either
  # order, and if {:EXIT} cleared state.port first, requiring a match here would
  # drop the status, hang waiters, and never release the lease.
  def handle_info({port, {:exit_status, status}}, %{exit_status: nil} = state)
      when is_port(port) do
    finalize(state, status)
  end

  def handle_info({_port, {:exit_status, _status}}, state), do: {:noreply, state}

  # Linked Port EXIT. If exit_status already finalized us, this is just cleanup.
  # If it has NOT (EXIT won the race, or the port died without a status),
  # finalize anyway so waiters get a reply and the lease is released — using the
  # exit reason as the terminal status since no numeric code is available.
  def handle_info({:EXIT, port, reason}, state) when is_port(port) do
    if is_nil(state.exit_status) do
      finalize(%{state | port: nil}, {:port_closed, reason})
    else
      {:noreply, %{state | port: nil}}
    end
  end

  # Max-runtime backstop fired and we are still running. Reap the whole tree
  # (a wedged child won't exit on its own; kill_tree gets its MCP-subproc
  # descendants too), then finalize with a synthetic terminal status so waiters
  # get a definitive result instead of hanging on their own await. We kill +
  # close here rather than leaving it to terminate/2 because finalize sets
  # exit_status (→ terminate's reap guard would skip) and nils the port.
  def handle_info(:max_runtime, %{exit_status: nil} = state) do
    Logger.warning(
      "agent #{state.agent_id} exceeded max runtime — reaping os_pid=#{inspect(state.os_pid)}"
    )

    if is_integer(state.os_pid), do: kill_tree(state.os_pid)
    safe_close(state.port)
    finalize(%{state | port: nil}, {:killed, :max_runtime})
  end

  # Already finalized (the agent exited before its deadline) — the stale timer
  # is a no-op.
  def handle_info(:max_runtime, state), do: {:noreply, state}

  def handle_info(_msg, state), do: {:noreply, state}

  # Terminal finalize: flush any partial line, release the lease, record the
  # exit status, reply to waiters, retain the result, and stop. `status` is an
  # integer for a clean exit or `{:port_closed, reason}` for an abnormal close.
  #
  # The ResultStore write happens-before the {:stop, ...} return (the GenServer
  # only terminates after this callback returns), so a late await/snapshot that
  # observes the runner as dead is guaranteed to find the retained result rather
  # than racing to {:error, :not_found} (#581). Retention is TTL-bounded — see
  # ResultStore — so a fan-out caller that collects results after exit still
  # works, without retaining forever.
  defp finalize(state, status) do
    state = if state.partial != "", do: push_line(%{state | partial: ""}, state.partial), else: state
    Logger.info("agent #{state.agent_id} exited status=#{inspect(status)}")
    release_status = maybe_release_lease(state, @release_reason)
    state = %{state | exit_status: status, port: nil, release_status: release_status}
    state = reply_waiters(state)
    ResultStore.put(state.agent_id, result(state))

    if status == 0 do
      {:stop, :normal, state}
    else
      {:stop, {:shutdown, {:exit_status, status}}, state}
    end
  end

  @impl true
  def terminate(_reason, state) do
    # Reap the whole process TREE when the child is still running at teardown
    # (operator stop / app shutdown — exit_status still nil). `claude`/SDK
    # children spawn their own subprocesses (MCP servers); closing the Port alone
    # orphans those descendants — they reparent to init and pile up on the
    # always-on orchestrator. This MUST run before safe_close: once the parent
    # dies its children reparent and can no longer be found by walking the tree.
    # Guarded on a NOT-yet-finalized state so we never signal a clean exit's
    # os_pid, which by now could belong to an unrelated reused pid (the same
    # guard AgentPort uses via its `exited` flag).
    if is_nil(state.exit_status) and is_integer(state.os_pid), do: kill_tree(state.os_pid)

    # Idempotent with the exit-status path: release returns :not_found harmlessly
    # if already released. Closing the port here kills the child on operator stop.
    if state.port, do: safe_close(state.port)
    # Retry the release UNLESS the finalize path already released cleanly
    # (:ok) or there was no lease (:no_lease). A previous {:error, _} (e.g. the
    # plane was briefly unreachable) must NOT be treated as done — a non-file
    # lease is held by an auto-renewing plane-side holder and will NOT self-heal
    # at TTL, so the release is the only thing that frees it.
    unless state.release_status in [:ok, :no_lease] do
      maybe_release_lease(state, @release_reason)
    end

    :ok
  end

  # ---------- internals ----------

  defp via(agent_id), do: {:via, Registry, {AgentOrchestrator.Registry, agent_id}}

  defp whereis(agent_id) do
    case Registry.lookup(AgentOrchestrator.Registry, agent_id) do
      [{pid, _}] -> pid
      [] -> nil
    end
  end

  defp call(agent_id, msg, timeout \\ 5_000) do
    case whereis(agent_id) do
      nil -> {:error, :not_found}
      pid -> GenServer.call(pid, msg, timeout)
    end
  end

  # Presence is DEFAULT-ON and best-effort: with no `:lease` key, an agent
  # registers an `agent:/<id>` presence row on the plane (migration 042 routes it
  # to the self-healing remote_heartbeat path). `lease: false` opts out entirely.
  # A `:lease` map overrides the defaults (e.g. `required: true` to make it a
  # gating lease, or a different `surface_id`).
  defp normalize_lease_cfg(false, _agent_id), do: nil

  defp normalize_lease_cfg(nil, agent_id), do: normalize_lease_cfg(%{}, agent_id)

  defp normalize_lease_cfg(%{} = cfg, agent_id) do
    %{
      # Best-effort by default: presence should NOT gate spawning. A caller that
      # genuinely needs a gating lease passes `required: true`.
      required: Map.get(cfg, :required, false),
      holder_agent_uuid: Map.get(cfg, :holder_agent_uuid) || uuid4(),
      surface_id: Map.get(cfg, :surface_id) || "agent:/" <> agent_id,
      ttl_s: Map.get(cfg, :ttl_s, Application.get_env(:agent_orchestrator, :default_lease_ttl_s, 300))
    }
  end

  # Returns {:ok, lease_id, presence} | {:error, :lease_denied, reason}.
  # presence is :disabled | :registered | :unregistered — a distinguishable
  # signal so a consumer of plane presence knows that an :unregistered agent is
  # live-but-not-on-the-plane (absence from the plane ≠ not running).
  defp maybe_acquire_lease(%{lease_cfg: nil}), do: {:ok, nil, :disabled}

  defp maybe_acquire_lease(%{agent_id: agent_id, lease_cfg: cfg, lease_client: client}) do
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
            "agent #{agent_id} presence UNREGISTERED (#{inspect(reason)}) — running " <>
              "WITHOUT a presence row; plane-absence does NOT imply not-running"
          )

          {:ok, nil, :unregistered}
        end
    end
  end

  defp maybe_release_lease(%{lease_id: nil}, _reason), do: :no_lease

  defp maybe_release_lease(%{lease_id: lease_id, lease_client: client}, reason) do
    case client.release(lease_id, reason) do
      :ok ->
        :ok

      {:error, r} = err ->
        Logger.warning("agent lease release failed lease=#{lease_id}: #{inspect(r)}")
        err
    end
  end

  defp open_port(spec, candidates) do
    cmd = Map.fetch!(spec, :cmd)

    case resolve_executable(cmd) do
      nil ->
        {:error, {:executable_not_found, cmd}}

      path ->
        # `|| []` not just a default: an explicit `env: nil` (spec built from
        # nullable config) must not crash the merge below — that exception
        # escapes open_port's rescue and orphans the just-acquired lease.
        env = Map.get(spec, :env, []) || []

        opts =
          [
            :binary,
            :exit_status,
            :stderr_to_stdout,
            {:line, @line_max_bytes},
            {:args, Map.get(spec, :args, [])}
          ]
          |> maybe_opt(:env, encode_env(env ++ provisioned_env(candidates, env)))
          |> maybe_opt(:cd, Map.get(spec, :cd))

        try do
          port = Port.open({:spawn_executable, path}, opts)
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

  # Lineage PROVISIONING (see moduledoc): candidate declarations only — the
  # child makes its own onboard call. Validated up front because a malformed
  # parent UUID does not fail here; it fails later as false ancestry in the
  # governance lineage DAG, attributed to whatever agent declared it.
  defp normalize_lineage_cfg(nil), do: {:ok, nil}

  defp normalize_lineage_cfg(%{} = cfg) do
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

  defp normalize_lineage_cfg(other), do: {:error, {:lineage_not_map, other}}

  # Scheme check only — a value with no http(s):// scheme fails at the child
  # as a confusing OFFLINE far from its cause; refuse it at the boundary.
  defp normalize_server_url(nil), do: {:ok, nil}

  defp normalize_server_url(url) when is_binary(url) do
    if String.starts_with?(url, ["http://", "https://"]) do
      {:ok, url}
    else
      {:error, {:server_url_not_http, url}}
    end
  end

  defp normalize_server_url(other), do: {:error, {:server_url_not_string, other}}

  # Non-empty-string check only — the server canonicalizes/sanitizes the session
  # key itself (see resolve_session_identity). An empty/blank anchor is refused
  # here because it would silently fall back to fresh-mint-per-turn, the exact
  # symptom this provisioning prevents (a confusing "still minting" far from the
  # blank-anchor cause).
  defp normalize_client_session_id(nil), do: {:ok, nil}

  defp normalize_client_session_id(v) when is_binary(v) do
    if String.trim(v) == "" do
      {:error, {:client_session_id_blank, v}}
    else
      {:ok, v}
    end
  end

  defp normalize_client_session_id(other), do: {:error, {:client_session_id_not_string, other}}

  # The full provisioned-candidate list for the child env, built from the
  # already-validated configs.
  defp candidate_env(lineage_cfg, server_url, client_session_id) do
    lineage =
      case lineage_cfg do
        nil -> []
        cfg -> [{@lineage_parent_var, cfg.parent_agent_uuid}, {@lineage_reason_var, cfg.spawn_reason}]
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
    explicit = MapSet.new(env, fn {k, _v} -> k end)

    # Explicit env wins: the caller setting the var directly has made the more
    # specific statement; silently overriding it would be the invasive version.
    Enum.reject(candidates, fn {k, _v} -> MapSet.member?(explicit, k) end)
  end

  defp maybe_opt(opts, _key, nil), do: opts
  defp maybe_opt(opts, _key, []), do: opts
  defp maybe_opt(opts, key, val), do: [{key, val} | opts]

  # Arm the self-reap timer unless this spawn opted out. The message lands in our
  # own mailbox after init returns, handled by handle_info(:max_runtime, ...).
  defp maybe_arm_max_runtime(spec) do
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
      {:ok, v} -> v
      :error -> Application.get_env(:agent_orchestrator, :default_max_runtime_ms, @default_max_runtime_ms)
    end
  end

  defp encode_env([]), do: []

  defp encode_env(env) do
    Enum.map(env, fn {k, v} -> {String.to_charlist(k), String.to_charlist(v)} end)
  end

  defp push_line(state, line) do
    # Bounded ring: keep newest max_output_lines. Stored newest-first for O(1)
    # prepend; reversed on read in result/1.
    output = [line | state.output]

    {output, count} =
      if state.output_count + 1 > state.max_output_lines do
        {Enum.take(output, state.max_output_lines), state.max_output_lines}
      else
        {output, state.output_count + 1}
      end

    %{state | output: output, output_count: count, partial: ""}
  end

  defp reply_waiters(state) do
    result = result(state)
    Enum.each(state.waiters, fn from -> GenServer.reply(from, {:ok, result}) end)
    %{state | waiters: []}
  end

  defp result(state) do
    %{
      agent_id: state.agent_id,
      os_pid: state.os_pid,
      lease_id: state.lease_id,
      presence: state.presence,
      lineage: state.lineage,
      exit_status: state.exit_status,
      running: state.exit_status == nil,
      lease_released: state.release_status == :ok,
      output: Enum.reverse(state.output)
    }
  end

  defp safe_close(port) do
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
  defp kill_tree(pid) do
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
  defp uuid4 do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    c = c &&& 0x0FFF ||| 0x4000
    d = d &&& 0x3FFF ||| 0x8000

    :io_lib.format("~8.16.0b-~4.16.0b-~4.16.0b-~4.16.0b-~12.16.0b", [a, b, c, d, e])
    |> IO.iodata_to_binary()
  end
end
