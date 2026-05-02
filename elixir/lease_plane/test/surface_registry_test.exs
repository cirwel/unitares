defmodule UnitaresLeasePlane.SurfaceRegistryTest do
  @moduledoc """
  R7 Phase 1 spike tests for the pure in-memory OTP lease server.

  These tests intentionally avoid the durable Postgres lease-plane path. The
  contract under test is the hot coordination primitive: one registry serializes
  surface ownership, one process owns each active lease TTL, and expiry removes
  the active claim without operator cleanup.
  """

  use ExUnit.Case, async: false

  import LeaseTestHelpers

  alias UnitaresLeasePlane.SurfaceRegistry

  @racer_count 50

  setup do
    registry = start_supervised!(SurfaceRegistry)
    {:ok, registry: registry, surface: unique_surface_id("memory")}
  end

  describe "acquire/status" do
    test "acquires an in-memory lease and returns status", ctx do
      params = memory_params(ctx.surface)

      assert {:ok, lease, :new} = SurfaceRegistry.acquire(ctx.registry, params)
      refute Map.has_key?(lease, :pid)
      assert lease.surface_id == ctx.surface
      assert lease.holder_agent_uuid == params.holder_agent_uuid
      assert lease.intent == params.intent
      assert lease.evidence_ref == params.evidence_ref
      assert %DateTime{} = lease.expires_at

      assert {:ok, fetched} = SurfaceRegistry.status(ctx.registry, ctx.surface)
      assert fetched.lease_id == lease.lease_id
    end

    test "same holder gets the existing active lease idempotently", ctx do
      holder = random_uuid()
      params = memory_params(ctx.surface, holder_agent_uuid: holder)

      assert {:ok, lease, :new} = SurfaceRegistry.acquire(ctx.registry, params)
      assert {:ok, same, :idempotent} = SurfaceRegistry.acquire(ctx.registry, params)

      assert same.lease_id == lease.lease_id
      assert same.holder_agent_uuid == holder
    end

    test "different holder receives typed conflict with current owner", ctx do
      holder_a = random_uuid()
      holder_b = random_uuid()

      assert {:ok, lease, :new} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder_a, intent: "writer A")
               )

      assert {:error, :held_by_other, conflict} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder_b)
               )

      assert conflict.lease_id == lease.lease_id
      assert conflict.held_by_uuid == holder_a
      assert conflict.intent == "writer A"
      assert %DateTime{} = conflict.expires_at
    end
  end

  describe "release and expiry" do
    test "holder release removes active status and allows reacquire", ctx do
      holder_a = random_uuid()
      holder_b = random_uuid()

      assert {:ok, lease, :new} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder_a)
               )

      assert :ok = SurfaceRegistry.release(ctx.registry, lease.lease_id, holder_a, "normal")
      assert {:ok, nil} = SurfaceRegistry.status(ctx.registry, ctx.surface)

      assert {:ok, next, :new} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder_b)
               )

      assert next.lease_id != lease.lease_id
      assert next.holder_agent_uuid == holder_b
    end

    test "non-holder cannot release another holder's lease", ctx do
      holder_a = random_uuid()
      holder_b = random_uuid()

      assert {:ok, lease, :new} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder_a)
               )

      assert {:error, :not_holder} =
               SurfaceRegistry.release(ctx.registry, lease.lease_id, holder_b, "normal")

      assert {:ok, still_active} = SurfaceRegistry.status(ctx.registry, ctx.surface)
      assert still_active.lease_id == lease.lease_id
    end

    test "holder renew extends the in-memory TTL", ctx do
      holder = random_uuid()

      assert {:ok, lease, :new} =
               SurfaceRegistry.acquire(
                 ctx.registry,
                 memory_params(ctx.surface, holder_agent_uuid: holder, ttl_ms: 80)
               )

      assert {:ok, renewed} = SurfaceRegistry.renew(ctx.registry, lease.lease_id, holder, 300)
      assert DateTime.compare(renewed.expires_at, lease.expires_at) == :gt

      Process.sleep(140)
      assert {:ok, still_active} = SurfaceRegistry.status(ctx.registry, ctx.surface)
      assert still_active.lease_id == lease.lease_id

      assert eventually(
               fn -> SurfaceRegistry.status(ctx.registry, ctx.surface) == {:ok, nil} end,
               500
             )
    end

    test "lease process TTL expiry removes active status", ctx do
      assert {:ok, lease, :new} =
               SurfaceRegistry.acquire(ctx.registry, memory_params(ctx.surface, ttl_ms: 40))

      assert lease.ttl_ms == 40
      assert eventually(fn -> SurfaceRegistry.status(ctx.registry, ctx.surface) == {:ok, nil} end)
    end
  end

  describe "concurrent acquire" do
    test "different holders racing one surface produce exactly one winner", ctx do
      results =
        1..@racer_count
        |> Task.async_stream(
          fn _i -> SurfaceRegistry.acquire(ctx.registry, memory_params(ctx.surface)) end,
          max_concurrency: System.schedulers_online() * 2,
          timeout: 5_000
        )
        |> Enum.map(fn {:ok, result} -> result end)

      winners = Enum.filter(results, &match?({:ok, _, :new}, &1))
      held = Enum.filter(results, &match?({:error, :held_by_other, _}, &1))

      assert length(winners) == 1
      assert length(held) == @racer_count - 1
    end
  end

  defp memory_params(surface_id, opts \\ []) do
    %{
      surface_id: surface_id,
      holder_agent_uuid: Keyword.get(opts, :holder_agent_uuid, random_uuid()),
      holder_label: Keyword.get(opts, :holder_label, "test-holder"),
      episode_id: Keyword.get(opts, :episode_id, "test-episode"),
      harness: Keyword.get(opts, :harness, "exunit"),
      intent: Keyword.get(opts, :intent, "phase 1 memory lease"),
      evidence_ref: Keyword.get(opts, :evidence_ref, "test:r7-phase1"),
      ttl_ms: Keyword.get(opts, :ttl_ms, 1_000)
    }
  end

  defp eventually(fun, deadline_ms \\ 1_000, step_ms \\ 10)

  defp eventually(_fun, deadline_ms, _step_ms) when deadline_ms <= 0, do: false

  defp eventually(fun, deadline_ms, step_ms) do
    if fun.() do
      true
    else
      Process.sleep(step_ms)
      eventually(fun, deadline_ms - step_ms, step_ms)
    end
  end
end
