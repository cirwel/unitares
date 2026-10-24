defmodule DialecticLive.TimelineFeedTest do
  # Not async: Req.Test ownership and named processes are per test, but the
  # feed's tasks resolve their stub through $callers, which is simplest to
  # reason about one test at a time.
  use ExUnit.Case, async: false

  import DialecticLive.Test.TimelineHelpers

  # Failure cases log the governance error they provoke.
  @moduletag :capture_log

  alias DialecticLive.TimelineFeed

  @agent "00000000-0000-4000-8000-0000000000a1"

  defp eisv(ts),
    do: %{
      "type" => "eisv_update",
      "agent_id" => @agent,
      "timestamp" => ts,
      "decision" => %{"action" => "proceed"}
    }

  defp paused(ts), do: %{"type" => "lifecycle_paused", "agent_id" => @agent, "timestamp" => ts}

  describe "load" do
    test "zero viewers make zero GETs, whatever arrives" do
      ctx = start_feed(reread_ms: 20)

      broadcast_frame(ctx, eisv(iso()))
      broadcast_frame(ctx, paused(iso()))
      broadcast_status(ctx, :connected)
      _ = :sys.get_state(ctx.feed)

      refute_receive {:timeline_get, _}, 150
    end

    test "N viewers share one GET per window per cycle" do
      ctx = start_feed()
      viewers = for _ <- 1..4, do: viewer(ctx.feed)

      gets = await_reread_gets()
      assert Map.keys(gets) |> Enum.sort() == ["1", "6"]
      refute_receive {:timeline_get, _}, 150

      Enum.each(viewers, &stop_viewer/1)
    end

    test "the 6 h window asks for limit=2000 and every window keeps substrate" do
      ctx = start_feed()
      _ = viewer(ctx.feed)

      gets = await_reread_gets()
      assert gets["6"]["limit"] == "2000"
      assert gets["1"]["limit"] == "500"
      assert gets["1"]["include_substrate"] == "true"
    end

    test "doorbells coalesce into one re-read per window" do
      ctx = start_feed(debounce_ms: 60)
      _ = viewer(ctx.feed)
      _ = await_reread_gets()
      _ = await_applied(ctx, "1h")
      _ = await_applied(ctx, "6h")

      for _ <- 1..6, do: broadcast_frame(ctx, paused(iso()))
      broadcast_frame(ctx, %{"type" => "dialectic_session_resolved", "timestamp" => iso()})

      _ = await_reread_gets()
      refute_receive {:timeline_get, _}, 200
    end

    test "a firehose reconnect re-reads" do
      ctx = start_feed()
      _ = viewer(ctx.feed)
      _ = await_reread_gets()
      _ = await_applied(ctx, "1h")
      _ = await_applied(ctx, "6h")

      broadcast_status(ctx, :connected)
      _ = await_reread_gets()
    end

    test "the floor re-reads while viewed and stops with the last viewer" do
      ctx = start_feed(reread_ms: 80)
      v = viewer(ctx.feed)
      _ = await_reread_gets()
      _ = await_reread_gets()

      stop_viewer(v)
      _ = :sys.get_state(ctx.feed)
      # Drain any re-read that was already in flight when the viewer left.
      receive do
        {:timeline_get, _} -> :ok
      after
        0 -> :ok
      end

      _ = :sys.get_state(ctx.feed)
      broadcast_status(ctx, :connected)
      broadcast_frame(ctx, paused(iso()))
      refute_receive {:timeline_get, _}, 250
    end
  end

  describe "failures keep the last window" do
    setup do
      events = [
        %{"kind" => "outcome", "ts" => iso(-1000), "agent_id" => @agent, "outcome_id" => "o-1"}
      ]

      ctx = start_feed(respond: fn conn -> Req.Test.json(conn, window_body(events)) end)
      _ = viewer(ctx.feed)
      _ = await_reread_gets()
      _ = await_applied(ctx, "1h")
      _ = await_applied(ctx, "6h")
      %{ctx: ctx}
    end

    defp fail_next_with(ctx, respond) do
      serve(ctx.stub, respond)
      broadcast_status(ctx, :connected)
      _ = await_reread_gets()
    end

    test "a 500 sets unavailable", %{ctx: ctx} do
      fail_next_with(ctx, &Plug.Conn.send_resp(&1, 500, "boom"))
      snap = await_applied(ctx, "1h", :unavailable)
      assert [%{"outcome_id" => "o-1"}] = snap.events
      assert snap.error =~ "500"
    end

    test "a transport timeout sets unavailable", %{ctx: ctx} do
      fail_next_with(ctx, &Req.Test.transport_error(&1, :timeout))
      snap = await_applied(ctx, "1h", :unavailable)
      assert [%{"outcome_id" => "o-1"}] = snap.events
    end

    test "a slow answer past the guard sets unavailable and is then ignored", %{ctx: ctx} do
      :sys.replace_state(ctx.feed, &%{&1 | fetch_timeout_ms: 50})

      fail_next_with(ctx, fn conn ->
        Process.sleep(400)
        Req.Test.json(conn, window_body([]))
      end)

      snap = await_applied(ctx, "1h", :unavailable)
      assert [%{"outcome_id" => "o-1"}] = snap.events
      assert snap.error =~ "in time"
      assert %{inflight: inflight} = :sys.get_state(ctx.feed)
      refute Map.has_key?(inflight, "1h")
    end

    test "a body that is not a timeline sets unavailable", %{ctx: ctx} do
      fail_next_with(ctx, &Req.Test.json(&1, %{"success" => false}))
      snap = await_applied(ctx, "1h", :unavailable)
      assert [%{"outcome_id" => "o-1"}] = snap.events
    end

    test "a 401 or 403 sets refused", %{ctx: ctx} do
      fail_next_with(ctx, &Plug.Conn.send_resp(&1, 401, ""))
      assert %{status: :refused} = await_applied(ctx, "1h", :refused)

      fail_next_with(ctx, &Plug.Conn.send_resp(&1, 403, ""))
      assert %{status: :refused, error: error} = await_applied(ctx, "6h", :refused)
      assert error =~ "refused credentials"
    end
  end

  describe "live marks" do
    setup do
      ctx = start_feed()
      Phoenix.PubSub.subscribe(ctx.pubsub, TimelineFeed.topic())
      _ = viewer(ctx.feed)
      _ = await_reread_gets()
      # Consume the first re-read's messages, so later asserts see only their own.
      assert_receive {:timeline_reread, "1h"}, 1_000
      assert_receive {:timeline_reread, "6h"}, 1_000
      %{ctx: ctx}
    end

    test "a frame becomes a provisional mark in both windows, without a GET", %{ctx: ctx} do
      ts = iso()
      broadcast_frame(ctx, eisv(ts))

      assert_receive {:timeline_delta, "1h", %{"kind" => "checkin", "ts" => ^ts}}
      assert_receive {:timeline_delta, "6h", %{"kind" => "checkin"}}
      {:ok, snap} = TimelineFeed.snapshot("1h", ctx.table)
      assert [%{"provisional" => true}] = snap.provisional
      refute_receive {:timeline_get, _}, 100
    end

    test "a re-read containing the row absorbs the mark and flags the row", %{ctx: ctx} do
      frame_ts = iso()
      broadcast_frame(ctx, eisv(frame_ts))
      assert_receive {:timeline_delta, "1h", _}

      row_ts =
        frame_ts
        |> DateTime.from_iso8601()
        |> elem(1)
        |> DateTime.add(-90, :millisecond)
        |> DateTime.to_iso8601()

      row = %{
        "kind" => "checkin",
        "ts" => row_ts,
        "agent_id" => @agent,
        "authored_by" => "agent",
        "action" => "approve"
      }

      serve(ctx.stub, &Req.Test.json(&1, window_body([row])))
      broadcast_status(ctx, :connected)
      _ = await_reread_gets()
      assert_receive {:timeline_reread, "1h"}
      _ = :sys.get_state(ctx.feed)

      {:ok, snap} = TimelineFeed.snapshot("1h", ctx.table)
      assert snap.provisional == []
      assert [%{"seen_live" => true, "action" => "approve"}] = snap.events
    end

    test "a frame that precedes its row survives one miss and expires after two", %{ctx: ctx} do
      broadcast_frame(ctx, paused(iso()))
      assert_receive {:timeline_delta, "1h", _}

      # The lifecycle doorbell re-reads; history does not have the row yet.
      _ = await_reread_gets()
      assert_receive {:timeline_reread, "1h"}
      _ = :sys.get_state(ctx.feed)
      {:ok, snap} = TimelineFeed.snapshot("1h", ctx.table)
      assert [%{"kind" => "lifecycle"}] = snap.provisional

      broadcast_status(ctx, :connected)
      _ = await_reread_gets()
      assert_receive {:timeline_reread, "1h"}
      _ = :sys.get_state(ctx.feed)
      {:ok, snap} = TimelineFeed.snapshot("1h", ctx.table)
      assert snap.provisional == []
    end

    test "a frame already in history adds nothing", %{ctx: ctx} do
      ts = iso(-500)

      row = %{
        "kind" => "lifecycle",
        "ts" => ts,
        "agent_id" => @agent,
        "event_type" => "lifecycle_paused",
        "event_id" => "e"
      }

      serve(ctx.stub, &Req.Test.json(&1, window_body([row])))
      broadcast_status(ctx, :connected)
      _ = await_reread_gets()
      assert_receive {:timeline_reread, "1h"}
      assert_receive {:timeline_reread, "6h"}

      broadcast_frame(ctx, paused(ts))
      refute_receive {:timeline_delta, _, _}, 100
    end
  end

  describe "storage" do
    test "a missing table reads as unavailable" do
      assert TimelineFeed.snapshot("1h", :no_such_timeline_table) == :unavailable
      assert TimelineFeed.snapshot_meta(:no_such_timeline_table) == :unavailable
    end

    test "an unknown window reads as unavailable" do
      ctx = start_feed()
      assert TimelineFeed.snapshot("24h", ctx.table) == :unavailable
    end

    test "a crashed feed's window stays readable through the heir and is reclaimed" do
      events = [
        %{"kind" => "outcome", "ts" => iso(-1000), "agent_id" => @agent, "outcome_id" => "o-1"}
      ]

      ctx = start_feed(respond: fn conn -> Req.Test.json(conn, window_body(events)) end)
      _ = viewer(ctx.feed)
      _ = await_reread_gets()
      _ = await_applied(ctx, "1h")

      ref = Process.monitor(ctx.feed)
      Process.exit(ctx.feed, :kill)
      assert_receive {:DOWN, ^ref, :process, _, :killed}

      assert {:ok, %{events: [%{"outcome_id" => "o-1"}]}} = TimelineFeed.snapshot("1h", ctx.table)

      # The test supervisor restarts the feed, which claims the same table.
      restarted =
        Enum.find_value(1..100, fn _ ->
          case Process.whereis(ctx.name) do
            nil -> Process.sleep(5) && nil
            pid -> pid
          end
        end)

      assert is_pid(restarted) and restarted != ctx.feed
      assert :ets.info(ctx.table, :owner) == restarted
      assert {:ok, %{events: [%{"outcome_id" => "o-1"}]}} = TimelineFeed.snapshot("1h", ctx.table)
    end
  end
end
