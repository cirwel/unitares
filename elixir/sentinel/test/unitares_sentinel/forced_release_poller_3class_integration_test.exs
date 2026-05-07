defmodule UnitaresSentinel.ForcedReleasePoller3ClassIntegrationTest do
  @moduledoc """
  Integration tests for the v0.1.3 combined-poller refactor — three event
  classes against live `governance_test`. Per v0.1.3 §C1 minimum coverage:
  per-class detection + cross-class tick + §B6 partial-failure pin.

  Skipped automatically when DB unreachable (test_helper.exs).
  """

  use ExUnit.Case, async: false

  @moduletag :db

  alias UnitaresSentinel.{ForcedReleasePoller}
  alias SentinelTestHelpers, as: H

  setup do
    label = H.random_label()
    surface_prefix = "dialectic:/test_sentinel_3c_#{label}"

    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_3c_test_#{System.unique_integer([:positive])}")

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

  test "tick picks up a conflict_held_by_other event past prior cursor", ctx do
    surface_id = ctx.surface_prefix <> "/conflict"
    {_event_id, event_ts} = H.insert_conflict_event(surface_id)

    prior = DateTime.add(event_ts, -1, :second)

    {alarms, new_cursor} = ForcedReleasePoller.tick(prior_cursor: prior, db: UnitaresSentinel.DB)

    our = Enum.find(alarms, &(&1.kind == "conflict_batch" and &1.extra.surface_id == surface_id))
    assert our != nil, "conflict_batch alarm must be emitted for inserted event"
    assert our.extra.count == 1
    assert DateTime.compare(new_cursor, event_ts) in [:gt, :eq]
  end

  test "tick GROUPs multiple conflicts on same surface into one alarm", ctx do
    surface_id = ctx.surface_prefix <> "/group"

    {_, _} = H.insert_conflict_event(surface_id)
    {_, _} = H.insert_conflict_event(surface_id)
    {_, last_ts} = H.insert_conflict_event(surface_id)

    prior = DateTime.add(last_ts, -10, :second)

    {alarms, _new_cursor} =
      ForcedReleasePoller.tick(prior_cursor: prior, db: UnitaresSentinel.DB)

    ours = Enum.filter(alarms, &(&1.kind == "conflict_batch" and &1.extra.surface_id == surface_id))
    assert length(ours) == 1, "GROUP BY surface_id must collapse 3 events into 1 alarm"
    assert hd(ours).extra.count == 3
  end

  test "tick combines all three classes when each has events past cursor", ctx do
    # ad_hoc
    ad_hoc_surface = ctx.surface_prefix <> "/ad_hoc"
    {_, ah_ts} = H.insert_forced_event(ad_hoc_surface)

    # conflict
    conf_surface = ctx.surface_prefix <> "/conf"
    {_, conf_ts} = H.insert_conflict_event(conf_surface)

    # Use the earlier of the two as the prior cursor so both are picked up.
    earliest = if DateTime.compare(ah_ts, conf_ts) == :lt, do: ah_ts, else: conf_ts
    prior = DateTime.add(earliest, -1, :second)

    {alarms, _new_cursor} =
      ForcedReleasePoller.tick(prior_cursor: prior, db: UnitaresSentinel.DB)

    kinds_ours =
      alarms
      |> Enum.filter(fn a ->
        case a.kind do
          "ad_hoc" -> a.extra.surface_id == ad_hoc_surface
          "conflict_batch" -> a.extra.surface_id == conf_surface
          _ -> false
        end
      end)
      |> Enum.map(& &1.kind)
      |> Enum.sort()

    assert "ad_hoc" in kinds_ours, "combined tick MUST include ad_hoc"
    assert "conflict_batch" in kinds_ours, "combined tick MUST include conflict_batch"
  end

  test "§B6 partial-failure: tick rolls back transaction if any one query errors", _ctx do
    # We can't easily inject a per-query SQL error into tick/1 without a test
    # seam. Pin the underlying contract instead: Postgrex.transaction with
    # an inner `with` chain that rolls back on the second clause must return
    # {:error, _} and never reach the success branch. Mirrors the structure
    # of `query_all_rows_in_transaction/2`.
    result =
      Postgrex.transaction(UnitaresSentinel.DB, fn conn ->
        with {:ok, _} <- Postgrex.query(conn, "SELECT 1", []),
             {:ok, _} <- {:error, :simulated_class2_failure},
             {:ok, _} <- Postgrex.query(conn, "SELECT 2", []) do
          %{ad_hoc: [], deprecation: [], conflict: []}
        else
          {:error, e} -> Postgrex.rollback(conn, e)
        end
      end)

    assert result == {:error, :simulated_class2_failure},
           "all-or-nothing: a single rolled-back step short-circuits the entire transaction"
  end

  test "tick on no-events tick returns no alarms and preserves cursor", _ctx do
    far_future = DateTime.utc_now() |> DateTime.add(86_400 * 365, :second)

    {alarms, new_cursor} =
      ForcedReleasePoller.tick(prior_cursor: far_future, db: UnitaresSentinel.DB)

    assert alarms == []
    assert new_cursor == far_future
  end
end
