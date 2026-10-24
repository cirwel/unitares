defmodule DialecticLive.TimelineAdapterTest do
  use ExUnit.Case, async: true

  alias DialecticLive.{TimelineAdapter, TimelineFeed}

  @agent "00000000-0000-4000-8000-0000000000a1"
  @other "00000000-0000-4000-8000-0000000000b2"

  defp frame(type, extra \\ %{}) do
    Map.merge(
      %{"type" => type, "agent_id" => @agent, "timestamp" => "2026-10-24T04:16:18.847670+00:00"},
      extra
    )
  end

  describe "map_frame/1" do
    test "a guided check-in carries the guide verdict as an EISV assessment, not as core" do
      {:mark, mark} =
        TimelineAdapter.map_frame(
          frame("eisv_update", %{"decision" => %{"action" => "proceed", "sub_action" => "guide"}})
        )

      assert mark["kind"] == "checkin"
      assert mark["authored_by"] == "unknown"
      assert mark["assessment"] == %{"source" => "eisv", "verdict" => "guide"}
      refute Map.has_key?(mark, "action")
    end

    test "each history lifecycle type maps and rings the doorbell" do
      for type <- TimelineAdapter.lifecycle_types() do
        assert {:mark_and_doorbell, %{"kind" => "lifecycle", "event_type" => ^type}} =
                 TimelineAdapter.map_frame(frame(type))
      end
    end

    test "lifecycle names outside the history set are dropped" do
      for type <- ~w(lifecycle_silent lifecycle_silent_critical lifecycle_created) do
        assert TimelineAdapter.map_frame(frame(type)) == :drop
      end
    end

    test "frames that carry other people's data never become marks" do
      for type <-
            ~w(knowledge_read identity_drift dashboard_login sentinel_finding watcher_finding) do
        assert TimelineAdapter.map_frame(frame(type, %{"operator_label" => "x"})) == :drop
      end
    end

    test "a mark-bearing frame without an agent is dropped" do
      assert TimelineAdapter.map_frame(Map.delete(frame("eisv_update"), "agent_id")) == :drop
      assert TimelineAdapter.map_frame(frame("knowledge_write", %{"agent_id" => ""})) == :drop
    end

    test "non-map input is dropped, never raised on" do
      assert TimelineAdapter.map_frame("eisv_update") == :drop
      assert TimelineAdapter.map_frame(nil) == :drop
    end
  end

  describe "cut_summary/1" do
    test "counts code points: emoji, modifiers and combining marks each count" do
      # 140 graphemes but 280 code points: kept whole, as Python's len() sees 280.
      combining = String.duplicate("é", 140)
      assert TimelineAdapter.cut_summary(combining) == combining

      # 141 graphemes, 282 code points: cut, splitting a pair, exactly as Python.
      cut = TimelineAdapter.cut_summary(String.duplicate("é", 141))
      assert length(String.codepoints(cut)) == 280
      assert String.ends_with?(cut, "e…")

      thumbs = String.duplicate("👍🏽", 141)
      assert length(String.codepoints(TimelineAdapter.cut_summary(thumbs))) == 280
    end
  end

  describe "matches?/2" do
    test "knowledge matches on discovery_id" do
      mark = %{"kind" => "knowledge", "discovery_id" => "d-1", "ts" => "x"}
      assert TimelineAdapter.matches?(mark, %{"kind" => "knowledge", "discovery_id" => "d-1"})
      refute TimelineAdapter.matches?(mark, %{"kind" => "knowledge", "discovery_id" => "d-2"})
    end

    test "lifecycle matches the parsed instant, however the offset is written" do
      mark = %{
        "kind" => "lifecycle",
        "agent_id" => @agent,
        "event_type" => "lifecycle_paused",
        "ts" => "2026-10-24T04:16:18.847670+00:00"
      }

      row = %{mark | "ts" => "2026-10-24T04:16:18.847670Z"} |> Map.delete("provisional")
      assert TimelineAdapter.matches?(mark, row)
      refute TimelineAdapter.matches?(mark, %{row | "event_type" => "lifecycle_resumed"})
      refute TimelineAdapter.matches?(mark, %{row | "agent_id" => @other})
      refute TimelineAdapter.matches?(mark, %{row | "ts" => "2026-10-24T04:16:18.847671Z"})
    end

    test "a check-in matches a row recorded up to 2 s before the frame, never after" do
      mark = %{
        "kind" => "checkin",
        "agent_id" => @agent,
        "ts" => "2026-10-24T04:16:18.847670+00:00"
      }

      row = fn ts -> %{"kind" => "checkin", "agent_id" => @agent, "ts" => ts} end

      # The measured lag was 63-138 ms: the row is written, then the frame stamped.
      assert TimelineAdapter.matches?(mark, row.("2026-10-24T04:16:18.760000+00:00"))
      assert TimelineAdapter.matches?(mark, row.("2026-10-24T04:16:16.847670+00:00"))
      assert TimelineAdapter.matches?(mark, row.("2026-10-24T04:16:18.847670+00:00"))
      refute TimelineAdapter.matches?(mark, row.("2026-10-24T04:16:16.847669+00:00"))
      refute TimelineAdapter.matches?(mark, row.("2026-10-24T04:16:18.847671+00:00"))

      refute TimelineAdapter.matches?(mark, %{
               row.("2026-10-24T04:16:18.8+00:00")
               | "agent_id" => @other
             })
    end

    test "kinds never cross-match" do
      refute TimelineAdapter.matches?(
               %{"kind" => "checkin", "agent_id" => @agent, "ts" => "2026-10-24T04:16:18Z"},
               %{"kind" => "lifecycle", "agent_id" => @agent, "ts" => "2026-10-24T04:16:18Z"}
             )
    end
  end

  describe "TimelineFeed.merge/4" do
    defp now_iso(offset_ms),
      do: DateTime.utc_now() |> DateTime.add(offset_ms, :millisecond) |> DateTime.to_iso8601()

    test "a mark the re-read contains is absorbed and its row flagged" do
      mark = %{
        "kind" => "knowledge",
        "discovery_id" => "d-1",
        "ts" => now_iso(0),
        "provisional" => true
      }

      row = %{
        "kind" => "knowledge",
        "discovery_id" => "d-1",
        "ts" => now_iso(-50),
        "event_id" => "e"
      }

      {kept, matched} = TimelineFeed.merge([{mark, 0, 0}], [row], 10, 1)
      assert kept == []
      assert MapSet.member?(matched, TimelineAdapter.key(row))
    end

    test "a frame that precedes its row survives a re-read that started before it" do
      mark = %{
        "kind" => "checkin",
        "agent_id" => @agent,
        "ts" => now_iso(0),
        "provisional" => true
      }

      received = 100

      # This re-read started before the mark arrived, so it cannot count as a miss.
      {[{^mark, ^received, 0}], _} = TimelineFeed.merge([{mark, received, 0}], [], 50, 1)
    end

    test "a mark missed by two re-reads that started after it expires" do
      mark = %{
        "kind" => "checkin",
        "agent_id" => @agent,
        "ts" => now_iso(0),
        "provisional" => true
      }

      {[{^mark, 1, 1}] = once, _} = TimelineFeed.merge([{mark, 1, 0}], [], 2, 1)
      {[], _} = TimelineFeed.merge(once, [], 3, 1)
    end

    test "a mark older than the window is dropped" do
      mark = %{"kind" => "checkin", "agent_id" => @agent, "ts" => now_iso(-3_700_000)}
      {[], _} = TimelineFeed.merge([{mark, 100, 0}], [], 50, 1)
    end
  end

  test "dom ids are stable, distinct for live and history, and DOM-safe" do
    row = %{"kind" => "outcome", "outcome_id" => "o-1"}
    assert TimelineAdapter.dom_id(row) == TimelineAdapter.dom_id(row)

    refute TimelineAdapter.dom_id(row) ==
             TimelineAdapter.dom_id(Map.put(row, "provisional", true))

    assert TimelineAdapter.dom_id(row) =~ ~r/^[hp]-[0-9a-f]+$/
  end
end
