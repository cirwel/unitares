defmodule UnitaresSentinel.SupervisionRestartTest do
  @moduledoc """
  Wave 1 condition 3 — "supervision tree absorbs at least one induced fault
  (kill a worker, supervisor restarts, no manual intervention)"
  (`docs/proposals/beam-footprint-roadmap-v0.md`:685).

  `forced_release_poller_structure_test.exs` pins that a dead-DB tick *exits* —
  the precondition for a restart. This test closes the condition itself: a
  `:one_for_one` supervisor (mirroring `UnitaresSentinel.Application`'s
  strategy) restarts a killed `ForcedReleasePoller` child with no manual
  intervention, and the restarted worker re-reads its on-disk cursor so the
  de-dup fence survives the fault (no alarm-replay regression — RFC §B2).

  No DB or HTTP is touched: intervals are set far in the future so no tick
  fires, lease advisory and findings emit are off, and `db:` is an unused
  registered name the worker never queries during init.
  """

  use ExUnit.Case, async: false

  alias UnitaresSentinel.ForcedReleasePoller

  setup do
    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_supervision_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    state_file = Path.join(tmpdir, ".sentinel_state")
    cursor_iso = "2026-05-04T12:00:00.000000Z"

    File.write!(
      state_file,
      Jason.encode!(%{"forced_release_alarm" => %{"last_event_ts" => cursor_iso}})
    )

    Application.put_env(:unitares_sentinel, :state_file_path, state_file)

    on_exit(fn ->
      Application.delete_env(:unitares_sentinel, :state_file_path)
      File.rm_rf!(tmpdir)
    end)

    {:ok, cursor_iso: cursor_iso}
  end

  test "one_for_one supervisor restarts a killed poller with no manual intervention", ctx do
    poller_name = :"supervised_poller_#{System.unique_integer([:positive])}"

    child_opts = [
      name: poller_name,
      # Never fire a tick during the test — DB/HTTP stay untouched.
      db: :"unused_db_#{System.unique_integer([:positive])}",
      interval_ms: 60_000,
      initial_delay_ms: 60_000,
      jitter_ms: 0,
      lease_advisory: false,
      emit_findings: false
    ]

    children = [
      %{id: poller_name, start: {ForcedReleasePoller, :start_link, [child_opts]}}
    ]

    {:ok, sup} =
      Supervisor.start_link(children,
        strategy: :one_for_one,
        name: :"sup_#{System.unique_integer([:positive])}"
      )

    on_exit(fn -> if Process.alive?(sup), do: Supervisor.stop(sup) end)

    pid_before = Process.whereis(poller_name)
    assert is_pid(pid_before) and Process.alive?(pid_before)

    # The worker loaded the on-disk cursor on init.
    {:ok, expected_cursor, _} = DateTime.from_iso8601(ctx.cursor_iso)
    assert :sys.get_state(pid_before).cursor == expected_cursor

    # --- Induce the fault: kill the worker. No manual restart follows. ---
    ref = Process.monitor(pid_before)
    Process.exit(pid_before, :kill)
    assert_receive {:DOWN, ^ref, :process, ^pid_before, :killed}, 2_000

    # --- The supervisor absorbs it: a NEW pid appears under the same name. ---
    pid_after = wait_for_restart(poller_name, pid_before, 2_000)

    assert pid_after != pid_before,
           "supervisor must start a fresh process, not resurrect the dead pid"

    assert Process.alive?(pid_after)

    # --- The de-dup fence survived: restarted worker re-read the cursor. ---
    assert :sys.get_state(pid_after).cursor == expected_cursor,
           "restarted worker MUST re-read the on-disk cursor (no alarm-replay regression)"

    # The supervisor still supervises exactly one running child.
    assert [{_id, ^pid_after, :worker, _modules}] = Supervisor.which_children(sup)
  end

  defp wait_for_restart(name, old_pid, timeout_ms) do
    deadline = System.monotonic_time(:millisecond) + timeout_ms
    do_wait_for_restart(name, old_pid, deadline)
  end

  defp do_wait_for_restart(name, old_pid, deadline) do
    case Process.whereis(name) do
      pid when is_pid(pid) and pid != old_pid ->
        if Process.alive?(pid),
          do: pid,
          else: retry_restart(name, old_pid, deadline)

      _ ->
        retry_restart(name, old_pid, deadline)
    end
  end

  defp retry_restart(name, old_pid, deadline) do
    if System.monotonic_time(:millisecond) >= deadline do
      flunk("supervisor did not restart #{inspect(name)} within the deadline")
    else
      Process.sleep(10)
      do_wait_for_restart(name, old_pid, deadline)
    end
  end
end
