defmodule UnitaresSentinel.AuditVolumeWatchTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.AuditVolumeWatch
  alias UnitaresSentinel.AuditVolumeWatch.Logic

  @min 60 * 1_000
  @storm_key "agent-storm-0000-0000"

  defp row(event_type, source, recent, prior),
    do: %{event_type: event_type, source: source, recent: recent, prior: prior}

  describe "Logic.evaluate/2" do
    test "a runaway writer trips the absolute rule as high" do
      assert [a] = Logic.evaluate([row("session_resolve_miss_observed", @storm_key, 5_850, 0)])
      assert a.rule == :absolute
      assert a.severity == "high"
    end

    test "a new loud source trips the relative rule as medium" do
      assert [a] = Logic.evaluate([row("x", "new-src", 350, 0)])
      assert a.rule == :relative
      assert a.severity == "medium"
    end

    # The largest legitimate per-source hour in the 30-day calibration window:
    # 484 rows against a steady ~326/h.
    test "the busiest legitimate source does not fire" do
      assert Logic.evaluate([row("cross_device_call", "orchestrator", 484, 326 * 23)]) == []
    end

    test "quiet sources and an established steady source do not fire" do
      rows = [row("knowledge_read", "a", 174, 15 * 23), row("bridge.delivery", "b", 97, 12 * 23)]
      assert Logic.evaluate(rows) == []
    end

    test "Postgres numeric sums are accepted" do
      assert [_] = Logic.evaluate([row("x", "s", Decimal.new(2_000), Decimal.new(0))])
    end

    test "the finding keys on event type and source, not on the counts" do
      a1 = hd(Logic.evaluate([row("t", "s", 1_500, 0)]))
      a2 = hd(Logic.evaluate([row("t", "s", 9_000, 100)]))
      assert Logic.to_finding(a1).fingerprint_extra == Logic.to_finding(a2).fingerprint_extra
    end
  end

  describe "tick/1" do
    defp tick_at(now_ms, rows, realert) do
      parent = self()

      http_post = fn _url, body, _headers, _timeout ->
        send(parent, {:posted, body})
        {:ok, 200, ~s({"success":true})}
      end

      AuditVolumeWatch.tick(
        now_ms: now_ms,
        realert: realert,
        query_fun: fn _floor -> {:ok, rows} end,
        findings_opts: [agent_id: "sentinel-test", http_post: http_post]
      )
    end

    test "posts a sentinel_finding with a change_token, then backs off" do
      rows = [row("session_resolve_miss_observed", @storm_key, 5_850, 0)]
      {[_], realert, []} = tick_at(0, rows, UnitaresSentinel.ReAlert.new())

      assert_receive {:posted, body}
      assert body["type"] == "sentinel_finding"
      assert body["finding_type"] == "audit_volume_anomaly"
      assert body["severity"] == "high"
      assert body["source"] == @storm_key
      assert is_binary(body["change_token"])

      # Still storming 5 and 55 minutes later: suppressed.
      {[], realert, []} = tick_at(5 * @min, rows, realert)
      {[], realert, []} = tick_at(55 * @min, rows, realert)
      refute_receive {:posted, _}

      # An hour on: re-alert, carrying the repeats it stands for.
      {[_], _, []} = tick_at(60 * @min, rows, realert)
      assert_receive {:posted, again}
      assert again["fingerprint"] == body["fingerprint"]
      assert again["change_token"] != body["change_token"]
      assert again["suppressed_since_last"] == 2
    end

    test "an undelivered alert is queued and resent even after the spike subsides" do
      parent = self()
      storm = [row("session_resolve_miss_observed", @storm_key, 5_850, 0)]

      down = fn _url, _body, _headers, _timeout -> {:error, :econnrefused} end

      up = fn _url, body, _headers, _timeout ->
        send(parent, {:posted, body})
        {:ok, 200, ~s({"success":true})}
      end

      run = fn now_ms, rows, realert, pending, http_post ->
        AuditVolumeWatch.tick(
          now_ms: now_ms,
          realert: realert,
          pending: pending,
          query_fun: fn _ -> {:ok, rows} end,
          findings_opts: [http_post: http_post]
        )
      end

      {[], realert, [queued]} = run.(0, storm, UnitaresSentinel.ReAlert.new(), [], down)
      # Still down: stays queued, and the backoff does not emit a duplicate.
      {[], realert, [^queued]} = run.(5 * @min, storm, realert, [queued], down)
      # Governance back, spike gone: the queued alert is delivered anyway.
      {[^queued], _, []} = run.(10 * @min, [], realert, [queued], up)
      assert_receive {:posted, %{"severity" => "high", "source" => @storm_key}}
      refute_receive {:posted, _}
    end

    test "a long queue and a stalling endpoint cannot starve the check" do
      parent = self()
      calls = :counters.new(1, [])

      stalling = fn _url, _body, _headers, _timeout ->
        :counters.add(calls, 1, 1)
        {:error, :timeout}
      end

      queue =
        for i <- 1..7,
            do: Logic.to_finding(hd(Logic.evaluate([row("t#{i}", "s", 2_000, 0)])))

      {[], _realert, pending} =
        AuditVolumeWatch.tick(
          now_ms: 0,
          pending: queue,
          query_fun: fn _ ->
            send(parent, :queried)
            {:ok, [row("fresh", "s", 2_000, 0)]}
          end,
          findings_opts: [http_post: stalling]
        )

      assert_received :queried
      # 5 POST attempts at most per tick, the fresh finding first; the three
      # not attempted move ahead of the five that just failed.
      assert :counters.get(calls, 1) == 5
      assert length(pending) == 8
      assert Enum.map(pending, & &1.extra.event_type_observed) ==
               ["t5", "t6", "t7", "fresh", "t1", "t2", "t3", "t4"]
    end

    test "a deduped response counts as delivered" do
      deduped = fn _url, _body, _headers, _timeout ->
        {:ok, 200, ~s({"success":true,"deduped":true})}
      end

      {[_], realert, []} =
        AuditVolumeWatch.tick(
          now_ms: 0,
          query_fun: fn _ -> {:ok, [row("t", "s", 2_000, 0)]} end,
          findings_opts: [http_post: deduped]
        )

      assert map_size(realert) == 1
    end

    test "a query failure posts nothing and keeps state" do
      {posted, state, []} =
        AuditVolumeWatch.tick(
          query_fun: fn _ -> {:error, :down} end,
          realert: %{},
          findings_opts: [http_post: fn _, _, _, _ -> flunk("posted") end]
        )

      assert posted == []
      assert state == %{}
    end
  end

  # Replay of the 2026-09-16 storm's shape: one session key, 50 rows every
  # 30 seconds, onset at t=0, nothing before it. Ticks every 5 minutes with
  # arbitrary phase. The check must page within the first half hour.
  test "replay: the 09-16 storm shape pages high within 15 minutes of onset" do
    # rows written in the trailing hour at minute m (onset at minute 0)
    recent_at = fn m -> if m <= 0, do: 0, else: min(m, 60) * 100 end

    parent = self()

    http_post = fn _url, body, _headers, _timeout ->
      send(parent, {:posted, body["severity"]})
      {:ok, 200, ~s({"success":true})}
    end

    first_by_severity =
      Enum.reduce(Enum.map(0..12, &(&1 * 5 - 2)), {%{}, %{}}, fn m, {first, realert} ->
        rows = [row("session_resolve_miss_observed", @storm_key, recent_at.(m), 0)]

        {posted, realert, _pending} =
          AuditVolumeWatch.tick(
            now_ms: m * @min,
            realert: realert,
            query_fun: fn floor ->
              {:ok, Enum.filter(rows, &(&1.recent >= floor))}
            end,
            findings_opts: [http_post: http_post]
          )

        first =
          Enum.reduce(posted, first, fn f, acc -> Map.put_new(acc, f.severity, m) end)

        {first, realert}
      end)
      |> elem(0)

    # 300 rows by minute 3 → medium on the first tick after; 1000 by minute 10 → high.
    assert first_by_severity["medium"] <= 8
    assert first_by_severity["high"] <= 15
  end

  @tag :db
  test "the real query weights throttled rows and places flushes at suppressed_last_at" do
    db = UnitaresSentinel.DB
    key = "volume-watch-test-#{System.unique_integer([:positive])}"

    result =
      Postgrex.transaction(db, fn conn ->
        Postgrex.query!(
          conn,
          """
          INSERT INTO audit.events (ts, agent_id, session_id, event_type, payload)
          SELECT now(), NULL, $1, 'volume_watch_probe', jsonb_build_object('suppressed_since_last', 99)
          FROM generate_series(1, 11)
          """,
          [key]
        )

        # A throttle_flush row stands for no miss of its own; its suppressed
        # count belongs at suppressed_last_at (naive local time, as the writer
        # stamps it), not at the row's own timestamp. One flush of old misses
        # (3h ago) must land in `prior`; one of recent misses in `recent`.
        Postgrex.query!(
          conn,
          """
          INSERT INTO audit.events (ts, agent_id, session_id, event_type, payload)
          VALUES
            (now(), NULL, $1, 'volume_watch_probe',
             jsonb_build_object('suppressed_since_last', 5000,
                                'resolution_source', 'throttle_flush',
                                'suppressed_last_at',
                                to_char(now() - interval '3 hours', 'YYYY-MM-DD"T"HH24:MI:SS'))),
            (now(), NULL, $1, 'volume_watch_probe',
             jsonb_build_object('suppressed_since_last', 700,
                                'resolution_source', 'throttle_flush',
                                'suppressed_last_at',
                                to_char(now() - interval '10 minutes', 'YYYY-MM-DD"T"HH24:MI:SS'))),
            (now(), NULL, $1, 'volume_watch_probe',
             jsonb_build_object('suppressed_since_last', 50,
                                'resolution_source', 'throttle_flush',
                                'suppressed_last_at', '2026-13-45T99:00:00')),
            (now(), NULL, $1, 'volume_watch_probe',
             jsonb_build_object('suppressed_since_last', 25,
                                'resolution_source', 'throttle_flush',
                                'suppressed_last_at',
                                to_char((now() - interval '10 minutes') AT TIME ZONE 'UTC',
                                        'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')))
          """,
          [key]
        )

        {:ok, rows} = AuditVolumeWatch.query_rows(conn, 1_000)
        Postgrex.rollback(conn, rows)
      end)

    assert {:error, rows} = result
    assert %{recent: recent, prior: prior} = Enum.find(rows, &(&1.source == key))
    # 11 rows, each standing for itself plus 99 suppressed misses, plus the
    # recent flush's 700, plus the malformed flush's 50 placed at its own ts
    # (the query must not abort on it), plus the offset-stamped flush's 25
    # (a UTC rendering must land at the same instant in any session zone);
    # the old flush's 5000 is prior traffic.
    assert Decimal.to_integer(recent) == 1_875
    assert Decimal.to_integer(prior) == 5_000
  end
end
