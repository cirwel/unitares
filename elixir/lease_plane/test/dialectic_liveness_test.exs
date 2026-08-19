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

  defp resolution(session_id) do
    %{rows: [[json]]} =
      Postgrex.query!(
        DB,
        "SELECT resolution_json FROM core.dialectic_sessions WHERE session_id = $1",
        [session_id]
      )

    json
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

  # The reap row is the only artifact that outlives the session. When it said
  # only `liveness_timeout`, every reader reconstructed "the agent walked away"
  # — while the record showed 25 of 26 swept sessions carrying a standing
  # reviewer rejection, all of them stalled on a human step that never came.
  test "a facilitation reap records that it was awaiting a human, and claims no verdict" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)

    session_id =
      insert_dialectic_session(reviewer_agent_id: "rev-1", awaiting_facilitation: true)

    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)

    res = resolution(session_id)
    # unchanged for existing readers
    assert res["action"] == "failed"
    assert res["reason"] == "liveness_timeout"
    # additive context
    assert res["awaiting_facilitation"] == true
    assert res["swept_by"] == "beam_liveness"
    assert res["phase"] == "synthesis"
    assert is_integer(res["inactive_seconds"])
    assert res["note"] =~ "awaiting human facilitation"
    # the sweeper does not read the transcript, so it must not assert a verdict
    assert res["note"] =~ "NOT a reviewer verdict"
  end

  test "an ordinary inactivity reap is distinguishable from a facilitation reap" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)

    session_id =
      insert_dialectic_session(reviewer_agent_id: "rev-1", awaiting_facilitation: false)

    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)

    res = resolution(session_id)
    assert res["awaiting_facilitation"] == false
    refute res["note"] =~ "awaiting human facilitation"
    assert res["note"] =~ "NOT a reviewer verdict"
  end

  # The condition this came from: a sweeper may CARRY a verdict already recorded
  # in the transcript, never form one. And it may not terminate on it — silence
  # after a rejection usually means the agent's session ended, not that it
  # assented.
  test "a reap carries a standing reviewer rejection without terminating on it" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)

    session_id =
      insert_dialectic_session(reviewer_agent_id: "rev-1", awaiting_facilitation: true)

    on_exit(fn -> cleanup_dialectic_session(session_id) end)
    insert_dialectic_message(session_id, "rev-1", "synthesis", agrees: false)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)

    res = resolution(session_id)
    assert res["standing_verdict"] == "reject"
    assert is_integer(res["verdict_message_id"])
    # nobody replied after the rejection — must NOT be recorded as acceptance
    assert res["verdict_acceptance"] == "no_reply"
    # the sweep ended it, not the verdict
    assert res["termination_basis"] == "liveness_sweep"
    assert res["reason"] == "liveness_timeout"
    assert res["note"] =~ "rejection was standing"
  end

  test "a paused-agent reply after the rejection is recorded as accepted or contested" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)

    session_id =
      insert_dialectic_session(
        paused_agent_id: "paused-1",
        reviewer_agent_id: "rev-1",
        awaiting_facilitation: true
      )

    on_exit(fn -> cleanup_dialectic_session(session_id) end)
    insert_dialectic_message(session_id, "rev-1", "synthesis", agrees: false)
    insert_dialectic_message(session_id, "paused-1", "synthesis", agrees: true)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)

    res = resolution(session_id)
    assert res["standing_verdict"] == "reject"
    assert res["verdict_acceptance"] == "accepted"
    # accepted is an OBSERVATION; it still must not resolve the session
    assert res["action"] == "failed"
  end

  test "a session with no rejection records no verdict to carry" do
    Application.put_env(:lease_plane, :dialectic_beam_liveness, true)
    session_id = insert_dialectic_session(reviewer_agent_id: "rev-1")
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    :started =
      DialecticLivenessSupervisor.ensure_started(session_id,
        hard_timeout_s: 0,
        initial_check_ms: 0,
        check_interval_ms: 50
      )

    assert :ok = wait_until(fn -> session_status(session_id) == "failed" end)

    res = resolution(session_id)
    assert res["standing_verdict"] == "none"
    assert res["verdict_message_id"] == nil
    assert res["verdict_acceptance"] == "not_applicable"
    refute res["note"] =~ "rejection was standing"
  end
end
