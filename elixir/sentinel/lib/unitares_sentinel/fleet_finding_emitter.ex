defmodule UnitaresSentinel.FleetFindingEmitter do
  @moduledoc """
  Runtime `sentinel_finding` emitter for BEAM Sentinel fleet analysis.

  This process is intentionally opt-in. The Wave 1 RFC forbids shadow-mode
  duplicate `sentinel_finding` emission, so production cutover must stop the
  Python Sentinel before enabling this GenServer.

  The governance check-in path is present but opt-in. It analyzes the BEAM
  `FleetState`, skips self-observations for finding emission, emits fleet
  findings to `/api/findings`, and can build/post the Python-compatible
  `process_agent_update` summary only when `:emit_checkins` is enabled.
  """

  use GenServer

  require Logger

  alias UnitaresSentinel.{
    CycleSummary,
    Findings,
    FleetAnalysis,
    FleetState,
    GovernanceCheckin,
    LeaseAdvisory,
    LeaseReclaim,
    LeaseStarvation
  }

  @default_interval_ms 300_000
  @default_initial_delay_ms 5_000
  @default_jitter_ms 5_000
  @default_tick_timeout_ms 45_000
  @default_agent_id "sentinel"
  @default_agent_name "Sentinel"

  @type tick_result :: %{
          fleet_findings: [map()],
          self_findings: [map()],
          posted_count: non_neg_integer(),
          checkin: map(),
          checkin_result: {:ok, map()} | {:error, term()} | nil,
          checkin_pause: map() | nil,
          recovery_outcome: :recovered | :refused | :error | :not_attempted | :not_paused
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

    checkin =
      CycleSummary.build(
        cycle_count: Keyword.get(opts, :cycle_count, 1),
        snapshot: snapshot,
        ws_connected?: Keyword.get(opts, :ws_connected?, Keyword.get(opts, :ws_connected, false)),
        fleet_findings: fleet_findings,
        self_findings: self_findings
      )

    checkin_result =
      if Keyword.get(
           opts,
           :emit_checkins,
           Keyword.get(
             opts,
             :emit_checkin,
             Application.get_env(:unitares_sentinel, :emit_checkins, false)
           )
         ) do
        GovernanceCheckin.checkin(checkin, Keyword.get(opts, :checkin_opts, []))
      end

    pause = handle_checkin_pause(checkin_result, self_agent_id, opts)

    %{
      fleet_findings: fleet_findings,
      self_findings: self_findings,
      posted_count: posted_count,
      checkin: checkin,
      checkin_result: checkin_result,
      checkin_pause: pause.detail,
      recovery_outcome: pause.recovery_outcome
    }
  end

  # The governance check-in surfaced a circuit-breaker PAUSE. Make it loud
  # (warning + a deduped self-finding so the dark state is visible on the
  # dashboard) and, if recovery is armed for this episode, ask governance for
  # a bounded `quick` resume. Recovery is server-gated, so a refusal just
  # leaves the resident surfaced for the operator rather than looping.
  defp handle_checkin_pause({:error, {:agent_paused, detail}}, self_agent_id, opts) do
    paused_at = Map.get(detail, "paused_at")

    Logger.warning(
      "FleetFindingEmitter: governance check-in REFUSED (AGENT_PAUSED) for #{self_agent_id} " <>
        "paused_at=#{inspect(paused_at)} — resident is DARK to governance until recovered."
    )

    if Keyword.get(opts, :emit_findings, true) do
      finding = %{
        type: "sentinel_self_pause",
        severity: "high",
        violation_class: "BEH",
        self_observation: true,
        summary:
          "Sentinel governance check-in refused: agent is PAUSED (circuit breaker), " <>
            "paused_at=#{inspect(paused_at)}. Check-ins are dark until recovered. " <>
            "Bounded quick-resume is attempted once per episode but is server-gated on " <>
            "coherence; a resident whose baseline coherence sits below that gate needs " <>
            "operator review / threshold attention, not self-resume."
      }

      finding_opts =
        opts
        |> Keyword.get(:findings_opts, [])
        |> Keyword.put_new(:agent_id, self_agent_id)
        |> Keyword.put_new(:agent_name, agent_name(opts))

      Findings.post_finding(finding, finding_opts)
    end

    %{detail: detail, recovery_outcome: maybe_recover(opts)}
  end

  defp handle_checkin_pause(_checkin_result, _self_agent_id, _opts),
    do: %{detail: nil, recovery_outcome: :not_paused}

  # Bounded, once-per-episode recovery: the GenServer disarms (`recovery_armed?:
  # false`) after a governance refusal so a single episode never loops. Default
  # armed so a standalone tick still attempts one server-gated resume.
  defp maybe_recover(opts) do
    armed? = Keyword.get(opts, :recovery_armed?, true)

    auto? =
      Keyword.get(opts, :auto_recover, Application.get_env(:unitares_sentinel, :auto_recover, true))

    if armed? and auto? do
      case GovernanceCheckin.recover(Keyword.get(opts, :checkin_opts, [])) do
        {:ok, _result} ->
          Logger.warning("FleetFindingEmitter: bounded self-recovery GRANTED by governance.")
          :recovered

        {:error, {:agent_paused, _}} ->
          Logger.warning("FleetFindingEmitter: self-recovery REFUSED (still paused) — staying surfaced.")
          :refused

        {:error, {:tool_error, reason}} ->
          Logger.warning(
            "FleetFindingEmitter: self-recovery REFUSED by governance (#{inspect(reason)}) — staying surfaced for operator."
          )

          :refused

        {:error, reason} ->
          Logger.warning(
            "FleetFindingEmitter: self-recovery transport error (#{inspect(reason)}) — will retry next cycle."
          )

          :error
      end
    else
      :not_attempted
    end
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

    # The emitter's lease surface is a property of the emitter, not of whatever
    # the caller remembered to pass. `LeaseAdvisory.acquire_cycle/1` defaults to
    # ForcedReleasePoller's `resident:/sentinel_cycle`, so an emitter started
    # without `lease_opts[:surface_id]` contended with the poller on the lease
    # plane *and* collided with it inside `LeaseStarvation` — same derived
    # sidecar path (two writers, one file) and same `fingerprint_extra` (one
    # resident's outage dedupping into the other's). `application.ex` documents
    # the distinct-surface invariant (KG 2026-05-08T02:14:43.822544+00:00);
    # defaulting here makes it structural instead of a property of one config
    # line that a future caller can omit.
    #
    # NOT `Keyword.put_new/3`: that keys on the key being PRESENT, so an explicit
    # `lease_opts: [surface_id: nil]` walked past it into the `fetch!` below,
    # reached `LeaseStarvation.require_surface_id/1`, and raised inside `init/1`
    # — a supervisor restart loop from a mistyped option. The defense is
    # value-shaped and lives with the requirement.
    lease_opts =
      opts
      |> Keyword.get(:lease_opts, [])
      |> LeaseStarvation.put_default_surface_id("resident:/sentinel_fleet_emit")

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
      lease_opts: lease_opts,
      cycle_count: Keyword.get(opts, :cycle_count, 0),
      running?: false,
      last_result: nil,
      # Disarmed for the rest of a pause episode once governance refuses a
      # bounded recovery, so a single episode never loops pause→resume→pause.
      # Re-armed when a check-in succeeds (episode cleared).
      recovery_blocked_episode?: false
    }

    # Lease-starvation tracker (2026-07-31 immortal-lease incident). Merged into
    # state rather than nested so `%{state | lease_blocked_*}` updates work and
    # `UnitaresSentinel.LeaseStarvation` never has to know this GenServer's
    # shape. `LeaseStarvation.new/1` reloads a persisted episode: a resident that
    # was restarted mid-outage (crash loop, or an operator running
    # `launchctl kickstart -k` because "sentinel looks stuck") must NOT get a
    # fresh threshold's worth of silence.
    state =
      Map.merge(
        state,
        LeaseStarvation.new(
          resident: "FleetFindingEmitter",
          # `fetch!`, not `get`: the surface is resolved above, and a nil here
          # would put both residents on one sidecar file and one fingerprint.
          surface_id: Keyword.fetch!(lease_opts, :surface_id),
          alert_after_seconds: Keyword.get(opts, :lease_blocked_alert_after_seconds),
          state_path: Keyword.get(opts, :lease_blocked_state_path, :derive)
        )
      )

    # Reclaim memory for acquire attempts whose response was lost twice over
    # (2026-08-01 incident): a later held_by_other naming one of these uuids is
    # our own stranded lease, releasable without operator intervention.
    state = Map.merge(state, LeaseReclaim.new())

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
    # Absorb BEFORE branching: both the blocked path (a failed attempt may
    # contribute a reclaim candidate) and the granted path (a success clears
    # them) update the memory.
    state = LeaseReclaim.absorb(state, lease)

    if lease_enforcement_blocked?(lease) do
      # 2026-07-31: this branch used to log and reschedule, nothing more. It
      # emitted "tick skipped by lease enforcement" by the thousand while every
      # liveness signal (launchctl, live PID, no crash) read healthy. Count the
      # episode and self-report it.
      state =
        state
        |> LeaseStarvation.record_blocked(lease)
        |> LeaseStarvation.maybe_emit(starvation_opts(state))

      Logger.warning(
        "FleetFindingEmitter: tick skipped by lease enforcement " <>
          "(blocked_ticks=#{state.lease_blocked_streak} " <>
          "alert_after=#{state.lease_blocked_alert_after_seconds}s)"
      )

      schedule_next_tick(state)
      {:noreply, %{state | running?: false}}
    else
      # The lease was granted (or advisory is off / the surface is unenforced):
      # whatever happens below, this tick was NOT starved. Clear HERE, above the
      # `try`, so the {:ok, _} arm, the :timeout arm and the task-exit path share
      # one reset and cannot drift apart. A runtime timeout means the lease WAS
      # acquired and the work was slow — a different failure with its own
      # warning; feeding it into the starvation counter would make the finding
      # lie about its own cause and point the operator at a force-release that
      # would not help.
      state = LeaseStarvation.clear(state, starvation_opts(state))

      try do
        case await_runtime_tick(state) do
          {:ok, result} ->
            schedule_next_tick(state)

            {:noreply,
             %{
               state
               | running?: false,
                 last_result: result,
                 cycle_count: state.cycle_count + 1,
                 recovery_blocked_episode?: next_recovery_gate(state, result)
             }}

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
  end

  defp await_runtime_tick(%{
         tick_timeout_ms: timeout_ms,
         opts: opts,
         cycle_count: cycle_count,
         recovery_blocked_episode?: blocked?
       }) do
    tick_opts =
      opts
      |> Keyword.put(:cycle_count, cycle_count + 1)
      |> Keyword.put(:recovery_armed?, not blocked?)

    task = Task.async(fn -> tick(tick_opts) end)
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

  # Episode gate, in priority order:
  #   1. A clean (non-paused) check-in means the episode cleared → re-arm.
  #   2. Any recovery ATTEMPT this cycle (refused / granted / transport error)
  #      disarms for the rest of the episode — strictly once-per-episode, so a
  #      granted-but-still-paused or a flapping-transport outcome cannot loop.
  #   3. Otherwise (paused, no attempt — already disarmed) hold the gate.
  defp next_recovery_gate(_state, %{checkin_pause: nil}), do: false

  defp next_recovery_gate(_state, %{recovery_outcome: outcome})
       when outcome in [:refused, :recovered, :error],
       do: true

  defp next_recovery_gate(state, _result),
    do: Map.get(state, :recovery_blocked_episode?, false)

  defp schedule_next_tick(state) do
    Process.send_after(self(), :tick, state.interval_ms + sample_jitter(state.jitter_ms))
  end

  # Mirrors the identity resolution in `handle_checkin_pause/3` so a starvation
  # self-finding fingerprints on the same agent the rest of this resident's
  # findings use.
  defp starvation_opts(%{opts: opts}) do
    findings_opts =
      opts
      |> Keyword.get(:findings_opts, [])
      |> Keyword.put_new(:agent_id, self_agent_id(opts))
      |> Keyword.put_new(:agent_name, agent_name(opts))

    [
      emit_findings?: Keyword.get(opts, :emit_findings, true),
      findings_opts: findings_opts
    ]
  end

  defp acquire_runtime_lease(%{lease_advisory?: false}),
    do: %{outcome: :service_unavailable, lease_id: nil}

  defp acquire_runtime_lease(%{lease_opts: lease_opts} = state),
    do: LeaseAdvisory.acquire_cycle(Keyword.merge(lease_opts, LeaseReclaim.acquire_opts(state)))

  defp lease_enforcement_blocked?(%{outcome: :enforcement_blocked}), do: true
  defp lease_enforcement_blocked?(_lease), do: false

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
