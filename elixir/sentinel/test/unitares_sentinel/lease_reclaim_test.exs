defmodule UnitaresSentinel.LeaseReclaimTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.LeaseReclaim

  @uuid_a "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  @uuid_b "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
  @lease_id "22222222-2222-2222-2222-222222222222"

  defp state(candidates \\ []), do: %{lease_reclaim_candidates: candidates, other: :untouched}

  test "new/0 merges an empty candidate list" do
    assert LeaseReclaim.new() == %{lease_reclaim_candidates: []}
  end

  test "acquire_opts/1 threads the candidates and is empty-safe on foreign state" do
    assert LeaseReclaim.acquire_opts(state([@uuid_a])) == [reclaim_candidates: [@uuid_a]]
    assert LeaseReclaim.acquire_opts(%{unrelated: true}) == []
  end

  test "a double transport failure contributes its attempted uuid" do
    scope = %{
      outcome: :service_unavailable,
      lease_id: nil,
      conflict: %{attempted_holder_uuid: @uuid_a}
    }

    assert %{lease_reclaim_candidates: [@uuid_a], other: :untouched} =
             LeaseReclaim.absorb(state(), scope)
  end

  test "the enforcement-wrapped variant contributes too" do
    scope = %{
      outcome: :enforcement_blocked,
      lease_id: nil,
      conflict: %{
        blocked_outcome: :service_unavailable,
        surface_id: "resident:/sentinel_cycle",
        attempted_holder_uuid: @uuid_b
      }
    }

    assert %{lease_reclaim_candidates: [@uuid_a, @uuid_b]} =
             LeaseReclaim.absorb(state([@uuid_a]), scope)
  end

  test "a successful acquire clears all candidates" do
    for outcome <- [:acquired_new, :acquired_idempotent] do
      scope = %{outcome: outcome, lease_id: @lease_id, conflict: nil}
      assert %{lease_reclaim_candidates: []} = LeaseReclaim.absorb(state([@uuid_a, @uuid_b]), scope)
    end
  end

  test "a reclaim clears resolved candidates even when the re-acquire failed" do
    # release succeeded, but the immediate re-acquire died at the transport:
    # the resolved candidates are gone (their lease was just released), only
    # the fresh attempt's uuid is worth remembering.
    scope = %{
      outcome: :service_unavailable,
      lease_id: nil,
      conflict: %{reclaimed_lease_id: @lease_id, attempted_holder_uuid: @uuid_b}
    }

    assert %{lease_reclaim_candidates: [@uuid_b]} = LeaseReclaim.absorb(state([@uuid_a]), scope)
  end

  test "a failed reclaim release keeps the candidates for the next tick" do
    scope = %{
      outcome: :held_by_other,
      lease_id: nil,
      conflict: %{
        held_by_uuid: @uuid_a,
        blocking_lease_id: @lease_id,
        reclaim_failed: true
      }
    }

    assert %{lease_reclaim_candidates: [@uuid_a]} = LeaseReclaim.absorb(state([@uuid_a]), scope)
  end

  test "conflict-free and foreign-conflict scopes leave the memory unchanged" do
    for scope <- [
          %{outcome: :service_unavailable, lease_id: nil},
          %{outcome: :service_unavailable, lease_id: nil, conflict: nil},
          %{outcome: :held_by_other, lease_id: nil, conflict: %{held_by_uuid: @uuid_b}},
          %{outcome: :permission_denied, lease_id: nil, conflict: nil}
        ] do
      assert %{lease_reclaim_candidates: [@uuid_a]} = LeaseReclaim.absorb(state([@uuid_a]), scope)
    end
  end

  test "candidates are bounded FIFO" do
    filled = Enum.map(1..64, &"uuid-#{&1}")

    scope = %{
      outcome: :service_unavailable,
      lease_id: nil,
      conflict: %{attempted_holder_uuid: @uuid_b}
    }

    %{lease_reclaim_candidates: next} = LeaseReclaim.absorb(state(filled), scope)

    assert length(next) == 64
    assert List.last(next) == @uuid_b
    refute "uuid-1" in next
    assert "uuid-2" in next
  end

  test "a state without the reclaim key passes through untouched" do
    scope = %{outcome: :acquired_new, lease_id: @lease_id, conflict: nil}
    assert %{unrelated: true} = LeaseReclaim.absorb(%{unrelated: true}, scope)
  end
end
