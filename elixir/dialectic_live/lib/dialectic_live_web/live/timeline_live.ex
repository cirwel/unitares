defmodule DialecticLiveWeb.TimelineLive do
  @moduledoc """
  The live Timeline pane (portal stage 2,
  `docs/proposals/active/portal-live-timeline-v0.md` §3.4).

  Reads `DialecticLive.TimelineFeed.snapshot/2` on mount and on each re-read,
  and inserts provisional marks as they arrive, without a refetch. It renders:

    * a reverse-chronological feed capped at 200 rows, provisional rows
      styled apart from history;
    * an "agents now" strip, one row per agent in the window's roster, capped
      at 25 with show-all and a text filter; order is recomputed on re-read
      only, while "last seen" ticks;
    * a header naming the firehose and LiveView socket state, time since the
      last frame and the last re-read, truncation, which kinds are live and
      which re-read only, and the event-to-push latency (frame timestamp to
      stream insert, measured here, server side) as trial telemetry.

  Grouping and styling use record fields only; nothing here branches on an
  agent's name, resident or vendor. The pane only reads: it makes no
  governance call of its own.
  """
  use DialecticLiveWeb, :live_view

  alias DialecticLive.{Firehose, TimelineAdapter, TimelineFeed}

  @feed_cap 200
  @agents_cap 25
  @latency_samples 50

  @impl true
  def mount(_params, _session, socket) do
    cfg = Application.get_env(:dialectic_live, :timeline, [])
    feed = Keyword.get(cfg, :feed, TimelineFeed)
    table = Keyword.get(cfg, :table, TimelineFeed.default_table())
    window = TimelineFeed.window_for_hours(Keyword.get(cfg, :default_window_hours, 1))
    pubsub = Keyword.get(cfg, :pubsub, DialecticLive.PubSub)

    socket =
      socket
      |> assign(
        page_title: "Timeline",
        feed: feed,
        table: table,
        window: window,
        substrate: true,
        filter: "",
        filter_form: to_form(%{"q" => ""}),
        show_all_agents: false,
        latencies: [],
        now: DateTime.utc_now(),
        meta: %{},
        feed_reachable: true
      )
      |> stream_configure(:feed, dom_id: &TimelineAdapter.dom_id/1)

    socket =
      if connected?(socket) do
        Phoenix.PubSub.subscribe(pubsub, TimelineFeed.topic())
        Phoenix.PubSub.subscribe(pubsub, Firehose.status_topic())
        :timer.send_interval(1_000, self(), :tick)
        assign(socket, feed_reachable: TimelineFeed.subscribe(feed) == :ok)
      else
        socket
      end

    {:ok, load(socket)}
  end

  # --- feed messages ---

  @impl true
  def handle_info({:timeline_delta, window, mark}, %{assigns: %{window: window}} = socket) do
    now = DateTime.utc_now()
    socket = record_latency(socket, mark, now)

    {:noreply,
     if(visible?(mark, socket.assigns.substrate), do: insert_mark(socket, mark), else: socket)}
  end

  def handle_info({:timeline_reread, window}, %{assigns: %{window: window}} = socket),
    do: {:noreply, load(socket)}

  def handle_info({:timeline_status, window}, %{assigns: %{window: window}} = socket),
    do: {:noreply, refresh_status(socket)}

  def handle_info(:timeline_feed_up, socket) do
    socket = assign(socket, feed_reachable: TimelineFeed.subscribe(socket.assigns.feed) == :ok)
    {:noreply, load(socket)}
  end

  def handle_info({:firehose_status, status}, socket),
    do: {:noreply, assign(socket, meta: Map.put(socket.assigns.meta, :firehose, status))}

  def handle_info(:tick, socket) do
    meta =
      case TimelineFeed.snapshot_meta(socket.assigns.table) do
        {:ok, meta} -> meta
        :unavailable -> socket.assigns.meta
      end

    {:noreply, assign(socket, now: DateTime.utc_now(), meta: meta)}
  end

  # Deltas and re-reads for the other window, and anything unexpected.
  def handle_info(_message, socket), do: {:noreply, socket}

  # --- controls ---

  @impl true
  def handle_event("window", %{"w" => window}, socket) do
    if window in TimelineFeed.windows() do
      {:noreply, socket |> assign(window: window) |> load()}
    else
      {:noreply, socket}
    end
  end

  def handle_event("substrate", _params, socket),
    do: {:noreply, socket |> assign(substrate: !socket.assigns.substrate) |> load()}

  def handle_event("filter", %{"q" => q}, socket),
    do: {:noreply, assign(socket, filter: String.trim(q), filter_form: to_form(%{"q" => q}))}

  def handle_event("show_all_agents", _params, socket),
    do: {:noreply, assign(socket, show_all_agents: !socket.assigns.show_all_agents)}

  # --- loading ---

  defp load(socket) do
    case TimelineFeed.snapshot(socket.assigns.window, socket.assigns.table) do
      {:ok, snap} ->
        marks = snap.events ++ snap.provisional

        rows =
          marks
          |> Enum.filter(&visible?(&1, socket.assigns.substrate))
          |> Enum.sort_by(&sort_key/1, :desc)
          |> Enum.take(@feed_cap)

        last_seen = Enum.reduce(marks, roster_last_seen(snap.agents), &bump_last_seen(&2, &1))

        socket
        |> assign(
          source: snap.status,
          source_error: snap.error,
          truncated: snap.truncated,
          last_reread_at: snap.last_reread_at,
          meta: snap.meta,
          labels: Map.new(snap.agents, &{&1["agent_id"], &1["label"]}),
          agents: order_agents(snap.agents, last_seen),
          last_seen: last_seen,
          row_ts: Enum.map(rows, &sort_key/1)
        )
        |> stream(:feed, rows, reset: true)

      :unavailable ->
        socket
        |> assign(
          source: :no_feed,
          source_error: "the timeline feed is not running",
          truncated: [],
          last_reread_at: nil,
          labels: %{},
          agents: [],
          last_seen: %{},
          row_ts: []
        )
        |> stream(:feed, [], reset: true)
    end
  end

  defp refresh_status(socket) do
    case TimelineFeed.snapshot(socket.assigns.window, socket.assigns.table) do
      {:ok, snap} ->
        assign(socket, source: snap.status, source_error: snap.error, meta: snap.meta)

      :unavailable ->
        assign(socket, source: :no_feed, source_error: "the timeline feed is not running")
    end
  end

  # With substrate off only the agent's own check-ins show, as the tab's
  # include_substrate=false does. A provisional check-in is hidden too: the
  # frame cannot tell substrate from agent.
  defp visible?(%{"kind" => "checkin", "authored_by" => by}, false), do: by == "agent"
  defp visible?(_mark, _substrate), do: true

  defp sort_key(mark), do: TimelineAdapter.ts_us(mark["ts"]) || 0

  # Streams cannot be read back, so the pane keeps the shown rows' timestamps
  # (newest first, at most the cap) to insert a delta at its place in time. A
  # delta older than every row of a full feed is not shown; the cap prunes
  # from the oldest end.
  defp insert_mark(socket, mark) do
    ts = sort_key(mark)
    {newer, older} = Enum.split_while(socket.assigns.row_ts, &(&1 >= ts))
    at = length(newer)
    # The agent was seen whether or not its mark fits in the feed.
    socket = assign(socket, last_seen: bump_last_seen(socket.assigns.last_seen, mark))

    if at >= @feed_cap do
      socket
    else
      socket
      |> stream_insert(:feed, mark, at: at, limit: @feed_cap)
      |> assign(row_ts: Enum.take(newer ++ [ts | older], @feed_cap))
    end
  end

  defp record_latency(socket, mark, now) do
    case TimelineAdapter.ts_us(mark["ts"]) do
      nil ->
        socket

      ts ->
        ms = div(DateTime.to_unix(now, :microsecond) - ts, 1000)
        assign(socket, latencies: Enum.take([ms | socket.assigns.latencies], @latency_samples))
    end
  end

  defp roster_last_seen(agents) do
    Map.new(agents, fn a -> {a["agent_id"], TimelineAdapter.ts_us(a["last_activity_at"])} end)
  end

  defp bump_last_seen(last_seen, %{"agent_id" => id, "ts" => ts}) when is_binary(id) do
    case TimelineAdapter.ts_us(ts) do
      nil -> last_seen
      us -> Map.update(last_seen, id, us, &max(&1 || 0, us))
    end
  end

  defp bump_last_seen(last_seen, _mark), do: last_seen

  defp order_agents(agents, last_seen) do
    Enum.sort_by(agents, &(Map.get(last_seen, &1["agent_id"]) || 0), :desc)
  end

  defp agents_cap, do: @agents_cap

  defp shown_agents(agents, filter, show_all?) do
    matching =
      case String.downcase(filter) do
        "" ->
          agents

        q ->
          Enum.filter(agents, fn a ->
            Enum.any?([a["label"], a["agent_id"], a["model_type"]], fn v ->
              is_binary(v) and String.contains?(String.downcase(v), q)
            end)
          end)
      end

    {if(show_all?, do: matching, else: Enum.take(matching, @agents_cap)), length(matching)}
  end

  # --- formatting ---

  defp agent_name(labels, id) when is_binary(id) do
    case Map.get(labels, id) do
      label when is_binary(label) and label != "" -> label
      _ -> String.slice(id, 0, 8)
    end
  end

  defp agent_name(_labels, _id), do: "unknown agent"

  defp clock(ts) do
    case TimelineAdapter.ts_us(ts) do
      nil -> "—"
      us -> us |> DateTime.from_unix!(:microsecond) |> Calendar.strftime("%H:%M:%S")
    end
  end

  defp ago(nil, _now), do: "never"

  defp ago(%DateTime{} = then, now), do: ago_us(DateTime.to_unix(then, :microsecond), now)

  defp ago_us(nil, _now), do: "—"

  defp ago_us(us, now) do
    secs = max(0, div(DateTime.to_unix(now, :microsecond) - us, 1_000_000))

    cond do
      secs < 90 -> "#{secs} s ago"
      secs < 5_400 -> "#{div(secs, 60)} min ago"
      true -> "#{div(secs, 3600)} h ago"
    end
  end

  defp firehose_label(:connected), do: "connected"
  defp firehose_label(:unreachable), do: "governance unreachable"
  defp firehose_label(:refused), do: "governance refused credentials"
  defp firehose_label(:connecting), do: "connecting"
  defp firehose_label(_), do: "not running"

  defp source_label(:ok), do: "ok"
  defp source_label(:loading), do: "loading"
  defp source_label(:refused), do: "refused"
  defp source_label(:unavailable), do: "unavailable, showing the last window"
  defp source_label(:no_feed), do: "unavailable"
  defp source_label(other), do: to_string(other)

  defp latency_summary([]), do: nil

  defp latency_summary(samples) do
    sorted = Enum.sort(samples)
    %{last: hd(samples), median: Enum.at(sorted, div(length(sorted), 2)), n: length(sorted)}
  end

  defp checkins_reread_only_reason(assigns) do
    cond do
      !assigns.substrate ->
        "Substrate is off: live check-ins are hidden, because a frame cannot tell substrate from agent. Check-ins arrive on re-read only."

      Map.get(assigns.meta, :firehose) != :connected ->
        "The firehose is not connected: every kind arrives on re-read only."

      Map.get(assigns.meta, :last_eisv_at) == nil ->
        "No eisv_update frame since the feed started. Live check-ins come from the EISV module's enrichment, so until one arrives check-ins arrive on re-read only."

      true ->
        nil
    end
  end

  # Record fields arrive from the server as JSON; anything that is not text or
  # a number renders as absent rather than raising in a template.
  defp txt(v) when is_binary(v) and v != "", do: v
  defp txt(v) when is_number(v), do: to_string(v)
  defp txt(_), do: nil

  defp joined(parts), do: parts |> Enum.filter(&is_binary/1) |> Enum.join(" · ")

  defp prefixed(_label, nil), do: nil
  defp prefixed(label, value), do: label <> " " <> value

  defp detail(%{"kind" => "checkin"} = m),
    do: joined([prefixed("by", txt(m["authored_by"])), txt(m["epistemic_class"])])

  defp detail(%{"kind" => "outcome"} = m),
    do:
      joined([
        txt(m["outcome_type"]),
        prefixed("score", txt(m["outcome_score"])),
        m["is_bad"] == true && "bad"
      ])

  defp detail(%{"kind" => "lifecycle"} = m), do: joined([txt(m["event_type"]), txt(m["reason"])])

  defp detail(%{"kind" => "knowledge"} = m),
    do: joined([txt(m["discovery_type"]), txt(m["summary"])])

  defp detail(%{"kind" => "lineage"} = m) do
    parent = txt(m["parent_agent_id"])

    joined([
      parent && "declared parent " <> String.slice(parent, 0, 8),
      txt(m["successor_class"])
    ])
  end

  defp detail(%{"kind" => "dialectic"} = m),
    do: joined([txt(m["session_type"]), txt(m["status"]), txt(m["phase"])])

  defp detail(_m), do: ""

  # The check-in verdict is a governance assessment, not part of the core
  # record. A live mark carries it as an EISV decoration; a history row carries
  # the persisted `action`, which is the same verdict. Both render the same way,
  # so a row does not change label when the re-read confirms it.
  defp assessment(%{"assessment" => %{"verdict" => verdict}}), do: assessment_text(txt(verdict))
  defp assessment(%{"kind" => "checkin", "action" => action}), do: assessment_text(txt(action))
  defp assessment(_m), do: nil

  defp assessment_text(nil), do: nil
  defp assessment_text(verdict), do: "assessment: " <> verdict

  # --- render ---

  @impl true
  def render(assigns) do
    assigns =
      assigns
      |> assign(:reread_only_reason, checkins_reread_only_reason(assigns))
      |> assign(:latency, latency_summary(assigns.latencies))
      |> assign(:shown, shown_agents(assigns.agents, assigns.filter, assigns.show_all_agents))

    ~H"""
    <div class="mx-auto max-w-5xl p-6 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h1 class="text-2xl font-semibold">Timeline — live</h1>
        <div class="flex items-center gap-2">
          <div class="join" id="window-toggle">
            <button
              :for={w <- DialecticLive.TimelineFeed.windows()}
              id={"window-#{w}"}
              phx-click="window"
              phx-value-w={w}
              class={["btn btn-sm join-item", @window == w && "btn-active"]}
            >
              {w}
            </button>
          </div>
          <button
            id="substrate-toggle"
            phx-click="substrate"
            class={["btn btn-sm", @substrate && "btn-active"]}
          >
            substrate {if @substrate, do: "on", else: "off"}
          </button>
        </div>
      </div>

      <section id="timeline-header" class="text-sm grid gap-1 sm:grid-cols-2">
        <div id="firehose-status">
          Firehose: <span class="font-medium">{firehose_label(Map.get(@meta, :firehose))}</span>
        </div>
        <div
          id="socket-status"
          phx-disconnected={JS.show(to: "#socket-lost")}
          phx-connected={JS.hide(to: "#socket-lost")}
        >
          Page socket:
          <span class="font-medium">{if connected?(@socket), do: "connected", else: "connecting"}</span>
        </div>
        <div id="last-frame">
          Last frame: {ago(Map.get(@meta, :last_frame_at), @now)} · last eisv_update: {ago(
            Map.get(@meta, :last_eisv_at),
            @now
          )}
        </div>
        <div id="source-status">
          History: <span class="font-medium">{source_label(@source)}</span>
          · re-read {ago(@last_reread_at, @now)}
          <span :if={@source_error} class="text-warning">({@source_error})</span>
        </div>
        <div id="live-kinds" class="sm:col-span-2 text-base-content/70">
          Live: {Enum.join(DialecticLive.TimelineAdapter.live_kinds(), ", ")} ·
          re-read only (at least every 60 s while viewed): {Enum.join(
            DialecticLive.TimelineAdapter.reread_only_kinds(),
            ", "
          )}
        </div>
        <div id="push-latency" class="sm:col-span-2 text-base-content/70">
          <%= if @latency do %>
            Event-to-push latency (server side): last {@latency.last} ms · median {@latency.median} ms over {@latency.n}
          <% else %>
            Event-to-push latency: no live mark yet
          <% end %>
        </div>
      </section>

      <div id="socket-lost" class="alert alert-error text-sm hidden">
        The page socket is disconnected: nothing on this page is updating until it reconnects.
      </div>

      <div :if={@truncated != []} id="truncation-banner" class="alert alert-warning text-sm">
        Truncated: {Enum.join(@truncated, ", ")}. The server keeps the newest rows per kind, so the oldest part of this window is missing for those kinds.
      </div>

      <div :if={@reread_only_reason} id="checkin-reread-only" class="alert alert-info text-sm">
        {@reread_only_reason}
      </div>

      <div
        :if={!@feed_reachable or @source == :no_feed}
        id="feed-unavailable"
        class="alert alert-warning text-sm"
      >
        The timeline feed is unavailable. Nothing is shown until it runs.
      </div>

      <section id="timeline-agents" class="space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="font-semibold">Agents now ({elem(@shown, 1)})</h2>
          <.form for={@filter_form} id="agent-filter" phx-change="filter">
            <.input field={@filter_form[:q]} type="text" placeholder="filter agents" />
          </.form>
        </div>
        <div :if={@agents == []} id="agents-empty" class="text-base-content/60 text-sm">
          No agents in this window.
        </div>
        <ul class="grid gap-1 sm:grid-cols-2 text-sm">
          <li
            :for={a <- elem(@shown, 0)}
            id={"agent-#{a["agent_id"]}"}
            class="flex justify-between gap-2 rounded bg-base-200 px-2 py-1"
          >
            <span class="truncate">
              {agent_name(@labels, a["agent_id"])}
              <span :if={a["model_type"]} class="text-base-content/60">· {a["model_type"]}</span>
            </span>
            <span class="text-base-content/60 shrink-0">
              {ago_us(Map.get(@last_seen, a["agent_id"]), @now)}
            </span>
          </li>
        </ul>
        <button
          :if={elem(@shown, 1) > agents_cap()}
          id="show-all-agents"
          phx-click="show_all_agents"
          class="btn btn-xs btn-ghost"
        >
          {if @show_all_agents, do: "show fewer", else: "show all #{elem(@shown, 1)}"}
        </button>
      </section>

      <section class="space-y-2">
        <h2 class="font-semibold">Feed (newest first, up to 200)</h2>
        <%!-- Outside the stream container, so stream_insert's `at:` indexes count rows only. --%>
        <p :if={@row_ts == []} id="feed-empty" class="text-sm text-base-content/60">
          Nothing in this window yet.
        </p>
        <ul id="timeline-feed" phx-update="stream" class="space-y-1 text-sm">
          <li
            :for={{id, m} <- @streams.feed}
            id={id}
            data-kind={m["kind"]}
            data-provisional={m["provisional"] && "true"}
            class={[
              "rounded px-2 py-1 flex gap-3",
              if(m["provisional"],
                do: "border border-dashed border-info",
                else: "bg-base-200"
              )
            ]}
          >
            <span class="font-mono text-base-content/60 shrink-0">{clock(m["ts"])}</span>
            <span class="badge badge-ghost badge-sm shrink-0">{m["kind"]}</span>
            <span class="truncate shrink-0 max-w-40">{agent_name(@labels, m["agent_id"])}</span>
            <span class="truncate grow">{detail(m)}</span>
            <span :if={assessment(m)} class="badge badge-outline badge-sm shrink-0">{assessment(m)}</span>
            <span :if={m["provisional"]} class="badge badge-info badge-sm shrink-0">live</span>
            <span :if={m["seen_live"]} class="badge badge-ghost badge-sm shrink-0">seen live</span>
          </li>
        </ul>
      </section>
    </div>
    """
  end
end
