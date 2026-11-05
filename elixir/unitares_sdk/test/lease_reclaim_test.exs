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

  describe "singleton intent-tag reclaim classification (#1462)" do
    @owner_intent "dispatch_beam:singleton:poller@host"

    test "200 with nil lease classifies as :already_free" do
      reply = {200, %{"ok" => true, "lease" => nil}}
      assert :already_free = LeaseReclaim.intent_reclaim_decision(reply, @owner_intent)
      assert :already_free = LeaseReclaim.own_orphan_status?(reply, @owner_intent)
    end

    test "200 with matching non-empty owner_intent and lease_id is {:reclaimable, lease_id}" do
      reply = {
        200,
        %{
          "ok" => true,
          "lease" => %{
            "lease_id" => @lease_id,
            "intent" => @owner_intent,
            "holder_uuid" => @uuid_a
          }
        }
      }

      assert {:reclaimable, @lease_id} =
               LeaseReclaim.intent_reclaim_decision(reply, @owner_intent)
    end

    test "different intent, missing lease_id, blank owner_intent, or non-200 classifies as :foreign" do
      matching_body = %{
        "ok" => true,
        "lease" => %{"lease_id" => @lease_id, "intent" => @owner_intent}
      }

      assert :foreign =
               LeaseReclaim.intent_reclaim_decision(
                 {200,
                  %{"ok" => true, "lease" => %{"lease_id" => @lease_id, "intent" => "other"}}},
                 @owner_intent
               )

      assert :foreign =
               LeaseReclaim.intent_reclaim_decision(
                 {200, %{"ok" => true, "lease" => %{"intent" => @owner_intent}}},
                 @owner_intent
               )

      assert :foreign = LeaseReclaim.intent_reclaim_decision({200, matching_body}, "")
      assert :foreign = LeaseReclaim.intent_reclaim_decision({200, matching_body}, "   ")
      assert :foreign = LeaseReclaim.intent_reclaim_decision({503, matching_body}, @owner_intent)
    end
  end

  describe "release reasons and 422-only vintage fallback (#1459 / #1462)" do
    test "canonical release reasons per mode" do
      assert LeaseReclaim.release_reason(:uuid_history) == "reclaimed_lost_acquire"
      assert LeaseReclaim.release_reason(:intent_tag) == "reaped_after_supervisor_failed"

      assert LeaseReclaim.reclaim_release_reasons(:uuid_history) == [
               "reclaimed_lost_acquire",
               "normal"
             ]

      assert LeaseReclaim.reclaim_release_reasons(:intent_tag) == [
               "reaped_after_supervisor_failed"
             ]
    end

    test "fallback_release_reason/2 retries with normal ONLY on 422 schema_invalid for uuid_history" do
      assert {:retry, "normal"} = LeaseReclaim.fallback_release_reason(:uuid_history, 422)

      assert {:retry, "normal"} =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:ok, %{"ok" => false, "error" => "schema_invalid"}}
               )

      assert {:retry, "normal"} =
               LeaseReclaim.fallback_release_reason(
                 :uuid_history,
                 {:error, {:release_refused, 422, "schema_invalid", %{}}}
               )

      assert :no_fallback = LeaseReclaim.fallback_release_reason(:uuid_history, 503)

      assert :no_fallback =
               LeaseReclaim.fallback_release_reason(:uuid_history, {:error, :timeout})

      assert :no_fallback = LeaseReclaim.fallback_release_reason(:intent_tag, 422)
    end
  end
end
