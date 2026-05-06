defmodule UnitaresSentinel.FleetFindingEmitter do
  @moduledoc """
  Runtime `sentinel_finding` emitter for BEAM Sentinel fleet analysis.

  This process is intentionally opt-in. The Wave 1 RFC forbids shadow-mode
  duplicate `sentinel_finding` emission, so production cutover must stop the
  Python Sentinel before enabling this GenServer.

  It is not the full governance check-in loop yet. It analyzes the BEAM
  `FleetState`, skips self-observations, and emits fleet findings to
  `/api/findings` using the Python-compatible `Findings.post_finding/2`
  contract.
  """

  use GenServer

  require Logger

  alias UnitaresSentinel.{Findings, FleetAnalysis, FleetState, LeaseAdvisory}

  @default_interval_ms 300_000
  @default_initial_delay_ms 5_000
  @default_jitter_ms 5_000
  @default_tick_timeout_ms 45_000
  @default_agent_id "sentinel"
  @default_agent_name "Sentinel"

  @type tick_result :: %{
          fleet_findings: [map()],
          self_findings: [map()],
          posted_count: non_neg_integer()
        }

  @doc false
  def child_spec(opts) do
    opts = Keyword.put_new(opts, :name, __MODULE__)

    %{
      id: Keyword.get(opts, :name),
      start: {__MODULE__, :start_link, [opts]}
    }
  end

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc """
  Run one fleet-finding emission pass.

  Options can inject a prebuilt `:snapshot`, `:snapshot_fun`, or
  `:analysis_fun` for deterministic tests. Runtime callers normally pass only
  `:fleet_state`, `:findings_opts`, and identity fields.
  """
  @spec tick(keyword()) :: tick_result()
  def tick(opts \\ []) do
    self_agent_id = self_agent_id(opts)
    snapshot = snapshot(opts)

    findings =
      opts
      |> Keyword.get(:analysis_fun, &FleetAnalysis.analyze/2)
      |> then(& &1.(snapshot, self_agent_id: self_agent_id))

    {self_findings, fleet_findings} =
      Enum.split_with(findings, &Map.get(&1, :self_observation, false))

    posted_count =
      if Keyword.get(opts, :emit_findings, true) do
        emit_fleet_findings(fleet_findings, self_agent_id, opts)
      else
        0
      end

    %{fleet_findings: fleet_findings, self_findings: self_findings, posted_count: posted_count}
  end

  @impl true
  def init(opts) do
    interval_ms =
      Keyword.get(
        opts,
        :interval_ms,
        Application.get_env(:unitares_sentinel, :analysis_interval_ms, @default_interval_ms)
      )

    initial_delay_ms =
      Keyword.get(
        opts,
        :initial_delay_ms,
        Application.get_env(
          :unitares_sentinel,
          :analysis_initial_delay_ms,
          @default_initial_delay_ms
        )
      )

    jitter_ms =
      Keyword.get(
        opts,
        :jitter_ms,
        Application.get_env(:unitares_sentinel, :analysis_jitter_ms, @default_jitter_ms)
      )

    state = %{
      opts:
        opts
        |> Keyword.put_new(:fleet_state, FleetState)
        |> Keyword.put_new(
          :emit_findings,
          Application.get_env(:unitares_sentinel, :emit_findings, true)
        ),
      interval_ms: interval_ms,
      jitter_ms: jitter_ms,
      tick_timeout_ms:
        Keyword.get(
          opts,
          :tick_timeout_ms,
          Application.get_env(
            :unitares_sentinel,
            :analysis_tick_timeout_ms,
            @default_tick_timeout_ms
          )
        ),
      lease_advisory?:
        Keyword.get(
          opts,
          :lease_advisory,
          Application.get_env(:unitares_sentinel, :lease_advisory_enabled, true)
        ),
      lease_opts: Keyword.get(opts, :lease_opts, []),
      running?: false,
      last_result: nil
    }

    Process.send_after(self(), :tick, initial_delay_ms + sample_jitter(jitter_ms))
    {:ok, state}
  end

  @impl true
  def handle_info(:tick, %{running?: true} = state) do
    Logger.warning("FleetFindingEmitter: skipping :tick - previous tick still in flight")
    {:noreply, state}
  end

  @impl true
  def handle_info(:tick, state) do
    state = %{state | running?: true}
    lease = acquire_runtime_lease(state)

    try do
      case await_runtime_tick(state) do
        {:ok, result} ->
          schedule_next_tick(state)
          {:noreply, %{state | running?: false, last_result: result}}

        :timeout ->
          Logger.warning(
            "FleetFindingEmitter: runtime tick exceeded #{state.tick_timeout_ms}ms - skipping"
          )

          schedule_next_tick(state)
          {:noreply, %{state | running?: false}}
      end
    after
      release_runtime_lease(lease, state)
    end
  end

  defp await_runtime_tick(%{tick_timeout_ms: timeout_ms, opts: opts}) do
    task = Task.async(fn -> tick(opts) end)
    Process.unlink(task.pid)

    case Task.yield(task, timeout_ms) || Task.shutdown(task, :brutal_kill) do
      {:ok, result} -> {:ok, result}
      {:exit, reason} -> exit(reason)
      nil -> :timeout
    end
  end

  defp snapshot(opts) do
    cond do
      Keyword.has_key?(opts, :snapshot) ->
        Keyword.fetch!(opts, :snapshot)

      snapshot_fun = Keyword.get(opts, :snapshot_fun) ->
        snapshot_fun.(Keyword.get(opts, :fleet_state, FleetState))

      true ->
        FleetState.snapshot(Keyword.get(opts, :fleet_state, FleetState))
    end
  end

  defp emit_fleet_findings(findings, self_agent_id, opts) do
    findings_opts =
      opts
      |> Keyword.get(:findings_opts, [])
      |> Keyword.put_new(:agent_id, self_agent_id)
      |> Keyword.put_new(:agent_name, agent_name(opts))

    Enum.count(findings, fn finding ->
      log_finding(finding)
      Findings.post_finding(finding, findings_opts)
    end)
  end

  defp log_finding(finding) do
    vcls = Map.get(finding, :violation_class, "")
    cls_tag = if vcls == "", do: "", else: "[#{vcls}] "
    Logger.info("FleetFindingEmitter: [#{finding.severity}] #{cls_tag}#{finding.summary}")
  end

  defp schedule_next_tick(state) do
    Process.send_after(self(), :tick, state.interval_ms + sample_jitter(state.jitter_ms))
  end

  defp acquire_runtime_lease(%{lease_advisory?: false}),
    do: %{outcome: :service_unavailable, lease_id: nil}

  defp acquire_runtime_lease(%{lease_opts: lease_opts}),
    do: LeaseAdvisory.acquire_cycle(lease_opts)

  defp release_runtime_lease(_lease, %{lease_advisory?: false}), do: :ok

  defp release_runtime_lease(lease, %{lease_opts: lease_opts}),
    do: LeaseAdvisory.release(lease, lease_opts)

  defp sample_jitter(0), do: 0

  defp sample_jitter(jitter_ms) when is_integer(jitter_ms) and jitter_ms > 0 do
    :rand.uniform(2 * jitter_ms + 1) - jitter_ms - 1
  end

  defp self_agent_id(opts) do
    Keyword.get(opts, :self_agent_id) ||
      opts
      |> Keyword.get(:findings_opts, [])
      |> Keyword.get(:agent_id) ||
      Application.get_env(:unitares_sentinel, :findings_agent_id) ||
      System.get_env("UNITARES_SENTINEL_AGENT_ID") ||
      @default_agent_id
  end

  defp agent_name(opts) do
    opts
    |> Keyword.get(:findings_opts, [])
    |> Keyword.get(:agent_name) ||
      Application.get_env(:unitares_sentinel, :findings_agent_name, @default_agent_name)
  end
end
