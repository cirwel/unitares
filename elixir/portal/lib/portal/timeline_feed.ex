defmodule Portal.TimelineFeed do
  @moduledoc """
  Holds the Timeline pane's windows: authoritative history from
  `GET /v1/timeline`, plus provisional marks from the firehose until history
  catches up (`docs/proposals/active/portal-live-timeline-v0.md` §3.2–3.3).

  ## Load

  The feed re-reads only while at least one `TimelineLive` is subscribed. It
  monitors subscriber pids, re-reads on the first subscribe, and stops on the
  last unsubscribe, so zero viewers make zero GETs. While viewers are present
  it re-reads both windows:

    * when the firehose reports `:connected` (after a governance restart the
      window repopulates within one re-read);
    * on a floor timer (`reread_ms`, default 60 s);
    * on a doorbell (a lifecycle or `dialectic_*` frame), debounced and
      coalesced (`debounce_ms`, default 5 s).

  Both windows refresh on the same cycle however many tabs are open, so load is
  flat in the number of viewers. Each fetch runs in
  `Task.Supervisor.async_nolink/2` with one fetch in flight per window, a
  generation counter that discards stale results, and a guard timer. A failed
  fetch keeps the last window and sets `:unavailable`, or `:refused` for a 401
  or 403. The feed never raises on a governance failure.

  ## Live marks

  The adapter (`Portal.TimelineAdapter`) maps firehose frames to
  provisional marks, broadcast as deltas so a LiveView inserts them without a
  refetch. A re-read replaces the window; a provisional mark survives until the
  re-read contains it, or until two re-reads that started after it have missed
  it. A history row that absorbed a live mark is flagged `"seen_live"`, which is
  the trial's "first seen live" share.

  ## Storage

  Windows live in a protected ETS table owned by the feed, with
  `Portal.TimelineTableHeir` as heir, so a feed crash keeps the last
  window readable. LiveViews read through `snapshot/2`, which returns
  `:unavailable` when the table is missing.

  ## Messages on `"timeline:window"`

    * `{:timeline_delta, window, mark}`: a provisional mark was added;
    * `{:timeline_reread, window}`: a re-read replaced the window;
    * `{:timeline_status, window}`: availability changed, the window kept;
    * `:timeline_feed_up`: the feed (re)started; LiveViews subscribe again.

  Options (all injectable for tests): `:name`, `:table`, `:pubsub`, `:heir`,
  `:task_supervisor`, `:req_options`, `:firehose` (a Firehose name, or `nil`
  when none runs), `:reread_ms`, `:debounce_ms`, `:fetch_timeout_ms`.
  """

  use GenServer

  alias Portal.{Firehose, Governance, TimelineAdapter, TimelineTableHeir}

  @topic "timeline:window"
  @table :portal_timeline

  # The 6 h window asks for the per-kind maximum so check-ins are not truncated
  # at today's volume; the 1 h window keeps the server default.
  @windows %{
    "1h" => %{hours: 1, limit: 500},
    "6h" => %{hours: 6, limit: 2000}
  }
  @window_order ["1h", "6h"]

  @default_reread_ms 60_000
  @default_debounce_ms 5_000
  @default_fetch_timeout_ms 12_000
  @provisional_cap 500
  @rereads_to_expire 2

  def topic, do: @topic
  def default_table, do: @table
  def windows, do: @window_order
  def window_hours(window), do: @windows |> Map.fetch!(window) |> Map.fetch!(:hours)

  @doc "The window key for a configured hour count (`1` → `\"1h\"`); unknown counts fall back to 1 h."
  def window_for_hours(6), do: "6h"
  def window_for_hours(_), do: "1h"

  def child_spec(opts) do
    %{id: Keyword.get(opts, :name, __MODULE__), start: {__MODULE__, :start_link, [opts]}}
  end

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc """
  Register the calling process as a viewer. The feed monitors it and re-reads
  while any viewer is alive. Returns `:ok`, or `{:error, :unavailable}` when no
  feed is running.
  """
  def subscribe(server \\ __MODULE__) do
    GenServer.call(server, {:subscribe, self()})
  catch
    :exit, _ -> {:error, :unavailable}
  end

  @doc """
  The held window plus feed metadata, read straight from ETS:
  `{:ok, %{window: ..., status: ..., events: ..., provisional: ..., meta: ...}}`,
  or `:unavailable` when the table or the window is missing.
  """
  def snapshot(window, table \\ @table) do
    with [{_, held}] <- :ets.lookup(table, {:window, window}),
         [{_, meta}] <- :ets.lookup(table, :meta) do
      {:ok, Map.put(held, :meta, meta)}
    else
      _ -> :unavailable
    end
  rescue
    ArgumentError -> :unavailable
  end

  @doc "Only the feed metadata (firehose state, last frame), without copying a window."
  def snapshot_meta(table \\ @table) do
    case :ets.lookup(table, :meta) do
      [{_, meta}] -> {:ok, meta}
      _ -> :unavailable
    end
  rescue
    ArgumentError -> :unavailable
  end

  # --- server ---

  @impl true
  def init(opts) do
    state = %{
      table: Keyword.get(opts, :table, @table),
      pubsub: Keyword.get(opts, :pubsub, Portal.PubSub),
      heir: Keyword.get(opts, :heir, TimelineTableHeir),
      task_supervisor: Keyword.get(opts, :task_supervisor, Portal.TaskSupervisor),
      req_options: Keyword.get(opts, :req_options, []),
      firehose: Keyword.get(opts, :firehose),
      reread_ms: Keyword.get(opts, :reread_ms, @default_reread_ms),
      debounce_ms: Keyword.get(opts, :debounce_ms, @default_debounce_ms),
      fetch_timeout_ms: Keyword.get(opts, :fetch_timeout_ms, @default_fetch_timeout_ms),
      held: Map.new(@window_order, &{&1, empty_window(&1)}),
      seen_live: Map.new(@window_order, &{&1, MapSet.new()}),
      inflight: %{},
      gen: Map.new(@window_order, &{&1, 0}),
      dirty: MapSet.new(),
      viewers: %{},
      floor_timer: nil,
      floor_token: nil,
      debounce_timer: nil,
      firehose_status: :off,
      last_frame_at: nil,
      last_eisv_at: nil,
      last_fetch_started: nil,
      heir_ref: nil
    }

    # Subscribe before asking the firehose for its state, so a change between
    # the two cannot be missed.
    Phoenix.PubSub.subscribe(state.pubsub, Firehose.all_topic())
    Phoenix.PubSub.subscribe(state.pubsub, Firehose.status_topic())

    state = %{state | firehose_status: initial_firehose_status(state.firehose)}
    state = attach_table(state)

    Enum.each(@window_order, &write_window(state, &1))
    write_meta(state)
    Phoenix.PubSub.broadcast(state.pubsub, @topic, :timeline_feed_up)

    {:ok, state}
  end

  defp empty_window(window) do
    %{
      window: window,
      hours: window_hours(window),
      status: :loading,
      error: nil,
      history: [],
      provisional: [],
      paired: %{},
      agents: [],
      truncated: [],
      counts: %{},
      last_reread_at: nil,
      last_attempt_at: nil
    }
  end

  defp initial_firehose_status(nil), do: :off

  defp initial_firehose_status(name) do
    Firehose.status(name)
  catch
    :exit, _ -> :off
  end

  defp attach_table(state) do
    heir_pid = resolve(state.heir)

    claimed =
      if heir_pid do
        try do
          TimelineTableHeir.claim(heir_pid, state.table)
        catch
          :exit, _ -> :none
        end
      else
        :none
      end

    state =
      case claimed do
        {:ok, _tid} ->
          # A previous feed's table: keep its last window readable until the
          # first re-read replaces it, by restoring it into state.
          restore_from_table(state)

        :none ->
          :ets.new(state.table, [:named_table, :protected, :set, {:read_concurrency, true}])
          state
      end

    arm_heir(state, heir_pid)
  end

  # Name the heir on the table every time: after a claim, and after the heir
  # itself restarted (a table naming a dead heir is deleted when its owner dies).
  defp arm_heir(state, nil), do: state

  defp arm_heir(state, heir_pid) do
    :ets.setopts(state.table, {:heir, heir_pid, state.table})
    if state.heir_ref, do: Process.demonitor(state.heir_ref, [:flush])
    %{state | heir_ref: Process.monitor(heir_pid)}
  end

  # Restores everything the previous feed held, provisional marks included,
  # so open panes lose nothing across a crash. Restored marks count as received
  # now: only re-reads started after the restart can expire them.
  defp restore_from_table(state) do
    now = System.monotonic_time()

    restored =
      Map.new(@window_order, fn w ->
        case :ets.lookup(state.table, {:window, w}) do
          [{_, saved}] ->
            seen =
              for %{"seen_live" => true} = row <- saved.events,
                  into: MapSet.new(),
                  do: TimelineAdapter.key(row)

            held = %{
              empty_window(w)
              | status: saved.status,
                error: saved.error,
                history: Enum.map(saved.events, &Map.delete(&1, "seen_live")),
                provisional: Enum.map(saved.provisional, &{&1, now, 0}),
                paired: Map.get(saved, :paired, %{}),
                agents: saved.agents,
                truncated: saved.truncated,
                counts: saved.counts,
                last_reread_at: saved.last_reread_at,
                last_attempt_at: saved.last_attempt_at
            }

            {w, {held, seen}}

          _ ->
            {w, {empty_window(w), MapSet.new()}}
        end
      end)

    %{
      state
      | held: Map.new(restored, fn {w, {held, _}} -> {w, held} end),
        seen_live: Map.new(restored, fn {w, {_, seen}} -> {w, seen} end)
    }
  end

  defp resolve(nil), do: nil
  defp resolve(pid) when is_pid(pid), do: pid
  defp resolve(name), do: Process.whereis(name)

  @impl true
  def handle_call({:subscribe, pid}, _from, state) do
    state =
      if Map.has_key?(state.viewers, pid) do
        state
      else
        ref = Process.monitor(pid)
        first? = map_size(state.viewers) == 0
        state = %{state | viewers: Map.put(state.viewers, pid, ref)}
        if first?, do: start_viewing(state), else: state
      end

    write_meta(state)
    {:reply, :ok, state}
  end

  @impl true
  # A fetch finished.
  def handle_info({ref, result}, state) when is_reference(ref) do
    case inflight_for(state, ref) do
      nil ->
        {:noreply, state}

      {window, fetch} ->
        Process.demonitor(ref, [:flush])
        {:noreply, finish_fetch(state, window, fetch, result)}
    end
  end

  def handle_info({:DOWN, ref, :process, _pid, _reason}, %{heir_ref: ref} = state) do
    Process.send_after(self(), :rearm_heir, 50)
    {:noreply, %{state | heir_ref: nil}}
  end

  def handle_info({:DOWN, ref, :process, pid, reason}, state) do
    case inflight_for(state, ref) do
      {window, fetch} ->
        {:noreply, finish_fetch(state, window, fetch, {:error, {:fetch_exit, reason}})}

      nil ->
        {:noreply, drop_viewer(state, pid)}
    end
  end

  def handle_info({:fetch_timeout, window, gen}, state) do
    case Map.get(state.inflight, window) do
      %{gen: ^gen, ref: ref, pid: pid} = fetch ->
        Process.demonitor(ref, [:flush])
        _ = Task.Supervisor.terminate_child(state.task_supervisor, pid)
        {:noreply, finish_fetch(state, window, fetch, {:error, :timeout})}

      _ ->
        {:noreply, state}
    end
  end

  def handle_info({:floor, token}, %{floor_token: token} = state) do
    state = state |> reread_soon() |> schedule_floor()
    {:noreply, state}
  end

  def handle_info({:floor, _stale}, state), do: {:noreply, state}

  def handle_info(:doorbell, state) do
    state = %{state | debounce_timer: nil}
    {:noreply, if(viewing?(state), do: reread_soon(state), else: state)}
  end

  def handle_info({:firehose_status, status}, state) do
    state = %{state | firehose_status: status}
    write_meta(state)

    state =
      if status == :connected and viewing?(state), do: reread_soon(state), else: state

    {:noreply, state}
  end

  def handle_info({:governance_event, frame}, state) do
    state = note_frame(state, frame)

    state =
      if viewing?(state) do
        state = apply_frame(state, TimelineAdapter.map_frame(frame))
        write_meta(state)
        state
      else
        state
      end

    {:noreply, state}
  end

  def handle_info(:rearm_heir, state) do
    case resolve(state.heir) do
      nil ->
        Process.send_after(self(), :rearm_heir, 200)
        {:noreply, state}

      pid ->
        {:noreply, arm_heir(state, pid)}
    end
  end

  # The heir's give_away on claim, and anything else, is ignored.
  def handle_info(_message, state), do: {:noreply, state}

  # --- viewers ---

  defp viewing?(state), do: map_size(state.viewers) > 0

  # The first viewer re-reads at once, unless a fetch started within the
  # debounce interval (a reload, or a socket flap by the only viewer): then the
  # re-read is debounced, so page reloads stay inside the rate bound.
  defp start_viewing(state), do: state |> reread_soon() |> schedule_floor()

  # The one gate every re-read passes (first viewer, reconnect, floor, and a
  # doorbell when it fires): fetch now only if the last fetch started at least
  # `debounce_ms` ago, cancelling any pending doorbell; otherwise make sure a
  # doorbell is pending, and let it pass this gate again when it fires. So two
  # re-reads never start closer together than the debounce, whatever mix of
  # reloads, reconnects and frames arrives.
  defp reread_soon(state) do
    case ms_since_last_fetch(state) do
      ms when is_integer(ms) and ms < state.debounce_ms ->
        schedule_doorbell(state, state.debounce_ms - ms)

      _ ->
        cancel(state.debounce_timer)
        reread_all(%{state | debounce_timer: nil})
    end
  end

  defp ms_since_last_fetch(%{last_fetch_started: nil}), do: nil

  defp ms_since_last_fetch(state) do
    System.convert_time_unit(
      System.monotonic_time() - state.last_fetch_started,
      :native,
      :millisecond
    )
  end

  defp schedule_doorbell(%{debounce_timer: nil} = state, ms),
    do: %{state | debounce_timer: Process.send_after(self(), :doorbell, max(ms, 1))}

  defp schedule_doorbell(state, _ms), do: state

  defp drop_viewer(state, pid) do
    case Map.pop(state.viewers, pid) do
      {nil, _} ->
        state

      {_ref, viewers} ->
        state = %{state | viewers: viewers}

        state =
          if viewers == %{} do
            cancel(state.floor_timer)
            cancel(state.debounce_timer)

            %{
              state
              | floor_timer: nil,
                floor_token: nil,
                debounce_timer: nil,
                dirty: MapSet.new()
            }
          else
            state
          end

        write_meta(state)
        state
    end
  end

  defp schedule_floor(state) do
    cancel(state.floor_timer)
    token = make_ref()
    timer = Process.send_after(self(), {:floor, token}, state.reread_ms)
    %{state | floor_timer: timer, floor_token: token}
  end

  defp ring(state) do
    if viewing?(state) and state.debounce_timer == nil do
      %{state | debounce_timer: Process.send_after(self(), :doorbell, state.debounce_ms)}
    else
      state
    end
  end

  defp cancel(nil), do: :ok
  defp cancel(timer), do: Process.cancel_timer(timer)

  # --- fetching ---

  defp reread_all(state), do: Enum.reduce(@window_order, state, &reread(&2, &1))

  defp reread(state, window) do
    if Map.has_key?(state.inflight, window) do
      # Coalesced: one more re-read once this one lands.
      %{state | dirty: MapSet.put(state.dirty, window)}
    else
      start_fetch(state, window)
    end
  end

  defp start_fetch(state, window) do
    gen = state.gen[window] + 1
    %{hours: hours, limit: limit} = Map.fetch!(@windows, window)
    params = [hours: hours, limit: limit, include_substrate: "true"]
    req_options = state.req_options

    task =
      Task.Supervisor.async_nolink(state.task_supervisor, fn ->
        Governance.timeline(params, req_options: req_options)
      end)

    timer = Process.send_after(self(), {:fetch_timeout, window, gen}, state.fetch_timeout_ms)

    fetch = %{
      ref: task.ref,
      pid: task.pid,
      gen: gen,
      started: System.monotonic_time(),
      timer: timer
    }

    held = Map.update!(state.held, window, &%{&1 | last_attempt_at: DateTime.utc_now()})

    %{
      state
      | inflight: Map.put(state.inflight, window, fetch),
        gen: Map.put(state.gen, window, gen),
        held: held,
        last_fetch_started: fetch.started
    }
  end

  defp inflight_for(state, ref) do
    Enum.find_value(state.inflight, fn
      {window, %{ref: ^ref} = fetch} -> {window, fetch}
      _ -> nil
    end)
  end

  defp finish_fetch(state, window, fetch, result) do
    cancel(fetch.timer)
    state = %{state | inflight: Map.delete(state.inflight, window)}

    state =
      if fetch.gen == state.gen[window] do
        apply_result(state, window, fetch, result)
      else
        state
      end

    if MapSet.member?(state.dirty, window) do
      ring(%{state | dirty: MapSet.delete(state.dirty, window)})
    else
      state
    end
  end

  defp apply_result(state, window, fetch, {:ok, %{"events" => events} = body})
       when is_list(events) do
    # Rows that are not objects, and agents without an id, are dropped rather
    # than allowed to crash the feed (and, by restart, the pane).
    events = Enum.filter(events, &is_map/1)
    old = state.held[window]

    {provisional, matched, new_pairs} =
      merge_with_pairs(old.provisional, events, fetch.started, old.hours, state.seen_live[window])

    history_keys = MapSet.new(events, &TimelineAdapter.key/1)
    history_ids = MapSet.new(events, &TimelineAdapter.dom_id/1)

    seen_live =
      state.seen_live[window]
      |> MapSet.intersection(history_keys)
      |> MapSet.union(matched)

    paired =
      old.paired
      |> Map.filter(fn {_mark_id, row_id} -> MapSet.member?(history_ids, row_id) end)
      |> Map.merge(new_pairs)

    held = %{
      old
      | status: :ok,
        error: nil,
        history: events,
        provisional: provisional,
        paired: paired,
        agents: body["agents"] |> list_or_empty() |> Enum.filter(&agent?/1),
        truncated: list_or_empty(body["truncated"]),
        counts: map_or_empty(body["counts"]),
        last_reread_at: DateTime.utc_now()
    }

    state = %{
      state
      | held: Map.put(state.held, window, held),
        seen_live: Map.put(state.seen_live, window, seen_live)
    }

    write_window(state, window)
    Phoenix.PubSub.broadcast(state.pubsub, @topic, {:timeline_reread, window})
    state
  end

  defp apply_result(state, window, fetch, {:ok, _not_a_window}),
    do: apply_result(state, window, fetch, {:error, :undecodable})

  defp apply_result(state, window, _fetch, {:error, reason}) do
    {status, error} = classify_error(reason)
    held = Map.update!(state.held, window, &%{&1 | status: status, error: error})
    state = %{state | held: held}
    write_window(state, window)
    Phoenix.PubSub.broadcast(state.pubsub, @topic, {:timeline_status, window})
    state
  end

  defp apply_result(state, window, fetch, _other),
    do: apply_result(state, window, fetch, {:error, :undecodable})

  defp classify_error({:http_status, status}) when status in [401, 403],
    do: {:refused, "governance refused credentials (HTTP #{status})"}

  defp classify_error({:http_status, status}),
    do: {:unavailable, "governance answered HTTP #{status}"}

  defp classify_error(:timeout), do: {:unavailable, "governance did not answer in time"}
  defp classify_error(:undecodable), do: {:unavailable, "governance sent an unreadable timeline"}

  defp classify_error(%{__exception__: true} = e),
    do: {:unavailable, "governance unreachable (#{Exception.message(e)})"}

  defp classify_error(reason), do: {:unavailable, "governance unreachable (#{inspect(reason)})"}

  defp agent?(%{"agent_id" => id}) when is_binary(id), do: true
  defp agent?(_), do: false

  defp list_or_empty(v) when is_list(v), do: v
  defp list_or_empty(_), do: []
  defp map_or_empty(v) when is_map(v), do: v
  defp map_or_empty(_), do: %{}

  @doc false
  # Pure: which provisional marks survive a re-read, and which history keys
  # absorbed one. Only a re-read that started after a mark arrived can miss it.
  #
  # Successive check-ins from one agent can fall within the 2 s lag window of
  # each other's rows, so a row is claimed by at most one mark: marks are
  # paired oldest first, each with the latest unclaimed row in its window.
  # Rows that absorbed a mark at an earlier re-read (`claimed`) are taken.
  def merge(provisional, history, fetch_started, hours, claimed \\ MapSet.new()) do
    {kept, matched, _pairs} =
      merge_with_pairs(provisional, history, fetch_started, hours, claimed)

    {kept, matched}
  end

  defp merge_with_pairs(provisional, history, fetch_started, hours, claimed) do
    cutoff_us = System.os_time(:microsecond) - hours * 3_600_000_000

    {kept, matched, pairs} =
      provisional
      |> Enum.reverse()
      |> Enum.reduce({[], MapSet.new(), %{}}, fn {mark, received, misses},
                                                 {kept, matched, pairs} ->
        taken = MapSet.union(claimed, matched)

        case pairable_row(mark, received, fetch_started, history, taken) do
          nil ->
            misses = if received < fetch_started, do: misses + 1, else: misses
            ts = TimelineAdapter.ts_us(mark["ts"])

            if misses >= @rereads_to_expire or (is_integer(ts) and ts < cutoff_us) do
              {kept, matched, pairs}
            else
              {[{mark, received, misses} | kept], matched, pairs}
            end

          row ->
            {kept, MapSet.put(matched, TimelineAdapter.key(row)),
             Map.put(pairs, TimelineAdapter.dom_id(mark), TimelineAdapter.dom_id(row))}
        end
      end)

    # `kept` was built oldest first, prepending: it is newest first again.
    {kept, matched, pairs}
  end

  # A check-in that arrived after this re-read started is not paired with it.
  # Its row is written before its frame, so the re-read may hold it, but a row
  # in its window may as well be the agent's previous check-in, which a
  # verdict cannot tell apart from its own (two pauses look alike). The next
  # re-read started after it arrived and so holds its row, which, as the
  # latest unclaimed row in its window, it then takes. Until then it shows
  # twice, as on arrival: better than dropping a real check-in.
  defp pairable_row(%{"kind" => "checkin"}, received, fetch_started, _history, _taken)
       when received >= fetch_started,
       do: nil

  defp pairable_row(mark, _received, _fetch_started, history, taken),
    do: best_row(mark, history, taken)

  defp best_row(%{"kind" => "checkin"} = mark, history, taken) do
    history
    |> Enum.filter(
      &(TimelineAdapter.matches?(mark, &1) and not MapSet.member?(taken, TimelineAdapter.key(&1)))
    )
    |> Enum.max_by(&(TimelineAdapter.ts_us(&1["ts"]) || 0), fn -> nil end)
  end

  defp best_row(mark, history, _taken),
    do: Enum.find(history, &TimelineAdapter.matches?(mark, &1))

  # --- frames ---

  defp note_frame(state, frame) do
    now = DateTime.utc_now()
    state = %{state | last_frame_at: now}
    if match?(%{"type" => "eisv_update"}, frame), do: %{state | last_eisv_at: now}, else: state
  end

  defp apply_frame(state, {:mark, mark}), do: add_mark(state, mark)
  defp apply_frame(state, {:mark_and_doorbell, mark}), do: state |> add_mark(mark) |> ring()
  defp apply_frame(state, :doorbell), do: ring(state)
  defp apply_frame(state, :drop), do: state

  defp add_mark(state, mark) do
    received = System.monotonic_time()

    Enum.reduce(@window_order, state, fn window, state ->
      held = state.held[window]

      # A check-in is never treated as already in history on arrival: a row
      # within the lag window may be the agent's previous check-in, and
      # dropping a real check-in is worse than showing it twice until the next
      # re-read pairs it. Knowledge and lifecycle keys are exact.
      duplicate? =
        (mark["kind"] != "checkin" and TimelineAdapter.in_history?(mark, held.history)) or
          Enum.any?(held.provisional, fn {m, _, _} -> m == mark end)

      if duplicate? do
        state
      else
        provisional = Enum.take([{mark, received, 0} | held.provisional], @provisional_cap)
        state = %{state | held: Map.put(state.held, window, %{held | provisional: provisional})}
        write_window(state, window)
        Phoenix.PubSub.broadcast(state.pubsub, @topic, {:timeline_delta, window, mark})
        state
      end
    end)
  end

  # --- ETS ---

  defp write_window(state, window) do
    held = state.held[window]
    seen = state.seen_live[window]

    history =
      if MapSet.size(seen) == 0 do
        held.history
      else
        Enum.map(held.history, fn row ->
          if MapSet.member?(seen, TimelineAdapter.key(row)),
            do: Map.put(row, "seen_live", true),
            else: row
        end)
      end

    snapshot = %{
      window: window,
      hours: held.hours,
      status: held.status,
      error: held.error,
      events: history,
      provisional: Enum.map(held.provisional, fn {mark, _, _} -> mark end),
      paired: held.paired,
      agents: held.agents,
      truncated: held.truncated,
      counts: held.counts,
      last_reread_at: held.last_reread_at,
      last_attempt_at: held.last_attempt_at
    }

    :ets.insert(state.table, {{:window, window}, snapshot})
  end

  defp write_meta(state) do
    :ets.insert(
      state.table,
      {:meta,
       %{
         firehose: state.firehose_status,
         last_frame_at: state.last_frame_at,
         last_eisv_at: state.last_eisv_at,
         viewers: map_size(state.viewers)
       }}
    )
  end
end
