defmodule PortalWeb.TimelineLive do
  @moduledoc """
  The hub's home screen: the live timeline, copied from `dialectic_live`
  (portal-hub-v0 H2d-2b). It mounts only behind the operator guard
  (`PortalWeb.Live.RequireOperator`), which runs before `mount/3`, so a
  refused socket never subscribes to the feed.

  The live Timeline pane (portal stage 2,
  `docs/proposals/active/portal-live-timeline-v0.md` §3.4).

  Reads `Portal.TimelineFeed.snapshot/2` on mount and on each re-read,
  and adds provisional marks as they arrive, without a refetch. It draws the
  same lanes as the dashboard's Timeline tab (`dashboard/redesign/sections/
  timeline.js`): one row per agent across the window, marks placed in time,
  bands for pauses and dialectic reviews, subagents nested under a visible
  parent. Selecting a mark shows its record. Marks too close together to
  tell apart draw as one counted group, in the tone of its most notable
  mark; selecting it lists them.

  Pure lane ordering, mark clustering, pause-band pairing, attention
  detection, and record detail formatting live in
  `PortalWeb.TimelineLive.Lanes`.
  """
  use PortalWeb, :live_view

  import PortalWeb.TimelineLive.Lanes

  alias Portal.{Firehose, TimelineAdapter, TimelineFeed}

  @latency_samples 50
  @prune_every_s 10

  @impl true
  def mount(_params, _session, socket) do
    cfg = Application.get_env(:portal, :timeline, [])
    feed = Keyword.get(cfg, :feed, TimelineFeed)
    table = Keyword.get(cfg, :table, TimelineFeed.default_table())
    window = TimelineFeed.window_for_hours(Keyword.get(cfg, :default_window_hours, 1))
    pubsub = Keyword.get(cfg, :pubsub, Portal.PubSub)
    now = DateTime.utc_now()

    socket =
      assign(socket,
        page_title: "Timeline",
        feed: feed,
        table: table,
        window: window,
        substrate: true,
        filter: "",
        filter_form: to_form(%{"q" => ""}),
        show_all_agents: false,
        latencies: [],
        now: now,
        anchor: DateTime.to_unix(now),
        pruned_at: DateTime.to_unix(now) - @prune_every_s,
        meta: %{},
        feed_reachable: true,
        marks: %{},
        clusters: %{},
        group_hint: nil,
        resumed_at: %{},
        row_order: [],
        selected: nil,
        show_background: false
      )

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

    now = DateTime.utc_now()
    socket = assign(socket, now: now, meta: meta)

    {:noreply,
     if DateTime.to_unix(now) - socket.assigns.pruned_at >= @prune_every_s do
       socket |> assign(pruned_at: DateTime.to_unix(now)) |> prune()
     else
       socket
     end}
  end

  # Deltas and re-reads for the other window, and anything unexpected.
  def handle_info(_message, socket), do: {:noreply, socket}

  # --- controls ---

  @impl true
  def handle_event("window", %{"w" => window}, socket) do
    if window in TimelineFeed.windows() do
      {:noreply, socket |> assign(window: window, selected: nil) |> load()}
    else
      {:noreply, socket}
    end
  end

  def handle_event("substrate", _params, socket),
    do: {:noreply, socket |> assign(substrate: !socket.assigns.substrate) |> load()}

  def handle_event("filter", %{"q" => q}, socket),
    do:
      {:noreply,
       socket |> assign(filter: String.trim(q), filter_form: to_form(%{"q" => q})) |> relane()}

  def handle_event("show_all_agents", _params, socket),
    do: {:noreply, socket |> assign(show_all_agents: !socket.assigns.show_all_agents) |> relane()}

  def handle_event("show_background", _params, socket),
    do: {:noreply, socket |> assign(show_background: !socket.assigns.show_background) |> relane()}

  def handle_event("select", %{"id" => id}, socket) do
    selected = if socket.assigns.selected == id, do: nil, else: id
    {:noreply, assign(socket, selected: selected)}
  end

  # --- loading ---

  defp load(socket) do
    case TimelineFeed.snapshot(socket.assigns.window, socket.assigns.table) do
      {:ok, snap} ->
        all = snap.events ++ snap.provisional
        last_seen = Enum.reduce(all, roster_last_seen(snap.agents), &bump_last_seen(&2, &1))

        read =
          all
          |> Enum.filter(&visible?(&1, socket.assigns.substrate))
          |> Map.new(&{TimelineAdapter.dom_id(&1), &1})

        roster = for %{"agent_id" => id} = a <- snap.agents, is_binary(id), into: %{}, do: {id, a}

        resumed_seed =
          if snap.status == :ok and snap.last_reread_at != socket.assigns[:last_reread_at],
            do: %{},
            else: socket.assigns.resumed_at

        resumed = Enum.reduce(all, resumed_seed, &note_resume(&2, &1))
        marks = Map.merge(held_pauses(socket.assigns, resumed), read)
        paired = Map.get(snap, :paired, %{})

        socket
        |> assign(
          source: snap.status,
          source_error: snap.error,
          truncated: snap.truncated,
          last_reread_at: snap.last_reread_at,
          meta: snap.meta,
          labels: Map.new(roster, fn {id, a} -> {id, a["label"]} end),
          roster: roster,
          last_seen: last_seen,
          marks: marks,
          resumed_at: resumed,
          selected: carry_selection(socket.assigns.selected, socket.assigns.marks, marks, paired),
          group_hint: carry_group(socket.assigns, marks, paired),
          row_order: order_rows(roster, marks, last_seen)
        )
        |> relane()

      :unavailable ->
        socket
        |> assign(
          source: :no_feed,
          source_error: feed_not_running(),
          truncated: [],
          last_reread_at: nil,
          labels: %{},
          roster: %{},
          last_seen: %{},
          marks: %{},
          resumed_at: %{},
          row_order: []
        )
        |> relane()
    end
  end

  defp prune(socket) do
    %{marks: marks, resumed_at: resumed, roster: roster} = socket.assigns

    start =
      DateTime.to_unix(socket.assigns.now, :microsecond) - hours(socket.assigns) * 3_600_000_000

    kept =
      marks
      |> Enum.filter(fn {_id, m} ->
        us = TimelineAdapter.ts_us(m["ts"]) || 0

        m["kind"] == "dialectic" or us >= start or
          (m["event_type"] == "lifecycle_paused" and us >= Map.get(resumed, m["agent_id"], -1))
      end)
      |> Map.new()

    live =
      kept
      |> Map.values()
      |> Enum.flat_map(&lane_ids/1)
      |> Enum.concat(Map.keys(roster))
      |> MapSet.new()

    in_live? = fn {id, _} -> MapSet.member?(live, id) end

    row_order =
      Enum.filter(socket.assigns.row_order, fn {id, _, _} -> MapSet.member?(live, id) end)

    if map_size(kept) == map_size(marks) and length(row_order) == length(socket.assigns.row_order) do
      socket
    else
      socket
      |> assign(
        marks: kept,
        row_order: row_order,
        last_seen: Map.filter(socket.assigns.last_seen, in_live?),
        resumed_at: Map.filter(resumed, in_live?)
      )
      |> relane()
    end
  end

  defp refresh_status(socket) do
    case TimelineFeed.snapshot(socket.assigns.window, socket.assigns.table) do
      {:ok, snap} ->
        assign(socket, source: snap.status, source_error: snap.error, meta: snap.meta)

      :unavailable ->
        assign(socket, source: :no_feed, source_error: feed_not_running())
    end
  end

  defp insert_mark(socket, mark) do
    socket =
      assign(socket,
        last_seen: bump_last_seen(socket.assigns.last_seen, mark),
        resumed_at: note_resume(socket.assigns.resumed_at, mark)
      )

    id = TimelineAdapter.dom_id(mark)

    cond do
      Map.has_key?(socket.assigns.marks, id) ->
        socket

      not in_window?(mark, socket.assigns) ->
        socket

      true ->
        marks = socket.assigns.marks |> Map.put(id, mark) |> cap_provisional() |> Map.new()

        socket
        |> assign(marks: marks)
        |> assign(row_order: join_order(socket.assigns.row_order, mark))
        |> relane()
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

  defp relane(socket), do: assign(socket, relane_assigns(socket.assigns))

  # One light for the person reading: live, slower, or not updating. The
  # detail behind it stays under "connection details".
  defp light(assigns) do
    cond do
      !Phoenix.LiveView.connected?(assigns.socket) ->
        {"connecting", "Connecting…"}

      !assigns.feed_reachable or assigns.source in [:no_feed, :refused] ->
        {"down", "Not updating: " <> (assigns.source_error || feed_not_running())}

      assigns.source == :unavailable ->
        {"down", "Not updating: governance is unreachable, showing the last window"}

      assigns.source == :loading ->
        {"connecting", "Loading…"}

      Map.get(assigns.meta, :firehose) != :connected ->
        {"slow", "Updating about once a minute: the live stream is not connected"}

      true ->
        {"live", "Live: marks appear as agents work"}
    end
  end

  # --- render ---

  attr :lane, :map, required: true
  attr :selected, :any, default: nil

  defp lane_row(assigns) do
    ~H"""
    <div
      id={"agent-#{@lane.id}"}
      class={["tl-row", @lane.depth > 0 && "child", @lane.needs && "needs"]}
    >
      <div class="tl-lab">
        <div class="tl-name" title={@lane.id}>{@lane.name}</div>
        <div :if={@lane.note} class="tl-note" title={@lane.note_title}>{@lane.note}</div>
        <div class="tl-sub">{@lane.sub}</div>
      </div>
      <div class="tl-track">
        <button
          :for={b <- @lane.bands}
          :key={b.id}
          type="button"
          id={b.id}
          class={["tl-band", b.cls, @selected == b.sel && "sel"]}
          style={band_style(b)}
          phx-click="select"
          phx-value-id={b.sel}
          title={b.label}
          aria-label={b.label}
        ></button>
        <button
          :for={m <- @lane.marks}
          :key={m.id}
          type="button"
          id={m.id}
          class={["tl-mk", m.cls, selected?(m, @selected) && "sel"]}
          style={mark_style(m)}
          data-kind={m.kind}
          data-provisional={m.provisional && "true"}
          data-seen-live={m.seen_live && "true"}
          phx-click="select"
          phx-value-id={m.id}
          title={m.label}
          aria-label={m.label}
        >
          <span class="tl-dot">{m.glyph}</span>
        </button>
      </div>
    </div>
    """
  end

  @impl true
  def render(assigns) do
    assigns =
      assigns
      |> assign(:reread_only_reason, checkins_reread_only_reason(assigns))
      |> assign(:latency, latency_summary(assigns.latencies))
      |> assign(:hours, hours(assigns))
      |> assign(:sel_mark, assigns.selected && Map.get(assigns.marks, assigns.selected))
      |> assign(:sel_group, assigns.selected && Map.get(assigns.clusters, assigns.selected))
      |> assign(:in_group, group_of(assigns.clusters, assigns.selected))

    ~H"""
    <div class="mx-auto max-w-7xl p-6 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h1 class="text-2xl font-semibold">Timeline — live</h1>
        <p id="signed-in" class="text-sm opacity-70">
          Signed in as {@operator.label} · <a href="/signout" class="link">Sign out</a>
        </p>
        <div class="flex items-center gap-2">
          <div class="join" id="window-toggle">
            <button
              :for={w <- Portal.TimelineFeed.windows()}
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

      <section id="timeline-header" class="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <% {state, text} = light(assigns) %>
        <span id="live-light" data-state={state} class="inline-flex items-center gap-2 font-medium">
          <span class={["tl-light", state]}></span>{text}
        </span>
        <details id="connection-details" class="text-base-content/70">
          <summary class="cursor-pointer select-none">connection details</summary>
          <div class="grid gap-1 sm:grid-cols-2 mt-2">
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
              Live: {Enum.join(Portal.TimelineAdapter.live_kinds(), ", ")} ·
              re-read only (at least every 60 s while viewed): {Enum.join(
                Portal.TimelineAdapter.reread_only_kinds(),
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
            <div :if={@reread_only_reason} id="checkin-reread-only" class="sm:col-span-2">
              {@reread_only_reason}
            </div>
          </div>
        </details>
      </section>

      <div id="socket-lost" class="alert alert-error text-sm hidden">
        The page socket is disconnected: nothing on this page is updating until it reconnects.
      </div>

      <div :if={@truncated != []} id="truncation-banner" class="alert alert-warning text-sm">
        Truncated: {Enum.join(@truncated, ", ")}. The server keeps the newest rows per kind, so the oldest part of this window is missing for those kinds.
      </div>

      <div
        :if={!@feed_reachable or @source == :no_feed}
        id="feed-unavailable"
        class="alert alert-warning text-sm"
      >
        The timeline feed is unavailable. Nothing is shown until it runs.
      </div>

      <%!-- Only after a successful read: "nothing needs you" is a claim about
           a window, and during an outage it is a claim about an older one. --%>
      <section
        :if={@last_reread_at}
        id="needs-attention"
        class={[
          "rounded-box border p-4 text-sm",
          if(@attention == [], do: "border-base-300", else: "border-error/60")
        ]}
      >
        <p :if={@source != :ok} id="attention-stale" class="text-warning mb-1">
          History is not updating; this is as of the last successful read, {ago(
            @last_reread_at,
            @now
          )}.
        </p>
        <%= if @attention == [] do %>
          <p class="text-base-content/70">Nothing needs your attention right now.</p>
        <% else %>
          <h2 class="font-semibold mb-1">Needs your attention</h2>
          <ul class="space-y-1">
            <li :for={a <- @attention} id={"attention-#{a.agent}"}>
              <button type="button" class="link" phx-click="select" phx-value-id={a.sel}>
                {a.name}
              </button>: {a.reason}
            </li>
          </ul>
        <% end %>
      </section>

      <div class="flex flex-wrap items-center justify-between gap-2">
        <p id="lane-counts" class="text-sm text-base-content/70">
          <% c = counts(@marks) %> Last {@hours} h · {@lanes_total} agent{if @lanes_total == 1,
            do: "",
            else: "s"} · {Map.get(c, "checkin", 0)} check-ins · {Map.get(c, "outcome", 0)} outcomes · {Map.get(
            c,
            "dialectic",
            0
          )} dialectic sessions · times UTC
        </p>
        <.form for={@filter_form} id="agent-filter" phx-change="filter">
          <.input field={@filter_form[:q]} type="text" placeholder="filter agents" />
        </.form>
      </div>

      <section id="timeline-lanes" class="tl-panel" style={lane_vars(@now, @anchor, @hours)}>
        <div class="tl-row tl-axis">
          <div class="tl-lab text-xs uppercase tracking-wide text-base-content/60">Agent</div>
          <div class="tl-track">
            <span
              :for={{pct, label} <- axis_ticks(@now, @hours)}
              class="tl-tick"
              style={"left:#{pct}%"}
            >
              {label}
            </span>
          </div>
        </div>
        <p :if={@lanes_total == 0} id="lanes-empty" class="px-5 py-4 text-sm text-base-content/60">
          Nothing in this window yet.
        </p>
        <p
          :if={@lanes_total > 0 and @lanes_any == 0}
          id="lanes-nomatch"
          class="px-5 py-4 text-sm text-base-content/60"
        >
          No agent matches “{@filter}”.
        </p>
        <.lane_row :for={lane <- @lanes} :key={lane.id} lane={lane} selected={@selected} />
        <div :if={@background_count > 0} id="background-agents" class="tl-row tl-group">
          <div class="tl-lab">
            <button
              type="button"
              id="toggle-background"
              phx-click="show_background"
              class="tl-name text-left cursor-pointer"
              aria-expanded={to_string(@background_open)}
            >
              {if @background_open, do: "▾", else: "▸"} Background agents ({@background_count})
            </button>
            <div class="tl-sub">run on their own schedule</div>
          </div>
          <div class="tl-groupnames">{@background_names}</div>
        </div>
        <.lane_row
          :for={lane <- @background_lanes}
          :key={lane.id}
          lane={lane}
          selected={@selected}
        />
      </section>

      <div :if={@lanes_matching > length(@lanes) or @show_all_agents} class="flex items-center gap-3">
        <button id="show-all-agents" phx-click="show_all_agents" class="btn btn-xs btn-ghost">
          {if @show_all_agents,
            do: "show the 25 most recent",
            else: "show all #{@lanes_matching} agents"}
        </button>
        <span class="text-xs text-base-content/60">
          {length(@lanes)} of {@lanes_matching} shown, most recent first
        </span>
      </div>

      <div class="tl-legend text-xs text-base-content/70" id="lane-legend">
        <span><span class="tl-mk checkin proceed"><span class="tl-dot"></span></span>proceed</span>
        <span><span class="tl-mk checkin guide"><span class="tl-dot"></span></span>guide</span>
        <span><span class="tl-mk checkin pause"><span class="tl-dot"></span></span>pause</span>
        <span>
          <span class="tl-mk checkin substrate"><span class="tl-dot"></span></span>hook observation
        </span>
        <span>
          <span class="tl-mk checkin unassessed" data-provisional="true"><span class="tl-dot"></span></span>live, not yet confirmed
        </span>
        <span><span class="tl-mk outcome"><span class="tl-dot">✓</span></span>outcome passed</span>
        <span><span class="tl-mk outcome bad"><span class="tl-dot">✕</span></span>outcome failed</span>
        <span><span class="tl-mk knowledge"><span class="tl-dot"></span></span>finding shared</span>
        <span><span class="tl-mk lineage"><span class="tl-dot"></span></span>lineage</span>
        <span><span class="tl-mk lifecycle"><span class="tl-dot"></span></span>lifecycle</span>
        <span>
          <span class="tl-mk group alert"><span class="tl-dot">3</span></span>events close together, coloured by the most notable
        </span>
        <span><span class="tl-band pause"></span>paused</span>
        <span><span class="tl-band dialectic"></span>dialectic review</span>
      </div>

      <section id="mark-detail" class="rounded-box border border-base-300 p-5 text-sm">
        <%= cond do %>
          <% @sel_group -> %>
            <h2 class="font-semibold mb-1">{length(@sel_group.members)} events close together</h2>
            <p class="text-base-content/70 mb-3">
              {utc(@sel_group.at)} to {utc(@sel_group.until)}. Select one to see its record.
            </p>
            <ul id="group-members" class="space-y-1">
              <li :for={mid <- @sel_group.members} :if={m = @marks[mid]}>
                <button
                  type="button"
                  id={"member-#{mid}"}
                  phx-click="select"
                  phx-value-id={mid}
                  class="link link-hover text-left"
                >
                  {joined([utc(m["ts"]), mark_title(m, @roster), assessment(m)])}
                </button>
              </li>
            </ul>
          <% @sel_mark -> %>
            <button
              :if={@in_group}
              type="button"
              id="back-to-group"
              phx-click="select"
              phx-value-id={@in_group.id}
              class="link link-hover text-xs text-base-content/70 mb-2"
            >
              ← {length(@in_group.members)} events close together
            </button>
            <h2 class="font-semibold mb-2">{mark_title(@sel_mark, @roster)}</h2>
            <p :if={detail_note(@sel_mark)} class="text-base-content/70 mb-3">
              {detail_note(@sel_mark)}
            </p>
            <dl class="grid gap-x-4 gap-y-1 sm:grid-cols-[10rem_1fr]">
              <%= for {k, v} <- detail_rows(@sel_mark, @labels, @roster) do %>
                <dt class="text-base-content/60">{k}</dt>
                <dd class="break-words">{v}</dd>
              <% end %>
            </dl>
          <% true -> %>
            <p class="text-base-content/60">Select a mark to see its record.</p>
        <% end %>
      </section>
    </div>
    """
  end

  # Why the feed is not running, when the hub knows: without its read-only key
  # the timeline does not start (config/runtime.exs).
  defp feed_not_running do
    governance = Application.get_env(:portal, :governance, [])

    if Keyword.get(governance, :read_token) in [nil, ""],
      do: "the timeline feed is not running (the hub has no read-only key, PORTAL_READ_TOKEN)",
      else: "the timeline feed is not running"
  end
end
