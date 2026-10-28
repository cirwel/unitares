defmodule Portal.TimelineParityTest do
  @moduledoc """
  The Elixir side of `tests/fixtures/timeline_parity/`. The Python side,
  `tests/test_timeline_parity_fixture.py`, asserts the same files against the
  server's own code, so the adapter and the server cannot drift apart.
  """
  use ExUnit.Case, async: true

  alias Portal.TimelineAdapter

  @fixture_dir Path.expand("../../../../tests/fixtures/timeline_parity", __DIR__)

  for name <- ~w(summary_cut lifecycle_types verdict frames) do
    @external_resource Path.join(@fixture_dir, name <> ".json")
  end

  defp load(name),
    do: @fixture_dir |> Path.join(name <> ".json") |> File.read!() |> Jason.decode!()

  test "the fixture directory is the shared one at the repo root" do
    assert File.dir?(@fixture_dir)
  end

  describe "summary cut" do
    test "the cut length and ellipsis match the fixture" do
      fixture = load("summary_cut")
      assert fixture["chars"] == TimelineAdapter.summary_chars()
      assert fixture["ellipsis"] == "…"
    end

    test "every case cuts as Python does" do
      for c <- load("summary_cut")["cases"] do
        assert TimelineAdapter.cut_summary(c["input"]) == c["expected"], c["name"]
      end
    end

    test "the fixture's code-point counts are code points, not graphemes" do
      for %{"input" => input} = c <- load("summary_cut")["cases"], is_binary(input) do
        assert length(String.codepoints(input)) == c["input_code_points"], c["name"]
      end
    end
  end

  test "the lifecycle type set is the history's" do
    assert TimelineAdapter.lifecycle_types() == load("lifecycle_types")["types"]
  end

  test "the verdict rule matches broadcaster.verdict_of" do
    for c <- load("verdict")["cases"] do
      assert TimelineAdapter.verdict_of(c["frame"]) == c["expected"], c["name"]
    end
  end

  test "every frame maps as the fixture says" do
    for c <- load("frames")["cases"] do
      actual = TimelineAdapter.map_frame(c["frame"])

      expected =
        case c["result"] do
          "mark" -> {:mark, c["mark"]}
          "mark_and_doorbell" -> {:mark_and_doorbell, c["mark"]}
          "doorbell" -> :doorbell
          "drop" -> :drop
        end

      assert actual == expected, c["name"]
    end
  end
end
