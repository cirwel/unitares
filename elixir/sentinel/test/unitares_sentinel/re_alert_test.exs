defmodule UnitaresSentinel.ReAlertTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.ReAlert

  @min 60 * 1_000
  @hour 60 * @min
  @opts [base_ms: @hour, cap_ms: 4 * @hour]

  # Drive one key every 5 minutes for `minutes`, return the minutes it emitted at.
  defp emissions(minutes, rank_at \\ fn _ -> 2 end) do
    {emitted, _} =
      Enum.reduce(0..minutes//5, {[], ReAlert.new()}, fn m, {acc, state} ->
        case ReAlert.decide(state, :k, rank_at.(m), m * @min, @opts) do
          {:emit, _info, state} -> {[m | acc], state}
          {:suppress, state} -> {acc, state}
        end
      end)

    Enum.reverse(emitted)
  end

  test "a condition that holds re-alerts at 1h, then doubling, capped" do
    # 0, +60, +120, +240, then +240 (cap) ...
    assert emissions(12 * 60) == [0, 60, 180, 420, 660]
  end

  test "an escalation emits immediately and restarts the backoff" do
    # medium until minute 20, high after.
    assert emissions(120, fn m -> if m < 20, do: 2, else: 3 end) == [0, 20, 80]
  end

  test "a rise after a lower-severity scheduled re-alert is an escalation" do
    # high at 0, medium from 5 (re-alert due at 60 goes out as medium), high again at 65.
    rank_at = fn m -> if m == 0 or m >= 65, do: 3, else: 2 end
    assert emissions(70, rank_at) == [0, 60, 65]
  end

  test "a de-escalation does not emit early" do
    assert emissions(50, fn m -> if m < 20, do: 3, else: 2 end) == [0]
  end

  test "an emission reports how many repeats it stands for" do
    state = ReAlert.new()
    {:emit, first, state} = ReAlert.decide(state, :k, 2, 0, @opts)
    assert first.suppressed_since_last == 0
    assert first.emit_seq == 1

    state =
      Enum.reduce(1..11, state, fn i, st ->
        {:suppress, st} = ReAlert.decide(st, :k, 2, i * 5 * @min, @opts)
        st
      end)

    {:emit, second, _} = ReAlert.decide(state, :k, 2, 60 * @min, @opts)
    assert second.suppressed_since_last == 11
    assert second.emit_seq == 2
    assert ReAlert.change_token(second, "high") != ReAlert.change_token(first, "high")
  end

  test "a condition quiet for longer than its interval is a new episode" do
    {:emit, _, state} = ReAlert.decide(ReAlert.new(), :k, 2, 0, @opts)
    {:suppress, state} = ReAlert.decide(state, :k, 2, 30 * @min, @opts)
    # Unseen from minute 30 to minute 95 (> 60 min interval) => cleared.
    assert {:emit, info, _} = ReAlert.decide(state, :k, 2, 95 * @min, @opts)
    assert info.emit_seq == 1
    assert ReAlert.prune(state, 95 * @min) == %{}
  end
end
