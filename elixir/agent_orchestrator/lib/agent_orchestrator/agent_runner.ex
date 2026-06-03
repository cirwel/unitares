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
        lease: nil | lease_cfg,            # nil = no lease; map = lease-bound
        lease_client: module()             # injectable, default LeasePlaneClient
      }

      lease_cfg :: %{
        required: boolean(),               # default true — refuse to start if acquire fails
        holder_agent_uuid: String.t(),     # the agent's governance UUID (generated if absent)
        ttl_s: pos_integer()               # default from config :default_lease_ttl_s
      }
  """

  use GenServer

  import Bitwise

  require Logger

  alias AgentOrchestrator.LeasePlaneClient

  @default_max_lines 1000
  @line_max_bytes 65_536

  defstruct [
    :agent_id,
    :port,
    :os_pid,
    :lease_id,
    :lease_client,
    :lease_cfg,
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
    call(agent_id, :await, timeout)
  catch
    :exit, {:timeout, _} ->
      {:error, :timeout}

    # The agent can exit between whereis/0 and the call landing in its mailbox
    # (it stops itself on exit). GenServer.call then exits :noproc/:normal —
    # catch it instead of crashing the caller. The final result is lost on this
    # narrow race; snapshot/1 during the run or await earlier to avoid it.
    :exit, _ ->
      {:error, :not_found}
  end

  @doc "Current captured output and status without blocking."
  @spec snapshot(String.t()) :: {:ok, map()} | {:error, :not_found}
  def snapshot(agent_id), do: call(agent_id, :snapshot)

  @doc "Stop the agent: close the Port (terminating the child) and release its lease."
  @spec stop(String.t(), term()) :: :ok | {:error, :not_found}
  def stop(agent_id, reason \\ :operator_stop) do
    case whereis(agent_id) do
      nil -> {:error, :not_found}
      pid -> GenServer.stop(pid, {:shutdown, reason})
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

    state = %__MODULE__{
      agent_id: agent_id,
      lease_client: lease_client,
      lease_cfg: lease_cfg,
      max_output_lines: Map.get(spec, :max_output_lines, @default_max_lines)
    }

    # Not a `with`: the acquired lease_id must be in scope on the port-open
    # failure path so we can release it. A `with/else` only sees the failing
    # clause's value, so the acquired lease_id would be invisible there and the
    # lease would orphan (and a non-file lease does NOT self-heal — see release
    # discipline in terminate/2).
    case maybe_acquire_lease(state) do
      {:error, :lease_denied, reason} ->
        {:stop, {:lease_denied, reason}}

      {:ok, lease_id} ->
        state = %{state | lease_id: lease_id}

        case open_port(spec) do
          {:ok, port, os_pid} ->
            Logger.info(
              "agent #{agent_id} started os_pid=#{os_pid} lease=#{lease_id || "none"} cmd=#{Map.get(spec, :cmd)}"
            )

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

  def handle_info(_msg, state), do: {:noreply, state}

  # Terminal finalize: flush any partial line, release the lease, record the
  # exit status, reply to waiters, and stop. `status` is an integer for a clean
  # exit or `{:port_closed, reason}` for an abnormal close.
  defp finalize(state, status) do
    state = if state.partial != "", do: push_line(%{state | partial: ""}, state.partial), else: state
    Logger.info("agent #{state.agent_id} exited status=#{inspect(status)}")
    release_status = maybe_release_lease(state, @release_reason)
    state = %{state | exit_status: status, port: nil, release_status: release_status}
    state = reply_waiters(state)

    if status == 0 do
      {:stop, :normal, state}
    else
      {:stop, {:shutdown, {:exit_status, status}}, state}
    end
  end

  @impl true
  def terminate(_reason, state) do
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

  defp normalize_lease_cfg(nil, _agent_id), do: nil

  defp normalize_lease_cfg(%{} = cfg, agent_id) do
    # Default surface is `agent:<id>`. NOTE: the lease plane's canonical scheme
    # list (`file dialectic resident capture td`) does not yet include `agent`,
    # so an `agent:` surface is rejected with invalid_scheme today. Adding the
    # scheme touches Canonicalize in BOTH Elixir and Python (single-writer
    # cross-repo coordination surface) — an operator/council follow-up. Callers
    # can override `:surface_id` with a currently-valid scheme in the meantime.
    %{
      required: Map.get(cfg, :required, true),
      holder_agent_uuid: Map.get(cfg, :holder_agent_uuid) || uuid4(),
      surface_id: Map.get(cfg, :surface_id) || "agent:" <> agent_id,
      ttl_s: Map.get(cfg, :ttl_s, Application.get_env(:agent_orchestrator, :default_lease_ttl_s, 300))
    }
  end

  defp maybe_acquire_lease(%{lease_cfg: nil}), do: {:ok, nil}

  defp maybe_acquire_lease(%{lease_cfg: cfg, lease_client: client}) do
    case client.acquire(cfg.surface_id, cfg.holder_agent_uuid, "remote_heartbeat", cfg.ttl_s) do
      {:ok, lease_id} ->
        {:ok, lease_id}

      {:error, reason} ->
        if cfg.required do
          {:error, :lease_denied, reason}
        else
          Logger.warning("agent lease acquire failed (best-effort, proceeding): #{inspect(reason)}")
          {:ok, nil}
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

  defp open_port(spec) do
    cmd = Map.fetch!(spec, :cmd)

    case resolve_executable(cmd) do
      nil ->
        {:error, {:executable_not_found, cmd}}

      path ->
        opts =
          [
            :binary,
            :exit_status,
            :stderr_to_stdout,
            {:line, @line_max_bytes},
            {:args, Map.get(spec, :args, [])}
          ]
          |> maybe_opt(:env, encode_env(Map.get(spec, :env, [])))
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

  defp maybe_opt(opts, _key, nil), do: opts
  defp maybe_opt(opts, _key, []), do: opts
  defp maybe_opt(opts, key, val), do: [{key, val} | opts]

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
