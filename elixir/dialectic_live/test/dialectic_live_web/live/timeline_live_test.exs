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

  # The feed's rows in DOM order, as text, read through LazyHTML selectors.
  defp feed_rows(view) do
    view
    |> render()
    |> LazyHTML.from_fragment()
    |> LazyHTML.query("#timeline-feed > li")
    |> Enum.map(&LazyHTML.text/1)
  end

  defp feed_with(events, agents \\ [], extra \\ %{}) do
    start_feed(respond: fn conn -> Req.Test.json(conn, window_body(events, agents, extra)) end)
    |> use_feed()
  end

  defp agent(id, label, model_type \\ nil) do
    %{
      "agent_id" => id,
      "label" => label,
      "model_type" => model_type,
      "last_activity_at" => iso(-60_000)
    }
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
    assert has_element?(view, "#timeline-feed li[data-kind=outcome]")
    assert has_element?(view, "#timeline-feed li[data-kind=lineage]")
  end

  test "an empty window renders its empty states", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    assert has_element?(view, "#feed-empty")
    assert has_element?(view, "#agents-empty")
    assert has_element?(view, "#socket-status", "connected")
    assert has_element?(view, "#source-status", "ok")
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
    assert has_element?(view, "#feed-empty")
  end

  test "history rows render newest first, with the truncation banner", %{conn: conn} do
    events = [
      %{
        "kind" => "outcome",
        "ts" => iso(-120_000),
        "agent_id" => @a,
        "outcome_id" => "o-1",
        "outcome_type" => "test_failed",
        "is_bad" => true
      },
      %{
        "kind" => "checkin",
        "ts" => iso(-60_000),
        "agent_id" => @a,
        "authored_by" => "agent",
        "action" => "guide"
      }
    ]

    ctx = feed_with(events, [agent(@a, "worker one")], %{"truncated" => ["checkin"]})
    view = mount_live(conn, ctx)

    assert has_element?(view, "#truncation-banner", "checkin")
    assert [checkin, outcome] = feed_rows(view)
    assert checkin =~ "assessment: guide"
    assert outcome =~ "test_failed"
    assert has_element?(view, "#timeline-feed li[data-kind=outcome]", "worker one")
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

    assert has_element?(view, "#timeline-feed li[data-provisional=true]", "a fresh note")
    assert has_element?(view, "#push-latency", "median")
    refute_receive {:timeline_get, _}, 100
  end

  test "a delayed delta takes its place in time, not the top", %{conn: conn} do
    events = [
      %{
        "kind" => "outcome",
        "ts" => iso(-60_000),
        "agent_id" => @a,
        "outcome_id" => "old",
        "outcome_type" => "oldest_row"
      },
      %{
        "kind" => "outcome",
        "ts" => iso(-1_000),
        "agent_id" => @a,
        "outcome_id" => "new",
        "outcome_type" => "newest_row"
      }
    ]

    ctx = feed_with(events)
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{
      "type" => "lifecycle_paused",
      "agent_id" => @b,
      "timestamp" => iso(-30_000),
      "reason" => "delayed_frame"
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert [newest, delayed, oldest] = feed_rows(view)
    assert newest =~ "newest_row"
    assert delayed =~ "delayed_frame"
    assert oldest =~ "oldest_row"
  end

  test "a delta older than a full feed is not shown and evicts nothing", %{conn: conn} do
    events =
      for i <- 1..200 do
        %{
          "kind" => "outcome",
          "ts" => iso(-i * 1_000),
          "agent_id" => @a,
          "outcome_id" => "o#{i}",
          "outcome_type" => "row#{i}"
        }
      end

    ctx = feed_with(events)
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{
      "type" => "lifecycle_paused",
      "agent_id" => @b,
      "timestamp" => iso(-500_000),
      "reason" => "too_old"
    })

    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    refute has_element?(view, "#timeline-feed", "too_old")
    assert has_element?(view, "#timeline-feed", "row200")
    # The agent was still seen: the strip's last-seen moves even so.
    assert Map.has_key?(:sys.get_state(view.pid).socket.assigns.last_seen, @b)
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
    assert has_element?(view, "#agent-#{@b}", String.slice(@b, 0, 8))
    assert has_element?(view, "#timeline-feed li[data-kind=lifecycle]", String.slice(@b, 0, 8))
  end

  test "an empty roster still renders every row by id", %{conn: conn} do
    events = [
      %{"kind" => "checkin", "ts" => iso(-1_000), "agent_id" => @a, "authored_by" => "agent"}
    ]

    ctx = feed_with(events, [])
    view = mount_live(conn, ctx)

    assert has_element?(view, "#agents-empty")
    assert has_element?(view, "#timeline-feed li[data-kind=checkin]", String.slice(@a, 0, 8))
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
    assert has_element?(view, "#timeline-feed li[data-provisional=true][data-kind=lifecycle]")
  end

  test "with zero optional subscribers a check-in renders with no assessment", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)

    broadcast_frame(ctx, %{"type" => "eisv_update", "agent_id" => @a, "timestamp" => iso()})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)

    assert has_element?(view, "#timeline-feed li[data-provisional=true][data-kind=checkin]")
    refute render(view) =~ "assessment:"
  end

  test "an EISV assessment renders labelled as an assessment", %{conn: conn} do
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
    assert has_element?(view, "#timeline-feed li[data-kind=checkin]", "assessment: guide")
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
    assert has_element?(view, "#timeline-feed", "substrate_interpretation")

    view |> element("#substrate-toggle") |> render_click()
    refute has_element?(view, "#timeline-feed", "substrate_interpretation")
    assert has_element?(view, "#timeline-feed", "agent_report")
    assert has_element?(view, "#checkin-reread-only", "Substrate is off")

    # Another agent: a frame from @a within 2 s of its history row would be
    # recognised as that row and add nothing.
    broadcast_frame(ctx, %{"type" => "eisv_update", "agent_id" => @b, "timestamp" => iso()})
    assert_receive {:timeline_delta, "1h", _}
    _ = :sys.get_state(ctx.feed)
    refute has_element?(view, "#timeline-feed li[data-provisional=true]")
  end

  test "the window toggle switches to the 6 h window", %{conn: conn} do
    ctx = feed_with([])
    view = mount_live(conn, ctx)
    assert_receive {:timeline_reread, "6h"}, 1_000

    view |> element("#window-6h") |> render_click()
    assert has_element?(view, "#window-6h.btn-active")
  end

  test "the agent filter narrows the strip", %{conn: conn} do
    ctx = feed_with([], [agent(@a, "alpha worker"), agent(@b, "beta worker")])
    view = mount_live(conn, ctx)

    view |> form("#agent-filter", %{"q" => "alpha"}) |> render_change()
    assert has_element?(view, "#agent-#{@a}")
    refute has_element?(view, "#agent-#{@b}")
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
end
