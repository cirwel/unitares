defmodule UnitaresSentinel.ForcedReleasePoller.LogicTest do
  @moduledoc """
  Pure-logic tests for the ad_hoc forced-release alarm builder.

  Mirrors the parity contract with `agents/sentinel/forced_release_alarm.py`
  for `event_type='forced'` rows: one alarm per event, max(ts) is the new
  cursor. Deprecation-batch and conflict-batch query classes are deferred
  to follow-up PRs.
  """

  use ExUnit.Case, async: true

  alias UnitaresSentinel.ForcedReleasePoller.Logic

  # Helper — Postgrex returns DateTime for timestamptz; we compose the same
  # shape here so the pure logic can be exercised without a real DB.
  defp dt(iso) do
    {:ok, dt, _} = DateTime.from_iso8601(iso)
    dt
  end

  defp ad_hoc_row(opts) do
    %{
      event_id: Keyword.fetch!(opts, :event_id),
      ts: Keyword.fetch!(opts, :ts),
      lease_id: Keyword.get(opts, :lease_id, "lease-uuid-stub"),
      surface_id: Keyword.fetch!(opts, :surface_id),
      surface_kind: Keyword.get(opts, :surface_kind, "dialectic")
    }
  end

  test "no rows + nil cursor → empty alarms, cursor unchanged" do
    assert {[], nil} = Logic.build_alarms([], nil)
  end

  test "no rows + existing cursor → empty alarms, cursor unchanged" do
    cursor = dt("2026-05-04T12:00:00Z")
    assert {[], ^cursor} = Logic.build_alarms([], cursor)
  end

  test "single ad_hoc row → one alarm with parity-shaped fields" do
    ts = dt("2026-05-05T01:23:45.000000Z")

    rows = [
      ad_hoc_row(
        event_id: "evt-001",
        ts: ts,
        lease_id: "lease-abc",
        surface_id: "dialectic:/test_surface_1",
        surface_kind: "dialectic"
      )
    ]

    {alarms, new_cursor} = Logic.build_alarms(rows, nil)

    assert [alarm] = alarms
    assert alarm.kind == "ad_hoc"
    assert alarm.severity == "high"
    assert alarm.summary == "forced release: dialectic:/test_surface_1 (lease lease-abc)"
    assert alarm.fingerprint == "forced_release:ad_hoc:evt-001"
    assert alarm.extra.event_id == "evt-001"
    assert alarm.extra.lease_id == "lease-abc"
    assert alarm.extra.surface_id == "dialectic:/test_surface_1"
    assert alarm.extra.surface_kind == "dialectic"
    assert alarm.extra.ts == DateTime.to_iso8601(ts)

    assert new_cursor == ts
  end

  test "multiple rows → cursor advances to max(ts) regardless of input order" do
    earlier = dt("2026-05-04T00:00:00Z")
    middle = dt("2026-05-04T12:00:00Z")
    latest = dt("2026-05-04T23:59:59Z")

    rows = [
      ad_hoc_row(event_id: "e2", ts: middle, surface_id: "dialectic:/s/2"),
      ad_hoc_row(event_id: "e3", ts: latest, surface_id: "dialectic:/s/3"),
      ad_hoc_row(event_id: "e1", ts: earlier, surface_id: "dialectic:/s/1")
    ]

    {alarms, new_cursor} = Logic.build_alarms(rows, nil)

    assert length(alarms) == 3
    assert new_cursor == latest
  end

  test "prior cursor is preserved when all new rows are older (caller filters before passing)" do
    # Defensive contract: if a buggy SQL filter ever lets older rows through,
    # the new cursor must NOT regress below the prior cursor.
    prior = dt("2026-05-04T12:00:00Z")
    older = dt("2026-05-03T00:00:00Z")

    rows = [ad_hoc_row(event_id: "stale", ts: older, surface_id: "dialectic:/old")]

    {_alarms, new_cursor} = Logic.build_alarms(rows, prior)

    assert new_cursor == prior, "cursor must never regress"
  end

  test "lease_id is nil-tolerant (fixture rows can have NULL lease_id)" do
    # In the live schema lease_id is nullable on lease_plane_events. Don't
    # explode the summary string when it's missing; mirror Python's `str()`
    # fallback which yields "None" — we use "<unknown>" to signal absence
    # without leaking Elixir nil rendering.
    ts = dt("2026-05-05T01:23:45Z")

    rows = [
      ad_hoc_row(
        event_id: "evt-no-lease",
        ts: ts,
        lease_id: nil,
        surface_id: "dialectic:/test_no_lease"
      )
    ]

    {[alarm], _} = Logic.build_alarms(rows, nil)

    assert alarm.summary =~ "forced release: dialectic:/test_no_lease"
    refute alarm.summary =~ "lease )" or alarm.summary =~ "lease nil)"
    assert alarm.extra.lease_id == nil
  end

  test "reserved + legacy test surfaces are suppressed but advance the cursor" do
    # The force-release contract test does a REAL force-release on every pytest
    # run; the active (BEAM) Sentinel must not page on those fixtures. Parity
    # with Python's `_is_reserved_test_surface` — both prefixes suppressed.
    ts_reserved = dt("2026-05-05T01:00:00Z")
    ts_legacy = dt("2026-05-05T02:00:00Z")
    ts_real = dt("2026-05-05T03:00:00Z")

    rows = [
      ad_hoc_row(event_id: "evt-reserved", ts: ts_reserved, surface_id: "td:/test/force-release-contract-abc"),
      ad_hoc_row(event_id: "evt-legacy", ts: ts_legacy, surface_id: "td:/force-release-contract-test-xyz"),
      ad_hoc_row(event_id: "evt-real", ts: ts_real, surface_id: "dialectic:/real_surface")
    ]

    {alarms, new_cursor} = Logic.build_alarms(rows, nil)

    # Only the genuine operator surface alarms.
    assert [alarm] = alarms
    assert alarm.extra.surface_id == "dialectic:/real_surface"
    # Cursor still advances past the suppressed fixtures so they aren't rescanned.
    assert new_cursor == ts_real
  end

  test "conflict_batch on a reserved test surface is suppressed but advances the cursor" do
    ts = dt("2026-05-05T04:00:00Z")

    rows = [
      %{
        surface_id: "td:/test/conflict-fixture",
        surface_kind: "td",
        event_count: 3,
        first_ts: ts,
        last_ts: ts
      }
    ]

    {alarms, new_cursor} = Logic.build_conflict_batch_alarms(rows, nil)

    assert alarms == []
    assert new_cursor == ts
  end

  test "fingerprint is unique per event_id (downstream dedup contract)" do
    ts = dt("2026-05-05T01:23:45Z")

    rows = [
      ad_hoc_row(event_id: "evt-A", ts: ts, surface_id: "dialectic:/x"),
      ad_hoc_row(event_id: "evt-B", ts: ts, surface_id: "dialectic:/x"),
      ad_hoc_row(event_id: "evt-C", ts: ts, surface_id: "dialectic:/x")
    ]

    {alarms, _} = Logic.build_alarms(rows, nil)

    fingerprints = Enum.map(alarms, & &1.fingerprint)
    assert Enum.uniq(fingerprints) == fingerprints,
           "every event_id must yield a distinct fingerprint"
  end
end
