defmodule Portal.TimelineAdapter do
  @moduledoc """
  Pure functions that turn `/ws/eisv` frames into provisional timeline marks
  and match them against the authoritative `GET /v1/timeline` window.

  This is an anti-corruption adapter over an undeclared stream, not a plugin
  seam. `/ws/eisv` is unversioned and unfiltered, so this module is expected to
  be thrown away once the core publishes a declared subscribe contract
  (`docs/proposals/active/portal-live-timeline-v0.md` §3.7).

  Marks are string-keyed maps shaped like `/v1/timeline` events, so a live mark
  and a history row render through the same code. A live mark also carries
  `"provisional" => true`.

  Three rules here mirror the Python server, and the shared parity fixture
  `tests/fixtures/timeline_parity/` pins both sides:

    * `verdict_of/1` is `src/broadcaster.py` `verdict_of`:
      `decision.sub_action || decision.action || "proceed"`, with Python's
      falsiness (an empty string falls through);
    * `cut_summary/1` is `src/http_routes/timeline.py`'s knowledge cut: over
      280 code points (not graphemes), keep 279 plus U+2026;
    * `lifecycle_types/0` is `src/http_routes/telemetry.py`
      `_LIFECYCLE_EVENT_TYPES`, the seven types the history reads.
  """

  @summary_chars 280

  @lifecycle_types ~w(
    lifecycle_paused
    lifecycle_resumed
    lifecycle_archived
    lifecycle_loop_detected
    lifecycle_stuck_detected
    circuit_breaker_trip
    circuit_breaker_reset
  )

  # A check-in frame is stamped after its state row is written, in the same
  # server process, so the row's recorded_at is at most this far before it.
  @checkin_lag_us 2_000_000

  @live_kinds ~w(checkin lifecycle knowledge)
  @reread_only_kinds ~w(outcome lineage dialectic)

  def lifecycle_types, do: @lifecycle_types
  def summary_chars, do: @summary_chars
  def live_kinds, do: @live_kinds
  def reread_only_kinds, do: @reread_only_kinds

  @typedoc "What a frame means for the timeline."
  @type mapped :: {:mark, map()} | {:mark_and_doorbell, map()} | :doorbell | :drop

  @doc """
  Map one decoded firehose frame.

    * `eisv_update` → a core `checkin` mark (`authored_by: "unknown"`) with an
      optional EISV `assessment` decoration;
    * one of the seven history lifecycle types → a `lifecycle` mark plus a
      doorbell;
    * `knowledge_write` → a `knowledge` mark, summary re-cut to Python's rule;
    * `dialectic_*` → a doorbell only;
    * anything else, and any mark-bearing frame without an `agent_id`, is
      dropped.
  """
  @spec map_frame(term()) :: mapped()
  def map_frame(%{"type" => "dialectic" <> _}), do: :doorbell

  def map_frame(%{"type" => type, "agent_id" => agent_id, "timestamp" => ts} = frame)
      when is_binary(agent_id) and agent_id != "" and is_binary(ts) do
    cond do
      type == "eisv_update" ->
        {:mark, checkin_mark(frame)}

      type in @lifecycle_types ->
        {:mark_and_doorbell,
         base_mark("lifecycle", frame)
         |> Map.put("event_type", type)
         |> Map.put("reason", frame["reason"])}

      type == "knowledge_write" ->
        {:mark,
         base_mark("knowledge", frame)
         |> Map.put("discovery_id", frame["discovery_id"])
         |> Map.put("discovery_type", frame["discovery_type"])
         |> Map.put("summary", cut_summary(frame["summary"]))}

      true ->
        :drop
    end
  end

  def map_frame(_frame), do: :drop

  defp checkin_mark(frame) do
    mark =
      base_mark("checkin", frame)
      |> Map.put("authored_by", "unknown")

    # The verdict is an EISV assessment, not core: it is attached as an
    # optional decoration, and a mark without it must render.
    case frame do
      %{"decision" => %{}} ->
        Map.put(mark, "assessment", %{"source" => "eisv", "verdict" => verdict_of(frame)})

      _ ->
        mark
    end
  end

  defp base_mark(kind, frame) do
    %{
      "kind" => kind,
      "ts" => frame["timestamp"],
      "agent_id" => frame["agent_id"],
      "provisional" => true
    }
  end

  @doc """
  The verdict an `eisv_update` carries, as `src/broadcaster.py` `verdict_of`
  reads it: `sub_action` when truthy, else `action`, else `"proceed"`.
  """
  @spec verdict_of(term()) :: term()
  def verdict_of(%{"decision" => %{} = decision}) do
    first_truthy([decision["sub_action"], decision["action"]]) || "proceed"
  end

  def verdict_of(_frame), do: "proceed"

  @doc """
  The bucket a verdict falls in, as `src/broadcaster.py` `verdict_bucket`
  sorts it: `"guide"`; `"proceed"` for no action or a proceed, approve or
  continue; `"pause"` for anything else, so a hard stop recorded as its
  sub-action (`risk_pause`, `cirs_block` and the like), or a new one, is a
  pause rather than a proceed.
  """
  @spec verdict_bucket(term()) :: String.t()
  def verdict_bucket(action) do
    case action |> to_string() |> String.downcase() do
      "guide" -> "guide"
      v when v in ["", "proceed", "approve", "continue"] -> "proceed"
      _ -> "pause"
    end
  end

  defp first_truthy(values), do: Enum.find(values, &py_truthy?/1)

  # Python's `or` skips None, False, 0, "" and empty containers.
  defp py_truthy?(nil), do: false
  defp py_truthy?(false), do: false
  defp py_truthy?(""), do: false
  defp py_truthy?(0), do: false
  defp py_truthy?(+0.0), do: false
  defp py_truthy?([]), do: false
  defp py_truthy?(map) when map == %{}, do: false
  defp py_truthy?(_), do: true

  @doc """
  Python's knowledge summary cut: when the code-point length is over 280, keep
  the first 279 code points plus U+2026. Code points, not graphemes, so an emoji
  modifier or a combining mark counts as its own character, as `len()` does.
  """
  @spec cut_summary(term()) :: term()
  def cut_summary(summary) when is_binary(summary) do
    points = String.codepoints(summary)

    if length(points) > @summary_chars do
      Enum.join(Enum.take(points, @summary_chars - 1)) <> "…"
    else
      summary
    end
  end

  def cut_summary(other), do: other

  @doc "Parse an ISO-8601 timestamp to Unix microseconds; `nil` when unparseable."
  @spec ts_us(term()) :: integer() | nil
  def ts_us(ts) when is_binary(ts) do
    case DateTime.from_iso8601(ts) do
      {:ok, dt, _offset} -> DateTime.to_unix(dt, :microsecond)
      _ -> nil
    end
  end

  def ts_us(_), do: nil

  @doc """
  A stable identity for a history row or mark, used for de-duplication and the
  DOM id. Provisional check-ins have no exact key until the server exposes
  `state_id` (proposal §4 item 1), so they match by `checkin_match?/2` instead.
  """
  @spec key(map()) :: tuple()
  def key(%{"kind" => "knowledge", "discovery_id" => id}) when is_binary(id),
    do: {"knowledge", id}

  def key(%{"kind" => "lifecycle"} = e),
    do: {"lifecycle", e["agent_id"], e["event_type"], ts_us(e["ts"])}

  def key(%{"kind" => "checkin"} = e), do: {"checkin", e["agent_id"], ts_us(e["ts"])}
  def key(%{"kind" => "outcome", "outcome_id" => id}), do: {"outcome", id}
  def key(%{"kind" => "dialectic", "session_id" => id}), do: {"dialectic", id}
  def key(%{"kind" => kind, "event_id" => id}) when is_binary(id), do: {kind, id}
  def key(%{} = e), do: {e["kind"], e["agent_id"], e["ts"]}
  def key(other), do: {:unkeyed, other}

  @doc "A DOM-safe id derived from `key/1`."
  @spec dom_id(map()) :: String.t()
  def dom_id(event) do
    digest = :crypto.hash(:sha256, :erlang.term_to_binary(key(event)))
    prefix = if event["provisional"], do: "p-", else: "h-"
    prefix <> Base.encode16(binary_part(digest, 0, 10), case: :lower)
  end

  @doc """
  Whether a history row is the persisted form of a provisional mark.

    * knowledge: same `discovery_id`;
    * lifecycle: same `agent_id`, `event_type` and parsed `ts` instant;
    * check-in: same `agent_id`, history `ts` in `[frame_ts − 2 s, frame_ts]`,
      and the same verdict bucket, so a pause arriving within 2 s of an
      earlier proceed is not taken for that proceed's row.
  """
  @spec matches?(map(), map()) :: boolean()
  def matches?(%{"kind" => "checkin"} = mark, %{"kind" => "checkin"} = row),
    do: checkin_match?(mark, row)

  def matches?(%{"kind" => kind} = mark, %{"kind" => kind} = row) do
    key(mark) == key(row) and elem(key(mark), tuple_size(key(mark)) - 1) != nil
  end

  def matches?(_mark, _row), do: false

  defp checkin_match?(mark, row) do
    frame_us = ts_us(mark["ts"])
    row_us = ts_us(row["ts"])

    mark["agent_id"] == row["agent_id"] and is_integer(frame_us) and is_integer(row_us) and
      row_us <= frame_us and row_us >= frame_us - @checkin_lag_us and
      same_verdict?(mark, row)
  end

  # A live mark carries the frame's verdict as its assessment; a row carries
  # the persisted action. Both are the decision's sub_action or action.
  defp same_verdict?(%{"assessment" => %{"verdict" => verdict}}, row),
    do: verdict_bucket(verdict) == verdict_bucket(row["action"])

  defp same_verdict?(_mark, _row), do: true

  @doc "Whether any row in `history` is the persisted form of `mark`."
  @spec in_history?(map(), [map()]) :: boolean()
  def in_history?(mark, history), do: Enum.any?(history, &matches?(mark, &1))
end
