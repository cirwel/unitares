defmodule AgentOrchestrator.Telemetry do
  @moduledoc """
  `:telemetry` event contract for the agent lifecycle, and a runtime-attachable
  logger for it.

  The point of emitting events rather than only logging is that handlers attach
  and detach **at runtime**. From a remote shell on the live node you can turn
  detailed per-agent reporting on, watch a suspect spawn, and turn it back off,
  without a redeploy or a restart. See `scripts/start.sh` for the named-node
  setup that makes that shell possible.

  ## Events

  `[:agent_orchestrator, :agent, :start]`

    * measurements — `:system_time`
    * metadata — `:execution_id`, `:agent_id`, `:cmd`, `:os_pid`, `:lease_id`,
      `:presence`, `:lineage`

  `[:agent_orchestrator, :agent, :stop]`

    * measurements — `:duration` (native units), `:output_lines`
    * metadata — `:execution_id`, `:agent_id`, `:cmd`, `:os_pid`,
      `:exit_status`, `:reason`, `:lease_id`

  `:reason` distinguishes the three ways an agent ends, which the log alone
  could not:

    * `:exited` — the child exited on its own, cleanly or not
    * `:max_runtime` — the backstop killed it
    * `:stopped` — an operator `DELETE`, or app shutdown, reaped it while it was
      still running

  `:stopped` is the case worth having. Until now a reaped agent logged its
  `started` line and nothing else, so the log's started/exited counts diverged
  and read as a leak when nothing had leaked.

  ## Attaching at runtime

      iex> AgentOrchestrator.Telemetry.attach_logger()
      :ok
      iex> AgentOrchestrator.Telemetry.detach_logger()
      :ok

  `attach_logger/1` takes a log level (default `:info`).
  """

  require Logger

  @handler_id "agent-orchestrator-telemetry-logger"

  @start [:agent_orchestrator, :agent, :start]
  @stop [:agent_orchestrator, :agent, :stop]

  @doc "Every event this application emits. Useful for wiring a metrics reporter."
  @spec events() :: [[atom()]]
  def events, do: [@start, @stop]

  @doc false
  @spec agent_start(map()) :: :ok
  def agent_start(metadata) do
    :telemetry.execute(@start, %{system_time: System.system_time()}, metadata)
  end

  @doc false
  @spec agent_stop(map(), map()) :: :ok
  def agent_stop(measurements, metadata) do
    :telemetry.execute(@stop, measurements, metadata)
  end

  @doc """
  Attach a logger for every lifecycle event. Idempotent: re-attaching replaces
  the existing handler rather than erroring, so it is safe to call from a remote
  shell without checking first.
  """
  @spec attach_logger(Logger.level()) :: :ok
  def attach_logger(level \\ :info) do
    _ = detach_logger()
    :telemetry.attach_many(@handler_id, events(), &__MODULE__.handle_event/4, level)
  end

  @doc "Detach the runtime logger. Safe to call when nothing is attached."
  @spec detach_logger() :: :ok
  def detach_logger do
    case :telemetry.detach(@handler_id) do
      :ok -> :ok
      {:error, :not_found} -> :ok
    end
  end

  @doc false
  def handle_event(@start, _measurements, meta, level) do
    Logger.log(level, fn ->
      "telemetry execution.start #{meta.execution_id} agent=#{meta.agent_id} " <>
        "cmd=#{meta.cmd} os_pid=#{inspect(meta.os_pid)}"
    end)
  end

  def handle_event(@stop, measurements, meta, level) do
    ms = System.convert_time_unit(measurements.duration, :native, :millisecond)

    Logger.log(level, fn ->
      "telemetry execution.stop #{meta.execution_id} agent=#{meta.agent_id} reason=#{meta.reason} " <>
        "status=#{inspect(meta.exit_status)} lines=#{measurements.output_lines} duration_ms=#{ms}"
    end)
  end
end
