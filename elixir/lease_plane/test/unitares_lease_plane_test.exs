defmodule UnitaresLeasePlaneTest do
  use ExUnit.Case, async: false

  import LeaseTestHelpers

  alias UnitaresLeasePlane.{LeaseHolder, LeaseSupervisor, Repo}

  setup do
    surface = unique_surface_id("api")
    on_exit(fn -> cleanup_surface(surface) end)
    {:ok, surface: surface}
  end

  defp wait_until(fun, deadline_ms \\ 1_000, step_ms \\ 20)

  defp wait_until(_fun, deadline_ms, _step_ms) when deadline_ms <= 0, do: false

  defp wait_until(fun, deadline_ms, step_ms) do
    if fun.() do
      true
    else
      Process.sleep(step_ms)
      wait_until(fun, deadline_ms - step_ms, step_ms)
    end
  end

  describe "acquire_local_beam/1" do
    test "spawns a holder, persists the row, and indexes by lease_id", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)

      assert lease.surface_id == ctx.surface
      assert lease.holder_kind == "local_beam"
      assert lease.heartbeat_required == false
      assert lease.original_ttl_s == params.ttl_s
      assert lease.earned_status == "provisional"
      assert lease.released_at == nil

      assert {:ok, pid} = LeaseSupervisor.holder_for(lease.lease_id)
      assert Process.alive?(pid)
      assert ^lease = LeaseHolder.lease(pid)
    end

    test "idempotent on retry from the same holder", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, _lease1, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      assert {:ok, _lease2, :idempotent} = UnitaresLeasePlane.acquire_local_beam(params)
    end

    test "held_by_other when a different holder is active", ctx do
      params_a = local_beam_params(ctx.surface)
      params_b = local_beam_params(ctx.surface, holder_agent_uuid: random_uuid())

      assert {:ok, _lease_a, :new} = UnitaresLeasePlane.acquire_local_beam(params_a)

      assert {:error, :held_by_other, %{held_by_uuid: held_uuid}} =
               UnitaresLeasePlane.acquire_local_beam(params_b)

      assert held_uuid == params_a.holder_agent_uuid
    end
  end

  describe "release/2" do
    test "release via the holder pid stops the GenServer and writes released_at", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      assert {:ok, pid} = LeaseSupervisor.holder_for(lease.lease_id)

      ref = Process.monitor(pid)
      assert :ok = UnitaresLeasePlane.release(lease.lease_id, "normal")
      assert_receive {:DOWN, ^ref, :process, ^pid, :normal}, 1_000

      assert {:ok, nil} = UnitaresLeasePlane.status(ctx.surface)
    end

    test "kill bypasses terminate/2 — reaper covers the abnormal-exit path", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      assert {:ok, pid} = LeaseSupervisor.holder_for(lease.lease_id)

      ref = Process.monitor(pid)
      Process.exit(pid, :kill)
      assert_receive {:DOWN, ^ref, :process, ^pid, :killed}, 1_000

      # Registry cleanup of dead-pid entries is async relative to our :DOWN —
      # poll briefly until it catches up.
      assert wait_until(fn -> LeaseSupervisor.holder_for(lease.lease_id) == :error end)
    end

    test "graceful exit (:shutdown) writes down_local via terminate/2", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      assert {:ok, pid} = LeaseSupervisor.holder_for(lease.lease_id)

      ref = Process.monitor(pid)
      Process.exit(pid, :shutdown)
      assert_receive {:DOWN, ^ref, :process, ^pid, :shutdown}, 1_000

      assert {:ok, nil} = UnitaresLeasePlane.status(ctx.surface)

      # Inspect the closed row directly — query by surface_id so we don't
      # have to round-trip the lease_id through Postgrex's UUID encoder.
      sql =
        "SELECT release_reason FROM lease_plane.surface_leases " <>
          "WHERE surface_id = $1 AND released_at IS NOT NULL " <>
          "ORDER BY released_at DESC LIMIT 1"

      {:ok, %{rows: [[reason]]}} = Postgrex.query(UnitaresLeasePlane.DB, sql, [ctx.surface])
      assert reason == "down_local"
      _ = lease
    end
  end

  describe "renew/1" do
    test "extends expires_at by original_ttl_s", ctx do
      params = local_beam_params(ctx.surface, ttl_s: 60)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      original_expiry = lease.expires_at

      Process.sleep(1100)
      assert :ok = UnitaresLeasePlane.renew(lease.lease_id)

      {:ok, refreshed} = UnitaresLeasePlane.status(ctx.surface)
      assert DateTime.compare(refreshed.expires_at, original_expiry) == :gt
      assert refreshed.original_ttl_s == 60
    end

    test "renew on a released lease returns :not_found", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      assert :ok = UnitaresLeasePlane.release(lease.lease_id, "normal")
      Process.sleep(50)

      assert {:error, :not_found} = Repo.renew(lease.lease_id)
    end
  end

  describe "status/1" do
    test "returns nil for unknown surface" do
      assert {:ok, nil} = UnitaresLeasePlane.status("test:elixir/never-acquired")
    end

    test "returns the active lease record", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)

      {:ok, fetched} = UnitaresLeasePlane.status(ctx.surface)
      assert fetched.lease_id == lease.lease_id
      assert fetched.holder_agent_uuid == lease.holder_agent_uuid
      assert fetched.earned_status == "provisional"
    end
  end
end
