defmodule UnitaresLeasePlane.AcquireConcurrencyTest do
  @moduledoc """
  Concurrent-acquire load tests for `UnitaresLeasePlane`.

  Existing `unitares_lease_plane_test.exs` covers acquire/release/handoff
  behavior serially: do A, then B, expect outcome. The lease plane's
  load-bearing invariant — exactly-one-winner under concurrent acquire on
  the same surface_id — only manifests under parallel pressure. Serial
  tests can't catch it.

  These tests spawn N tasks via `Task.async_stream` (default concurrency =
  schedulers, so genuine parallelism on multi-core macOS), all racing the
  same operation. They assert the contract holds:

  - **Exactly one winner** (different holders racing same surface_id)
  - **Idempotent winner** (same holder racing same surface_id — all return
    the same lease)
  - **Exactly one accept** (N tasks racing handoff_accept on same handoff_id)

  `async: false` because every test reuses the same DB schema; concurrent
  test files would interleave inserts/deletes and corrupt cleanup.
  """

  use ExUnit.Case, async: false

  import LeaseTestHelpers

  alias UnitaresLeasePlane

  # 50 racers is enough to surface a race in the typical case without
  # blowing up CI runtime. Each Task spawns a Postgrex connection request
  # so we're bounded by the test pool size (configured pool_size: 2 per
  # config/test.exs). Tasks that don't get a connection slot wait, which
  # is fine — the race window is checkout-then-INSERT, not the spawn.
  @racer_count 50

  describe "concurrent acquire — different holders" do
    @tag :capture_log
    test "exactly one of #{@racer_count} racers wins; the rest get held_by_other" do
      surface = unique_surface_id("conc_diff")
      on_exit(fn -> cleanup_surface(surface) end)

      results =
        1..@racer_count
        |> Task.async_stream(
          fn _i ->
            params = local_beam_params(surface)
            UnitaresLeasePlane.acquire_local_beam(params)
          end,
          max_concurrency: System.schedulers_online() * 2,
          timeout: 15_000
        )
        |> Enum.map(fn {:ok, result} -> result end)

      winners = Enum.filter(results, &match?({:ok, _, :new}, &1))
      held = Enum.filter(results, &match?({:error, :held_by_other, _}, &1))
      other = results -- (winners ++ held)

      assert length(winners) == 1,
             "exactly-one-winner invariant violated: expected 1 winner, got #{length(winners)}.\n  results: #{inspect(results)}"

      assert length(held) == @racer_count - 1,
             "expected #{@racer_count - 1} held_by_other, got #{length(held)}.\n  unaccounted: #{inspect(other)}"

      assert other == [],
             "all responses must classify as winner or held_by_other; unclassified: #{inspect(other)}"
    end
  end

  describe "concurrent acquire — same holder (idempotency)" do
    test "all #{@racer_count} racers with same holder_uuid converge to one lease" do
      surface = unique_surface_id("conc_same")
      on_exit(fn -> cleanup_surface(surface) end)

      holder = random_uuid()
      params = local_beam_params(surface, holder_agent_uuid: holder)

      results =
        1..@racer_count
        |> Task.async_stream(
          fn _i -> UnitaresLeasePlane.acquire_local_beam(params) end,
          max_concurrency: System.schedulers_online() * 2,
          timeout: 15_000
        )
        |> Enum.map(fn {:ok, result} -> result end)

      successes = Enum.filter(results, &match?({:ok, _, _}, &1))
      failures = results -- successes

      assert length(successes) == @racer_count,
             "expected all #{@racer_count} same-holder acquires to succeed (idempotent). failures: #{inspect(failures)}"

      lease_ids =
        successes
        |> Enum.map(fn {:ok, lease, _} -> lease.lease_id end)
        |> Enum.uniq()

      assert length(lease_ids) == 1,
             "idempotent same-holder acquires must converge to a single lease_id. got: #{inspect(lease_ids)}"

      kinds = successes |> Enum.map(fn {:ok, _, kind} -> kind end) |> Enum.frequencies()

      # Exactly one :new is the winner; the rest are :idempotent. We don't
      # assert order (race is real), just the cardinality.
      assert kinds[:new] == 1,
             "exactly one :new is expected (the racer that won the create); got #{inspect(kinds)}"

      assert kinds[:idempotent] == @racer_count - 1,
             "the remaining #{@racer_count - 1} acquires must be :idempotent; got #{inspect(kinds)}"
    end
  end

  describe "concurrent handoff_accept (GenServer.call serialization)" do
    # Council NIT 1: HandoffServer is a single named GenServer; all accepts
    # serialize through its mailbox. This test verifies the contract (exactly
    # one accept honored, rest get :not_found) but does NOT exercise a DB-level
    # race — the GenServer's serial dispatch makes that impossible by
    # construction. Kept as a contract test.
    test "exactly one of N racers wins handoff_accept; the rest get not_found" do
      surface = unique_surface_id("conc_handoff")
      on_exit(fn -> cleanup_surface(surface) end)

      holder_a = random_uuid()
      holder_b = random_uuid()

      {:ok, lease, :new} =
        UnitaresLeasePlane.acquire_local_beam(
          local_beam_params(surface, holder_agent_uuid: holder_a, intent: "handoff race source")
        )

      {:ok, handoff_id} = UnitaresLeasePlane.handoff_offer(lease.lease_id, holder_b, 30)

      racers = 20

      results =
        1..racers
        |> Task.async_stream(
          fn _i -> UnitaresLeasePlane.handoff_accept(handoff_id) end,
          max_concurrency: System.schedulers_online() * 2,
          timeout: 15_000
        )
        |> Enum.map(fn {:ok, result} -> result end)

      oks = Enum.count(results, &(&1 == :ok))
      not_found = Enum.count(results, &match?({:error, :not_found}, &1))

      assert oks == 1,
             "exactly-one-handoff-accept invariant violated: expected 1 :ok, got #{oks}.\n  results: #{inspect(results)}"

      assert oks + not_found == racers,
             "all responses must be :ok or {:error, :not_found}; unclassified: #{inspect(results -- List.duplicate(:ok, oks))}"
    end
  end
end
