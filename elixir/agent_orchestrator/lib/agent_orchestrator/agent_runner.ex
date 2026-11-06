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
    2. The Port is opened with `:exit_status`, `:eof`, and line framing;
       stdout+stderr are captured into a bounded buffer.
    3. Only after BOTH `{:exit_status, status}` and `{port, :eof}` arrive is the
       lease released and the runner stopped `:normal` (status 0) or
       `{:shutdown, {:exit_status, n}}` (non-zero). Erlang does not specify the
       order of those Port messages, so the EOF is the drain barrier that keeps
       late output from being discarded.
    4. `terminate/2` releases the lease on any shutdown path (kill, app stop), so
       the lease is freed promptly rather than waiting out its TTL — the TTL is
       the backstop for a crash that skips `terminate/2`, not the normal path.

  ## Spec

      %{
        agent_id: String.t() | nil,        # logical metadata; defaults to execution_id
        execution_id: String.t(),          # server-generated; caller values are ignored
        cmd: String.t(),                   # executable name or absolute path (required)
        args: [String.t()],                # argv (default [])
        env: [{String.t(), String.t() | false}], # extra env for the child (default []);
                                           # false unsets an inherited variable
        cd: String.t() | nil,              # working dir
        max_output_lines: pos_integer(),   # buffer cap (default 1000)
        max_runtime_ms: pos_integer(),     # wall-clock lifetime cap; self-reaps on expiry
                                           # (default :default_max_runtime_ms; nil/<=0 disables)
        lease: nil | false | lease_cfg,    # default (absent/nil) = best-effort agent:/ presence;
                                           # false = no presence; map = override
        lease_client: module(),            # injectable, default LeasePlaneClient
        lineage: nil | lineage_cfg,        # default nil = no lineage provisioning
        server_url: String.t() | nil,      # default nil = no server-URL provisioning
        client_session_id: String.t() | nil, # default nil = no session-anchor provisioning
        redact: [String.t()],              # values replaced in captured output (default [])
        secret_env: (-> [{String.t(), String.t()}]), # secret variables for the child,
                                           # behind a closure so no report prints them
        profile: String.t() | nil          # launch profile this execution belongs to
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

  require Logger

  alias AgentOrchestrator.AgentRunner.LeaseGovernance
  alias AgentOrchestrator.AgentRunner.OutputBuffer
  alias AgentOrchestrator.LeasePlaneClient
  alias AgentOrchestrator.ResultStore
  alias AgentOrchestrator.Telemetry

  @default_max_lines 1000
  @line_max_bytes 65_536

  @doc false
  def line_max_bytes, do: @line_max_bytes

  defstruct [
    :execution_id,
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
    :pending_exit_status,
    :release_status,
    # Monotonic timestamp of a successful spawn, for the telemetry stop span.
    # Monotonic (not system) time so a clock adjustment cannot produce a
    # negative or wildly wrong duration.
    :started_mono,
    # Wall-clock timestamp for operator-facing execution inventory.
    :started_at,
    :cmd,
    # The launch profile this execution was started from, if any; the
    # profiles server recounts its limits from live executions.
    :profile,
    output: [],
    output_count: 0,
    max_output_lines: @default_max_lines,
    # Secret values a profile spawn resolved into the child env; each is
    # replaced in captured output before it is stored or returned. Held behind
    # a closure: crash reports and `:sys` dumps print a fun, never its values.
    redact: nil,
    partial: "",
    eof_received: false,
    waiters: []
  ]

  # Orchestrator-initiated lease release reason. The plane validates
  # release_reason against an allowlist (normal | down_local | reaped_* |
  # handoff); "normal" is the orderly-release member.
  @release_reason "normal"

  # ---------- public API ----------

  def start_link(%{} = spec) do
    execution_id = Map.fetch!(spec, :execution_id)
    GenServer.start_link(__MODULE__, spec, name: via(execution_id))
  end

  @doc "Block until an execution exits (or `timeout` ms)."
  @spec await(String.t(), timeout()) :: {:ok, map()} | {:error, :timeout | :not_found}
  def await(execution_id, timeout \\ 30_000) do
    case call(execution_id, :await, timeout) do
      # whereis/0 already saw the runner gone — fall back to the retained result.
      {:error, :not_found} -> retained_or_not_found(execution_id)
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
      retained_or_not_found(execution_id)
  end

  @doc "Current captured output and status without blocking."
  @spec snapshot(String.t()) :: {:ok, map()} | {:error, :not_found}
  def snapshot(execution_id) do
    case call(execution_id, :snapshot) do
      {:error, :not_found} -> retained_or_not_found(execution_id)
      reply -> reply
    end
  catch
    # Same dead-mid-call race as await/2: a live whereis/0 followed by the
    # process exiting before the snapshot lands. Fall back to the retained
    # terminal result instead of crashing the caller with the GenServer exit.
    :exit, _ ->
      retained_or_not_found(execution_id)
  end

  @doc "Current execution metadata without captured output."
  @spec describe(String.t()) :: {:ok, map()} | {:error, :not_found}
  def describe(execution_id) do
    call(execution_id, :describe)
  catch
    :exit, _ -> {:error, :not_found}
  end

  # On a dead runner, the terminal result may have been retained by finalize/2.
  defp retained_or_not_found(execution_id) do
    case ResultStore.fetch(execution_id) do
      {:ok, result} -> {:ok, result}
      :error -> {:error, :not_found}
    end
  end

  @doc "Stop one execution: close its Port and release its lease."
  @spec stop(String.t(), term()) :: :ok | {:error, :not_found}
  def stop(execution_id, reason \\ :operator_stop) do
    case whereis(execution_id) do
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

  @doc "List live execution ids."
  @spec list() :: [String.t()]
  def list do
    Registry.select(AgentOrchestrator.Registry, [{{:"$1", :_, :_}, [], [:"$1"]}])
  end

  @doc "List metadata for live executions without copying captured output."
  @spec list_details() :: [map()]
  def list_details do
    list()
    |> Enum.flat_map(fn execution_id ->
      case describe(execution_id) do
        {:ok, details} -> [details]
        {:error, :not_found} -> []
      end
    end)
    |> Enum.sort_by(& &1.execution_id)
  end

  @doc "Generate an immutable UUID-backed execution id."
  @spec generate_execution_id() :: String.t()
  def generate_execution_id, do: "ex-" <> LeaseGovernance.uuid4()

  # ---------- GenServer ----------

  @impl true
  def init(spec) do
    Process.flag(:trap_exit, true)
    execution_id = Map.fetch!(spec, :execution_id)
    agent_id = Map.fetch!(spec, :agent_id)
    lease_client = Map.get(spec, :lease_client, LeasePlaneClient)
    lease_cfg = LeaseGovernance.normalize_lease_cfg(Map.get(spec, :lease), execution_id)

    # Validate lineage BEFORE the lease acquire: a refused spawn must not have
    # touched the plane (no acquire-then-release churn for a config error).
    case LeaseGovernance.normalize_lineage_cfg(Map.get(spec, :lineage)) do
      {:error, reason} ->
        {:stop, {:invalid_lineage, reason}}

      {:ok, lineage_cfg} ->
        case LeaseGovernance.normalize_server_url(Map.get(spec, :server_url)) do
          {:error, reason} ->
            {:stop, {:invalid_server_url, reason}}

          {:ok, server_url} ->
            case LeaseGovernance.normalize_client_session_id(Map.get(spec, :client_session_id)) do
              {:error, reason} ->
                {:stop, {:invalid_client_session_id, reason}}

              {:ok, client_session_id} ->
                state = %__MODULE__{
                  execution_id: execution_id,
                  agent_id: agent_id,
                  lease_client: lease_client,
                  lease_cfg: lease_cfg,
                  lineage: if(lineage_cfg, do: :provisioned, else: :none),
                  max_output_lines: Map.get(spec, :max_output_lines, @default_max_lines),
                  redact: OutputBuffer.redact_closure(spec),
                  profile: Map.get(spec, :profile)
                }

                init_with_lease(
                  state,
                  spec,
                  LeaseGovernance.candidate_env(lineage_cfg, server_url, client_session_id)
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
    case LeaseGovernance.maybe_acquire_lease(state) do
      {:error, :lease_denied, reason} ->
        {:stop, {:lease_denied, reason}}

      {:ok, lease_id, presence} ->
        state = %{state | lease_id: lease_id, presence: presence}

        case LeaseGovernance.open_port(spec, candidates) do
          {:ok, port, os_pid} ->
            cmd = Map.get(spec, :cmd)

            Logger.info(
              "execution #{state.execution_id} agent=#{state.agent_id} started " <>
                "os_pid=#{os_pid} lease=#{lease_id || "none"} cmd=#{cmd}"
            )

            Telemetry.agent_start(%{
              execution_id: state.execution_id,
              agent_id: state.agent_id,
              cmd: cmd,
              os_pid: os_pid,
              lease_id: lease_id,
              presence: presence,
              lineage: state.lineage
            })

            # Arm the max-runtime backstop. No caller is obligated to DELETE a
            # wedged agent (the live dialectic reviewer explicitly "leaves it
            # async" on a 504), so without this an agent that never exits leaks
            # its OS process + subprocess tree until the orchestrator restarts.
            # The timer fires :max_runtime into our own mailbox; we self-reap.
            LeaseGovernance.maybe_arm_max_runtime(spec)

            {:ok,
             %{
               state
               | port: port,
                 os_pid: os_pid,
                 cmd: cmd,
                 started_mono: System.monotonic_time(),
                 started_at: DateTime.utc_now() |> DateTime.to_iso8601()
             }}

          {:error, reason} ->
            # Port failed to open after a lease was taken — release so we don't
            # leak the surface (state now carries lease_id).
            _ = LeaseGovernance.maybe_release_lease(state, @release_reason)
            {:stop, reason}
        end
    end
  end

  @impl true
  def handle_call(:snapshot, _from, state),
    do: {:reply, {:ok, OutputBuffer.result(state)}, state}

  def handle_call(:describe, _from, state),
    do: {:reply, {:ok, OutputBuffer.execution_summary(state)}, state}

  def handle_call(:await, from, %{exit_status: nil} = state) do
    {:noreply, %{state | waiters: [from | state.waiters]}}
  end

  def handle_call(:await, _from, state),
    do: {:reply, {:ok, OutputBuffer.result(state)}, state}

  @impl true
  # Line-framed stdout/stderr from the child.
  def handle_info({port, {:data, {:eol, line}}}, %{port: port} = state) do
    {:noreply, OutputBuffer.push_line(state, state.partial <> line)}
  end

  def handle_info({port, {:data, {:noeol, chunk}}}, %{port: port} = state) do
    partial = state.partial <> chunk

    # Bound the partial buffer: a child that emits a line longer than
    # @line_max_bytes with no newline would otherwise grow `partial` without
    # limit. Flush the oversized fragment as a synthetic line instead.
    if byte_size(partial) >= @line_max_bytes do
      {:noreply, OutputBuffer.flush_oversized(state, partial)}
    else
      {:noreply, %{state | partial: partial}}
    end
  end

  # `:exit_status` and `:eof` are independent Port messages whose ordering is
  # unspecified. Keep the numeric status pending until EOF proves stdout/stderr
  # has drained; finalizing on status alone can discard the assistant's last
  # line even after a clean process exit.
  def handle_info({port, {:exit_status, status}}, %{exit_status: nil} = state)
      when is_port(port) do
    maybe_finalize_after_drain(%{state | pending_exit_status: status})
  end

  def handle_info({_port, {:exit_status, _status}}, state), do: {:noreply, state}

  def handle_info({port, :eof}, %{port: port, exit_status: nil} = state) do
    maybe_finalize_after_drain(%{state | eof_received: true})
  end

  def handle_info({_port, :eof}, state), do: {:noreply, state}

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
      "execution #{state.execution_id} agent=#{state.agent_id} exceeded max runtime — " <>
        "reaping os_pid=#{inspect(state.os_pid)}"
    )

    if is_integer(state.os_pid), do: LeaseGovernance.kill_tree(state.os_pid)
    LeaseGovernance.safe_close(state.port)
    finalize(%{state | port: nil}, {:killed, :max_runtime})
  end

  # Already finalized (the agent exited before its deadline) — the stale timer
  # is a no-op.
  def handle_info(:max_runtime, state), do: {:noreply, state}

  def handle_info(_msg, state), do: {:noreply, state}

  defp maybe_finalize_after_drain(%{eof_received: true, pending_exit_status: status} = state)
       when not is_nil(status) do
    # With the :eof Port option, EOF deliberately leaves the port open. Close
    # it only after the numeric status is also known, then publish the complete
    # output as one terminal result.
    LeaseGovernance.safe_close(state.port)
    finalize(%{state | port: nil}, status)
  end

  defp maybe_finalize_after_drain(state), do: {:noreply, state}

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
    state =
      if state.partial != "",
        do: OutputBuffer.push_line(%{state | partial: ""}, state.partial),
        else: state

    Logger.info(
      "execution #{state.execution_id} agent=#{state.agent_id} exited status=#{inspect(status)}"
    )

    emit_stop(state, status, stop_reason(status))
    release_status = LeaseGovernance.maybe_release_lease(state, @release_reason)
    state = %{state | exit_status: status, port: nil, release_status: release_status}
    state = reply_waiters(state)
    ResultStore.put(state.execution_id, OutputBuffer.result(state))

    if status == 0 do
      {:stop, :normal, state}
    else
      {:stop, {:shutdown, {:exit_status, status}}, state}
    end
  end

  defp stop_reason({:killed, :max_runtime}), do: :max_runtime
  defp stop_reason(_), do: :exited

  # One emit point for both endings. `started_mono` is nil when the port never
  # opened, in which case there is no span to report.
  defp emit_stop(%{started_mono: nil}, _status, _reason), do: :ok

  defp emit_stop(state, status, reason) do
    Telemetry.agent_stop(
      %{
        duration: System.monotonic_time() - state.started_mono,
        output_lines: state.output_count
      },
      %{
        execution_id: state.execution_id,
        agent_id: state.agent_id,
        cmd: state.cmd,
        os_pid: state.os_pid,
        exit_status: status,
        reason: reason,
        lease_id: state.lease_id
      }
    )
  end

  # The reap path's counterpart to finalize/2's `exited` line, keeping the
  # `exited` token so log arithmetic balances while status=nil + the reason
  # mark it as a reap rather than a natural exit. Guarded like emit_stop: when
  # the port never opened there was no `started` line, so logging an ending
  # would skew the count the other way.
  defp log_reaped(%{started_mono: nil}), do: :ok

  defp log_reaped(state) do
    Logger.info(
      "execution #{state.execution_id} agent=#{state.agent_id} exited " <>
        "status=nil reason=stopped (reaped while running)"
    )
  end

  @impl true
  def terminate(_reason, state) do
    # An agent reaped while still running (operator DELETE, app shutdown) never
    # reaches finalize/2, so before this it produced a `started` line and
    # nothing else — the one ending with no record at all. Emit its span AND
    # its `exited` log line here, guarded on not-yet-finalized so a clean exit
    # is never double-counted: without the log line, `grep -c started` vs
    # `grep -c exited` arithmetic on the raw log diverged on every reap and
    # read as a process leak when nothing leaked.
    if is_nil(state.exit_status) do
      log_reaped(state)
      emit_stop(state, nil, :stopped)
    end

    # Reap the whole process TREE when the child is still running at teardown
    # (operator stop / app shutdown — exit_status still nil). `claude`/SDK
    # children spawn their own subprocesses (MCP servers); closing the Port alone
    # orphans those descendants — they reparent to init and pile up on the
    # always-on orchestrator. This MUST run before safe_close: once the parent
    # dies its children reparent and can no longer be found by walking the tree.
    # Guarded on a NOT-yet-finalized state so we never signal a clean exit's
    # os_pid, which by now could belong to an unrelated reused pid (the same
    # guard AgentPort uses via its `exited` flag).
    if is_nil(state.exit_status) and is_integer(state.os_pid),
      do: LeaseGovernance.kill_tree(state.os_pid)

    # Idempotent with the exit-status path: release returns :not_found harmlessly
    # if already released. Closing the port here kills the child on operator stop.
    if state.port, do: LeaseGovernance.safe_close(state.port)
    # Retry the release UNLESS the finalize path already released cleanly
    # (:ok) or there was no lease (:no_lease). A previous {:error, _} (e.g. the
    # plane was briefly unreachable) must NOT be treated as done — a non-file
    # lease is held by an auto-renewing plane-side holder and will NOT self-heal
    # at TTL, so the release is the only thing that frees it.
    unless state.release_status in [:ok, :no_lease] do
      LeaseGovernance.maybe_release_lease(state, @release_reason)
    end

    :ok
  end

  # ---------- internals ----------

  defp via(execution_id), do: {:via, Registry, {AgentOrchestrator.Registry, execution_id}}

  defp whereis(execution_id) do
    case Registry.lookup(AgentOrchestrator.Registry, execution_id) do
      [{pid, _}] -> pid
      [] -> nil
    end
  end

  defp call(execution_id, msg, timeout \\ 5_000) do
    case whereis(execution_id) do
      nil -> {:error, :not_found}
      pid -> GenServer.call(pid, msg, timeout)
    end
  end

  defp reply_waiters(state) do
    result = OutputBuffer.result(state)
    Enum.each(state.waiters, fn from -> GenServer.reply(from, {:ok, result}) end)
    %{state | waiters: []}
  end
end
