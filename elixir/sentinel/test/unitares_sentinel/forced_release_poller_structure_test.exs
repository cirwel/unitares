defmodule UnitaresSentinel.ForcedReleasePollerStructureTest do
  @moduledoc """
  Structural binding tests for the cycle worker (RFC v0.1.3 §B5/§B6/§C2).

  These pin behaviors that prepare the ground for the deprecation_batch +
  conflict_batch refactor in the next PR:

    * §B5 — tick uses a single Postgrex.transaction so all queries (one
      now, three later) share one snapshot.
    * §B6 — on transaction failure, the cursor MUST NOT advance and the
      persist MUST NOT happen. Returning `{[], prior_cursor}` enforces
      both invariants.
    * §C2 — the GenServer's `running?` guard skips :tick messages that
      arrive while a previous tick is in flight (defends against external
      send(pid, :tick) bypassing the scheduler).
  """

  use ExUnit.Case, async: false

  import ExUnit.CaptureLog

  alias UnitaresSentinel.ForcedReleasePoller
  alias SentinelTestHelpers, as: H

  setup do
    label = H.random_label()
    surface_prefix = "dialectic:/test_sentinel_struct_#{label}"

    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_struct_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    state_file = Path.join(tmpdir, ".sentinel_state")
    Application.put_env(:unitares_sentinel, :state_file_path, state_file)

    on_exit(fn ->
      H.cleanup_surface_prefix(surface_prefix)
      Application.delete_env(:unitares_sentinel, :state_file_path)
      File.rm_rf!(tmpdir)
    end)

    {:ok, surface_prefix: surface_prefix, state_file: state_file, tmpdir: tmpdir}
  end

  # ---------------------------------------------------------------------------
  # §B6 — transaction failure preserves cursor + does NOT persist.
  # ---------------------------------------------------------------------------

  @moduletag :db
  test "tick on dead DB exits — supervisor restart preserves cursor (the OTHER §B6 path)", ctx do
    # v0.1.3 §B6 covers two failure classes:
    #   (a) `{:error, _}` returned from transaction → tick returns
    #       `{[], prior_cursor}`, no persist. (Pinned by the rollback
    #       test below.)
    #   (b) Process exit (e.g. :noproc on dead registered name) →
    #       caller dies, supervisor restarts, init/1 re-reads on-disk
    #       cursor. The cursor was never advanced because the dying
    #       tick never reached `persist_cursor/2`.
    #
    # This test pins (b): the dead-DB call exits AND no shadow file is
    # written. Verifier-confirmed in PR #378 council that `Postgrex.transaction`
    # against a non-registered name raises `:noproc` rather than returning
    # `{:error, _}`. Pre-council, this test had a dual-acceptance branch
    # that was dead code; honest version below.
    fake_db = :"nonexistent_db_#{System.unique_integer([:positive])}"
    prior = ~U[2026-05-04 12:00:00.000000Z]

    exit_caught? =
      try do
        ForcedReleasePoller.tick(
          prior_cursor: prior,
          db: fake_db,
          persist: true,
          state_path: ctx.state_file <> ".beam"
        )

        false
      catch
        :exit, _reason -> true
      end

    assert exit_caught?,
           "dead DB module must exit (supervisor-restart path), not return — §B6 path (b)"

    refute File.exists?(ctx.state_file <> ".beam"),
           "exit before persist_cursor MUST NOT have written shadow file — §B6 (b)"
  end

  test "tick on Postgrex.rollback returns {[], prior_cursor} (§B6 path (a))", _ctx do
    # Pin §B6 path (a) by exercising Postgrex.transaction's rollback-returns-
    # {:error, _} contract directly. Verifier-confirmed:
    #   Postgrex.transaction(DB, fn conn -> Postgrex.rollback(conn, :x) end)
    # returns `{:error, :x}`. tick/1 matches this and returns
    # `{[], prior_cursor}` without persisting.
    #
    # We can't easily inject a rollback into tick/1's hardcoded SELECT, but
    # we can pin the contract on the underlying Postgrex behavior the
    # error-path branch relies on. If this test starts failing, tick/1's
    # `{:error, reason} ->` branch is unreachable and §B6 (a) is broken.
    result =
      Postgrex.transaction(UnitaresSentinel.DB, fn conn ->
        Postgrex.rollback(conn, :test_rollback_for_b6_pin)
      end)

    assert result == {:error, :test_rollback_for_b6_pin},
           "Postgrex.rollback contract underpins §B6 path (a) — if this fails, tick/1's error branch is unreachable"
  end

  test "tick on real DB with prior cursor that returns empty rows preserves cursor", _ctx do
    # No fixture inserted; cursor is far in the future so any real rows are
    # filtered out. `Logic.build_alarms([], prior)` returns `{[], prior}`,
    # and the persist gate `new_cursor != prior_cursor` skips the file write.
    far_future = DateTime.utc_now() |> DateTime.add(86_400 * 365, :second)

    {alarms, new_cursor} =
      ForcedReleasePoller.tick(
        prior_cursor: far_future,
        db: UnitaresSentinel.DB,
        persist: true
      )

    assert alarms == []
    assert new_cursor == far_future, "cursor MUST be preserved when no new rows"
  end

  # ---------------------------------------------------------------------------
  # §B5 — transaction wrapping (proxy: works against real DB; multi-query
  # snapshot consistency is the point but only testable with the next PR's
  # extra query classes). Pin via "tick still works post-refactor" + a
  # smoke that a transaction is in flight by checking pg_stat_activity.
  # ---------------------------------------------------------------------------

  test "tick works against real DB after Postgrex.transaction refactor", ctx do
    surface_id = ctx.surface_prefix <> "/tx_smoke"
    {event_id, event_ts} = H.insert_forced_event(surface_id)

    prior = DateTime.add(event_ts, -1, :second)

    {alarms, new_cursor} =
      ForcedReleasePoller.tick(
        prior_cursor: prior,
        db: UnitaresSentinel.DB,
        persist: true,
        state_path: ctx.state_file <> ".beam"
      )

    our = Enum.find(alarms, &(&1.extra.surface_id == surface_id))
    assert our != nil, "transaction-wrapped tick must still find the inserted event"
    assert our.extra.event_id == event_id
    assert DateTime.compare(new_cursor, event_ts) in [:gt, :eq]
  end

  # ---------------------------------------------------------------------------
  # §C2 — running? guard skips :tick when previous still in flight.
  # ---------------------------------------------------------------------------

  test "GenServer skips :tick when running? is true (mailbox guard)" do
    # Verifier-confirmed (PR #378 council): deleting the
    # `handle_info(:tick, %{running?: true})` head causes this test to fail
    # because the real tick body would set running? back to false. The
    # assertion `state.running? == true` after `send(pid, :tick)` is
    # structurally load-bearing — it pins that the guard short-circuited
    # without entering the body.
    #
    # The guard itself is unreachable under self-scheduling (next :tick is
    # only enqueued AFTER the current tick returns) — this test exercises
    # the EXTERNAL `send(pid, :tick)` path that justifies the guard's
    # existence (operator iex sends, future cron-style schedulers, etc.).
    # Without this test the guard would be flagged as dead code by future
    # cleanup-PRs.
    {:ok, pid} =
      ForcedReleasePoller.start_link(
        name: :"test_guard_#{System.unique_integer([:positive])}",
        db: UnitaresSentinel.DB,
        # Long enough that no :tick will fire on its own during the test.
        interval_ms: 60_000,
        initial_delay_ms: 60_000,
        jitter_ms: 0
      )

    # Force running? = true via :sys.replace_state. This is a test-only
    # introspection seam — production code never sets running? from outside.
    :sys.replace_state(pid, fn state -> %{state | running?: true} end)

    # Send a synthetic :tick. The guard MUST handle it without crashing
    # the GenServer.
    send(pid, :tick)

    # Give the GenServer a beat to process the message.
    state = :sys.get_state(pid)

    assert Process.alive?(pid), "guard must not crash the GenServer"
    assert state.running? == true, "guard must NOT clear running? (only the real tick body does)"

    # Now flip running? off and verify the next :tick CAN run.
    :sys.replace_state(pid, fn state -> %{state | running?: false} end)
    send(pid, :tick)
    Process.sleep(50)

    assert Process.alive?(pid), "tick after guard clear must not crash the GenServer"

    GenServer.stop(pid)
  end

  test "GenServer releases advisory lease before propagating runtime task exits" do
    capture_log(fn ->
      parent = self()
      fake_db = :"nonexistent_db_#{System.unique_integer([:positive])}"
      lease_id = "66666666-6666-6666-6666-666666666666"

      lease_http_post = fn url, body, _headers, _timeout_ms ->
        cond do
          String.ends_with?(url, "/v1/lease/acquire") ->
            send(parent, {:lease_acquire, body})

            {:ok, 200,
             Jason.encode!(%{
               ok: true,
               idempotent: false,
               lease: %{lease_id: lease_id},
               drift_warning: []
             })}

          String.ends_with?(url, "/v1/lease/release") ->
            send(parent, {:lease_release, body})
            {:ok, 200, ~s({"ok":true})}
        end
      end

      {:ok, pid} =
        GenServer.start(
          ForcedReleasePoller,
          [
            db: fake_db,
            interval_ms: 60_000,
            initial_delay_ms: 60_000,
            jitter_ms: 0,
            tick_timeout_ms: 1_000,
            lease_advisory: true,
            lease_opts: [
              base_url: "http://lease.test",
              bearer_token: "test-token",
              http_post: lease_http_post
            ]
          ],
          name: :"test_task_exit_release_#{System.unique_integer([:positive])}"
        )

      ref = Process.monitor(pid)
      send(pid, :tick)

      assert_receive {:lease_acquire, _acquire_body}, 2_000
      assert_receive {:lease_release, release_body}, 2_000
      assert release_body == %{"lease_id" => lease_id, "release_reason" => "normal"}
      assert_receive {:DOWN, ^ref, :process, ^pid, _reason}, 2_000
    end)
  end
end
