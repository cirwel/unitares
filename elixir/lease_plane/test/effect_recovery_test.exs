defmodule UnitaresLeasePlane.EffectRecoveryTest do
  @moduledoc """
  The periodic recovery sweep: the boot scan only catches orphans from a full VM
  restart, so a single-request crash (handler dies, VM lives) would leave its
  orphan until reboot. These tests assert the sweep re-scans on a timer and can
  be disabled.
  """
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.EffectRecovery

  # Records each scan and reports no orphans (reconciliation itself is covered by
  # effect_reconcile_test.exs).
  defmodule EmptyRepo do
    def orphaned_payloads do
      send(self(), :scanned)
      {:ok, []}
    end
  end

  test "init runs the boot scan and schedules a periodic sweep" do
    assert {:ok, state} = EffectRecovery.init(repo: EmptyRepo, sweep_ms: 50)
    assert_received :scanned
    assert state.sweep_ms == 50
    # the scheduled sweep fires and re-scans
    assert_receive :sweep, 300
  end

  test "handle_info(:sweep) re-scans and stays alive" do
    state = %{repo: EmptyRepo, sweep_ms: 0, last: nil}
    assert {:noreply, new_state} = EffectRecovery.handle_info(:sweep, state)
    assert_received :scanned
    assert new_state.last == %{scanned: 0, recovered: 0}
  end

  test "sweep_ms <= 0 disables the periodic sweep (boot scan only)" do
    assert {:ok, _state} = EffectRecovery.init(repo: EmptyRepo, sweep_ms: 0)
    assert_received :scanned
    refute_receive :sweep, 120
  end
end
