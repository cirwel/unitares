defmodule UnitaresSentinel.CutoverTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.Cutover

  setup do
    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_cutover_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    canonical = Path.join(tmpdir, ".sentinel_state")
    shadow = canonical <> ".beam"

    on_exit(fn -> File.rm_rf!(tmpdir) end)

    {:ok, canonical: canonical, shadow: shadow}
  end

  test "cutover_to_beam writes max cursor to shadow with beam runtime", ctx do
    write_state(ctx.canonical, "2026-05-05T01:00:00Z")
    write_state(ctx.shadow, "2026-05-05T02:00:00Z", %{"runtime" => "shadow"})

    {:ok, result} = Cutover.cutover_to_beam(canonical: ctx.canonical, shadow: ctx.shadow)

    assert result.runtime == "beam_canonical"
    assert result.cursor == "2026-05-05T02:00:00Z"

    decoded = decode!(ctx.shadow)
    assert decoded["runtime"] == "beam_canonical"

    assert get_in(decoded, ["forced_release_alarm", "last_event_ts"]) ==
             "2026-05-05T02:00:00Z"

    canonical = decode!(ctx.canonical)
    refute Map.has_key?(canonical, "runtime")
  end

  test "cutover_to_beam lets canonical win when canonical cursor is newer", ctx do
    write_state(ctx.canonical, "2026-05-05T03:00:00Z")
    write_state(ctx.shadow, "2026-05-05T02:00:00Z")

    {:ok, result} = Cutover.cutover_to_beam(canonical: ctx.canonical, shadow: ctx.shadow)

    assert result.cursor == "2026-05-05T03:00:00Z"

    assert get_in(decode!(ctx.shadow), ["forced_release_alarm", "last_event_ts"]) ==
             "2026-05-05T03:00:00Z"
  end

  test "cutover_to_beam parses mixed ISO precision before max-cursor merge", ctx do
    # Raw string comparison would pick the older "Z" cursor because "Z" sorts
    # above "." after the whole-second prefix.
    write_state(ctx.canonical, "2026-05-05T02:00:00Z")
    write_state(ctx.shadow, "2026-05-05T02:00:00.000001+00:00")

    {:ok, result} = Cutover.cutover_to_beam(canonical: ctx.canonical, shadow: ctx.shadow)

    assert result.cursor == "2026-05-05T02:00:00.000001+00:00"

    assert get_in(decode!(ctx.shadow), ["forced_release_alarm", "last_event_ts"]) ==
             "2026-05-05T02:00:00.000001+00:00"
  end

  test "rollback_to_python copies max cursor back to canonical and marks shadow", ctx do
    write_state(ctx.canonical, "2026-05-05T01:00:00Z")
    write_state(ctx.shadow, "2026-05-05T04:00:00Z", %{"runtime" => "beam_canonical"})

    {:ok, result} = Cutover.rollback_to_python(canonical: ctx.canonical, shadow: ctx.shadow)

    assert result.runtime == "python_canonical"
    assert result.cursor == "2026-05-05T04:00:00Z"

    canonical = decode!(ctx.canonical)

    assert get_in(canonical, ["forced_release_alarm", "last_event_ts"]) ==
             "2026-05-05T04:00:00Z"

    refute Map.has_key?(canonical, "runtime")

    shadow = decode!(ctx.shadow)
    assert shadow["runtime"] == "python_canonical"

    assert get_in(shadow, ["forced_release_alarm", "last_event_ts"]) ==
             "2026-05-05T04:00:00Z"
  end

  defp write_state(path, cursor, extra \\ %{}) do
    state =
      Map.merge(extra, %{
        "forced_release_alarm" => %{"last_event_ts" => cursor}
      })

    File.write!(path, Jason.encode!(state))
  end

  defp decode!(path), do: path |> File.read!() |> Jason.decode!()
end
