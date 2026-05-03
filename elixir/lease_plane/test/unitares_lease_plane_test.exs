defmodule UnitaresLeasePlaneTest do
  use ExUnit.Case, async: false

  import LeaseTestHelpers

  alias UnitaresLeasePlane.{AuditOutboxForwarder, LeaseHolder, LeaseSupervisor, Reaper, Repo}

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

  describe "handoff" do
    test "offer and accept transfer active lease to a remote-heartbeat holder", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      to_holder = random_uuid()

      assert {:ok, handoff_id} = UnitaresLeasePlane.handoff_offer(lease.lease_id, to_holder, 45)
      assert :ok = UnitaresLeasePlane.handoff_accept(handoff_id)

      assert {:ok, transferred} = UnitaresLeasePlane.status(ctx.surface)
      assert transferred.lease_id != lease.lease_id
      assert transferred.holder_agent_uuid == to_holder
      assert transferred.holder_kind == "remote_heartbeat"
      assert transferred.heartbeat_required == true
      assert transferred.original_ttl_s == 45

      sql =
        "SELECT release_reason FROM lease_plane.surface_leases " <>
          "WHERE lease_id = $1 AND released_at IS NOT NULL"

      {:ok, %{rows: [[reason]]}} =
        Postgrex.query(UnitaresLeasePlane.DB, sql, [uuid_to_binary(lease.lease_id)])

      assert reason == "handoff"
    end

    test "unknown handoff returns not_found" do
      assert {:error, :not_found} = UnitaresLeasePlane.handoff_accept(random_uuid())
    end
  end

  describe "reaper" do
    test "releases expired remote heartbeat leases with reaped_remote_ttl", ctx do
      params = local_beam_params(ctx.surface, ttl_s: 1)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_remote_heartbeat(params)

      Process.sleep(1100)
      assert {:ok, %{reaped: reaped}} = Reaper.perform(%{limit: 100})
      assert reaped >= 1
      assert {:ok, nil} = UnitaresLeasePlane.status(ctx.surface)

      sql =
        "SELECT release_reason FROM lease_plane.surface_leases " <>
          "WHERE lease_id = $1 AND released_at IS NOT NULL"

      {:ok, %{rows: [[reason]]}} =
        Postgrex.query(UnitaresLeasePlane.DB, sql, [uuid_to_binary(lease.lease_id)])

      assert reason == "reaped_remote_ttl"
    end
  end

  describe "audit outbox forwarder" do
    test "projects lease events into audit.tool_usage and marks them forwarded", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, _lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)

      event_id = acquire_event_id(ctx.surface)

      assert {:ok, %{forwarded: 1, failed: 0}} =
               AuditOutboxForwarder.perform(%{limit: 10, surface_id: ctx.surface})

      sql = """
      SELECT tool_name, payload->>'surface_id'
      FROM audit.tool_usage
      WHERE payload->>'lease_event_id' = $1
      """

      {:ok, %{rows: [["lease.acquire", surface_id]]}} =
        Postgrex.query(UnitaresLeasePlane.DB, sql, [event_id])

      assert surface_id == ctx.surface

      {:ok, %{rows: [[forwarded_at]]}} =
        Postgrex.query(
          UnitaresLeasePlane.DB,
          "SELECT forwarded_at FROM lease_plane.lease_plane_events WHERE event_id = $1",
          [uuid_to_binary(event_id)]
        )

      assert %DateTime{} = forwarded_at
    end

    test "§7.2.8 contract — top-level keys present, surface_id un-encoded, §6.1 LIKE works", ctx do
      params = local_beam_params(ctx.surface)
      assert {:ok, _lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      event_id = acquire_event_id(ctx.surface)

      assert {:ok, %{forwarded: 1, failed: 0}} =
               AuditOutboxForwarder.perform(%{limit: 10, surface_id: ctx.surface})

      {:ok, %{rows: [[payload]]}} =
        Postgrex.query(
          UnitaresLeasePlane.DB,
          "SELECT payload FROM audit.tool_usage WHERE payload->>'lease_event_id' = $1",
          [event_id]
        )

      for key <- ~w(surface_id surface_kind lease_id lease_event_id
                     holder_agent_uuid holder_class advisory_mode earned_status) do
        assert Map.has_key?(payload, key),
               "§7.2.8: payload missing top-level key '#{key}'"
      end

      assert payload["surface_id"] == ctx.surface

      refute String.contains?(payload["surface_id"], "%"),
             "§7.2.8: surface_id must not be percent-encoded in audit payload"

      [expected_kind, _] = String.split(ctx.surface, ":", parts: 2)
      assert payload["surface_kind"] == expected_kind

      {:ok, %{rows: [[1]]}} =
        Postgrex.query(
          UnitaresLeasePlane.DB,
          "SELECT 1 FROM audit.tool_usage " <>
            "WHERE payload->>'lease_event_id' = $1 " <>
            "AND payload->>'surface_id' LIKE $2",
          [event_id, expected_kind <> ":%"]
        )
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

  defp acquire_event_id(surface_id) do
    {:ok, %{rows: [[event_id]]}} =
      Postgrex.query(
        UnitaresLeasePlane.DB,
        "SELECT event_id::text FROM lease_plane.lease_plane_events WHERE surface_id = $1 AND event_type = 'acquire' ORDER BY ts DESC LIMIT 1",
        [surface_id]
      )

    event_id
  end

  defp uuid_to_binary(
         <<a::binary-size(8), "-", b::binary-size(4), "-", c::binary-size(4), "-",
           d::binary-size(4), "-", e::binary-size(12)>>
       ) do
    Base.decode16!(a <> b <> c <> d <> e, case: :mixed)
  end
end
