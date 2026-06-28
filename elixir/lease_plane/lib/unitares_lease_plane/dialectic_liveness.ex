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

  defp fail_stuck(state, info) do
    payload = %{"action" => "failed", "reason" => "liveness_timeout"}

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
