defmodule UnitaresLeasePlane.DialecticLiveness do
  @moduledoc """
  Per-session liveness process for an active dialectic session (dialectic-on-BEAM
  Slice 2 — the live-timer aliveness layer).

  One supervised GenServer per active session, registered in
  `UnitaresLeasePlane.DialecticLivenessRegistry`. It replaces the Python
  `auto_resolve` stuck-session sweep's 10-minute poll with a live timer: instead
  of a fleet-wide cron scanning the table, each session has its own process that
  knows it is alive and, when it goes inactive past the hard timeout, acts.

  The process self-terminates as soon as its session is terminal or gone, so the
  live set of these processes IS the live dialectic set — a process-level
  liveness signal, not a derived query.

  ## Acting is flag-gated and corruption-safe

  When `:dialectic_beam_liveness` is enabled and a session has been inactive past
  the hard timeout, the timer drives a `failed` resolve through
  `DialecticSaga.resolve/1` — i.e. through the saga + the guarded session-row
  write. That composition makes it safe to run alongside the Python sweeper with
  NO cross-runtime coordination flag:

    * the saga serializes (one in-flight per session);
    * the Python sweeper already skips saga-held sessions (C1);
    * B-4's guarded write makes a double-fail a no-op.

  So the worst case is the sweeper occasionally beating BEAM to the same terminal
  outcome — benign and idempotent, never corruption. When acting is disabled
  (default), the process is pure liveness/observation and never writes.
  """

  use GenServer

  alias UnitaresLeasePlane.DialecticSaga

  require Logger

  # Default hard inactivity timeout before a stuck session is failed (4h, matching
  # the Python FACILITATION_TIMEOUT). The check cadence is far finer than the old
  # 10-minute sweep so the judgment is near-live.
  @default_hard_timeout_s 14_400
  @default_check_interval_ms 30_000

  def child_spec(opts) do
    session_id = Keyword.fetch!(opts, :session_id)

    %{
      id: {__MODULE__, session_id},
      start: {__MODULE__, :start_link, [opts]},
      restart: :transient,
      type: :worker
    }
  end

  def start_link(opts) do
    session_id = Keyword.fetch!(opts, :session_id)
    GenServer.start_link(__MODULE__, opts, name: via(session_id))
  end

  @doc "Registry tuple for a session's liveness process."
  def via(session_id),
    do: {:via, Registry, {UnitaresLeasePlane.DialecticLivenessRegistry, session_id}}

  @doc "In-memory snapshot of this session's liveness, or :gone if no process."
  def snapshot(session_id) do
    case Registry.lookup(UnitaresLeasePlane.DialecticLivenessRegistry, session_id) do
      [{pid, _}] -> GenServer.call(pid, :snapshot)
      [] -> :gone
    end
  end

  @impl true
  def init(opts) do
    state = %{
      session_id: Keyword.fetch!(opts, :session_id),
      hard_timeout_s: Keyword.get(opts, :hard_timeout_s, @default_hard_timeout_s),
      check_interval_ms: Keyword.get(opts, :check_interval_ms, @default_check_interval_ms),
      inactive_seconds: 0,
      stuck: false
    }

    Process.send_after(
      self(),
      :check,
      Keyword.get(opts, :initial_check_ms, state.check_interval_ms)
    )

    {:ok, state}
  end

  @impl true
  def handle_call(:snapshot, _from, state) do
    {:reply,
     %{
       session_id: state.session_id,
       inactive_seconds: state.inactive_seconds,
       stuck: state.stuck
     }, state}
  end

  @impl true
  def handle_info(:check, state) do
    case DialecticSaga.get_session_liveness(state.session_id) do
      {:ok, nil} ->
        # Session gone — nothing to watch.
        {:stop, :normal, state}

      {:ok, %{status: status}} when status in ["resolved", "failed", "escalated"] ->
        # Reached a terminal state (by us, the sweeper, or normal flow) — done.
        {:stop, :normal, state}

      {:ok, info} ->
        evaluate(state, info)

      {:error, _} ->
        # Transient DB issue — try again next tick.
        reschedule(state)
        {:noreply, state}
    end
  end

  defp evaluate(state, info) do
    inactive = info.inactive_seconds
    stuck? = inactive >= state.hard_timeout_s
    state = %{state | inactive_seconds: inactive, stuck: stuck?}

    cond do
      stuck? and acting_enabled?() and is_binary(info.reviewer_agent_id) ->
        fail_stuck(state, info)
        {:stop, :normal, state}

      stuck? ->
        # Detected stuck but not acting (flag off, or no reviewer to attribute).
        # The Python sweeper remains the backstop. Surface via snapshot only.
        reschedule(state)
        {:noreply, state}

      true ->
        reschedule(state)
        {:noreply, state}
    end
  end

  # The payload was `%{"action" => "failed", "reason" => "liveness_timeout"}` and
  # nothing else. That row is the only artifact that outlives the session, and a
  # reader who has it reconstructs "the agent opened a session and walked away".
  # The record says otherwise: over 2026-07-28..08-18, 25 of 26 swept sessions
  # carried a standing REVIEWER REJECTION and every one of them was sitting in
  # `awaiting_facilitation` — the paused agent came back, was correctly refused
  # by the self-clear guard, and no operator arrived. The protocol ran; the
  # human step did not.
  #
  # The Python sweeper learned this and writes `_describe_reap/1`
  # (`src/mcp_handlers/dialectic/auto_resolve.py`) explaining the distinction.
  # This sweeper acts first — 30s cadence against a 10-minute sweep — so it
  # wins the race and that description is never the one that lands. Same
  # discipline here: report what was OBSERVED, claim no verdict. The sweeper
  # does not read the transcript, so it must not assert why the parties stopped.
  #
  # `action` and `reason` keep their exact prior values; everything else is
  # additive, so existing readers are untouched.
  defp fail_stuck(state, info) do
    awaiting = Map.get(info, :awaiting_facilitation, false)

    verdict_clause =
      case Map.get(info, :standing_verdict, "none") do
        "reject" ->
          " A reviewer rejection was standing when the sweep ran (acceptance: " <>
            "#{Map.get(info, :verdict_acceptance, "unknown")}); read it in the transcript."

        _ ->
          ""
      end

    note =
      if awaiting do
        "Swept while awaiting human facilitation; no operator acted. " <>
          "A sweep outcome, NOT a reviewer verdict, and not evidence that " <>
          "the paused agent abandoned the session." <> verdict_clause
      else
        "Swept for inactivity. A sweep outcome, NOT a reviewer verdict — " <>
          "read the last synthesis for the position standing when it ran." <>
          verdict_clause
      end

    # standing_verdict / verdict_message_id / verdict_acceptance are CARRIED from
    # core.dialectic_messages, never formed here. termination_basis names what
    # actually ended the session, which is always the sweep — recording a
    # verdict alongside it must not read as the verdict having terminated it.
    #
    # Deliberately NOT done: terminating on a standing rejection. That was the
    # original proposal and it was withdrawn under review. Acceptance would be
    # inferred from silence, and silence here usually means the agent's session
    # ended before the verdict landed (20 of 25 cases) rather than assent.
    # Terminating on it needs verdict_acceptance to become a real protocol
    # transition first.
    payload = %{
      "action" => "failed",
      "reason" => "liveness_timeout",
      "swept_by" => "beam_liveness",
      "phase" => Map.get(info, :phase),
      "awaiting_facilitation" => awaiting,
      "inactive_seconds" => info.inactive_seconds,
      "standing_verdict" => Map.get(info, :standing_verdict, "none"),
      "verdict_message_id" => Map.get(info, :verdict_message_id),
      "verdict_acceptance" => Map.get(info, :verdict_acceptance, "not_applicable"),
      "termination_basis" => "liveness_sweep",
      "note" => note
    }

    result =
      DialecticSaga.resolve(%{
        session_id: state.session_id,
        paused_agent_id: info.paused_agent_id,
        reviewer_agent_id: info.reviewer_agent_id,
        resolution_payload: payload,
        status: "failed"
      })

    Logger.warning(
      "dialectic_liveness: failed stuck session #{String.slice(state.session_id, 0, 16)} " <>
        "(inactive #{info.inactive_seconds}s) -> #{inspect(result)}"
    )
  end

  defp reschedule(state),
    do: Process.send_after(self(), :check, state.check_interval_ms)

  defp acting_enabled?,
    do: Application.get_env(:lease_plane, :dialectic_beam_liveness, false) == true
end
