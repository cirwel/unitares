defmodule UnitaresSentinel.ForcedReleasePoller.Logic3ClassTest do
  @moduledoc """
  Pure-logic tests for the deprecation_batch + conflict_batch + combined
  builders (v0.1.3 §B1 combined poller binding).

  Per v0.1.3 §C1 floor (21+ pure across three classes); ad_hoc class is
  already covered by `forced_release_poller_logic_test.exs` (7 tests).
  This file adds 7+ for deprecation_batch, 7+ for conflict_batch, and
  cross-class coverage via build_all_alarms/4.
  """

  use ExUnit.Case, async: true

  alias UnitaresSentinel.ForcedReleasePoller.Logic

  defp dt(iso) do
    {:ok, dt, _} = DateTime.from_iso8601(iso)
    dt
  end

  defp dep_row(opts) do
    %{
      deprecation_id: Keyword.fetch!(opts, :deprecation_id),
      surface_kind: Keyword.get(opts, :surface_kind, "dialectic"),
      sweep_completed_at: Keyword.fetch!(opts, :sweep_completed_at),
      event_count: Keyword.get(opts, :event_count, 5),
      first_ts: Keyword.fetch!(opts, :first_ts),
      last_ts: Keyword.fetch!(opts, :last_ts)
    }
  end

  defp conf_row(opts) do
    %{
      surface_id: Keyword.fetch!(opts, :surface_id),
      surface_kind: Keyword.get(opts, :surface_kind, "dialectic"),
      event_count: Keyword.get(opts, :event_count, 3),
      first_ts: Keyword.fetch!(opts, :first_ts),
      last_ts: Keyword.fetch!(opts, :last_ts)
    }
  end

  defp ad_hoc_row(opts) do
    %{
      event_id: Keyword.fetch!(opts, :event_id),
      ts: Keyword.fetch!(opts, :ts),
      lease_id: Keyword.get(opts, :lease_id, "lease-stub"),
      surface_id: Keyword.fetch!(opts, :surface_id),
      surface_kind: Keyword.get(opts, :surface_kind, "dialectic")
    }
  end

  # =========================================================================
  # deprecation_batch (7 tests)
  # =========================================================================

  test "deprecation_batch: empty rows + nil cursor → empty alarms, cursor unchanged" do
    assert {[], nil} = Logic.build_deprecation_batch_alarms([], nil)
  end

  test "deprecation_batch: single row produces one alarm with parity-shaped fields" do
    row =
      dep_row(
        deprecation_id: "depr-uuid-1",
        surface_kind: "dialectic",
        sweep_completed_at: dt("2026-05-05T10:00:00Z"),
        event_count: 12,
        first_ts: dt("2026-05-05T09:00:00Z"),
        last_ts: dt("2026-05-05T09:30:00Z")
      )

    {[alarm], cursor} = Logic.build_deprecation_batch_alarms([row], nil)

    assert alarm.kind == "deprecation_batch"
    assert alarm.severity == "medium"
    assert alarm.summary == "deprecation sweep complete: kind=dialectic count=12"
    assert alarm.fingerprint == "forced_release:deprecation_batch:depr-uuid-1"
    assert alarm.extra.deprecation_id == "depr-uuid-1"
    assert alarm.extra.kind == "dialectic"
    assert alarm.extra.count == 12
    assert alarm.extra.last_ts == DateTime.to_iso8601(row.last_ts)
    assert alarm.extra.sweep_completed_at == DateTime.to_iso8601(row.sweep_completed_at)
    # v0.1.3 §B2 asymmetry: cursor advances on last_ts, NOT sweep_completed_at
    assert cursor == row.last_ts
  end

  test "deprecation_batch: §B2 asymmetry — cursor advances on last_ts even when sweep_completed_at is later" do
    # sweep_completed_at > last_ts is the typical case (sweep finishes after
    # last event), but the cursor MUST advance on last_ts not sweep_completed_at.
    row =
      dep_row(
        deprecation_id: "depr-2",
        sweep_completed_at: dt("2026-05-05T15:00:00Z"),
        first_ts: dt("2026-05-05T08:00:00Z"),
        last_ts: dt("2026-05-05T09:00:00Z")
      )

    {[_alarm], cursor} = Logic.build_deprecation_batch_alarms([row], nil)
    assert cursor == row.last_ts, "cursor MUST advance on last_ts, not sweep_completed_at"
    refute cursor == row.sweep_completed_at,
           "Python PR 5 council fix: never mix event-stream and table-metadata timestamps"
  end

  test "deprecation_batch: multi-row cursor advances to max(last_ts)" do
    older = dt("2026-05-04T00:00:00Z")
    newer = dt("2026-05-05T00:00:00Z")

    rows = [
      dep_row(deprecation_id: "d1", sweep_completed_at: dt("2026-05-04T01:00:00Z"), first_ts: older, last_ts: older),
      dep_row(deprecation_id: "d2", sweep_completed_at: dt("2026-05-05T01:00:00Z"), first_ts: older, last_ts: newer)
    ]

    {alarms, cursor} = Logic.build_deprecation_batch_alarms(rows, nil)
    assert length(alarms) == 2
    assert cursor == newer
  end

  test "deprecation_batch: cursor never regresses below prior" do
    prior = dt("2026-05-10T00:00:00Z")
    row = dep_row(deprecation_id: "old", sweep_completed_at: dt("2026-05-09T00:00:00Z"), first_ts: dt("2026-05-08T00:00:00Z"), last_ts: dt("2026-05-08T00:00:00Z"))

    {_alarms, cursor} = Logic.build_deprecation_batch_alarms([row], prior)
    assert cursor == prior, "cursor must NOT regress"
  end

  test "deprecation_batch: fingerprint is unique per deprecation_id" do
    rows = [
      dep_row(deprecation_id: "A", sweep_completed_at: dt("2026-05-05T01:00:00Z"), first_ts: dt("2026-05-05T00:00:00Z"), last_ts: dt("2026-05-05T00:00:00Z")),
      dep_row(deprecation_id: "B", sweep_completed_at: dt("2026-05-05T01:00:00Z"), first_ts: dt("2026-05-05T00:00:00Z"), last_ts: dt("2026-05-05T00:00:00Z"))
    ]

    {alarms, _} = Logic.build_deprecation_batch_alarms(rows, nil)
    fps = Enum.map(alarms, & &1.fingerprint)
    assert Enum.uniq(fps) == fps
  end

  test "deprecation_batch: nil cursor + rows → cursor becomes max last_ts" do
    row = dep_row(deprecation_id: "x", sweep_completed_at: dt("2026-05-05T01:00:00Z"), first_ts: dt("2026-05-04T00:00:00Z"), last_ts: dt("2026-05-04T12:00:00Z"))
    {_, cursor} = Logic.build_deprecation_batch_alarms([row], nil)
    assert cursor == row.last_ts
  end

  # =========================================================================
  # conflict_batch (7 tests)
  # =========================================================================

  test "conflict_batch: empty rows + nil cursor → empty alarms, cursor unchanged" do
    assert {[], nil} = Logic.build_conflict_batch_alarms([], nil)
  end

  test "conflict_batch: single row produces one alarm with parity-shaped fields" do
    row =
      conf_row(
        surface_id: "dialectic:/conflict_test",
        surface_kind: "dialectic",
        event_count: 7,
        first_ts: dt("2026-05-05T08:00:00Z"),
        last_ts: dt("2026-05-05T09:00:00Z")
      )

    {[alarm], cursor} = Logic.build_conflict_batch_alarms([row], nil)

    assert alarm.kind == "conflict_batch"
    assert alarm.severity == "medium"
    assert alarm.summary == "held-by-other conflicts: dialectic:/conflict_test (count=7)"
    # Fingerprint includes last_ts so a later cycle yields a distinct alarm
    assert alarm.fingerprint == "forced_release:conflict_batch:dialectic:/conflict_test:#{DateTime.to_iso8601(row.last_ts)}"
    assert alarm.extra.surface_id == "dialectic:/conflict_test"
    assert alarm.extra.count == 7
    assert cursor == row.last_ts
  end

  test "conflict_batch: fingerprint includes last_ts (later cycle = distinct alarm)" do
    early =
      conf_row(
        surface_id: "dialectic:/x",
        first_ts: dt("2026-05-05T00:00:00Z"),
        last_ts: dt("2026-05-05T01:00:00Z")
      )

    late =
      conf_row(
        surface_id: "dialectic:/x",
        first_ts: dt("2026-05-05T02:00:00Z"),
        last_ts: dt("2026-05-05T03:00:00Z")
      )

    {[a1], _} = Logic.build_conflict_batch_alarms([early], nil)
    {[a2], _} = Logic.build_conflict_batch_alarms([late], nil)
    refute a1.fingerprint == a2.fingerprint,
           "same surface, different cycle = different fingerprint (downstream dedup contract)"
  end

  test "conflict_batch: multiple surfaces produce distinct alarms" do
    rows = [
      conf_row(surface_id: "dialectic:/a", first_ts: dt("2026-05-05T00:00:00Z"), last_ts: dt("2026-05-05T01:00:00Z")),
      conf_row(surface_id: "dialectic:/b", first_ts: dt("2026-05-05T00:00:00Z"), last_ts: dt("2026-05-05T01:00:00Z"))
    ]

    {alarms, _} = Logic.build_conflict_batch_alarms(rows, nil)
    assert length(alarms) == 2
    fps = Enum.map(alarms, & &1.fingerprint)
    assert Enum.uniq(fps) == fps
  end

  test "conflict_batch: cursor advances to max(last_ts)" do
    earlier = dt("2026-05-04T01:00:00Z")
    later = dt("2026-05-05T01:00:00Z")

    rows = [
      conf_row(surface_id: "dialectic:/a", first_ts: dt("2026-05-04T00:00:00Z"), last_ts: earlier),
      conf_row(surface_id: "dialectic:/b", first_ts: dt("2026-05-05T00:00:00Z"), last_ts: later)
    ]

    {_, cursor} = Logic.build_conflict_batch_alarms(rows, nil)
    assert cursor == later
  end

  test "conflict_batch: cursor never regresses below prior" do
    prior = dt("2026-06-01T00:00:00Z")
    row = conf_row(surface_id: "dialectic:/old", first_ts: dt("2026-05-01T00:00:00Z"), last_ts: dt("2026-05-01T01:00:00Z"))
    {_, cursor} = Logic.build_conflict_batch_alarms([row], prior)
    assert cursor == prior
  end

  test "conflict_batch: empty list with prior preserves prior" do
    prior = dt("2026-05-05T12:00:00Z")
    assert {[], ^prior} = Logic.build_conflict_batch_alarms([], prior)
  end

  # =========================================================================
  # build_all_alarms — combined cross-class (7 tests)
  # =========================================================================

  test "build_all_alarms: all empty + nil cursor → empty alarms, nil cursor" do
    assert {[], nil} = Logic.build_all_alarms([], [], [], nil)
  end

  test "build_all_alarms: cursor is max across all three class-cursors and prior" do
    ad_hoc = [ad_hoc_row(event_id: "e1", ts: dt("2026-05-05T01:00:00Z"), surface_id: "dialectic:/a")]
    dep = [dep_row(deprecation_id: "d1", sweep_completed_at: dt("2026-05-05T05:00:00Z"), first_ts: dt("2026-05-05T02:00:00Z"), last_ts: dt("2026-05-05T03:00:00Z"))]
    conf = [conf_row(surface_id: "dialectic:/c", first_ts: dt("2026-05-05T04:00:00Z"), last_ts: dt("2026-05-05T04:30:00Z"))]

    {alarms, cursor} = Logic.build_all_alarms(ad_hoc, dep, conf, nil)
    assert length(alarms) == 3
    # Max of 01:00 (ad_hoc), 03:00 (dep last_ts), 04:30 (conf) → 04:30
    assert cursor == dt("2026-05-05T04:30:00Z")
  end

  test "build_all_alarms: prior cursor wins if it's the max" do
    prior = dt("2026-06-01T00:00:00Z")
    ad_hoc = [ad_hoc_row(event_id: "e", ts: dt("2026-05-05T01:00:00Z"), surface_id: "dialectic:/x")]
    {_alarms, cursor} = Logic.build_all_alarms(ad_hoc, [], [], prior)
    assert cursor == prior
  end

  test "build_all_alarms: combined alarm list contains all three kinds" do
    ad_hoc = [ad_hoc_row(event_id: "e1", ts: dt("2026-05-05T01:00:00Z"), surface_id: "dialectic:/a")]
    dep = [dep_row(deprecation_id: "d1", sweep_completed_at: dt("2026-05-05T05:00:00Z"), first_ts: dt("2026-05-05T02:00:00Z"), last_ts: dt("2026-05-05T03:00:00Z"))]
    conf = [conf_row(surface_id: "dialectic:/c", first_ts: dt("2026-05-05T04:00:00Z"), last_ts: dt("2026-05-05T04:30:00Z"))]

    {alarms, _} = Logic.build_all_alarms(ad_hoc, dep, conf, nil)
    kinds = alarms |> Enum.map(& &1.kind) |> Enum.sort()
    assert kinds == ["ad_hoc", "conflict_batch", "deprecation_batch"]
  end

  test "build_all_alarms: only ad_hoc populated → only ad_hoc alarms" do
    ad_hoc = [ad_hoc_row(event_id: "e", ts: dt("2026-05-05T01:00:00Z"), surface_id: "dialectic:/a")]
    {alarms, cursor} = Logic.build_all_alarms(ad_hoc, [], [], nil)
    assert length(alarms) == 1
    assert hd(alarms).kind == "ad_hoc"
    assert cursor == dt("2026-05-05T01:00:00Z")
  end

  test "build_all_alarms: only deprecation populated → only deprecation alarms" do
    dep = [dep_row(deprecation_id: "d1", sweep_completed_at: dt("2026-05-05T05:00:00Z"), first_ts: dt("2026-05-05T02:00:00Z"), last_ts: dt("2026-05-05T03:00:00Z"))]
    {alarms, _cursor} = Logic.build_all_alarms([], dep, [], nil)
    assert length(alarms) == 1
    assert hd(alarms).kind == "deprecation_batch"
  end

  test "build_all_alarms: only conflict populated → only conflict alarms" do
    conf = [conf_row(surface_id: "dialectic:/c", first_ts: dt("2026-05-05T04:00:00Z"), last_ts: dt("2026-05-05T04:30:00Z"))]
    {alarms, _cursor} = Logic.build_all_alarms([], [], conf, nil)
    assert length(alarms) == 1
    assert hd(alarms).kind == "conflict_batch"
  end
end
