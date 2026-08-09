defmodule AgentOrchestrator.Metrics do
  @moduledoc """
  In-memory aggregate of the lifecycle telemetry, served at `GET /v1/metrics`.

  `AgentOrchestrator.Telemetry` makes each agent's ending *observable*; this
  makes the history *queryable* without grepping a log. It exists because the
  log could not answer "how many agents were reaped rather than exiting" at all
  — a reaped agent left no line — and could not answer "how long do agents
  actually run" without arithmetic on timestamps.

  Counters are process-local and reset when the orchestrator restarts. That is
  deliberate: this is a liveness/see-what-is-happening surface, not a durable
  metrics store. Anything that needs history should scrape it or subscribe to
  the telemetry events directly.

  ## Sizing

  This is a plain GenServer that handles one cast per lifecycle event, which
  would be the wrong shape for a hot path. It is the right shape here: the
  orchestrator's real spawn volume is single-digit per hour (74 spawns over the
  service's entire lifetime as of 2026-08-09). If that ever changes, move the
  counters to `:ets.update_counter/3` — the read API can stay identical.
  """

  use GenServer

  alias AgentOrchestrator.Telemetry

  @handler_id "agent-orchestrator-metrics"

  @empty %{
    started: 0,
    stopped: %{},
    exits: %{},
    duration_ms: %{count: 0, sum: 0, max: 0},
    output_lines: 0,
    last_start_at: nil,
    last_stop_at: nil
  }

  # --- API --------------------------------------------------------------

  def start_link(opts), do: GenServer.start_link(__MODULE__, :ok, Keyword.put_new(opts, :name, __MODULE__))

  @doc """
  Current aggregate. `running` is read from the supervisor rather than derived
  from the counters, so it stays truthful even if an event were ever missed.
  """
  @spec snapshot() :: map()
  def snapshot do
    __MODULE__
    |> GenServer.call(:snapshot)
    |> Map.put(:running, length(AgentOrchestrator.list()))
  end

  @doc false
  def reset, do: GenServer.call(__MODULE__, :reset)

  @doc false
  def handle_event([:agent_orchestrator, :agent, :start], _measurements, meta, _cfg) do
    GenServer.cast(__MODULE__, {:start, meta})
  end

  def handle_event([:agent_orchestrator, :agent, :stop], measurements, meta, _cfg) do
    GenServer.cast(__MODULE__, {:stop, measurements, meta})
  end

  # --- Server -----------------------------------------------------------

  @impl true
  def init(:ok) do
    :ok =
      :telemetry.attach_many(
        @handler_id,
        Telemetry.events(),
        &__MODULE__.handle_event/4,
        nil
      )

    {:ok, @empty}
  end

  @impl true
  def terminate(_reason, _state), do: :telemetry.detach(@handler_id)

  @impl true
  def handle_call(:snapshot, _from, state), do: {:reply, state, state}
  def handle_call(:reset, _from, _state), do: {:reply, :ok, @empty}

  @impl true
  def handle_cast({:start, _meta}, state) do
    {:noreply, %{state | started: state.started + 1, last_start_at: now_iso()}}
  end

  def handle_cast({:stop, measurements, meta}, state) do
    ms = System.convert_time_unit(measurements.duration, :native, :millisecond)
    d = state.duration_ms

    {:noreply,
     %{
       state
       | stopped: bump(state.stopped, meta.reason),
         exits: bump(state.exits, exit_class(meta.exit_status)),
         duration_ms: %{count: d.count + 1, sum: d.sum + ms, max: max(d.max, ms)},
         output_lines: state.output_lines + measurements.output_lines,
         last_stop_at: now_iso()
     }}
  end

  # An exit status is 0, another integer, or a stringified abnormal tuple. Keep
  # the classes coarse — the per-agent detail lives in the telemetry event, and
  # a counter keyed on raw status would grow unbounded.
  defp exit_class(0), do: :ok
  defp exit_class(n) when is_integer(n), do: :nonzero
  defp exit_class(nil), do: :none
  defp exit_class(_), do: :abnormal

  defp bump(map, key), do: Map.update(map, key, 1, &(&1 + 1))

  defp now_iso, do: DateTime.utc_now() |> DateTime.to_iso8601()
end
