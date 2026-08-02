defmodule UnitaresSentinel.LeaseReclaimTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.LeaseReclaim

  @uuid_a "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  @uuid_b "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
  @lease_id "22222222-2222-2222-2222-222222222222"
  @now ~U[2026-08-01 22:00:00Z]

  defp state(candidates \\ []), do: %{lease_reclaim_candidates: candidates, other: :untouched}

  defp uuids(%{lease_reclaim_candidates: candidates}), do: Enum.map(candidates, &elem(&1, 0))

  defp acquired(uuid \\ nil) do
    base = %{outcome: :acquired_new, lease_id: @lease_id, conflict: nil}
    if uuid, do: Map.put(base, :holder_uuid, uuid), else: base
  end

  test "new/0 merges an empty candidate list" do
    assert LeaseReclaim.new() == %{lease_reclaim_candidates: []}
  end

  test "acquire_opts/1 threads bare uuids and is empty-safe on foreign state" do
    assert LeaseReclaim.acquire_opts(state([{@uuid_a, nil}])) ==
             [reclaim_candidates: [@uuid_a]]

    assert LeaseReclaim.acquire_opts(%{unrelated: true}) == []
  end

  test "a double transport failure contributes its attempted uuid, unproven" do
    scope = %{
      outcome: :service_unavailable,
      lease_id: nil,
      conflict: %{attempted_holder_uuid: @uuid_a}
    }

    next = LeaseReclaim.absorb(state(), scope, now: @now)
    assert next.lease_reclaim_candidates == [{@uuid_a, nil}]
    assert next.other == :untouched
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

    next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), scope, now: @now)
    assert uuids(next) == [@uuid_a, @uuid_b]
  end

  # If the eventual release request is lost, the lease plane auto-renews the
  # lease forever; the acquired holder uuid is the only handle on that orphan.
  test "a successful acquire contributes its own holder uuid, unproven" do
    next = LeaseReclaim.absorb(state(), acquired(@uuid_b), now: @now)
    assert next.lease_reclaim_candidates == [{@uuid_b, nil}]
  end

  test "a successful acquire stamps EXISTING entries absence-proven but not its own" do
    next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), acquired(@uuid_b), now: @now)
    assert next.lease_reclaim_candidates == [{@uuid_a, @now}, {@uuid_b, nil}]
  end

  test "an earlier absence stamp is not overwritten by a later success" do
    earlier = DateTime.add(@now, -60, :second)
    next = LeaseReclaim.absorb(state([{@uuid_a, earlier}]), acquired(@uuid_b), now: @now)
    assert {@uuid_a, ^earlier} = hd(next.lease_reclaim_candidates)
  end

  # An orphan lives unboundedly (plane-side auto-renew), so a uuid that might
  # hold one must outlive any stall. Pure age expiry would forget the
  # stall-opening uuid — the one whose INSERT committed — during a stall
  # longer than the window.
  test "unproven entries survive arbitrarily long" do
    scope = %{outcome: :held_by_other, lease_id: nil, conflict: %{held_by_uuid: "other"}}

    next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), scope, now: @now)
    assert uuids(next) == [@uuid_a]
  end

  test "proven entries drop only after the grace window" do
    recent = DateTime.add(@now, -14 * 60, :second)
    stale = DateTime.add(@now, -16 * 60, :second)
    scope = %{outcome: :held_by_other, lease_id: nil, conflict: %{held_by_uuid: "other"}}

    next =
      LeaseReclaim.absorb(state([{@uuid_a, stale}, {@uuid_b, recent}]), scope, now: @now)

    assert uuids(next) == [@uuid_b]
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

    next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), scope, now: @now)
    assert uuids(next) == [@uuid_a]
  end

  test "conflict-free and foreign-conflict scopes leave the memory unchanged" do
    for scope <- [
          %{outcome: :service_unavailable, lease_id: nil},
          %{outcome: :service_unavailable, lease_id: nil, conflict: nil},
          %{outcome: :held_by_other, lease_id: nil, conflict: %{held_by_uuid: @uuid_b}},
          %{outcome: :permission_denied, lease_id: nil, conflict: nil}
        ] do
      next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), scope, now: @now)
      assert next.lease_reclaim_candidates == [{@uuid_a, nil}]
    end
  end

  test "candidates are bounded as a backstop" do
    filled = Enum.map(1..4096, &{"uuid-#{&1}", nil})

    scope = %{
      outcome: :service_unavailable,
      lease_id: nil,
      conflict: %{attempted_holder_uuid: @uuid_b}
    }

    next = LeaseReclaim.absorb(state(filled), scope, now: @now)

    assert length(next.lease_reclaim_candidates) == 4096
    assert List.last(uuids(next)) == @uuid_b
    refute "uuid-1" in uuids(next)
    assert "uuid-2" in uuids(next)
  end

  test "a state without the reclaim key passes through untouched" do
    assert %{unrelated: true} =
             LeaseReclaim.absorb(%{unrelated: true}, acquired(@uuid_a), now: @now)
  end
end
