defmodule PortalWeb.TimelineLive.Lanes do
  @moduledoc """
  Pure lane ordering, mark clustering, pause-band pairing, attention
  detection, and record detail formatting for `PortalWeb.TimelineLive`.

  Kept separate from the LiveView process callbacks and HEEx template so the
  data transformations over snapshot/provisional marks remain pure functions
  of plain maps and lists.
  """

  alias Portal.{TimelineAdapter, TimelineFeed}

  @agents_cap 25
  @ticks 6
  # The feed's own provisional cap.
  @provisional_cap 500
  # What ends a pause. Archiving sets the agent's status directly and emits no
  # resume, so an archive closes a pause too.
  @pause_ends ~w(lifecycle_resumed lifecycle_archived)
  # A lane's marks closer than one marker's width overlap into a smear, so
  # the window is cut into this many slots, fixed to the page anchor, and a
  # slot holding two or more marks draws one counted group at its centre.
  # At the track's 520 px minimum a slot is about one marker wide; slots stay
  # put as the window slides, so a group does not regroup as marks expire.
  @cluster_slots 30

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

  @turn_summary ~r/^Turn summary \([^)]*\);\s*(response excerpt:\s*)?/

  # --- window & selection state helpers ---

  # A recorded pause older than the window is not in a re-read, but while no
  # later resume is known the agent is still paused: its band runs to now and
  # it still needs attention, as `prune/1` keeps it between re-reads. Carry it
  # across the re-read. Only a history row is carried, and only once it is
  # older than the window: a live mark the feed has expired, or a row the
  # window still covers, is the re-read's to keep or drop. A resume the page
  # already knew of still counts, even when it too has left the window.
  def held_pauses(assigns, resumed) do
    %{marks: marks, resumed_at: known, substrate: substrate} = assigns

    start =
      DateTime.to_unix(DateTime.utc_now(), :microsecond) - hours(assigns) * 3_600_000_000

    Map.filter(marks, fn {_id, m} ->
      us = TimelineAdapter.ts_us(m["ts"])
      agent = m["agent_id"]

      m["event_type"] == "lifecycle_paused" and m["provisional"] != true and is_binary(agent) and
        is_integer(us) and us < start and
        us >= max(Map.get(known, agent, -1), Map.get(resumed, agent, -1)) and
        visible?(m, substrate)
    end)
  end

  # A re-read that confirms a provisional mark gives it a history id (`p-`
  # becomes `h-`), so a selection on the live mark follows it to the row
  # that absorbed it rather than clearing as the record arrives. Follow the
  # feed's mark-to-row pairing first; fall back to the latest matching row when
  # the mark arrived directly on the page without a feed pairing.
  def carry_selection(nil, _old, _new, _paired), do: nil
  # A group is not a mark; `relane/1` resolves it against the new groups.
  def carry_selection("c-" <> _ = id, _old, _new, _paired), do: id

  def carry_selection(id, old, new, paired) do
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

  # A selected group's marks, each carried as a selected mark is, so that
  # `relane/1` can find them when the re-read confirms them into another
  # slot (a live mark and its history row can sit either side of a boundary).
  def carry_group(%{selected: "c-" <> _ = id} = assigns, new, paired) do
    case Map.get(assigns.clusters, id) do
      nil ->
        nil

      group ->
        group.members
        |> Enum.map(&carry_selection(&1, assigns.marks, new, paired))
        |> Enum.reject(&is_nil/1)
    end
  end

  def carry_group(_assigns, _new, _paired), do: nil

  # Each agent's latest resume or archive, kept apart from the marks: it is
  # what closes a pause band, so it must survive the cap and the window prune
  # that drop the mark itself.
  def note_resume(acc, %{"event_type" => type, "agent_id" => a} = m)
      when type in @pause_ends and is_binary(a) do
    case TimelineAdapter.ts_us(m["ts"]) do
      nil -> acc
      us -> Map.update(acc, a, us, &max(&1, us))
    end
  end

  def note_resume(acc, _mark), do: acc

  def cap_provisional(marks) do
    {live, held} = Enum.split_with(marks, fn {_id, m} -> m["provisional"] == true end)

    if length(live) > @provisional_cap do
      held ++
        (live |> Enum.sort_by(fn {_, m} -> sort_key(m) end, :desc) |> Enum.take(@provisional_cap))
    else
      marks
    end
  end

  # With substrate off only the agent's own check-ins show, as the tab's
  # include_substrate=false does. A provisional check-in is hidden too: the
  # frame cannot tell substrate from agent.
  def visible?(%{"kind" => "checkin", "authored_by" => by}, false), do: by == "agent"
  def visible?(_mark, _substrate), do: true

  def in_window?(mark, assigns) do
    case TimelineAdapter.ts_us(mark["ts"]) do
      nil ->
        false

      us ->
        start = DateTime.to_unix(assigns.now, :microsecond) - hours(assigns) * 3_600_000_000
        us >= start
    end
  end

  def join_order(order, mark) do
    present = MapSet.new(order, fn {id, _depth, _parent} -> id end)

    mark
    |> lane_ids()
    |> Enum.reject(&MapSet.member?(present, &1))
    |> Enum.reverse()
    |> Enum.reduce(order, fn id, acc -> [{id, 0, nil} | acc] end)
  end

  def roster_last_seen(agents) do
    for %{"agent_id" => id} = a <- agents, is_binary(id), into: %{} do
      {id, TimelineAdapter.ts_us(a["last_activity_at"])}
    end
  end

  # A lineage mark belongs to its parent's row; the child gets a row too.
  def bump_last_seen(last_seen, mark) do
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
  def owner(%{"kind" => "lineage", "parent_agent_id" => parent}) when is_binary(parent),
    do: parent

  def owner(%{"agent_id" => id}) when is_binary(id), do: id
  def owner(_mark), do: nil

  # Every row a mark implies: its owner, and a lineage child's own row.
  def lane_ids(mark) do
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
  def order_rows(roster, marks, last_seen) do
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

  # Builds the shown lanes into assigns, so the one-second tick, which only
  # moves the window's CSS variables, never re-renders a mark.
  # Agents that run on their own schedule fold into one collapsed group, so
  # the rows on top are the sessions doing work. A family follows its root.
  def relane_assigns(assigns) do
    %{row_order: order, marks: marks, labels: labels, roster: roster} = assigns
    by_owner = marks |> Enum.group_by(fn {_id, m} -> owner(m) end) |> Map.delete(nil)
    filter = String.downcase(assigns.filter)

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
      if assigns.show_all_agents, do: working, else: Enum.take(working, @agents_cap)

    attention = needs_attention(order, by_owner, labels, assigns.resumed_at)
    flagged = MapSet.new(attention, & &1.agent)
    lane = &build_lane(&1, by_owner, flagged, assigns)
    open_background = assigns.show_background or filter != ""
    lanes = Enum.map(shown, lane)
    background_lanes = if open_background, do: Enum.map(background, lane), else: []

    clusters =
      for l <- lanes ++ background_lanes,
          %{cluster: true} = c <- l.marks,
          into: %{},
          do: {c.id, c}

    [
      lanes: lanes,
      background_lanes: background_lanes,
      clusters: clusters,
      selected: resolve_group(assigns, clusters),
      group_hint: nil,
      background_open: open_background,
      background_count: length(background),
      background_names:
        for({id, 0, _} <- background, do: agent_name(labels, id)) |> Enum.join(" · "),
      attention: attention,
      lanes_matching: length(working),
      lanes_any: length(matching),
      lanes_total: length(order)
    ]
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
        )
        |> group_marks(id, hours(assigns) * 3600 / @cluster_slots),
      bands: bands(own, anchor, labels, Map.get(assigns.resumed_at, id))
    }
  end

  # `own` is in time order, and so is the result. A mark with no time stays
  # as it is (hidden), last. Lifecycle marks stay single: they mark the pause
  # bands' edges, and an unresumed pause is kept after it leaves the window,
  # where a group would be held at the window's start.
  defp group_marks(views, lane_id, slot) do
    {timed, untimed} = Enum.split_with(views, &is_number(&1.t))
    {grouping, single} = Enum.split_with(timed, &(&1.kind != "lifecycle"))

    grouping
    |> Enum.chunk_by(&floor(&1.t / slot))
    |> Enum.map(fn
      [one] -> one
      group -> group_view(group, lane_id, slot)
    end)
    |> Kernel.++(single)
    |> Enum.sort_by(& &1.t)
    |> Kernel.++(untimed)
  end

  # The group takes the look of its most notable mark, so a pause verdict or
  # a failed outcome inside it still shows red.
  defp group_view(group, lane_id, slot) do
    n = floor(hd(group).t / slot)
    top = Enum.max_by(group, &notability/1)
    first = hd(group)
    last = List.last(group)

    %{
      id: "c-#{lane_id}-#{n}",
      cluster: true,
      members: Enum.map(group, & &1.id),
      kind: "group",
      cls: "group " <> group_tone(notability(top)),
      glyph: Integer.to_string(length(group)),
      t: Float.round((n + 0.5) * slot, 1),
      at: first.at,
      until: last.at,
      provisional: Enum.all?(group, & &1.provisional),
      seen_live: false,
      label:
        joined([
          "#{length(group)} events, #{utc(first.at)} to #{utc(last.at)}",
          "most notable: " <> top.label
        ])
    }
  end

  defp notability(%{cls: cls}) do
    cond do
      cls in ["checkin pause", "outcome bad"] -> 4
      cls == "checkin guide" -> 3
      cls in ["checkin substrate", "checkin unassessed"] -> 1
      true -> 2
    end
  end

  defp group_tone(4), do: "alert"
  defp group_tone(3), do: "warn"
  defp group_tone(2), do: "ok"
  defp group_tone(_), do: "quiet"

  # A selected group that no longer exists follows its marks: to the group
  # that now holds one of them, or else to the first still shown on its own.
  # After a re-read its marks are the ones `carry_group/3` carried.
  def resolve_group(%{selected: "c-" <> _ = id} = assigns, new) do
    members =
      assigns.group_hint ||
        case Map.get(assigns.clusters, id) do
          nil -> []
          group -> group.members
        end

    cond do
      Map.has_key?(new, id) ->
        id

      holder = Enum.find_value(members, &group_of(new, &1)) ->
        holder.id

      true ->
        Enum.find(members, &Map.has_key?(assigns.marks, &1))
    end
  end

  def resolve_group(%{selected: id}, _new), do: id

  def group_of(_clusters, nil), do: nil

  def group_of(clusters, id),
    do: clusters |> Map.values() |> Enum.find(&(id in &1.members))

  def selected?(m, selected), do: selected == m.id or selected in Map.get(m, :members, [])

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
    resume = Enum.find(newest, fn {_, m} -> m["event_type"] in @pause_ends end)

    verdict =
      Enum.find(newest, fn {_, m} -> m["kind"] == "checkin" and is_binary(verdict_of(m)) end)

    # An archive after the agent's latest check-in settles it: the agent is
    # gone, so its last verdict no longer asks anything of a person.
    archived_after? =
      verdict &&
        Enum.any?(newest, fn {_, m} ->
          m["event_type"] == "lifecycle_archived" and sort_key(m) > sort_key(elem(verdict, 1))
        end)

    cond do
      pause && open_pause?(pause, resume, resumed_us) ->
        {pid, pm} = pause
        %{sel: pid, reason: "paused since " <> clock(pm["ts"])}

      verdict && not archived_after? && action_class(verdict_of(elem(verdict, 1))) == "pause" ->
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
      at: m["ts"],
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

  defp action_class(action), do: TimelineAdapter.verdict_bucket(action)

  # Pause bands pair lifecycle_paused with the next lifecycle_resumed on the
  # same row; a resume with no pause in the window started before it, and a
  # pause with no resume runs to now. An archive closes a pause it follows,
  # and draws nothing on its own. Dialectic sessions draw from creation
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

        {_id, %{"kind" => "lifecycle", "event_type" => "lifecycle_archived"}}, {acc, nil} ->
          {acc, nil}

        {id, %{"kind" => "lifecycle", "event_type" => type} = m}, {acc, held}
        when type in @pause_ends ->
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

  def sort_key(mark), do: TimelineAdapter.ts_us(mark["ts"]) || 0

  def hours(assigns), do: TimelineFeed.window_hours(assigns.window)

  # --- formatting ---

  def agent_name(labels, id) when is_binary(id) do
    case Map.get(labels, id) do
      label when is_binary(label) and label != "" -> label
      _ -> String.slice(id, 0, 8)
    end
  end

  def agent_name(_labels, _id), do: "unknown agent"

  def utc(ts) do
    case TimelineAdapter.ts_us(ts) do
      nil -> "—"
      us -> us |> DateTime.from_unix!(:microsecond) |> Calendar.strftime("%Y-%m-%d %H:%M:%S UTC")
    end
  end

  def clock(ts) do
    case TimelineAdapter.ts_us(ts) do
      nil -> "an unknown time"
      us -> us |> DateTime.from_unix!(:microsecond) |> Calendar.strftime("%H:%M UTC")
    end
  end

  def ago(nil, _now), do: "never"

  def ago(%DateTime{} = then, now), do: ago_us(DateTime.to_unix(then, :microsecond), now)

  defp ago_us(us, now) do
    secs = max(0, div(DateTime.to_unix(now, :microsecond) - us, 1_000_000))

    cond do
      secs < 90 -> "#{secs} s ago"
      secs < 5_400 -> "#{div(secs, 60)} min ago"
      true -> "#{div(secs, 3600)} h ago"
    end
  end

  def firehose_label(:connected), do: "connected"
  def firehose_label(:unreachable), do: "governance unreachable"
  def firehose_label(:refused), do: "governance refused credentials"
  def firehose_label(:connecting), do: "connecting"
  def firehose_label(_), do: "not running"

  def source_label(:ok), do: "ok"
  def source_label(:loading), do: "loading"
  def source_label(:refused), do: "refused"
  def source_label(:unavailable), do: "unavailable, showing the last window"
  def source_label(:no_feed), do: "unavailable"
  def source_label(other), do: to_string(other)

  def latency_summary([]), do: nil

  def latency_summary(samples) do
    sorted = Enum.sort(samples)
    %{last: hd(samples), median: Enum.at(sorted, div(length(sorted), 2)), n: length(sorted)}
  end

  def checkins_reread_only_reason(assigns) do
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
  def lane_vars(now, anchor, hours) do
    span = hours * 3600
    at = DateTime.to_unix(now) - anchor
    "--now:#{at};--start:#{at - span};--span:#{span}"
  end

  def axis_ticks(now, hours) do
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

  def mark_style(%{t: nil}), do: "display:none"
  def mark_style(%{t: t}), do: "--t:#{t}"

  def band_style(%{t: :before} = b), do: "--t:-1000000000" <> band_end(b)
  def band_style(%{t: nil}), do: "display:none"
  def band_style(%{t: t} = b), do: "--t:#{t}" <> band_end(b)

  # An open band leaves --t2 unset and the CSS falls back to --now.
  defp band_end(%{t2: nil}), do: ""
  defp band_end(%{t2: t2}), do: ";--t2:#{t2}"

  def counts(marks) do
    Enum.reduce(Map.values(marks), %{}, fn m, acc -> Map.update(acc, m["kind"], 1, &(&1 + 1)) end)
  end

  # Record fields arrive from the server as JSON; anything that is not text or
  # a number renders as absent rather than raising in a template.
  def txt(v) when is_binary(v) and v != "", do: v
  def txt(v) when is_number(v), do: to_string(v)
  def txt(_), do: nil

  def joined(parts), do: parts |> Enum.filter(&is_binary/1) |> Enum.join(" · ")

  def num(v) when is_number(v), do: :erlang.float_to_binary(v / 1, decimals: 2)
  def num(_), do: nil

  def mark_title(%{"kind" => "checkin"} = m, _roster) do
    case m["authored_by"] do
      "agent" -> "Check-in · " <> (txt(m["action"]) || "unknown")
      "unknown" -> "Live check-in"
      _ -> "Hook observation"
    end
  end

  def mark_title(%{"kind" => "outcome"} = m, _roster),
    do: "Outcome · " <> if(m["is_bad"] == true, do: "failed", else: "passed")

  def mark_title(%{"kind" => "knowledge"}, _roster), do: "Finding shared"

  def mark_title(%{"kind" => "lineage"} = m, roster),
    do: relation_mark(lineage_relation(m, roster))

  def mark_title(%{"kind" => "lifecycle"} = m, _roster) do
    case txt(m["event_type"]) do
      nil -> "Lifecycle"
      t -> t |> String.replace_prefix("lifecycle_", "") |> String.replace("_", " ")
    end
  end

  def mark_title(%{"kind" => "dialectic"}, _roster), do: "Dialectic review"
  def mark_title(m, _roster), do: txt(m["kind"]) || "event"

  # The check-in verdict is a governance assessment, not part of the core
  # record. A live mark carries it as an EISV decoration; a history row carries
  # the persisted `action`, which is the same verdict. Both read the same way,
  # so a mark does not change label when the re-read confirms it.
  def assessment(%{"assessment" => %{"verdict" => verdict}}), do: assessment_text(txt(verdict))
  def assessment(%{"kind" => "checkin", "action" => action}), do: assessment_text(txt(action))
  def assessment(_m), do: nil

  defp assessment_text(nil), do: nil
  defp assessment_text(verdict), do: "assessment: " <> verdict

  # The selected record as label/value rows, mirroring the tab's panel.
  def detail_rows(m, labels, roster) do
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

  def detail_note(m) do
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
end
