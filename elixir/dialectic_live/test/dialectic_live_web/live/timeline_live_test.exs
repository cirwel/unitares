defmodule DialecticLiveWeb.TimelineLiveTest do
  # Not async: the LiveView finds its feed through application config.
  use DialecticLiveWeb.ConnCase, async: false

  import Phoenix.LiveViewTest
  import DialecticLive.Test.TimelineHelpers

  alias DialecticLive.TimelineFeed

  @moduletag :capture_log

  @a "00000000-0000-4000-8000-0000000000a1"
  @b "00000000-0000-4000-8000-0000000000b2"

  defp use_feed(ctx) do
    previous = Application.get_env(:dialectic_live, :timeline, [])

    Application.put_env(
      :dialectic_live,
      :timeline,
      Keyword.merge(previous, feed: ctx.name, table: ctx.table, pubsub: ctx.pubsub)
    )

    on_exit(fn -> Application.put_env(:dialectic_live, :timeline, previous) end)
    Phoenix.PubSub.subscribe(ctx.pubsub, TimelineFeed.topic())
    ctx
  end

  # Mount, then wait until the first re-read has reached the LiveView.
  defp mount_live(conn, ctx) do
    {:ok, view, _html} = live(conn, ~p"/timeline")
    _ = await_reread_gets()
    await_reread_delivered(ctx, view)
    view
  end

  defp await_reread_delivered(ctx, view) do
    assert_receive {kind, "1h"} when kind in [:timeline_reread, :timeline_status], 1_000
    _ = :sys.get_state(ctx.feed)
    _ = render(view)
    :ok
  end

  defp nodes(view, selector) do
    view
    |> render()
    |> LazyHTML.from_fragment()
    |> LazyHTML.query(selector)
    |> Enum.to_list()
  end

  # The agent lanes in DOM order, as agent ids.
  defp lane_order(view) do
    view
    |> nodes("#timeline-lanes > .tl-row:not(.tl-axis):not(.tl-group)")
    |> Enum.map(fn node -> node |> LazyHTML.attribute("id") |> hd() end)
    |> Enum.map(&String.replace_prefix(&1, "agent-", ""))
  end

  # The marks on one lane, as {data-kind, --t seconds}, in DOM order.
  defp lane_marks(view, id) do
    view
    |> nodes("#agent-#{id} .tl-mk")
    |> Enum.map(fn node ->
      [kind] = LazyHTML.attribute(node, "data-kind")
      [style] = LazyHTML.attribute(node, "style")
      [_, t] = Regex.run(~r/--t:(-?[\d.]+)/, style)
      {kind, String.to_float(t)}
    end)
  end

  defp feed_with(events, agents \\ [], extra \\ %{}) do
    start_feed(respond: fn conn -> Req.Test.json(conn, window_body(events, agents, extra)) end)
    |> use_feed()
  end

  defp agent(id, label, model_type \\ nil, extra \\ %{}) do
    Map.merge(
      %{
        "agent_id" => id,
        "label" => label,
        "model_type" => model_type,
        "last_activity_at" => iso(-60_000)
      },
      extra
    )
  end

  test "the dead render serves the pane before the socket connects", %{conn: conn} do
    _ = feed_with([])
    html = conn |> get(~p"/timeline") |> html_response(200)
    assert html =~ "Timeline — live"
    assert html =~ "connecting"
  end

  test "a lost page socket is announced client-side, since the server cannot say so" do
    # The header text is rendered by the server, so it cannot change once the
    # socket is gone; the phx-disconnected binding shows the warning instead.
    ctx = feed_with([])
    {:ok, view, _} = live(build_conn(), ~p"/timeline")
    _ = ctx
    assert has_element?(view, "#socket-lost.hidden")
    assert has_element?(view, "#socket-status[phx-disconnected]")
    assert has_element?(view, "#socket-status[phx-connected]")
  end

  test "odd field types render as absent instead of crashing the pane", %{conn: conn} do
    events = [
      %{
        "kind" => "outcome",
        "ts" => iso(-1_000),
        "agent_id" => @a,
        "outcome_id" => "o-9",
        "outcome_type" => %{"nested" => true},
        "outcome_score" => [1],
        "is_bad" => "yes"
      },
      %{
        "kind" => "lineage",
        "ts" => iso(-2_000),
        "agent_id" => @a,
        "event_id" => "l-1",
        "parent_agent_id" => 42,
        "successor_class" => %{}
      }
    ]

    ctx =
      feed_with(events, [%{"agent_id" => @a, "label" => %{"x" => 1}, "last_activity_at" => 5}])

    view = mount_live(conn, ctx)
    assert has_element?(view, "#agent-#{@a} .tl-mk[data-kind=outcome]")
    assert has_element?(view, "#agent-#{@a} .tl-mk[data-kind=lineage]")

    # The detail panel takes the same odd record without raising.
    [id] =
      view |> nodes("#agent-#{@a} .tl-mk[data-kind=outcome]") |> hd() |> LazyHTML.attribute("id")

    view |> element("##{id}") |> render_click()
    assert has_element?(view, "#mark-detail", "Outcome · passed")
  end

  test "an empty window renders its empty states", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    assert has_element?(view, "#lanes-empty")
    assert has_element?(view, "#socket-status", "connected")
    assert has_element?(view, "#source-status", "ok")
    assert has_element?(view, "#mark-detail", "Select a mark")
    refute has_element?(view, "#truncation-banner")
  end

  test "an unavailable source keeps the pane up and says so", %{conn: conn} do
    ctx = start_feed(respond: &Plug.Conn.send_resp(&1, 500, "boom")) |> use_feed()
    view = mount_live(conn, ctx)

    assert has_element?(view, "#source-status", "unavailable")
    assert has_element?(view, "#source-status", "HTTP 500")
  end

  test "a refused source says refused", %{conn: conn} do
    ctx = start_feed(respond: &Plug.Conn.send_resp(&1, 401, "")) |> use_feed()
    view = mount_live(conn, ctx)
    assert has_element?(view, "#source-status", "refused")
  end

  test "with no feed running the pane renders unavailable", %{conn: conn} do
    previous = Application.get_env(:dialectic_live, :timeline, [])

    Application.put_env(
      :dialectic_live,
      :timeline,
      Keyword.merge(previous, feed: :no_such_feed, table: :no_such_table)
    )

    on_exit(fn -> Application.put_env(:dialectic_live, :timeline, previous) end)

    {:ok, view, _} = live(conn, ~p"/timeline")
    assert has_element?(view, "#feed-unavailable")
    assert has_element?(view, "#lanes-empty")
  end

  test "history marks sit on their agent's lane in time order, with the truncation banner",
       %{conn: conn} do
    events = [
      %{
        "kind" => "checkin",
        "ts" => iso(-60_000),
        "agent_id" => @a,
        "authored_by" => "agent",
        "action" => "guide"
      },
      %{
        "kind" => "outcome",
        "ts" => iso(-120_000),
        "agent_id" => @a,
        "outcome_id" => "o-1",
        "outcome_type" => "test_failed",
        "is_bad" => true
      }
    ]

    ctx = feed_with(events, [agent(@a, "worker one")], %{"truncated" => ["checkin"]})
    view = mount_live(conn, ctx)

    assert has_element?(view, "#truncation-banner", "checkin")
    assert has_element?(view, "#agent-#{@a} .tl-name", "worker one")
    assert has_element?(view, "#agent-#{@a} .tl-mk.checkin.guide[title*='assessment: guide']")
    assert has_element?(view, "#agent-#{@a} .tl-mk.outcome.bad", "✕")

    assert [{"outcome", t_outcome}, {"checkin", t_checkin}] = lane_marks(view, @a)
    assert_in_delta t_checkin - t_outcome, 60.0, 0.2
    assert has_element?(view, "#timeline-lanes[style*='--span:3600']")
  end

  test "lanes order by most recent event, with a subagent nested under its parent",
       %{conn: conn} do
    c = "00000000-0000-4000-8000-0000000000c3"

    agents = [
      agent(@a, "older parent"),
      agent(@b, "busy child", nil, %{"parent_agent_id" => @a, "spawn_reason" => "subagent"}),
      agent(c, "newest loner")
    ]

    events = [
      %{"kind" => "outcome", "ts" => iso(-50_000), "agent_id" => @a, "outcome_id" => "1"},
      %{"kind" => "outcome", "ts" => iso(-20_000), "agent_id" => @b, "outcome_id" => "2"},
      %{"kind" => "outcome", "ts" => iso(-30_000), "agent_id" => c, "outcome_id" => "3"},
      %{
        "kind" => "lineage",
        "ts" => iso(-40_000),
        "agent_id" => @b,
        "event_id" => "l",
        "parent_agent_id" => @a
      }
    ]

    ctx = feed_with(events, agents)
    view = mount_live(conn, ctx)

    # The family sorts by its busiest member, so the parent leads over c.
    assert lane_order(view) == [@a, @b, c]
    assert has_element?(view, "#agent-#{@b}.child", "subagent of older parent")
    # The lineage mark is drawn on the parent's lane.
    assert has_element?(view, "#agent-#{@a} .tl-mk.lineage[title*='Started a subagent']")
    refute has_element?(view, "#agent-#{@b} .tl-mk.lineage")
  end

  test "a pause and its resume draw a band; an unresumed pause runs to now", %{conn: conn} do
    events = [
      %{
        "kind" => "lifecycle",
        "ts" => iso(-50_000),
        "agent_id" => @a,
        "event_type" => "lifecycle_paused",
        "event_id" => "p1"
      },
      %{
        "kind" => "lifecycle",
        "ts" => iso(-40_000),
        "agent_id" => @a,
        "event_type" => "lifecycle_resumed",
        "event_id" => "r1"
      },
      %{
        "kind" => "lifecycle",
        "ts" => iso(-10_000),
        "agent_id" => @b,
        "event_type" => "lifecycle_paused",
        "event_id" => "p2"
      }
    ]

    ctx = feed_with(events)
    view = mount_live(conn, ctx)

    assert [closed] = nodes(view, "#agent-#{@a} .tl-band.pause")
    assert [style] = LazyHTML.attribute(closed, "style")
    assert style =~ "--t2:"
    assert [open] = nodes(view, "#agent-#{@b} .tl-band.pause")
    refute open |> LazyHTML.attribute("style") |> hd() =~ "--t2:"
  end

  test "selecting a check-in shows its record; selecting it again clears it", %{conn: conn} do
    events = [
      %{
        "kind" => "checkin",
        "ts" => iso(-5_000),
        "agent_id" => @a,
        "authored_by" => "agent",
        "action" => "proceed",
        "report_excerpt" => "shipped the lanes",
        "confidence_reported" => 0.8
      }
    ]

    ctx = feed_with(events, [agent(@a, "worker one")])
    view = mount_live(conn, ctx)

    [id] = view |> nodes("#agent-#{@a} .tl-mk") |> hd() |> LazyHTML.attribute("id")
    view |> element("##{id}") |> render_click()

    assert has_element?(view, "##{id}.sel")
    assert has_element?(view, "#mark-detail", "shipped the lanes")
    assert has_element?(view, "#mark-detail", "0.80")
    assert has_element?(view, "#mark-detail", "The agent (sync_state)")

    view |> element("##{id}") |> render_click()
    assert has_element?(view, "#mark-detail", "Select a mark")
  end

  test "a live delta renders without a refetch", %{conn: conn} do
    ctx = feed_with([], [agent(@a, "worker one")])
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{
      "type" => "knowledge_write",
      "agent_id" => @a,
      "timestamp" => iso(),
      "discovery_id" => "d-1",
      "discovery_type" => "note",
      "summary" => "a fresh note"
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert has_element?(view, "#agent-#{@a} .tl-mk.knowledge[data-provisional=true]")
    assert has_element?(view, "#push-latency", "median")
    refute_receive {:timeline_get, _}, 100
  end

  test "an agent first seen live joins at the top", %{conn: conn} do
    events = [%{"kind" => "outcome", "ts" => iso(-1_000), "agent_id" => @a, "outcome_id" => "o"}]
    ctx = feed_with(events)
    view = mount_live(conn, ctx)
    assert lane_order(view) == [@a]

    broadcast_frame(ctx, %{
      "type" => "lifecycle_paused",
      "agent_id" => @b,
      "timestamp" => iso(-30_000),
      "reason" => "delayed_frame"
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert lane_order(view) == [@b, @a]
    assert has_element?(view, "#agent-#{@b} .tl-mk.lifecycle[data-provisional=true]")
  end

  test "a delta the pane already holds is drawn once", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    mark = %{
      "kind" => "lifecycle",
      "ts" => iso(-500),
      "agent_id" => @b,
      "event_type" => "lifecycle_paused",
      "reason" => "already_shown",
      "provisional" => true
    }

    send(view.pid, {:timeline_delta, "1h", mark})
    send(view.pid, {:timeline_delta, "1h", mark})

    assert [_one] = nodes(view, "#agent-#{@b} .tl-mk[data-kind=lifecycle]")
  end

  test "a delta from before the window is not drawn, but its agent was seen", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    send(
      view.pid,
      {:timeline_delta, "1h",
       %{
         "kind" => "lifecycle",
         "ts" => iso(-2 * 3_600_000),
         "agent_id" => @b,
         "event_type" => "lifecycle_paused",
         "provisional" => true
       }}
    )

    _ = render(view)
    refute has_element?(view, "#agent-#{@b}")
    assert Map.has_key?(:sys.get_state(view.pid).socket.assigns.last_seen, @b)
  end

  # Polls the rendered view until `check` holds (a re-read lands
  # asynchronously after its doorbell).
  defp eventually(check, tries \\ 50) do
    cond do
      check.() -> true
      tries == 0 -> false
      true -> Process.sleep(20) && eventually(check, tries - 1)
    end
  end

  test "a selection on a live mark follows it when the re-read confirms it", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    ts = iso(-1_000)

    broadcast_frame(ctx, %{
      "type" => "knowledge_write",
      "agent_id" => @a,
      "timestamp" => ts,
      "discovery_id" => "d-7",
      "discovery_type" => "note",
      "summary" => "confirmed note"
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)
    [live_id] = view |> nodes("#agent-#{@a} .tl-mk.knowledge") |> hd() |> LazyHTML.attribute("id")
    assert String.starts_with?(live_id, "p-")
    view |> element("##{live_id}") |> render_click()

    row = %{
      "kind" => "knowledge",
      "ts" => ts,
      "agent_id" => @a,
      "event_id" => "k-7",
      "discovery_id" => "d-7",
      "discovery_type" => "note",
      "summary" => "confirmed note"
    }

    serve(ctx.stub, fn conn -> Req.Test.json(conn, window_body([row])) end)
    broadcast_frame(ctx, %{"type" => "dialectic_session_resolved", "timestamp" => iso()})

    assert eventually(fn ->
             has_element?(view, "#agent-#{@a} .tl-mk.knowledge.sel:not([data-provisional])")
           end)

    assert has_element?(view, "#mark-detail", "confirmed note")
  end

  test "marks that slide out of the window are pruned even without a re-read", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    send(
      view.pid,
      {:timeline_delta, "1h",
       %{
         "kind" => "lifecycle",
         "ts" => iso(-3_599_900),
         "agent_id" => @b,
         "event_type" => "lifecycle_resumed",
         "provisional" => true
       }}
    )

    assert has_element?(view, "#agent-#{@b} .tl-mk[data-kind=lifecycle]")
    Process.sleep(200)
    send(view.pid, :tick)
    refute has_element?(view, "#agent-#{@b} .tl-mk[data-kind=lifecycle]")
    # Seen only live, so its row and last-seen leave with the mark.
    refute has_element?(view, "#agent-#{@b}")
    refute Map.has_key?(:sys.get_state(view.pid).socket.assigns.last_seen, @b)
  end

  test "a pause stays closed after the cap evicts its live resume", %{conn: conn} do
    base = DateTime.utc_now()
    at = fn s -> base |> DateTime.add(s, :second) |> DateTime.to_iso8601() end

    paused = %{
      "kind" => "lifecycle",
      "ts" => at.(-1_100),
      "agent_id" => @a,
      "event_type" => "lifecycle_paused",
      "event_id" => "p"
    }

    ctx = feed_with([paused])
    view = mount_live(conn, ctx)

    resume = fn agent, s ->
      send(
        view.pid,
        {:timeline_delta, "1h",
         %{
           "kind" => "lifecycle",
           "ts" => at.(s),
           "agent_id" => agent,
           "event_type" => "lifecycle_resumed",
           "provisional" => true
         }}
      )
    end

    resume.(@a, -1_000)
    # 500 newer live lifecycle marks from another agent push @a's resume out.
    for i <- 1..500, do: resume.(@b, -i)

    _ = render(view)
    marks = Map.values(:sys.get_state(view.pid).socket.assigns.marks)
    refute Enum.any?(marks, &(&1["agent_id"] == @a and &1["event_type"] == "lifecycle_resumed"))

    [band] = nodes(view, "#agent-#{@a} .tl-band.pause")
    assert band |> LazyHTML.attribute("style") |> hd() =~ "--t2:"
    assert band |> LazyHTML.attribute("title") |> hd() =~ "since resumed"

    # Reloading during an outage (or toggling substrate without a new re-read)
    # preserves the retained pause closure even though the resume mark is gone.
    view |> element("#substrate-toggle") |> render_click()
    [band] = nodes(view, "#agent-#{@a} .tl-band.pause")
    assert band |> LazyHTML.attribute("style") |> hd() =~ "--t2:"
    assert band |> LazyHTML.attribute("title") |> hd() =~ "since resumed"

    # A second live pause after the evicted resume closes the first pause at
    # the retained resume and draws an open band for the new pause.
    send(
      view.pid,
      {:timeline_delta, "1h",
       %{
         "kind" => "lifecycle",
         "ts" => at.(-500),
         "agent_id" => @a,
         "event_type" => "lifecycle_paused",
         "provisional" => true
       }}
    )

    [first_band, second_band] = nodes(view, "#agent-#{@a} .tl-band.pause")
    assert first_band |> LazyHTML.attribute("style") |> hd() =~ "--t2:"
    assert first_band |> LazyHTML.attribute("title") |> hd() =~ "since resumed"
    refute second_band |> LazyHTML.attribute("style") |> hd() =~ "--t2:"
    assert second_band |> LazyHTML.attribute("title") |> hd() =~ "Paused since"
  end

  test "rows freed by the cap leave on the next prune", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    base = DateTime.utc_now()

    # 501 live marks from 501 agents: the cap drops the oldest agent's mark.
    for i <- 1..501 do
      id = "00000000-0000-4000-9000-#{String.pad_leading(Integer.to_string(i), 12, "0")}"

      send(
        view.pid,
        {:timeline_delta, "1h",
         %{
           "kind" => "lifecycle",
           "ts" => base |> DateTime.add(-i, :second) |> DateTime.to_iso8601(),
           "agent_id" => id,
           "event_type" => "lifecycle_resumed",
           "provisional" => true
         }}
      )
    end

    send(view.pid, :tick)
    _ = render(view)
    assigns = :sys.get_state(view.pid).socket.assigns
    assert length(assigns.row_order) == 500
    assert map_size(assigns.last_seen) == 500
  end

  test "a confirmed check-in selection takes the latest matching history row",
       %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    frame_ts = iso(-500)

    broadcast_frame(ctx, %{"type" => "eisv_update", "agent_id" => @a, "timestamp" => frame_ts})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)
    [live_id] = view |> nodes("#agent-#{@a} .tl-mk.checkin") |> hd() |> LazyHTML.attribute("id")
    view |> element("##{live_id}") |> render_click()

    row = fn ms, text ->
      %{
        "kind" => "checkin",
        "ts" => iso(ms),
        "agent_id" => @a,
        "authored_by" => "agent",
        "state_id" => text,
        "report_excerpt" => text
      }
    end

    # Two history check-ins within the 2 s lag before the frame.
    serve(ctx.stub, fn conn ->
      Req.Test.json(
        conn,
        window_body([row.(-2_000, "earlier report"), row.(-700, "later report")])
      )
    end)

    broadcast_frame(ctx, %{"type" => "dialectic_session_resolved", "timestamp" => iso()})

    assert eventually(fn -> has_element?(view, "#mark-detail", "later report") end)
  end

  test "a confirmed check-in selection follows the feed's claimed pairing across rapid frames",
       %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    first_frame_ts = iso(-600)
    second_frame_ts = iso(-400)

    broadcast_frame(ctx, %{
      "type" => "eisv_update",
      "agent_id" => @a,
      "timestamp" => first_frame_ts
    })

    broadcast_frame(ctx, %{
      "type" => "eisv_update",
      "agent_id" => @a,
      "timestamp" => second_frame_ts
    })

    assert_receive {:timeline_delta, "1h", _}
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    # Select the second live check-in mark (lanes render marks oldest first).
    [_first_node, second_node] = nodes(view, "#agent-#{@a} .tl-mk.checkin")
    [second_live_id] = LazyHTML.attribute(second_node, "id")
    view |> element("##{second_live_id}") |> render_click()

    row = fn ms, text ->
      %{
        "kind" => "checkin",
        "ts" => iso(ms),
        "agent_id" => @a,
        "authored_by" => "agent",
        "state_id" => text,
        "report_excerpt" => text
      }
    end

    # Both rows fall within both 2 s lag windows; the feed claims the latest
    # row (-700 ms) for the oldest mark and pairs the remaining row (-800 ms)
    # with the second mark.
    serve(ctx.stub, fn conn ->
      Req.Test.json(
        conn,
        window_body([row.(-800, "paired with second mark"), row.(-700, "claimed by first mark")])
      )
    end)

    broadcast_frame(ctx, %{"type" => "dialectic_session_resolved", "timestamp" => iso()})

    assert eventually(fn -> has_element?(view, "#mark-detail", "paired with second mark") end)
  end

  test "pruning keeps an unresumed pause but lets a resumed one age out with its resume",
       %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    delta = fn agent, type, ms ->
      send(
        view.pid,
        {:timeline_delta, "1h",
         %{
           "kind" => "lifecycle",
           "ts" => iso(ms),
           "agent_id" => agent,
           "event_type" => type,
           "provisional" => true
         }}
      )
    end

    # @a paused and resumed just inside the window; @b paused and never resumed.
    delta.(@a, "lifecycle_paused", -3_599_900)
    delta.(@a, "lifecycle_resumed", -3_599_850)
    delta.(@b, "lifecycle_paused", -3_599_900)
    assert has_element?(view, "#agent-#{@a} .tl-band.pause")

    Process.sleep(200)
    send(view.pid, :tick)
    refute has_element?(view, "#agent-#{@a} .tl-band.pause")
    assert has_element?(view, "#agent-#{@b} .tl-band.pause")
  end

  test "live marks are capped at the feed's provisional cap", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    base = DateTime.utc_now()

    for i <- 1..501 do
      send(
        view.pid,
        {:timeline_delta, "1h",
         %{
           "kind" => "lifecycle",
           "ts" => base |> DateTime.add(-i, :second) |> DateTime.to_iso8601(),
           "agent_id" => @b,
           "event_type" => "lifecycle_resumed",
           "provisional" => true
         }}
      )
    end

    _ = render(view)
    marks = Map.values(:sys.get_state(view.pid).socket.assigns.marks)
    assert length(marks) == 500

    # The oldest went: the earliest kept is 500 s before the base.
    oldest = marks |> Enum.map(&DialecticLive.TimelineAdapter.ts_us(&1["ts"])) |> Enum.min()
    assert oldest == DateTime.to_unix(DateTime.add(base, -500, :second), :microsecond)
  end

  test "a foreign roster renders from record fields alone", %{conn: conn} do
    agents = [
      agent(@a, "an agent from a harness nobody has heard of", "unheard-harness-9"),
      agent(@b, nil, nil)
    ]

    events = [
      %{
        "kind" => "lifecycle",
        "ts" => iso(-30_000),
        "agent_id" => @b,
        "event_type" => "lifecycle_paused",
        "event_id" => "e"
      }
    ]

    ctx = feed_with(events, agents)
    view = mount_live(conn, ctx)

    assert has_element?(view, "#agent-#{@a}", "unheard-harness-9")
    assert has_element?(view, "#agent-#{@b} .tl-name", String.slice(@b, 0, 8))
    assert has_element?(view, "#agent-#{@b} .tl-mk[data-kind=lifecycle]")
  end

  test "an empty roster still draws every lane by id", %{conn: conn} do
    events = [
      %{"kind" => "checkin", "ts" => iso(-1_000), "agent_id" => @a, "authored_by" => "agent"}
    ]

    ctx = feed_with(events, [])
    view = mount_live(conn, ctx)

    assert has_element?(view, "#agent-#{@a} .tl-name", String.slice(@a, 0, 8))
    assert has_element?(view, "#agent-#{@a} .tl-mk[data-kind=checkin]")
  end

  test "with EISV absent the header says check-ins arrive on re-read", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    broadcast_status(ctx, :connected)
    _ = await_reread_gets()
    await_reread_delivered(ctx, view)
    send(view.pid, :tick)

    assert has_element?(view, "#firehose-status", "connected")
    assert has_element?(view, "#checkin-reread-only", "No eisv_update")

    # A core kind still arrives live.
    broadcast_frame(ctx, %{"type" => "lifecycle_resumed", "agent_id" => @a, "timestamp" => iso()})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert has_element?(
             view,
             "#timeline-lanes .tl-mk[data-provisional=true][data-kind=lifecycle]"
           )
  end

  test "with zero optional subscribers a live check-in renders unassessed", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{"type" => "eisv_update", "agent_id" => @a, "timestamp" => iso()})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert has_element?(view, "#agent-#{@a} .tl-mk.checkin.unassessed[data-provisional=true]")
    refute render(view) =~ "assessment:"
  end

  test "an EISV assessment shapes a live check-in and is labelled as an assessment",
       %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{
      "type" => "eisv_update",
      "agent_id" => @a,
      "timestamp" => iso(),
      "decision" => %{"action" => "proceed", "sub_action" => "guide"}
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)
    assert has_element?(view, "#agent-#{@a} .tl-mk.checkin.guide[title*='assessment: guide']")
  end

  test "substrate off hides provisional and substrate check-ins and says why", %{conn: conn} do
    events = [
      %{
        "kind" => "checkin",
        "ts" => iso(-2_000),
        "agent_id" => @a,
        "authored_by" => "substrate",
        "epistemic_class" => "substrate_interpretation"
      },
      %{
        "kind" => "checkin",
        "ts" => iso(-1_000),
        "agent_id" => @a,
        "authored_by" => "agent",
        "epistemic_class" => "agent_report"
      }
    ]

    ctx = feed_with(events)
    view = mount_live(conn, ctx)
    assert has_element?(view, "#agent-#{@a} .tl-mk.checkin.substrate")

    view |> element("#substrate-toggle") |> render_click()
    refute has_element?(view, "#agent-#{@a} .tl-mk.checkin.substrate")
    assert has_element?(view, "#agent-#{@a} .tl-mk.checkin.proceed")
    assert has_element?(view, "#checkin-reread-only", "Substrate is off")

    # Another agent: a frame from @a within 2 s of its history row would be
    # recognised as that row and add nothing.
    broadcast_frame(ctx, %{"type" => "eisv_update", "agent_id" => @b, "timestamp" => iso()})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)
    refute has_element?(view, "#timeline-lanes .tl-mk[data-provisional=true]")
  end

  test "the window toggle switches to the 6 h window", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    assert_receive {:timeline_reread, "6h"}, 1_000

    view |> element("#window-6h") |> render_click()
    assert has_element?(view, "#window-6h.btn-active")
    assert has_element?(view, "#timeline-lanes[style*='--span:21600']")
  end

  test "the agent filter narrows the lanes", %{conn: conn} do
    ctx = feed_with([], [agent(@a, "alpha worker"), agent(@b, "beta worker")])
    view = mount_live(conn, ctx)
    assert has_element?(view, "#agent-#{@b}")

    view |> form("#agent-filter", %{"q" => "alpha"}) |> render_change()
    assert has_element?(view, "#agent-#{@a}")
    refute has_element?(view, "#agent-#{@b}")

    view |> form("#agent-filter", %{"q" => "nobody"}) |> render_change()
    assert has_element?(view, "#lanes-nomatch", "nobody")
  end

  test "more than 25 lanes show the most recent until show all", %{conn: conn} do
    agents =
      for i <- 1..30 do
        id = "00000000-0000-4000-8000-#{String.pad_leading(Integer.to_string(i), 12, "0")}"
        agent(id, "worker #{i}", nil, %{"last_activity_at" => iso(-i * 1_000)})
      end

    ctx = feed_with([], agents)
    view = mount_live(conn, ctx)

    assert length(lane_order(view)) == 25
    view |> element("#show-all-agents") |> render_click()
    assert length(lane_order(view)) == 30
  end

  test "a feed restart re-subscribes the open pane", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    # Forget the viewer, as a restarted feed would, then announce a restart.
    :sys.replace_state(ctx.feed, fn st ->
      Enum.each(st.viewers, fn {_pid, ref} -> Process.demonitor(ref, [:flush]) end)
      %{st | viewers: %{}}
    end)

    Phoenix.PubSub.broadcast(ctx.pubsub, TimelineFeed.topic(), :timeline_feed_up)
    _ = render(view)
    assert %{viewers: viewers} = :sys.get_state(ctx.feed)
    assert Map.has_key?(viewers, view.pid)
  end

  test "a lane shows the agent's latest note, without a hook's framing", %{conn: conn} do
    events = [
      %{
        "kind" => "checkin",
        "ts" => iso(-20_000),
        "agent_id" => @a,
        "authored_by" => "agent",
        "action" => "proceed",
        "report_excerpt" => "an older report"
      },
      %{
        "kind" => "checkin",
        "ts" => iso(-10_000),
        "agent_id" => @a,
        "authored_by" => "substrate",
        "action" => "proceed",
        "report_excerpt" =>
          "Turn summary (claude; tool count unavailable); response excerpt: Fixing PR #3076"
      },
      %{"kind" => "outcome", "ts" => iso(-5_000), "agent_id" => @a, "outcome_id" => "o"},
      # A newer mechanical note does not displace what the agent said.
      %{
        "kind" => "checkin",
        "ts" => iso(-4_000),
        "agent_id" => @a,
        "authored_by" => "substrate",
        "report_excerpt" => "Auto: 5 edits across 2 files"
      },
      %{"kind" => "checkin", "ts" => iso(-1_000), "agent_id" => @b, "authored_by" => "agent"},
      %{
        "kind" => "checkin",
        "ts" => iso(-1_500),
        "agent_id" => c = "00000000-0000-4000-8000-0000000000c3",
        "authored_by" => "substrate",
        "report_excerpt" => "35 tool calls completed over 10m"
      }
    ]

    ctx = feed_with(events, [agent(@a, "worker one"), agent(@b, "worker two")])
    view = mount_live(conn, ctx)

    assert [note] = nodes(view, "#agent-#{@a} .tl-note")
    assert LazyHTML.text(note) == "Fixing PR #3076"
    assert note |> LazyHTML.attribute("title") |> hd() =~ "UTC"
    refute has_element?(view, "#agent-#{@b} .tl-note")
    # With nothing the agent said, the mechanical note stands in.
    assert has_element?(view, "#agent-#{c} .tl-note", "35 tool calls")

    # With substrate off, the hook's note goes with its mark.
    view |> element("#substrate-toggle") |> render_click()
    assert has_element?(view, "#agent-#{@a} .tl-note", "an older report")
  end

  test "agents tagged autonomous fold into a background group until opened", %{conn: conn} do
    c = "00000000-0000-4000-8000-0000000000c3"

    agents = [
      agent(@a, "session worker"),
      agent(@b, "scheduled one", nil, %{"tags" => ["persistent", "autonomous"]}),
      agent(c, "its helper", nil, %{"parent_agent_id" => @b, "spawn_reason" => "subagent"})
    ]

    events = [
      %{"kind" => "outcome", "ts" => iso(-1_000), "agent_id" => @b, "outcome_id" => "1"},
      %{"kind" => "outcome", "ts" => iso(-2_000), "agent_id" => c, "outcome_id" => "2"},
      %{"kind" => "outcome", "ts" => iso(-30_000), "agent_id" => @a, "outcome_id" => "3"}
    ]

    ctx = feed_with(events, agents)
    view = mount_live(conn, ctx)

    # The session leads although the scheduled family is newer.
    assert lane_order(view) == [@a]
    assert has_element?(view, "#toggle-background", "Background agents (2)")
    assert has_element?(view, "#background-agents", "scheduled one")

    view |> element("#toggle-background") |> render_click()
    assert lane_order(view) == [@a, @b, c]
    assert has_element?(view, "#agent-#{c}.child")

    view |> element("#toggle-background") |> render_click()
    view |> form("#agent-filter", %{"q" => "scheduled"}) |> render_change()
    assert lane_order(view) == [@b]
  end

  test "needs attention lists an open pause and a pause verdict, not a resumed pause",
       %{conn: conn} do
    c = "00000000-0000-4000-8000-0000000000c3"

    lifecycle = fn agent_id, type, ms, id ->
      %{
        "kind" => "lifecycle",
        "ts" => iso(ms),
        "agent_id" => agent_id,
        "event_type" => type,
        "event_id" => id
      }
    end

    events = [
      lifecycle.(@a, "lifecycle_paused", -30_000, "p1"),
      lifecycle.(@b, "lifecycle_paused", -30_000, "p2"),
      lifecycle.(@b, "lifecycle_resumed", -20_000, "r2"),
      %{
        "kind" => "checkin",
        "ts" => iso(-5_000),
        "agent_id" => c,
        "authored_by" => "agent",
        "action" => "reject"
      }
    ]

    ctx =
      feed_with(events, [agent(@a, "held one"), agent(@b, "resumed one"), agent(c, "flagged")])

    view = mount_live(conn, ctx)

    assert has_element?(view, "#attention-#{@a}", "paused since")
    assert has_element?(view, "#attention-#{c}", "pause verdict")
    refute has_element?(view, "#attention-#{@b}")
    assert has_element?(view, "#agent-#{@a}.needs")
    refute has_element?(view, "#agent-#{@b}.needs")

    view |> element("#attention-#{@a} button") |> render_click()
    assert has_element?(view, "#mark-detail", "paused")
  end

  test "a live verdict updates the attention box without a re-read", %{conn: conn} do
    events = [
      %{
        "kind" => "checkin",
        "ts" => iso(-30_000),
        "agent_id" => @b,
        "authored_by" => "agent",
        "action" => "reject"
      }
    ]

    ctx = feed_with(events, [agent(@a, "worker one"), agent(@b, "worker two")])
    view = mount_live(conn, ctx)
    assert has_element?(view, "#attention-#{@b}")

    broadcast_frame(ctx, %{
      "type" => "eisv_update",
      "agent_id" => @a,
      "timestamp" => iso(),
      "decision" => %{"action" => "pause"}
    })

    broadcast_frame(ctx, %{
      "type" => "eisv_update",
      "agent_id" => @b,
      "timestamp" => iso(),
      "decision" => %{"action" => "proceed"}
    })

    assert_receive {:timeline_delta, "1h", _}
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert has_element?(view, "#attention-#{@a}", "pause verdict")
    refute has_element?(view, "#attention-#{@b}")
  end

  test "with no successful read the attention box makes no claim", %{conn: conn} do
    ctx = start_feed(respond: &Plug.Conn.send_resp(&1, 500, "boom")) |> use_feed()
    view = mount_live(conn, ctx)
    refute has_element?(view, "#needs-attention")
  end

  test "during an outage the attention box says it is stale", %{conn: conn} do
    {:ok, fail} = Agent.start_link(fn -> false end)

    ctx =
      start_feed(
        respond: fn conn ->
          if Agent.get(fail, & &1),
            do: Plug.Conn.send_resp(conn, 500, "boom"),
            else: Req.Test.json(conn, window_body([], [agent(@a, "worker one")]))
        end
      )
      |> use_feed()

    view = mount_live(conn, ctx)
    assert has_element?(view, "#needs-attention", "Nothing needs your attention")
    refute has_element?(view, "#attention-stale")

    Agent.update(fail, fn _ -> true end)
    broadcast_status(ctx, :connected)
    _ = await_reread_gets()
    await_reread_delivered(ctx, view)

    assert has_element?(view, "#attention-stale", "last successful read")
  end

  test "nothing to attend to says so", %{conn: conn} do
    ctx = feed_with([], [agent(@a, "worker one")])
    view = mount_live(conn, ctx)
    assert has_element?(view, "#needs-attention", "Nothing needs your attention")
  end

  test "one light says how current the page is", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    assert has_element?(view, "#live-light[data-state=slow]", "about once a minute")

    broadcast_status(ctx, :connected)
    _ = await_reread_gets()
    await_reread_delivered(ctx, view)
    send(view.pid, :tick)
    assert has_element?(view, "#live-light[data-state=live]", "Live")
  end

  test "an unavailable source turns the light off", %{conn: conn} do
    ctx = start_feed(respond: &Plug.Conn.send_resp(&1, 500, "boom")) |> use_feed()
    view = mount_live(conn, ctx)
    assert has_element?(view, "#live-light[data-state=down]", "Not updating")
  end
end
