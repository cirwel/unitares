defmodule Portal.Test.TimelineHelpers do
  @moduledoc """
  Starts an isolated `Portal.TimelineFeed` for one test: its own
  PubSub, task supervisor, ETS heir and table, with `GET /v1/timeline`
  answered by a `Req.Test` stub. Every GET the stub serves is reported to the
  test process as `{:timeline_get, query_params}`.
  """

  import ExUnit.Assertions
  import ExUnit.Callbacks

  alias Portal.{TimelineFeed, TimelineTableHeir}

  def start_feed(opts \\ []) do
    id = System.unique_integer([:positive])
    pubsub = :"timeline_pubsub_#{id}"
    task_sup = :"timeline_tasks_#{id}"
    heir = :"timeline_heir_#{id}"
    name = :"timeline_feed_#{id}"
    table = :"timeline_table_#{id}"
    stub = :"timeline_stub_#{id}"

    start_supervised!({Phoenix.PubSub, name: pubsub}, id: pubsub)
    start_supervised!({Task.Supervisor, name: task_sup}, id: task_sup)
    start_supervised!({TimelineTableHeir, name: heir})

    serve(stub, Keyword.get(opts, :respond, fn conn -> Req.Test.json(conn, window_body()) end))

    feed_opts =
      [
        name: name,
        table: table,
        pubsub: pubsub,
        heir: heir,
        task_supervisor: task_sup,
        req_options: [plug: {Req.Test, stub}],
        firehose: nil,
        reread_ms: 60_000,
        debounce_ms: 50,
        fetch_timeout_ms: 2_000
      ]
      |> Keyword.merge(Keyword.drop(opts, [:respond]))

    feed = start_supervised!({TimelineFeed, feed_opts})
    Req.Test.allow(stub, self(), feed)

    %{feed: feed, name: name, table: table, pubsub: pubsub, stub: stub, heir: heir}
  end

  @doc "Point the stub at a new responder; every GET is still reported."
  def serve(stub, respond) do
    test = self()

    Req.Test.stub(stub, fn conn ->
      conn = Plug.Conn.fetch_query_params(conn)
      send(test, {:timeline_get, conn.query_params})
      respond.(conn)
    end)
  end

  @doc "Spawn a process that subscribes as a viewer; stop it with `stop_viewer/1`."
  def viewer(feed) do
    parent = self()

    pid =
      spawn(fn ->
        :ok = TimelineFeed.subscribe(feed)
        send(parent, {:viewer_up, self()})

        receive do
          :stop -> :ok
        end
      end)

    assert_receive {:viewer_up, ^pid}
    pid
  end

  def stop_viewer(pid) do
    ref = Process.monitor(pid)
    send(pid, :stop)
    assert_receive {:DOWN, ^ref, :process, ^pid, _}
  end

  @doc "Wait for one GET per window and return their query params by `hours`."
  def await_reread_gets do
    gets =
      for _ <- TimelineFeed.windows() do
        assert_receive {:timeline_get, params}, 1_000
        params
      end

    Map.new(gets, &{&1["hours"], &1})
  end

  @doc "Wait until the feed has applied a result for `window`."
  def await_applied(ctx, window, status \\ :ok) do
    Enum.reduce_while(1..200, nil, fn _, _ ->
      _ = :sys.get_state(ctx.feed)

      case TimelineFeed.snapshot(window, ctx.table) do
        {:ok, %{status: ^status} = snap} -> {:halt, snap}
        _ -> Process.sleep(5) && {:cont, nil}
      end
    end) || flunk("window #{window} never reached #{inspect(status)}")
  end

  def broadcast_frame(ctx, frame),
    do:
      Phoenix.PubSub.broadcast(
        ctx.pubsub,
        Portal.Firehose.all_topic(),
        {:governance_event, frame}
      )

  def broadcast_status(ctx, status),
    do:
      Phoenix.PubSub.broadcast(
        ctx.pubsub,
        Portal.Firehose.status_topic(),
        {:firehose_status, status}
      )

  def iso(offset_ms \\ 0) do
    DateTime.utc_now() |> DateTime.add(offset_ms, :millisecond) |> DateTime.to_iso8601()
  end

  def window_body(events \\ [], agents \\ [], extra \\ %{}) do
    Map.merge(
      %{
        "success" => true,
        "window" => %{"hours" => 1},
        "truncated" => [],
        "counts" => %{},
        "agents" => agents,
        "events" => events
      },
      extra
    )
  end
end
