defmodule UnitaresSdk.LeaseReclaimTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.LeaseReclaim

  @uuid_a "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  @uuid_b "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
  @lease_id "22222222-2222-2222-2222-222222222222"
  @now ~U[2026-08-01 22:00:00Z]

  defp state(candidates \\ []), do: %{lease_reclaim_candidates: candidates, other: :untouched}
  defp uuids(%{lease_reclaim_candidates: candidates}), do: Enum.map(candidates, &elem(&1, 0))

  defp acquired(uuid) do
    %{outcome: :acquired_new, lease_id: @lease_id, conflict: nil, holder_uuid: uuid}
  end

  describe "uuid-history reclaim state machine (#1459 / #1462)" do
    test "new/0 returns an empty candidate list" do
      assert LeaseReclaim.new() == %{lease_reclaim_candidates: []}
    end

    test "acquire_opts/1 threads bare uuids and is empty-safe on foreign state" do
      assert LeaseReclaim.acquire_opts(state([{@uuid_a, nil}])) ==
               [reclaim_candidates: [@uuid_a]]

      assert LeaseReclaim.acquire_opts(%{unrelated: true}) == []
    end

    test "own_orphan_uuid?/2 matches against state maps, keyword opts, and candidate lists" do
      st = state([{@uuid_a, nil}])
      opts = LeaseReclaim.acquire_opts(st)

      assert LeaseReclaim.own_orphan_uuid?(@uuid_a, st)
      assert LeaseReclaim.own_orphan_uuid?(@uuid_a, opts)
      assert LeaseReclaim.own_orphan_uuid?(@uuid_a, [@uuid_a])
      assert LeaseReclaim.own_orphan_uuid?(@uuid_a, [{@uuid_a, @now}])

      refute LeaseReclaim.own_orphan_uuid?(@uuid_b, st)
      refute LeaseReclaim.own_orphan_uuid?(@uuid_b, opts)
      refute LeaseReclaim.own_orphan_uuid?(nil, st)
      refute LeaseReclaim.own_orphan_uuid?("", st)
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

    test "unproven entries survive arbitrarily long (never age-expired)" do
      scope = %{outcome: :held_by_other, lease_id: nil, conflict: %{held_by_uuid: "other"}}

      next = LeaseReclaim.absorb(state([{@uuid_a, nil}]), scope, now: @now)
      assert uuids(next) == [@uuid_a]
    end

    test "proven entries drop only after the 15-minute grace window" do
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

    test "candidates are bounded at 4096 as a backstop" do
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

  describe "release reason and 422-only vintage fallback (#1459 / #1462)" do
    test "canonical release reason" do
      assert LeaseReclaim.release_reason() == "reclaimed_lost_acquire"
      assert LeaseReclaim.release_reason(:uuid_history) == "reclaimed_lost_acquire"
    end

    test "fallback_release_reason/2 retries with normal ONLY on HTTP 422 schema_invalid" do
      assert {:retry, "normal"} =
               LeaseReclaim.fallback_release_reason(
                 {:ok, 422, %{"ok" => false, "error" => "schema_invalid"}}
               )

      assert {:retry, "normal"} =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:ok, 422, %{"ok" => false, "error" => "schema_invalid"}}
               )

      assert {:retry, "normal"} =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:error, {:release_refused, 422, "schema_invalid", %{}}}
               )

      # Bare 422 without schema_invalid, or schema_invalid with non-422 status, must not fall back
      assert :no_fallback = LeaseReclaim.fallback_release_reason(:uuid_history, 422)

      assert :no_fallback =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:ok, 422, %{"ok" => false, "error" => "other_error"}}
               )

      assert :no_fallback =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:ok, 503, %{"ok" => false, "error" => "schema_invalid"}}
               )

      assert :no_fallback =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:ok, %{"ok" => false, "error" => "schema_invalid"}}
               )

      assert :no_fallback = LeaseReclaim.fallback_release_reason(:uuid_history, 503)

      assert :no_fallback =
               LeaseReclaim.fallback_release_reason(:uuid_history, {:error, :timeout})
    end
  end
end
