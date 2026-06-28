defmodule UnitaresLeasePlane.DialecticLivenessTest do
  @moduledoc """
  Tests for the per-session liveness layer (dialectic-on-BEAM Slice 2): the
  reconciler starts a watcher per active session, and a watcher whose session is
  stuck past the hard timeout fails it via the saga path — but only when the
  `:dialectic_beam_liveness` flag is enabled.
  """
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.{
    DialecticLiveness,
    DialecticLivenessSupervisor,
    DialecticLivenessReconciler,
    DB
  }

  import LeaseTestHelpers

  setup do
    prior = Application.get_env(:lease_plane, :dialectic_beam_liveness, false)
    on_exit(fn -> Application.put_env(:lease_plane, :dialectic_beam_liveness, prior) end)
    :ok
  end

  defp session_status(session_id) do
    %{rows: [[status]]} =
      Postgrex.query!(DB, "SELECT status FROM core.dialectic_sessions WHERE session_id = $1", [
        session_id
      ])

    status
  end

  defp wait_until(fun, tries \\ 50) do
    cond do
      fun.() ->
        :ok

      tries <= 0 ->
        :timeout

      true ->
        Process.sleep(20)
        wait_until(fun, tries - 1)
    end
  end

  test "reconciler starts a watcher for an active session" do
    session_id = insert_dialectic_session()
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:ok, %{}} = DialecticLivenessReconciler.perform(%{})
    # A watcher process now exists for the session.
    assert :gone != DialecticLiveness.snapshot(session_id)
    # ensure_started is idempotent.
    assert :already_started = DialecticLivenessSupervisor.ensure_started(session_id)
  end

  test "watcher fails a stuck session when acting is enabled" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)
    session_id = insert_dialectic_session(reviewer_agent_id: "rev-1")
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    # hard_timeout_s: 0 -> any age is "stuck"; initial_check_ms: 0 -> act now.
    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)
  end

  test "watcher does NOT write when acting is disabled (default)" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, false)
    session_id = insert_dialectic_session(reviewer_agent_id: "rev-1")
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    # Give the timer a chance to fire; the session must remain active.
    Process.sleep(120)
    assert session_status(session_id) == "active"
    # The watcher reports stuck in its snapshot even though it didn't act.
    snap = DialecticLiveness.snapshot(session_id)
    assert snap != :gone and snap.stuck == true
  end

  test "watcher self-terminates when the session is already terminal" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)
    session_id = insert_dialectic_session(status: "resolved", phase: "resolved")
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> DialecticLiveness.snapshot(session_id) == :gone end)
    # Untouched: it was already resolved before the watcher ran.
    assert session_status(session_id) == "resolved"
  end
end
