defmodule UnitaresLeasePlane.EffectCustodyFenceTest do
  use ExUnit.Case, async: false
  alias UnitaresLeasePlane.{Canonicalize, DB, EffectRepo, FileWriteExecutor, Repo}
  @moduletag :tmp_dir

  defmodule PausingFileOps do
    def read(path), do: File.read(path)

    def write(path, bytes) do
      send(Process.get(:observer), {:before_write, self(), bytes})

      receive do
        :write ->
          File.write(path, bytes)

        :partial_failure ->
          File.write!(path, "partial")
          {:error, :eio}
      after
        5_000 -> raise "test did not resume writer"
      end
    end

    def rm(path), do: File.rm(path)
  end

  setup %{tmp_dir: dir} do
    path = Path.join(dir, "reserved.txt")
    {:ok, surface} = Canonicalize.canonicalize("file://" <> path)
    # Canonicalization may fold the temporary directory's case on macOS.
    path = String.replace_prefix(surface, "file://", "")
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, "original")

    params = %{
      surface_id: surface,
      holder_agent_uuid: LeaseTestHelpers.random_uuid(),
      holder_kind: "remote_heartbeat",
      ttl_s: 300
    }

    {:ok, lease, :new} = Repo.acquire(params)
    lease = Map.put(lease, :surface, surface)
    id = "fence-#{LeaseTestHelpers.random_uuid()}"
    payload = %{"path" => path, "content" => "replacement"}

    :ok =
      EffectRepo.insert_effect_payload(%{
        effect_id: id,
        effect_type: "file_write",
        payload_bytes: "replacement",
        payload_sha256: sha("replacement"),
        required_leases: [%{"surface" => surface}],
        idempotency_key: id,
        idempotency_digest: id
      })

    Application.put_env(:lease_plane, :execute_file_write_commit_enabled, true)
    Application.put_env(:lease_plane, :effect_file_ops, PausingFileOps)

    on_exit(fn ->
      Application.delete_env(:lease_plane, :execute_file_write_commit_enabled)
      Application.delete_env(:lease_plane, :effect_file_ops)
      LeaseTestHelpers.cleanup_surface(surface)
      LeaseTestHelpers.cleanup_governed_effect(id)
      File.rm(path)
    end)

    %{path: path, surface: surface, params: params, lease: lease, id: id, payload: payload}
  end

  defp sha(s), do: :crypto.hash(:sha256, s) |> Base.encode16(case: :lower)

  defp expire(ctx) do
    Postgrex.query!(
      DB,
      "UPDATE lease_plane.surface_leases SET expires_at = clock_timestamp() - interval '1 second' WHERE lease_id::text = $1",
      [ctx.lease.lease_id]
    )
  end

  defp writer(ctx) do
    parent = self()

    {pid, ref} =
      spawn_monitor(fn ->
        Process.put(:observer, parent)
        send(parent, {:result, FileWriteExecutor.apply_effect(ctx.id, ctx.payload, [ctx.lease])})
      end)

    on_exit(fn -> if Process.alive?(pid), do: Process.exit(pid, :kill) end)
    assert_receive {:before_write, ^pid, "replacement"}, 2_000
    {pid, ref}
  end

  test "failed payload preparation rolls back both claim and reservation", ctx do
    id = "aborted-" <> ctx.id
    on_exit(fn -> LeaseTestHelpers.cleanup_governed_effect(id) end)

    assert {:error, :persist_failed} =
             Repo.begin_effect_custody(id, [ctx.lease], fn conn ->
               assert :inserted =
                        EffectRepo.claim_promotion_payload(
                          %{
                            effect_id: id,
                            effect_type: "file_write",
                            payload_bytes: "replacement",
                            payload_sha256: sha("replacement"),
                            idempotency_key: id,
                            idempotency_digest: id
                          },
                          conn
                        )

               {:error, :persist_failed}
             end)

    assert {:ok, nil} = EffectRepo.get_payload(id)
    assert {:ok, %{effect_fence: nil}} = Repo.status(ctx.surface)
    assert File.read!(ctx.path) == "original"
    assert :ok = Repo.release(ctx.lease.lease_id, "normal")
  end

  test "release before admission wins; stale executor cannot touch successor bytes", ctx do
    assert :ok = Repo.release(ctx.lease.lease_id, "forced")
    assert {:ok, next, :new} = Repo.acquire(ctx.params)
    File.write!(ctx.path, "successor")

    assert {:rejected, :revoked} =
             FileWriteExecutor.apply_effect(ctx.id, ctx.payload, [ctx.lease])

    assert File.read!(ctx.path) == "successor"
    assert {:ok, %{lease_id: id}} = Repo.status(ctx.surface)
    assert id == next.lease_id
  end

  test "expiry before admission denies all filesystem writes", ctx do
    expire(ctx)

    assert {:rejected, :custody_expired} =
             FileWriteExecutor.apply_effect(ctx.id, ctx.payload, [ctx.lease])

    assert File.read!(ctx.path) == "original"
  end

  @tag :fence_barrier
  test "paused OS write excludes release, handoff, expiry reaping and both successor identities",
       ctx do
    {pid, ref} = writer(ctx)
    assert {:error, :effect_in_progress} = Repo.release(ctx.lease.lease_id, "forced")

    assert {:error, :effect_in_progress} =
             Repo.accept_handoff(%{
               lease_id: ctx.lease.lease_id,
               to_holder_agent_uuid: LeaseTestHelpers.random_uuid(),
               ttl_s: 300,
               handoff_id: "test-fence"
             })

    expire(ctx)

    assert {:error, :effect_in_progress} =
             Repo.release_if_expired(ctx.lease.lease_id, "reaped_remote_ttl")

    assert {:ok, expired} = Repo.expired_active_leases(100_000)
    refute Enum.any?(expired, &(&1.lease_id == ctx.lease.lease_id))
    assert {:error, :effect_in_progress} = Repo.acquire(ctx.params)

    assert {:error, :effect_in_progress} =
             Repo.acquire(%{ctx.params | holder_agent_uuid: LeaseTestHelpers.random_uuid()})

    assert {:ok, orphans} = EffectRepo.orphaned_payloads()
    refute Enum.any?(orphans, &(&1.effect_id == ctx.id))
    assert File.read!(ctx.path) == "original"
    send(pid, :write)
    assert_receive {:result, {:committed, _}}, 2_000
    assert_receive {:DOWN, ^ref, :process, ^pid, :normal}
    assert File.read!(ctx.path) == "replacement"
    assert {:ok, next, :new} = Repo.acquire(ctx.params)
    refute next.lease_id == ctx.lease.lease_id
    File.write!(ctx.path, "successor")
    assert File.read!(ctx.path) == "successor"
  end

  test "compensation retains reservation until its last write returns", ctx do
    {pid, ref} = writer(ctx)
    send(pid, :partial_failure)
    assert_receive {:before_write, ^pid, "original"}, 2_000
    expire(ctx)
    assert {:error, :effect_in_progress} = Repo.release(ctx.lease.lease_id, "forced")
    assert {:error, :effect_in_progress} = Repo.acquire(ctx.params)
    assert File.read!(ctx.path) == "partial"
    send(pid, :write)
    assert_receive {:result, {:rejected, {:committed_failed_rolled_back, :eio}}}, 2_000
    assert_receive {:DOWN, ^ref, :process, ^pid, :normal}
    assert File.read!(ctx.path) == "original"
    assert :ok = Repo.release(ctx.lease.lease_id, "forced")
  end

  test "crashed writer leaves durable custody and recovery cannot certify its pending bytes",
       ctx do
    {pid, ref} = writer(ctx)
    Process.exit(pid, :kill)
    assert_receive {:DOWN, ^ref, :process, ^pid, :killed}
    expire(ctx)
    assert {:error, :effect_in_progress} = Repo.release(ctx.lease.lease_id, "forced")
    assert {:error, :effect_in_progress} = Repo.acquire(ctx.params)
    assert {:ok, orphans} = EffectRepo.orphaned_payloads()
    refute Enum.any?(orphans, &(&1.effect_id == ctx.id))
    assert {:ok, %{rollback_state: "pending"}} = EffectRepo.get_payload(ctx.id)
    assert File.read!(ctx.path) == "original"
  end

  test "lost database session does not retire the reservation", ctx do
    # Give the reservation its own disposable connection. Closing it models the
    # failure that releases row/advisory locks; this committed fence must remain.
    url = Application.fetch_env!(:lease_plane, :database_url)
    parsed = UnitaresLeasePlane.Application.parse_database_url(url)

    {:ok, conn} =
      Postgrex.start_link(
        hostname: parsed.host,
        port: parsed.port,
        username: parsed.username,
        password: parsed.password,
        database: parsed.database
      )

    Postgrex.query!(
      conn,
      "UPDATE lease_plane.surface_leases SET effect_fence = $2 WHERE lease_id::text = $1",
      [ctx.lease.lease_id, ctx.id]
    )

    GenServer.stop(conn)
    expire(ctx)
    assert {:error, :effect_in_progress} = Repo.release(ctx.lease.lease_id, "forced")
    assert {:error, :effect_in_progress} = Repo.acquire(ctx.params)

    assert {:error, :custody_unavailable} =
             Repo.finish_effect_custody("wrong-effect", [ctx.lease])

    assert {:ok, %{effect_fence: fence}} = Repo.status(ctx.surface)
    assert fence == ctx.id
  end

  test "untracked or replayed payload cannot authorize a write", ctx do
    Process.put(:observer, self())

    assert {:rejected, {:persist_failed, :pre_image_not_fresh}} =
             FileWriteExecutor.apply_effect("missing-" <> ctx.id, ctx.payload, [ctx.lease])

    refute_received {:before_write, _, _}
    :ok = EffectRepo.mark_committed(ctx.id)

    assert {:rejected, {:persist_failed, :pre_image_not_fresh}} =
             FileWriteExecutor.apply_effect(ctx.id, ctx.payload, [ctx.lease])

    refute_received {:before_write, _, _}
    assert File.read!(ctx.path) == "original"
  end

  test "HTTP release and acquire report busy without acknowledging revocation", ctx do
    {pid, ref} = writer(ctx)

    previous =
      for key <- [
            :bearer_token,
            :force_release_token,
            :identity_binding_mode,
            :identity_verifier
          ],
          into: %{},
          do: {key, Application.get_env(:lease_plane, key)}

    Application.put_env(:lease_plane, :bearer_token, "fence-test")
    Application.put_env(:lease_plane, :force_release_token, "fence-operator-test")
    Application.put_env(:lease_plane, :identity_binding_mode, :off)

    Application.put_env(:lease_plane, :identity_verifier, fn holder, proof, _context ->
      if proof == "proof:" <> holder, do: :ok, else: {:error, :invalid}
    end)

    on_exit(fn ->
      Enum.each(previous, fn {key, val} ->
        if is_nil(val),
          do: Application.delete_env(:lease_plane, key),
          else: Application.put_env(:lease_plane, key, val)
      end)
    end)

    for {route, body, token} <- [
          {"release", %{lease_id: ctx.lease.lease_id}, "fence-test"},
          {"force-release", %{lease_id: ctx.lease.lease_id}, "fence-operator-test"},
          {"acquire", Map.put(ctx.params, :holder_class, "process_instance"), "fence-test"}
        ] do
      conn =
        Plug.Test.conn(:post, "/v1/lease/" <> route, Jason.encode!(body))
        |> Plug.Conn.put_req_header("content-type", "application/json")
        |> Plug.Conn.put_req_header("authorization", "Bearer " <> token)
        |> Plug.Conn.put_req_header(
          "x-unitares-identity-proof",
          "proof:" <> ctx.params.holder_agent_uuid
        )
        |> UnitaresLeasePlane.HTTPRouter.call(UnitaresLeasePlane.HTTPRouter.init([]))

      assert conn.status == 409
      assert Jason.decode!(conn.resp_body)["error"] == "effect_in_progress"
    end

    send(pid, :write)
    assert_receive {:result, {:committed, _}}, 2_000
    assert_receive {:DOWN, ^ref, :process, ^pid, :normal}
  end

  test "admission rechecks expiry after waiting for an unchanged lease-row lock", ctx do
    parent = self()

    locker =
      Task.async(fn ->
        parsed =
          UnitaresLeasePlane.Application.parse_database_url(
            Application.fetch_env!(:lease_plane, :database_url)
          )

        {:ok, conn} =
          Postgrex.start_link(
            hostname: parsed.host,
            port: parsed.port,
            username: parsed.username,
            password: parsed.password,
            database: parsed.database
          )

        try do
          Postgrex.query!(
            conn,
            "UPDATE lease_plane.surface_leases SET expires_at = clock_timestamp() + interval '2 seconds' WHERE lease_id::text = $1",
            [ctx.lease.lease_id]
          )

          Postgrex.transaction(conn, fn conn ->
            Postgrex.query!(
              conn,
              "SELECT lease_id FROM lease_plane.surface_leases WHERE lease_id::text = $1 FOR UPDATE",
              [ctx.lease.lease_id]
            )

            %{rows: [[backend]]} = Postgrex.query!(conn, "SELECT pg_backend_pid()", [])
            send(parent, {:locked, backend})

            receive do
              :unlock -> :ok
            after
              5_000 -> raise "test lock timeout"
            end

            # No row update: the clock alone expires it while admission waits.
          end)
        after
          GenServer.stop(conn)
        end
      end)

    assert_receive {:locked, backend}, 1_000
    contender = Task.async(fn -> Repo.begin_effect_custody(ctx.id, [ctx.lease]) end)

    wait_until(fn ->
      %{rows: [[blocked]]} =
        Postgrex.query!(
          DB,
          "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)))",
          [backend]
        )

      blocked
    end)

    wait_until(fn ->
      %{rows: [[expired]]} =
        Postgrex.query!(
          DB,
          "SELECT expires_at <= clock_timestamp() FROM lease_plane.surface_leases WHERE lease_id::text = $1",
          [ctx.lease.lease_id]
        )

      expired
    end)

    send(locker.pid, :unlock)
    assert {:ok, :ok} = Task.await(locker)
    assert {:error, :custody_expired} = Task.await(contender)
    assert File.read!(ctx.path) == "original"
  end

  defp wait_until(fun, attempts \\ 300)
  defp wait_until(_fun, 0), do: flunk("database interleaving did not occur")

  defp wait_until(fun, attempts) do
    unless fun.() do
      Process.sleep(10)
      wait_until(fun, attempts - 1)
    end
  end
end
