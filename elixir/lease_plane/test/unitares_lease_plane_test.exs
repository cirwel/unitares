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

    test "renew/3 with substrate persists for local_beam-held leases (regression for 2026-05-04 canary)" do
      # Bug caught by canary smoke test post-PR-322: HTTP /v1/lease/acquire
      # always spawns a local_beam holder via acquire_local_beam, so every
      # SDK-emitted resident substrate update hits LeaseSupervisor.holder_for
      # and would dispatch to LeaseHolder.renew/1 — which has no substrate
      # path. The substrate update was silently dropped (200 OK from renew,
      # but DB unchanged). This test pins the fix: when substrate is
      # provided, renew/3 routes through Repo.renew/3 unconditionally.
      #
      # Uses a fresh resident:/ surface (not ctx.surface, which is dialectic:/
      # via the LeaseTestHelpers default) because migration-034's
      # substrate_state_only_on_resident_kind CHECK forbids substrate writes
      # on non-resident leases.
      surface = "resident:/test_elixir_substrate_renew_#{:erlang.unique_integer([:positive])}"
      on_exit(fn -> cleanup_surface(surface) end)

      params = local_beam_params(surface, ttl_s: 60)
      assert {:ok, _lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)

      {:ok, before} = UnitaresLeasePlane.status(surface)
      assert before.substrate_state == nil
      assert before.substrate_state_observed_at == nil

      observed_at = DateTime.utc_now()
      assert :ok =
               UnitaresLeasePlane.renew(
                 before.lease_id,
                 %{
                   "E" => 0.7,
                   "I" => 0.3,
                   "S" => 0.1,
                   "V" => 0.4,
                   "sensor" => %{"status" => "degraded"}
                 },
                 observed_at
               )

      {:ok, after_renew} = UnitaresLeasePlane.status(surface)
      assert after_renew.substrate_state["E"] == 0.7
      assert after_renew.substrate_state["sensor"]["status"] == "degraded"
      assert DateTime.compare(after_renew.substrate_state_observed_at, observed_at) == :eq
    end

    test "renew/3 without substrate keeps fast-path (LeaseHolder.renew unchanged)", ctx do
      # Sibling test to the regression above. Substrate-less renews continue
      # to use the LeaseHolder fast-path so the existing in-process timer
      # logic stays in effect — the fix only re-routes when substrate is
      # provided.
      params = local_beam_params(ctx.surface, ttl_s: 60)
      assert {:ok, lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)
      original_expiry = lease.expires_at

      Process.sleep(1100)
      assert :ok = UnitaresLeasePlane.renew(lease.lease_id)

      {:ok, refreshed} = UnitaresLeasePlane.status(ctx.surface)
      assert DateTime.compare(refreshed.expires_at, original_expiry) == :gt
      # Substrate stays nil — substrate-less renew doesn't write substrate columns.
      assert refreshed.substrate_state == nil
    end

    test "idempotent re-acquire with substrate UPDATEs the existing row (regression for SDK-resident multi-cycle path)" do
      # SDK-based residents (Vigil/Sentinel/Watcher/Chronicler) use run_once
      # which builds a fresh GovernanceClient per cycle. The client's
      # _substrate_lease_cache resets each cycle, so substrate emission
      # always calls acquire — never renew. Pre-fix, idempotent acquire
      # returned the existing row unchanged, so subsequent cycles' substrate
      # observations were silently dropped. Caught 2026-05-04 multi-resident
      # canary debug: Sentinel had a substrate row from cycle 1 stuck while
      # subsequent cycles ran. Fix: idempotent acquire COALESCE-updates
      # substrate columns when caller provided them.

      surface = "resident:/test_elixir_idempotent_substrate_#{:erlang.unique_integer([:positive])}"
      on_exit(fn -> cleanup_surface(surface) end)

      params = local_beam_params(surface, ttl_s: 60)
      params_with_substrate_v1 = Map.merge(params, %{
        substrate_state: %{
          "E" => 0.5,
          "I" => 0.5,
          "S" => 0.0,
          "V" => 0.05,
          "sensor" => %{"status" => "healthy"}
        },
        substrate_state_observed_at: DateTime.utc_now()
      })

      assert {:ok, lease, :new} =
               UnitaresLeasePlane.acquire_local_beam(params_with_substrate_v1)
      first_observed_at = lease.substrate_state_observed_at
      assert lease.substrate_state["V"] == 0.05

      # Cycle 2: same holder, same surface, NEW substrate values.
      observed_at_2 = DateTime.add(DateTime.utc_now(), 1, :second)
      params_with_substrate_v2 = Map.merge(params, %{
        substrate_state: %{
          "E" => 0.7,
          "I" => 0.3,
          "S" => 0.2,
          "V" => 0.15,
          "sensor" => %{"status" => "degraded"}
        },
        substrate_state_observed_at: observed_at_2
      })

      assert {:ok, refreshed, :idempotent} =
               UnitaresLeasePlane.acquire_local_beam(params_with_substrate_v2)
      # New substrate values overwrote the old (THE BUG WAS: refreshed.substrate_state["V"] == 0.05)
      assert refreshed.substrate_state["V"] == 0.15
      assert refreshed.substrate_state["sensor"]["status"] == "degraded"
      # observed_at advanced
      assert DateTime.compare(refreshed.substrate_state_observed_at, first_observed_at) == :gt

      # SELECT confirms the row in DB matches the response (not a phantom in-memory update)
      {:ok, from_db} = UnitaresLeasePlane.status(surface)
      assert from_db.substrate_state["V"] == 0.15
    end

    test "idempotent re-acquire WITHOUT substrate is unchanged (legacy callers preserved)" do
      surface = "resident:/test_elixir_idempotent_no_substrate_#{:erlang.unique_integer([:positive])}"
      on_exit(fn -> cleanup_surface(surface) end)

      params = local_beam_params(surface, ttl_s: 60)
      assert {:ok, _lease, :new} = UnitaresLeasePlane.acquire_local_beam(params)

      # Re-acquire WITHOUT substrate fields — must take the legacy idempotent
      # path that returns the existing row unchanged (no UPDATE, no observable
      # behavior change for callers that never used substrate).
      assert {:ok, refreshed, :idempotent} = UnitaresLeasePlane.acquire_local_beam(params)
      assert refreshed.substrate_state == nil
      assert refreshed.substrate_state_observed_at == nil
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

  describe "audit outbox selection ordering" do
    # Regression: 2026-06 partition-gap wedge. 2,199 permanently-failing rows
    # at the head of ORDER BY ts ASC monopolized every LIMIT-100 batch for 11
    # days — zero forwards. Selection now orders by forward_attempts ASC
    # first, so failing rows sink behind fresh work but are still retried
    # once fresher rows drain (self-healing after the underlying repair, no
    # parked/dead-letter state).
    test "failing rows do not head-of-line block fresh rows", ctx do
      insert_event = fn ts_offset_s, attempts ->
        %{rows: [[event_id]]} =
          Postgrex.query!(
            UnitaresLeasePlane.DB,
            """
            INSERT INTO lease_plane.lease_plane_events
              (ts, event_type, surface_id, surface_kind, advisory_mode,
               payload, forward_attempts)
            VALUES (now() - make_interval(secs => $1), 'acquire', $2,
                    'dialectic', true, '{}'::jsonb, $3)
            RETURNING event_id::text
            """,
            [ts_offset_s, ctx.surface, attempts]
          )

        event_id
      end

      poison_old = insert_event.(300, 7)
      poison_older = insert_event.(600, 7)
      fresh = insert_event.(30, 0)

      # A batch smaller than the backlog must pick the fresh row first, even
      # though both poison rows are older.
      assert {:ok, [first]} = Repo.unforwarded_events(1, surface_id: ctx.surface)
      assert first.event_id == fresh

      # Failing rows are deprioritized, not dropped: a batch large enough for
      # everything still includes them, oldest-first within the same attempt
      # count.
      assert {:ok, events} = Repo.unforwarded_events(10, surface_id: ctx.surface)
      assert Enum.map(events, & &1.event_id) == [fresh, poison_older, poison_old]
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
