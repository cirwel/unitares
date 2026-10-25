defmodule DialecticLiveWeb.TimelineLive do
  @moduledoc """
  The live Timeline pane (portal stage 2,
  `docs/proposals/active/portal-live-timeline-v0.md` §3.4).

  Reads `DialecticLive.TimelineFeed.snapshot/2` on mount and on each re-read,
  and adds provisional marks as they arrive, without a refetch. It draws the
  same lanes as the dashboard's Timeline tab (`dashboard/redesign/sections/
  timeline.js`): one row per agent across the window, marks placed in time,
  bands for pauses and dialectic reviews, subagents nested under a visible
  parent. Selecting a mark shows its record. Around the lanes:

    * rows ordered by the most recent event of each family, recomputed on
      re-read only so rows do not jump while you read; an agent first seen
      live joins at the top. The first 25 show until "show all", with a text
      filter on label, id or model type;
    * each row shows what its agent last said in the window: its own report,
      or a hook's excerpt of its reply;
    * agents tagged `autonomous` (they run on their own schedule) fold into
      one collapsed group under the working sessions;
    * a "needs your attention" box naming agents paused with no later resume
      or whose latest check-in drew a pause verdict;
    * one light saying whether the page is live, slower or not updating.
      Under "connection details" sit the firehose and LiveView socket state,
      time since the last frame and the last re-read, which kinds are live
      and which re-read only, and the event-to-push latency (frame timestamp
      to the mark's arrival here, server side) as trial telemetry.

  The window slides without re-rendering the marks: each mark carries its
  time as `--t` (seconds from a per-page anchor, small enough for `calc()`
  to stay precise), and the lanes container sets `--start`, `--span` and
  `--now`, which the one-second tick updates (`assets/css/app.css`).

  The M1 form, a reverse-chronological feed plus an "agents now" strip, read
  as a long list beside the tab in the operator's first look (2026-10-25), so
  the pane took the tab's lanes. A second look the same day found the lanes
  said who was alive but not what anyone was doing, which the notes, the
  background group and the attention box answer.

  Grouping and styling use record fields only (the background group reads
  the roster's `tags`); nothing here branches on an agent's name, resident
  or vendor. The pane only reads: it makes no
  governance call of its own.
  """
  use DialecticLiveWeb, :live_view

  alias DialecticLive.{Firehose, TimelineAdapter, TimelineFeed}

  @agents_cap 25
  @latency_samples 50
  @ticks 6
  # The feed's own provisional cap.
  @provisional_cap 500
  @prune_every_s 10

  # How a lineage child relates to its parent, by its spawn_reason. Mirrors
  # the tab and src/identity/lineage_semantics.py: only dispatched children
  # are subagents; explicit/new_session are successions and compaction
  # continues the same work.
  @relations %{
    "subagent" => :dispatched,
    "dialectic_reviewer" => :dispatched,
    "dispatch" => :dispatched,
    "compaction" => :continuation,
    "explicit" => :succession,
    "new_session" => :succession
  }

  @impl true
  def mount(_params, _session, socket) do
    cfg = Application.get_env(:dialectic_live, :timeline, [])
    feed = Keyword.get(cfg, :feed, TimelineFeed)
    table = Keyword.get(cfg, :table, TimelineFeed.default_table())
    window = TimelineFeed.window_for_hours(Keyword.get(cfg, :default_window_hours, 1))
    pubsub = Keyword.get(cfg, :pubsub, DialecticLive.PubSub)
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

        marks =
          all
          |> Enum.filter(&visible?(&1, socket.assigns.substrate))
          |> Map.new(&{TimelineAdapter.dom_id(&1), &1})

        roster = for %{"agent_id" => id} = a <- snap.agents, is_binary(id), into: %{}, do: {id, a}

        resumed_seed =
          if snap.status == :ok and snap.last_reread_at != socket.assigns[:last_reread_at],
            do: %{},
            else: socket.assigns.resumed_at

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
          resumed_at: Enum.reduce(all, resumed_seed, &note_resume(&2, &1)),
          selected:
            carry_selection(
              socket.assigns.selected,
              socket.assigns.marks,
              marks,
              Map.get(snap, :paired, %{})
            ),
          row_order: order_rows(roster, marks, last_seen)
        )
        |> relane()

      :unavailable ->
        socket
        |> assign(
          source: :no_feed,
          source_error: "the timeline feed is not running",
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

  # A re-read that confirms a provisional mark gives it a history id (`p-`
  # becomes `h-`), so a selection on the live mark follows it to the row
  # that absorbed it rather than clearing as the record arrives. Follow the
  # feed's mark-to-row pairing first; fall back to the latest matching row when
  # the mark arrived directly on the page without a feed pairing.
  defp carry_selection(nil, _old, _new, _paired), do: nil

  defp carry_selection(id, old, new, paired) do
    cond do
      Map.has_key?(new, id) ->
        id

      paired_id = Map.get(paired, id) ->
        if Map.has_key?(new, paired_id), do: paired_id, else: nil

      mark = Map.get(old, id) ->
        new
        |> Enum.filter(fn {_new_id, row} -> TimelineAdapter.matches?(mark, row) end)
        |> Enum.max_by(fn {_new_id, row} -> sort_key(row) end, fn -> {nil, nil} end)
        |> elem(0)

      true ->
        nil
    end
  end

  # Each agent's latest resume, kept apart from the marks: it is what closes
  # a pause band, so it must survive the cap and the window prune that drop
  # the resume mark itself.
  defp note_resume(acc, %{"event_type" => "lifecycle_resumed", "agent_id" => a} = m)
       when is_binary(a) do
    case TimelineAdapter.ts_us(m["ts"]) do
      nil -> acc
      us -> Map.update(acc, a, us, &max(&1, us))
    end
  end

  defp note_resume(acc, _mark), do: acc

  # Drops marks that slid out of the window, so a history outage with the
  # firehose still flowing cannot grow the page without bound (the cap on
  # insert bounds a burst inside the window). A successful re-read replaces
  # the set anyway. A pause stays while no later resume is known: its band
  # runs to now. Dialectic sessions arrive on re-read only, so a re-read
  # bounds them. Rows and per-agent state then keep only agents the roster
  # names or a kept mark is drawn for, whether the mark left by age or by
  # the cap.
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

  defp cap_provisional(marks) do
    {live, held} = Enum.split_with(marks, fn {_id, m} -> m["provisional"] == true end)

    if length(live) > @provisional_cap do
      held ++
        (live |> Enum.sort_by(fn {_, m} -> sort_key(m) end, :desc) |> Enum.take(@provisional_cap))
    else
      marks
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

  # A delta is drawn on its owner's lane. A delta already held (a re-read
  # that already had it, handled before its queued delta) changes nothing,
  # and one from before the window would sit off the left edge, so neither
  # is added. The agent was seen either way. An owner with no row yet joins
  # at the top; the rest keep their order until the next re-read.
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

  defp in_window?(mark, assigns) do
    case TimelineAdapter.ts_us(mark["ts"]) do
      nil ->
        false

      us ->
        start = DateTime.to_unix(assigns.now, :microsecond) - hours(assigns) * 3_600_000_000
        us >= start
    end
  end

  defp join_order(order, mark) do
    present = MapSet.new(order, fn {id, _depth, _parent} -> id end)

    mark
    |> lane_ids()
    |> Enum.reject(&MapSet.member?(present, &1))
    |> Enum.reverse()
    |> Enum.reduce(order, fn id, acc -> [{id, 0, nil} | acc] end)
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
    for %{"agent_id" => id} = a <- agents, is_binary(id), into: %{} do
      {id, TimelineAdapter.ts_us(a["last_activity_at"])}
    end
  end

  # A lineage mark belongs to its parent's row; the child gets a row too.
  defp bump_last_seen(last_seen, mark) do
    case TimelineAdapter.ts_us(mark["ts"]) do
      nil ->
        last_seen

      us ->
        Enum.reduce(lane_ids(mark), last_seen, fn id, acc ->
          Map.update(acc, id, us, &max(&1 || 0, us))
        end)
    end
  end

  # --- lanes ---

  # The row a mark is drawn on: a lineage mark on the parent that declared
  # the child, every other mark on its own agent.
  defp owner(%{"kind" => "lineage", "parent_agent_id" => parent}) when is_binary(parent),
    do: parent

  defp owner(%{"agent_id" => id}) when is_binary(id), do: id
  defp owner(_mark), do: nil

  # Every row a mark implies: its owner, and a lineage child's own row.
  defp lane_ids(mark) do
    child = if is_binary(mark["agent_id"]), do: [mark["agent_id"]], else: []
    Enum.uniq(Enum.filter([owner(mark)], &is_binary/1) ++ child)
  end

  defp parent_of(id, roster, lineage_parents) do
    from_roster = get_in(roster, [id, "parent_agent_id"])
    if is_binary(from_roster), do: from_roster, else: Map.get(lineage_parents, id)
  end

  # Rows as `{agent_id, depth, parent}`. A family sorts by its most recent
  # member, so a busy subagent under a quiet parent still lands near the top
  # rather than past the row cap; children follow their parent.
  defp order_rows(roster, marks, last_seen) do
    ids =
      marks
      |> Map.values()
      |> Enum.flat_map(&lane_ids/1)
      |> Enum.concat(Map.keys(roster))
      |> Enum.uniq()

    present = MapSet.new(ids)

    lineage_parents =
      for {_, %{"kind" => "lineage", "agent_id" => child, "parent_agent_id" => parent}} <- marks,
          is_binary(child) and is_binary(parent),
          into: %{},
          do: {child, parent}

    parent =
      Map.new(ids, fn id ->
        p = parent_of(id, roster, lineage_parents)
        {id, if(p != id and MapSet.member?(present, p), do: p, else: nil)}
      end)

    kids = Enum.group_by(Enum.filter(ids, &parent[&1]), &parent[&1])
    recency = Map.new(ids, &{&1, family_last(&1, kids, last_seen, MapSet.new())})
    by_recency = Enum.sort_by(ids, &recency[&1], :desc)

    roots = Enum.filter(by_recency, &is_nil(parent[&1]))

    {ordered, placed} =
      Enum.reduce(roots, {[], MapSet.new()}, &place(&1, 0, &2, kids, recency, parent))

    # Anything left sits in a parent cycle; place it flat.
    {ordered, _} =
      Enum.reduce(by_recency, {ordered, placed}, fn id, {acc, seen} ->
        if MapSet.member?(seen, id),
          do: {acc, seen},
          else: {[{id, 0, nil} | acc], MapSet.put(seen, id)}
      end)

    Enum.reverse(ordered)
  end

  defp family_last(id, kids, last_seen, seen) do
    own = Map.get(last_seen, id) || 0

    if MapSet.member?(seen, id) do
      own
    else
      seen = MapSet.put(seen, id)
      Enum.reduce(Map.get(kids, id, []), own, &max(&2, family_last(&1, kids, last_seen, seen)))
    end
  end

  defp place(id, depth, {acc, placed}, kids, recency, parent) do
    if MapSet.member?(placed, id) do
      {acc, placed}
    else
      acc = [{id, depth, if(depth > 0, do: parent[id])} | acc]

      kids
      |> Map.get(id, [])
      |> Enum.sort_by(&recency[&1], :desc)
      |> Enum.reduce(
        {acc, MapSet.put(placed, id)},
        &place(&1, depth + 1, &2, kids, recency, parent)
      )
    end
  end

  # Builds the shown lanes into an assign, so the one-second tick, which only
  # moves the window's CSS variables, never re-renders a mark.
  # Agents that run on their own schedule fold into one collapsed group, so
  # the rows on top are the sessions doing work. A family follows its root.
  defp relane(socket) do
    %{row_order: order, marks: marks, labels: labels, roster: roster} = socket.assigns
    by_owner = marks |> Enum.group_by(fn {_id, m} -> owner(m) end) |> Map.delete(nil)
    filter = String.downcase(socket.assigns.filter)

    matching =
      case filter do
        "" ->
          order

        q ->
          Enum.filter(order, fn {id, _, _} ->
            Enum.any?([labels[id], id, get_in(roster, [id, "model_type"])], fn v ->
              is_binary(v) and String.contains?(String.downcase(v), q)
            end)
          end)
      end

    root = family_roots(order)

    {background, working} =
      Enum.split_with(matching, fn {id, _, _} -> background?(roster, root[id]) end)

    shown =
      if socket.assigns.show_all_agents, do: working, else: Enum.take(working, @agents_cap)

    attention = needs_attention(order, by_owner, labels, socket.assigns.resumed_at)
    flagged = MapSet.new(attention, & &1.agent)
    lane = &build_lane(&1, by_owner, flagged, socket.assigns)
    open_background = socket.assigns.show_background or filter != ""

    assign(socket,
      lanes: Enum.map(shown, lane),
      background_lanes: if(open_background, do: Enum.map(background, lane), else: []),
      background_open: open_background,
      background_count: length(background),
      background_names:
        for({id, 0, _} <- background, do: agent_name(labels, id)) |> Enum.join(" · "),
      attention: attention,
      lanes_matching: length(working),
      lanes_any: length(matching),
      lanes_total: length(order)
    )
  end

  defp build_lane({id, depth, parent}, by_owner, flagged, assigns) do
    %{anchor: anchor, labels: labels, roster: roster} = assigns
    own = by_owner |> Map.get(id, []) |> Enum.sort_by(fn {_, m} -> sort_key(m) end)
    meta = Map.get(roster, id, %{})
    note = latest_note(own)

    %{
      id: id,
      depth: depth,
      name: agent_name(labels, id),
      note: note && elem(note, 0),
      note_title: note && elem(note, 0) <> " (" <> utc(elem(note, 1)) <> ")",
      needs: MapSet.member?(flagged, id),
      sub: lane_sub(depth, parent, meta, own, labels),
      marks:
        for(
          {mid, m} <- own,
          m["kind"] != "dialectic",
          do: mark_view(mid, m, anchor, roster, labels)
        ),
      bands: bands(own, anchor, labels, Map.get(assigns.resumed_at, id))
    }
  end

  # Each row's family root: the nearest row above it at depth 0.
  defp family_roots(order) do
    {pairs, _} =
      Enum.map_reduce(order, nil, fn {id, depth, _}, root ->
        root = if depth == 0 or is_nil(root), do: id, else: root
        {{id, root}, root}
      end)

    Map.new(pairs)
  end

  # By the `autonomous` capability tag, which the server's resident
  # classifier also reads; never by name.
  defp background?(roster, id) do
    case get_in(roster, [id, "tags"]) do
      tags when is_list(tags) -> "autonomous" in tags
      _ -> false
    end
  end

  @turn_summary ~r/^Turn summary \([^)]*\);\s*(response excerpt:\s*)?/

  # What the agent last said in the window, so the row reads as what it is
  # doing: its own report or a hook's excerpt of its reply, with the hook's
  # framing dropped. Only without either does a mechanical note (an edit or
  # tool count) stand in.
  defp latest_note(own) do
    notes =
      for {_id, %{"kind" => "checkin"} = m} <- Enum.reverse(own),
          text = txt(m["report_excerpt"]),
          is_binary(text),
          do: {text, m}

    case Enum.find(notes, fn {text, m} ->
           m["authored_by"] == "agent" or Regex.match?(@turn_summary, text)
         end) || List.first(notes) do
      nil -> nil
      {text, m} -> {note_text(text), m["ts"]}
    end
  end

  defp note_text(text) do
    case Regex.replace(@turn_summary, text, "") |> String.trim() do
      "" -> text
      stripped -> stripped
    end
  end

  # Rows that need a person, each pointing at the mark that says so: an
  # agent paused with no later resume, or whose latest recorded check-in drew
  # a pause verdict.
  defp needs_attention(order, by_owner, labels, resumed_at) do
    Enum.flat_map(order, fn {id, _depth, _parent} ->
      case attention(Map.get(by_owner, id, []), Map.get(resumed_at, id)) do
        nil -> []
        item -> [Map.merge(item, %{agent: id, name: agent_name(labels, id)})]
      end
    end)
  end

  defp attention(own, resumed_us) do
    newest = Enum.sort_by(own, fn {_, m} -> sort_key(m) end, :desc)
    pause = Enum.find(newest, fn {_, m} -> m["event_type"] == "lifecycle_paused" end)
    resume = Enum.find(newest, fn {_, m} -> m["event_type"] == "lifecycle_resumed" end)

    verdict =
      Enum.find(newest, fn {_, m} -> m["kind"] == "checkin" and is_binary(verdict_of(m)) end)

    cond do
      pause && open_pause?(pause, resume, resumed_us) ->
        {pid, pm} = pause
        %{sel: pid, reason: "paused since " <> clock(pm["ts"])}

      verdict && action_class(verdict_of(elem(verdict, 1))) == "pause" ->
        {vid, vm} = verdict
        %{sel: vid, reason: "its latest check-in drew a pause verdict at " <> clock(vm["ts"])}

      true ->
        nil
    end
  end

  # A history row's persisted action, or a live mark's EISV assessment: the
  # same verdict either way, as `assessment/1` reads it.
  defp verdict_of(m), do: txt(m["action"]) || live_verdict(m)

  defp open_pause?({_, pause}, resume, resumed_us) do
    resume_us = if resume, do: sort_key(elem(resume, 1)), else: -1
    sort_key(pause) > max(resume_us, resumed_us || -1)
  end

  defp lane_sub(depth, parent, meta, _own, labels) when depth > 0 and is_binary(parent),
    do:
      "↳ " <> relation_row(relation_of(meta["spawn_reason"])) <> " " <> agent_name(labels, parent)

  defp lane_sub(_depth, _parent, meta, own, _labels) do
    checkins =
      Enum.count(own, fn {_, m} -> m["kind"] == "checkin" and m["authored_by"] == "agent" end)

    joined([
      txt(meta["model_type"]),
      txt(meta["trust_tier"]),
      "#{checkins} check-in#{if checkins == 1, do: "", else: "s"}"
    ])
  end

  defp relation_of(reason) when is_binary(reason), do: Map.get(@relations, reason, :unknown)
  defp relation_of(_), do: :unknown

  defp relation_row(:dispatched), do: "subagent of"
  defp relation_row(:succession), do: "successor of"
  defp relation_row(:continuation), do: "continues"
  defp relation_row(:unknown), do: "declared lineage from"

  defp relation_mark(:dispatched), do: "Started a subagent"
  defp relation_mark(:succession), do: "Handed off to a successor"
  defp relation_mark(:continuation), do: "Continued after compaction"
  defp relation_mark(:unknown), do: "Declared lineage"

  defp relation_name(:dispatched), do: "dispatched subagent"
  defp relation_name(:succession), do: "succession (handoff)"
  defp relation_name(:continuation), do: "context continuation"
  defp relation_name(:unknown), do: "declared, kind not registered"

  defp lineage_relation(mark, roster),
    do: relation_of(get_in(roster, [mark["agent_id"], "spawn_reason"]))

  # Seconds from the page's anchor, one decimal: small numbers keep calc()
  # precise in every engine.
  defp rel_s(ts, anchor) do
    case TimelineAdapter.ts_us(ts) do
      nil -> nil
      us -> Float.round(us / 1_000_000 - anchor, 1)
    end
  end

  defp mark_view(id, m, anchor, roster, labels) do
    {cls, glyph} = mark_class(m)

    %{
      id: id,
      kind: txt(m["kind"]),
      cls: cls,
      glyph: glyph,
      t: rel_s(m["ts"], anchor),
      provisional: m["provisional"] == true,
      seen_live: m["seen_live"] == true,
      label:
        joined([
          mark_title(m, roster),
          agent_name(labels, owner(m)),
          utc(m["ts"]),
          assessment(m)
        ])
    }
  end

  defp mark_class(%{"kind" => "checkin"} = m) do
    case m["authored_by"] do
      "agent" ->
        {"checkin " <> action_class(m["action"]), ""}

      # A live check-in: the frame cannot say who wrote it. Its shape comes
      # from the EISV assessment when one rode along.
      "unknown" ->
        case live_verdict(m) do
          nil -> {"checkin unassessed", ""}
          verdict -> {"checkin " <> action_class(verdict), ""}
        end

      _ ->
        {"checkin substrate", ""}
    end
  end

  defp mark_class(%{"kind" => "outcome", "is_bad" => true}), do: {"outcome bad", "✕"}
  defp mark_class(%{"kind" => "outcome"}), do: {"outcome", "✓"}
  defp mark_class(%{"kind" => kind}) when kind in ~w(knowledge lineage lifecycle), do: {kind, ""}
  defp mark_class(_m), do: {"other", ""}

  defp live_verdict(%{"assessment" => %{"verdict" => v}}) when is_binary(v), do: v
  defp live_verdict(_m), do: nil

  defp action_class(action) do
    case action |> to_string() |> String.downcase() do
      "guide" -> "guide"
      v when v in ["pause", "reject"] -> "pause"
      _ -> "proceed"
    end
  end

  # Pause bands pair lifecycle_paused with the next lifecycle_resumed on the
  # same row; a resume with no pause in the window started before it, and a
  # pause with no resume runs to now. Dialectic sessions draw from creation
  # to close, or to now while active; the server's `ts` may precede the
  # window, which the CSS clamps.
  defp bands(own, anchor, labels, resumed_us) do
    {pauses, open} =
      Enum.reduce(own, {[], nil}, fn
        {id, %{"kind" => "lifecycle", "event_type" => "lifecycle_paused"} = m}, {acc, nil} ->
          {acc, {id, m}}

        {id, %{"kind" => "lifecycle", "event_type" => "lifecycle_paused"} = m},
        {acc, {_from_id, from} = held} ->
          from_us = TimelineAdapter.ts_us(from["ts"]) || 0
          new_us = TimelineAdapter.ts_us(m["ts"]) || 0

          if is_integer(resumed_us) and resumed_us >= from_us and resumed_us < new_us do
            {[close_evicted_pause(held, anchor, resumed_us) | acc], {id, m}}
          else
            {acc, held}
          end

        {id, %{"kind" => "lifecycle", "event_type" => "lifecycle_resumed"} = m}, {acc, held} ->
          {from_id, from} = held || {id, nil}

          band = %{
            id: "pause-" <> from_id,
            sel: from_id,
            cls: "pause",
            t: if(from, do: rel_s(from["ts"], anchor), else: :before),
            t2: rel_s(m["ts"], anchor),
            label:
              "Paused, " <>
                if(from, do: utc(from["ts"]), else: "before the window") <> " to " <> utc(m["ts"])
          }

          {[band | acc], nil}

        _, state ->
          state
      end)

    pauses =
      case open do
        nil ->
          pauses

        # The resume mark may be gone (capped or aged out) while the agent's
        # latest resume is still known: the pause ended there.
        {_id, m} = held ->
          pause_us = TimelineAdapter.ts_us(m["ts"]) || 0

          band =
            if is_integer(resumed_us) and resumed_us >= pause_us do
              close_evicted_pause(held, anchor, resumed_us)
            else
              pause_band(held, anchor, nil, "Paused since " <> utc(m["ts"]))
            end

          [band | pauses]
      end

    dialectic =
      for {id, %{"kind" => "dialectic"} = m} <- own do
        %{
          id: "dialectic-" <> id,
          sel: id,
          cls: "dialectic",
          t: rel_s(m["ts"], anchor),
          t2: rel_s(m["end"], anchor),
          label:
            joined([
              "Dialectic review",
              txt(m["status"]),
              m["reviewer_agent_id"] && "reviewer " <> agent_name(labels, m["reviewer_agent_id"])
            ])
        }
      end

    Enum.reverse(pauses) ++ dialectic
  end

  defp pause_band({id, m}, anchor, t2, label) do
    %{
      id: "pause-" <> id,
      sel: id,
      cls: "pause",
      t: rel_s(m["ts"], anchor),
      t2: t2,
      label: label
    }
  end

  defp close_evicted_pause({_id, m} = held, anchor, resumed_us) do
    t2 = Float.round(resumed_us / 1_000_000 - anchor, 1)
    pause_band(held, anchor, t2, "Paused, " <> utc(m["ts"]) <> ", since resumed")
  end

  defp sort_key(mark), do: TimelineAdapter.ts_us(mark["ts"]) || 0

  defp hours(assigns), do: TimelineFeed.window_hours(assigns.window)

  # --- formatting ---

  defp agent_name(labels, id) when is_binary(id) do
    case Map.get(labels, id) do
      label when is_binary(label) and label != "" -> label
      _ -> String.slice(id, 0, 8)
    end
  end

  defp agent_name(_labels, _id), do: "unknown agent"

  defp utc(ts) do
    case TimelineAdapter.ts_us(ts) do
      nil -> "—"
      us -> us |> DateTime.from_unix!(:microsecond) |> Calendar.strftime("%Y-%m-%d %H:%M:%S UTC")
    end
  end

  defp clock(ts) do
    case TimelineAdapter.ts_us(ts) do
      nil -> "an unknown time"
      us -> us |> DateTime.from_unix!(:microsecond) |> Calendar.strftime("%H:%M UTC")
    end
  end

  # One light for the person reading: live, slower, or not updating. The
  # detail behind it stays under "connection details".
  defp light(assigns) do
    cond do
      !Phoenix.LiveView.connected?(assigns.socket) ->
        {"connecting", "Connecting…"}

      !assigns.feed_reachable or assigns.source in [:no_feed, :refused] ->
        {"down", "Not updating: " <> (assigns.source_error || "the timeline feed is not running")}

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

  defp ago(nil, _now), do: "never"

  defp ago(%DateTime{} = then, now), do: ago_us(DateTime.to_unix(then, :microsecond), now)

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

  # The window's CSS variables, in seconds from the anchor.
  defp lane_vars(now, anchor, hours) do
    span = hours * 3600
    at = DateTime.to_unix(now) - anchor
    "--now:#{at};--start:#{at - span};--span:#{span}"
  end

  defp axis_ticks(now, hours) do
    span = hours * 3600
    end_s = DateTime.to_unix(now)

    for i <- 0..@ticks do
      label =
        if i == @ticks,
          do: "now",
          else:
            (end_s - span + div(i * span, @ticks))
            |> DateTime.from_unix!()
            |> Calendar.strftime("%H:%M")

      {Float.round(i * 100 / @ticks, 3), label}
    end
  end

  defp mark_style(%{t: nil}), do: "display:none"
  defp mark_style(%{t: t}), do: "--t:#{t}"

  defp band_style(%{t: :before} = b), do: "--t:-1000000000" <> band_end(b)
  defp band_style(%{t: nil}), do: "display:none"
  defp band_style(%{t: t} = b), do: "--t:#{t}" <> band_end(b)

  # An open band leaves --t2 unset and the CSS falls back to --now.
  defp band_end(%{t2: nil}), do: ""
  defp band_end(%{t2: t2}), do: ";--t2:#{t2}"

  defp counts(marks) do
    Enum.reduce(Map.values(marks), %{}, fn m, acc -> Map.update(acc, m["kind"], 1, &(&1 + 1)) end)
  end

  # Record fields arrive from the server as JSON; anything that is not text or
  # a number renders as absent rather than raising in a template.
  defp txt(v) when is_binary(v) and v != "", do: v
  defp txt(v) when is_number(v), do: to_string(v)
  defp txt(_), do: nil

  defp joined(parts), do: parts |> Enum.filter(&is_binary/1) |> Enum.join(" · ")

  defp num(v) when is_number(v), do: :erlang.float_to_binary(v / 1, decimals: 2)
  defp num(_), do: nil

  defp mark_title(%{"kind" => "checkin"} = m, _roster) do
    case m["authored_by"] do
      "agent" -> "Check-in · " <> (txt(m["action"]) || "unknown")
      "unknown" -> "Live check-in"
      _ -> "Hook observation"
    end
  end

  defp mark_title(%{"kind" => "outcome"} = m, _roster),
    do: "Outcome · " <> if(m["is_bad"] == true, do: "failed", else: "passed")

  defp mark_title(%{"kind" => "knowledge"}, _roster), do: "Finding shared"

  defp mark_title(%{"kind" => "lineage"} = m, roster),
    do: relation_mark(lineage_relation(m, roster))

  defp mark_title(%{"kind" => "lifecycle"} = m, _roster) do
    case txt(m["event_type"]) do
      nil -> "Lifecycle"
      t -> t |> String.replace_prefix("lifecycle_", "") |> String.replace("_", " ")
    end
  end

  defp mark_title(%{"kind" => "dialectic"}, _roster), do: "Dialectic review"
  defp mark_title(m, _roster), do: txt(m["kind"]) || "event"

  # The check-in verdict is a governance assessment, not part of the core
  # record. A live mark carries it as an EISV decoration; a history row carries
  # the persisted `action`, which is the same verdict. Both read the same way,
  # so a mark does not change label when the re-read confirms it.
  defp assessment(%{"assessment" => %{"verdict" => verdict}}), do: assessment_text(txt(verdict))
  defp assessment(%{"kind" => "checkin", "action" => action}), do: assessment_text(txt(action))
  defp assessment(_m), do: nil

  defp assessment_text(nil), do: nil
  defp assessment_text(verdict), do: "assessment: " <> verdict

  # The selected record as label/value rows, mirroring the tab's panel.
  defp detail_rows(m, labels, roster) do
    name = fn id -> if is_binary(id), do: agent_name(labels, id), else: "—" end
    or_dash = fn v -> txt(v) || "—" end
    base = [{"Agent", name.(owner(m))}, {"Time", utc(m["ts"])}]

    kind_rows =
      case m["kind"] do
        "checkin" ->
          by =
            case m["authored_by"] do
              "agent" -> "The agent (sync_state)"
              "unknown" -> "Not known until the re-read confirms it"
              _ -> "A hook, not the agent"
            end

          [
            {"Written by", by},
            {"Action", or_dash.(m["action"])},
            {"Verdict", or_dash.(m["verdict"])},
            {"Risk", num(m["risk_score"]) || "—"}
          ] ++
            if m["authored_by"] == "agent" do
              [
                {"Report",
                 txt(m["report_excerpt"]) || "Report text not retained for this check-in."}
              ] ++
                if Map.has_key?(m, "confidence_reported"),
                  do: [{"Reported confidence", num(m["confidence_reported"]) || "none reported"}],
                  else: []
            else
              []
            end ++
            if(assessment(m) && m["authored_by"] == "unknown",
              do: [{"Assessment", live_verdict(m) || "—"}],
              else: []
            )

        "outcome" ->
          [
            {"Type", or_dash.(m["outcome_type"])},
            {"Result", if(m["is_bad"] == true, do: "failed", else: "passed")},
            {"Score", txt(m["outcome_score"]) || "—"},
            {"Reported confidence", num(m["reported_confidence"]) || "none reported"},
            {"Verified by", (txt(m["verification_source"]) || "—") |> String.replace("_", " ")}
          ]

        "knowledge" ->
          [{"Summary", or_dash.(m["summary"])}, {"Type", or_dash.(m["discovery_type"])}]

        "lineage" ->
          relation = lineage_relation(m, roster)

          [
            {"Child", name.(m["agent_id"])},
            {"Parent", name.(m["parent_agent_id"])},
            {"Relationship", relation_name(relation)},
            {"Spawn reason",
             txt(get_in(roster, [m["agent_id"], "spawn_reason"])) || "not recorded"}
          ]

        "lifecycle" ->
          [
            {"Event", (txt(m["event_type"]) || "—") |> String.replace("_", " ")},
            {"Reason", or_dash.(m["reason"])}
          ]

        "dialectic" ->
          [
            {"Closed", if(m["end"], do: utc(m["end"]), else: "still open")},
            {"Reviewer", name.(m["reviewer_agent_id"])},
            {"Status", or_dash.(m["status"])},
            {"Phase", or_dash.(m["phase"])}
          ]

        _ ->
          []
      end

    base ++ kind_rows
  end

  defp detail_note(m) do
    cond do
      m["provisional"] == true ->
        "Arrived live. A re-read confirms it against the server's history."

      m["seen_live"] == true ->
        "Seen live before the re-read confirmed it."

      m["kind"] == "checkin" and m["authored_by"] not in ["agent", "unknown"] ->
        "Recorded automatically. It is evidence about the agent, not a claim the agent made."

      true ->
        nil
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
          class={["tl-mk", m.cls, @selected == m.id && "sel"]}
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

    ~H"""
    <div class="mx-auto max-w-7xl p-6 space-y-4">
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
        <span><span class="tl-band pause"></span>paused</span>
        <span><span class="tl-band dialectic"></span>dialectic review</span>
      </div>

      <section id="mark-detail" class="rounded-box border border-base-300 p-5 text-sm">
        <%= if @sel_mark do %>
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
        <% else %>
          <p class="text-base-content/60">Select a mark to see its record.</p>
        <% end %>
      </section>
    </div>
    """
  end
end
