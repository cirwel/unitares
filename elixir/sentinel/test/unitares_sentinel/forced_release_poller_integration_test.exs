defmodule UnitaresSentinel.ForcedReleasePollerIntegrationTest do
  @moduledoc """
  Integration tests for the cycle worker against a real `governance_test`
  Postgres. Skipped automatically when the DB is unreachable (see
  `test/test_helper.exs`).

  Surface 1 cycle worker contract: a single tick reads the cursor from
  `CycleState`, queries `lease_plane.lease_plane_events` for `event_type='forced'`
  rows newer than the cursor, builds alarms via `Logic.build_alarms/2`,
  advances the cursor to max(rows.ts), persists via `CycleState.save/2`.

  Findings emit (POSTing alarms) is Surface 2 — out of scope for this PR.
  The tick returns `{alarms, new_cursor}` so callers can wire emit later
  without changing this surface.

  Async: false because we mutate global Application env (state_file_path)
  and rely on Postgrex.start_link in test_helper having registered the DB.
  """

  use ExUnit.Case, async: false

  @moduletag :db

  alias UnitaresSentinel.{CycleState, ForcedReleasePoller}
  alias SentinelTestHelpers, as: H

  setup do
    label = H.random_label()
    surface_prefix = "dialectic:/test_sentinel_surface1_#{label}"

    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_poller_test_#{System.unique_integer([:positive])}")

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

  test "tick on empty cursor + zero matching rows → no alarms, cursor unchanged", _ctx do
    {alarms, new_cursor} = ForcedReleasePoller.tick(prior_cursor: nil, db: UnitaresSentinel.DB)

    assert alarms == []
    assert new_cursor == nil
  end

  test "tick picks up a newly-inserted forced event past the prior cursor", ctx do
    surface_id = ctx.surface_prefix <> "/inserted"
    {event_id, event_ts} = H.insert_forced_event(surface_id)

    # Use a prior cursor 1 second before the inserted event so the SQL
    # filter must include this row.
    prior = DateTime.add(event_ts, -1, :second)

    {alarms, new_cursor} = ForcedReleasePoller.tick(prior_cursor: prior, db: UnitaresSentinel.DB)

    # Filter to alarms for OUR surface — concurrent tests may have
    # inserted other forced events with their own prefixes.
    our_alarms = Enum.filter(alarms, &(&1.extra.surface_id == surface_id))

    assert length(our_alarms) == 1
    assert hd(our_alarms).extra.event_id == event_id
    assert hd(our_alarms).fingerprint == "forced_release:ad_hoc:#{event_id}"

    # Cursor must advance at least to our event_ts (or further if other tests
    # inserted later events). Truncated comparison to avoid microsecond churn.
    assert DateTime.compare(new_cursor, event_ts) in [:gt, :eq]
  end

  test "tick with cursor PAST the event ts excludes already-seen events", ctx do
    surface_id = ctx.surface_prefix <> "/already_seen"
    {_event_id, event_ts} = H.insert_forced_event(surface_id)

    # Cursor is 1 second AFTER the event — SQL filter `ts > $1` excludes it.
    future = DateTime.add(event_ts, 1, :second)

    {alarms, _new_cursor} =
      ForcedReleasePoller.tick(prior_cursor: future, db: UnitaresSentinel.DB)

    refute Enum.any?(alarms, &(&1.extra.surface_id == surface_id)),
           "events past the cursor must not re-fire"
  end

  test "tick persists the new cursor through CycleState.save", ctx do
    surface_id = ctx.surface_prefix <> "/persistence"
    {_event_id, event_ts} = H.insert_forced_event(surface_id)

    prior = DateTime.add(event_ts, -1, :second)

    {_alarms, new_cursor} =
      ForcedReleasePoller.tick(
        prior_cursor: prior,
        db: UnitaresSentinel.DB,
        persist: true,
        state_path: ctx.state_file <> ".beam"
      )

    # Reading back via CycleState must show the same cursor.
    persisted = CycleState.load(canonical: ctx.state_file, shadow: ctx.state_file <> ".beam")
    persisted_ts = CycleState.get_last_event_ts(persisted)

    assert is_binary(persisted_ts)
    assert persisted_ts == DateTime.to_iso8601(new_cursor)
  end

  test "tick with persist: false does NOT write CycleState", ctx do
    surface_id = ctx.surface_prefix <> "/no_persist"
    {_event_id, event_ts} = H.insert_forced_event(surface_id)

    prior = DateTime.add(event_ts, -1, :second)

    _ = ForcedReleasePoller.tick(prior_cursor: prior, db: UnitaresSentinel.DB, persist: false)

    refute File.exists?(ctx.state_file <> ".beam"),
           "persist: false must not create the shadow file"
  end

  # Council fold: reviewer Critical-1 (PR #376). persist_cursor was building
  # from %{} on every tick, silently erasing the v0.1.2 §B3 runtime flag and
  # any other sibling keys. This test pins the contract: an existing
  # `runtime: "beam_canonical"` flag in the shadow file MUST survive a tick
  # that persists a new cursor.
  test "tick preserves existing runtime flag in shadow file", ctx do
    shadow_path = ctx.state_file <> ".beam"

    # Pre-stage the shadow file with the cutover flag and an old cursor.
    File.write!(
      shadow_path,
      ~s({"runtime": "beam_canonical", "forced_release_alarm": {"last_event_ts": "2026-05-01T00:00:00.000000+00:00"}})
    )

    # Insert a new event so the tick has something to advance to.
    surface_id = ctx.surface_prefix <> "/runtime_preserve"
    {_event_id, event_ts} = H.insert_forced_event(surface_id)

    prior = DateTime.add(event_ts, -1, :second)

    _ =
      ForcedReleasePoller.tick(
        prior_cursor: prior,
        db: UnitaresSentinel.DB,
        persist: true,
        state_path: shadow_path
      )

    # Re-read the shadow file directly (bypass max-on-boot canonical merge
    # by using a non-existent canonical path).
    raw = File.read!(shadow_path)
    decoded = Jason.decode!(raw)

    assert decoded["runtime"] == "beam_canonical",
           "runtime flag must survive cycle-worker tick (was: #{inspect(decoded)})"

    # Cursor still advanced.
    assert get_in(decoded, ["forced_release_alarm", "last_event_ts"]) ==
             DateTime.to_iso8601(event_ts)
  end
end
