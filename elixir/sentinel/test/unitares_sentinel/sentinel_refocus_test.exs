defmodule UnitaresSentinel.SentinelRefocusTest do
  @moduledoc """
  The two demotions from the 2026-09-24 resident audit: per-cycle
  held-by-other alarms now back off per surface, and entropy_outlier /
  correlated_events stop arriving as findings while still feeding the
  cycle check-in.
  """

  use ExUnit.Case, async: true

  alias UnitaresSentinel.{FleetFindingEmitter, ReAlert}
  alias UnitaresSentinel.ForcedReleasePoller.Logic

  @min 60 * 1_000

  defp dt(iso) do
    {:ok, dt, _} = DateTime.from_iso8601(iso)
    dt
  end

  defp conflict_alarms(surface_id, minute) do
    ts = DateTime.add(dt("2026-09-01T00:00:00Z"), minute * 60, :second)

    {alarms, _} =
      Logic.build_conflict_batch_alarms(
        [
          %{
            surface_id: surface_id,
            surface_kind: "resident",
            event_count: 4,
            first_ts: ts,
            last_ts: ts
          }
        ],
        nil
      )

    alarms
  end

  describe "conflict_batch throttle" do
    test "a surface contended every 30s cycle pages at 0, 60 and 180 minutes over 3 hours" do
      {emitted_at, _} =
        Enum.reduce(0..(180 * 2), {[], ReAlert.new()}, fn half_minute, {acc, state} ->
          minute = div(half_minute, 2)
          alarms = conflict_alarms("resident:/busy", minute)
          {kept, state} = Logic.throttle_conflicts(alarms, state, half_minute * 30_000)
          {if(kept == [], do: acc, else: [minute | acc]), state}
        end)

      assert Enum.reverse(emitted_at) == [0, 60, 180]
    end

    test "an emitted repeat says how many cycles it stands for; fingerprint shape unchanged" do
      {[first], state} = Logic.throttle_conflicts(conflict_alarms("s", 0), ReAlert.new(), 0)
      refute Map.has_key?(first.extra, :suppressed_cycles_since_last)

      state =
        Enum.reduce(1..3, state, fn i, st ->
          {[], st} = Logic.throttle_conflicts(conflict_alarms("s", i), st, i * @min)
          st
        end)

      {[again], _} = Logic.throttle_conflicts(conflict_alarms("s", 60), state, 60 * @min)
      assert again.extra.suppressed_cycles_since_last == 3
      assert again.summary =~ "+3 contended cycles"
      assert String.starts_with?(again.fingerprint, "forced_release:conflict_batch:s:")
    end

    test "delivery order: severity first, this tick ahead of the backlog" do
      alarm = fn id, severity -> %{kind: "k", severity: severity, summary: id, fingerprint: id, extra: %{}} end
      backlog = for i <- 1..200, do: alarm.("old-medium-#{i}", "medium")
      fresh = [alarm.("new-medium", "medium"), alarm.("new-high", "high")]

      ordered = Logic.order_for_delivery(fresh, backlog)
      assert Enum.map(Enum.take(ordered, 2), & &1.summary) == ["new-high", "new-medium"]
      # Capping the queue at 200 evicts old backlog, never the fresh alarms.
      kept = ordered |> Enum.take(200) |> Enum.map(& &1.summary)
      assert "new-high" in kept and "new-medium" in kept
      refute "old-medium-200" in kept
    end

    test "surfaces are independent, and other alarm classes pass untouched" do
      ad_hoc = %{
        kind: "ad_hoc",
        severity: "high",
        summary: "forced release",
        fingerprint: "forced_release:ad_hoc:e1",
        extra: %{surface_id: "s"}
      }

      {[_], state} = Logic.throttle_conflicts(conflict_alarms("s", 0), ReAlert.new(), 0)
      {kept, _} = Logic.throttle_conflicts([ad_hoc | conflict_alarms("t", 1) ++ conflict_alarms("s", 1)], state, @min)

      assert Enum.map(kept, & &1.kind) == ["ad_hoc", "conflict_batch"]
      assert Enum.at(kept, 1).extra.surface_id == "t"
    end
  end

  describe "retry queue cap" do
    alias UnitaresSentinel.Findings

    test "a full queue evicts the oldest, never the fresh failure at the tail" do
      old = for i <- 1..200, do: %{id: "old-#{i}", severity: "high", queued_ms: i}
      fresh = %{id: "fresh", severity: "high", queued_ms: 1_000}

      capped = Findings.cap_queue(old ++ [fresh], 200)
      assert length(capped) == 200
      assert List.last(capped).id == "fresh"
      refute Enum.any?(capped, &(&1.id == "old-1"))
      # Order is otherwise untouched.
      assert hd(capped).id == "old-2"
    end

    test "fresh findings beyond the per-tick cap are stamped and survive a full queue" do
      down = [findings_opts: [http_post: fn _, _, _, _ -> {:error, :down} end]]
      # Stamps are BEAM monotonic milliseconds (often negative); queue the old
      # ones a minute ago on that same clock.
      t0 = System.monotonic_time(:millisecond) - 60_000

      old =
        for i <- 1..200,
            do: %{type: "t", severity: "high", summary: "old-#{i}", queued_ms: t0 + i}

      fresh = for i <- 1..6, do: %{type: "t", severity: "high", summary: "fresh-#{i}"}

      {[], undelivered} = Findings.deliver_bounded(fresh ++ old, 5, down, "test")
      capped = Findings.cap_queue(undelivered, 200)
      kept = MapSet.new(capped, & &1.summary)

      for i <- 1..6, do: assert("fresh-#{i}" in kept)
      refute "old-1" in kept
    end

    test "an unstamped item counts as newest whatever the clock's sign" do
      old = for i <- 1..3, do: %{id: "old-#{i}", severity: "high", queued_ms: 1_000_000 + i}
      assert [_, _, %{id: "new"}] = Findings.cap_queue(old ++ [%{id: "new", severity: "high"}], 3)
    end

    test "lower severity goes first, whatever its age" do
      queue = [
        %{id: "old-high", severity: "high", queued_ms: 1},
        %{id: "new-medium", severity: "medium", queued_ms: 9}
      ]

      assert [%{id: "old-high"}] = Findings.cap_queue(queue, 1)
    end

    test "deliver_bounded stamps a failure once and keeps the stamp on retry" do
      down = [findings_opts: [http_post: fn _, _, _, _ -> {:error, :down} end]]
      f = %{type: "t", severity: "high", summary: "s"}
      {[], [stamped]} = Findings.deliver_bounded([f], 5, down, "test")
      assert is_integer(stamped.queued_ms)
      {[], [again]} = Findings.deliver_bounded([stamped], 5, down, "test")
      assert again.queued_ms == stamped.queued_ms
    end
  end

  describe "log-only fleet finding types" do
    defp finding(type, severity),
      do: %{type: type, violation_class: "ENT", severity: severity, summary: "#{type} summary"}

    test "entropy_outlier and correlated_events are not posted but still reach the check-in" do
      parent = self()

      http_post = fn _url, body, _headers, _timeout ->
        send(parent, {:posted, body["finding_type"]})
        {:ok, 200, ~s({"success":true})}
      end

      result =
        FleetFindingEmitter.tick(
          snapshot: %{agents: %{}, events: []},
          analysis_fun: fn _, _ ->
            [
              finding("entropy_outlier", "medium"),
              finding("correlated_events", "medium"),
              finding("coordinated_degradation", "high")
            ]
          end,
          self_agent_id: "sentinel-test",
          findings_opts: [http_post: http_post]
        )

      assert result.posted_count == 1
      assert_receive {:posted, "coordinated_degradation"}
      refute_receive {:posted, _}

      assert length(result.fleet_findings) == 3
      assert result.checkin.response_text =~ "entropy_outlier summary"
      assert result.checkin.response_text =~ "correlated_events summary"
    end

    test "the log-only set is configurable, and empty restores posting" do
      parent = self()

      http_post = fn _url, body, _headers, _timeout ->
        send(parent, {:posted, body["finding_type"]})
        {:ok, 200, ~s({"success":true})}
      end

      result =
        FleetFindingEmitter.tick(
          snapshot: %{agents: %{}, events: []},
          analysis_fun: fn _, _ -> [finding("entropy_outlier", "medium")] end,
          self_agent_id: "sentinel-test",
          log_only_finding_types: [],
          findings_opts: [http_post: http_post]
        )

      assert result.posted_count == 1
      assert_receive {:posted, "entropy_outlier"}
    end
  end
end
