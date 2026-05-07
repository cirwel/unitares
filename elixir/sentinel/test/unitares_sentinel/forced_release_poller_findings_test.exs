defmodule UnitaresSentinel.ForcedReleasePollerFindingsTest do
  @moduledoc """
  Surface 2 bindings for forced-release findings emission.

  The GenServer runtime path must POST alarms before persisting the candidate
  cursor. If the process crashes between poll and emit, the cursor remains
  behind and the next boot can replay the alarms, matching Python ordering.
  """

  use ExUnit.Case, async: false

  @moduletag :db

  alias SentinelTestHelpers, as: H
  alias UnitaresSentinel.ForcedReleasePoller

  setup do
    label = H.random_label()
    surface_prefix = "dialectic:/test_sentinel_findings_#{label}"

    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_findings_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    state_file = Path.join(tmpdir, ".sentinel_state")
    Application.put_env(:unitares_sentinel, :state_file_path, state_file)

    on_exit(fn ->
      H.cleanup_surface_prefix(surface_prefix)
      Application.delete_env(:unitares_sentinel, :state_file_path)
      File.rm_rf!(tmpdir)
    end)

    {:ok, surface_prefix: surface_prefix, state_file: state_file}
  end

  test "GenServer emits findings before persisting candidate cursor", ctx do
    parent = self()
    shadow_path = ctx.state_file <> ".beam"
    prior = ~U[2030-01-01 00:00:00.000000Z]
    event_ts = DateTime.add(prior, 1, :second)
    surface_id = ctx.surface_prefix <> "/emit_order"

    File.write!(
      ctx.state_file,
      ~s({"forced_release_alarm":{"last_event_ts":"#{DateTime.to_iso8601(prior)}"}})
    )

    {event_id, _returned_ts} = H.insert_forced_event(surface_id, event_ts)

    http_post = fn _url, body, _headers, _timeout_ms ->
      if body["event_id"] == event_id do
        send(parent, {:posted_target_alarm, body, File.exists?(shadow_path)})
      end

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    {:ok, pid} =
      ForcedReleasePoller.start_link(
        name: :"test_findings_emit_#{System.unique_integer([:positive])}",
        db: UnitaresSentinel.DB,
        interval_ms: 60_000,
        initial_delay_ms: 60_000,
        jitter_ms: 0,
        emit_findings: true,
        findings_opts: [
          agent_id: "sentinel-test",
          agent_name: "Sentinel",
          http_post: http_post
        ]
      )

    send(pid, :tick)

    assert_receive {:posted_target_alarm, body, persisted_before_post?}, 2_000

    refute persisted_before_post?,
           "cursor must not be written until after the Surface 2 emit loop completes"

    assert body["type"] == "sentinel_alarm_finding"
    assert body["alarm_kind"] == "ad_hoc"
    assert body["fingerprint"] == "forced_release:ad_hoc:#{event_id}"

    Process.sleep(50)
    assert File.exists?(shadow_path), "cursor should persist after emit loop"

    decoded = shadow_path |> File.read!() |> Jason.decode!()

    assert get_in(decoded, ["forced_release_alarm", "last_event_ts"]) ==
             DateTime.to_iso8601(event_ts)

    GenServer.stop(pid)
  end

  test "GenServer first boot bounds nil cursor by lookback window", ctx do
    parent = self()
    now = DateTime.utc_now()
    old_surface = ctx.surface_prefix <> "/old_backfill"
    new_surface = ctx.surface_prefix <> "/within_lookback"

    {old_event_id, _old_ts} = H.insert_forced_event(old_surface, DateTime.add(now, -120, :second))
    {new_event_id, _new_ts} = H.insert_forced_event(new_surface, DateTime.add(now, -10, :second))

    http_post = fn _url, body, _headers, _timeout_ms ->
      cond do
        body["event_id"] == old_event_id ->
          send(parent, {:posted_old_backfill, body})

        body["event_id"] == new_event_id ->
          send(parent, {:posted_within_lookback, body})

        true ->
          :ok
      end

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    {:ok, pid} =
      ForcedReleasePoller.start_link(
        name: :"test_first_boot_lookback_#{System.unique_integer([:positive])}",
        db: UnitaresSentinel.DB,
        interval_ms: 60_000,
        initial_delay_ms: 60_000,
        jitter_ms: 0,
        first_boot_lookback_seconds: 60,
        emit_findings: true,
        findings_opts: [http_post: http_post]
      )

    send(pid, :tick)

    assert_receive {:posted_within_lookback, body}, 2_000
    assert body["event_id"] == new_event_id
    refute_receive {:posted_old_backfill, _body}, 200

    GenServer.stop(pid)
  end

  test "GenServer wraps runtime tick with advisory acquire and release", ctx do
    parent = self()
    shadow_path = ctx.state_file <> ".beam"
    prior = ~U[2030-01-02 00:00:00.000000Z]
    event_ts = DateTime.add(prior, 1, :second)
    surface_id = ctx.surface_prefix <> "/lease_advisory"
    lease_id = "33333333-3333-3333-3333-333333333333"
    holder_uuid = "44444444-4444-4444-4444-444444444444"

    File.write!(
      ctx.state_file,
      ~s({"forced_release_alarm":{"last_event_ts":"#{DateTime.to_iso8601(prior)}"}})
    )

    {event_id, _returned_ts} = H.insert_forced_event(surface_id, event_ts)

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      cond do
        String.ends_with?(url, "/v1/lease/acquire") ->
          send(parent, {:lease_acquire, body})

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: lease_id},
             drift_warning: []
           })}

        String.ends_with?(url, "/v1/lease/release") ->
          send(parent, {:lease_release, body, File.exists?(shadow_path)})
          {:ok, 200, ~s({"ok":true})}
      end
    end

    findings_http_post = fn _url, body, _headers, _timeout_ms ->
      if body["event_id"] == event_id do
        send(parent, {:posted_target_alarm, body, File.exists?(shadow_path)})
      end

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    {:ok, pid} =
      ForcedReleasePoller.start_link(
        name: :"test_lease_advisory_#{System.unique_integer([:positive])}",
        db: UnitaresSentinel.DB,
        interval_ms: 60_000,
        initial_delay_ms: 60_000,
        jitter_ms: 0,
        lease_advisory: true,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          holder_agent_uuid: holder_uuid,
          http_post: lease_http_post
        ],
        emit_findings: true,
        findings_opts: [http_post: findings_http_post]
      )

    send(pid, :tick)

    assert_receive {:lease_acquire, acquire_body}, 2_000
    assert acquire_body["surface_id"] == "resident:/sentinel_cycle"
    assert acquire_body["holder_agent_uuid"] == holder_uuid
    assert acquire_body["holder_class"] == "process_instance"
    assert acquire_body["holder_kind"] == "remote_heartbeat"
    assert acquire_body["ttl_s"] == 300
    assert acquire_body["intent"] == "sentinel analysis cycle"

    assert_receive {:posted_target_alarm, body, persisted_before_post?}, 2_000
    assert body["event_id"] == event_id
    refute persisted_before_post?

    assert_receive {:lease_release, release_body, persisted_before_release?}, 2_000
    assert release_body == %{"lease_id" => lease_id, "release_reason" => "normal"}
    assert persisted_before_release?

    GenServer.stop(pid)
  end

  test "GenServer runtime task timeout preserves cursor and releases advisory lease", ctx do
    parent = self()
    shadow_path = ctx.state_file <> ".beam"
    prior = ~U[2030-01-03 00:00:00.000000Z]
    event_ts = DateTime.add(prior, 1, :second)
    surface_id = ctx.surface_prefix <> "/runtime_timeout"
    lease_id = "55555555-5555-5555-5555-555555555555"

    File.write!(
      ctx.state_file,
      ~s({"forced_release_alarm":{"last_event_ts":"#{DateTime.to_iso8601(prior)}"}})
    )

    {event_id, _returned_ts} = H.insert_forced_event(surface_id, event_ts)

    lease_http_post = fn url, body, _headers, _timeout_ms ->
      cond do
        String.ends_with?(url, "/v1/lease/acquire") ->
          send(parent, {:lease_acquire, body})

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: lease_id},
             drift_warning: []
           })}

        String.ends_with?(url, "/v1/lease/release") ->
          send(parent, {:lease_release, body, File.exists?(shadow_path)})
          {:ok, 200, ~s({"ok":true})}
      end
    end

    findings_http_post = fn _url, body, _headers, _timeout_ms ->
      if body["event_id"] == event_id do
        send(parent, {:finding_emit_started, body})
        Process.sleep(5_000)
      end

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    {:ok, pid} =
      ForcedReleasePoller.start_link(
        name: :"test_runtime_timeout_#{System.unique_integer([:positive])}",
        db: UnitaresSentinel.DB,
        interval_ms: 60_000,
        initial_delay_ms: 60_000,
        jitter_ms: 0,
        tick_timeout_ms: 25,
        lease_advisory: true,
        lease_opts: [
          base_url: "http://lease.test",
          bearer_token: "test-token",
          http_post: lease_http_post
        ],
        emit_findings: true,
        findings_opts: [http_post: findings_http_post]
      )

    send(pid, :tick)

    assert_receive {:lease_acquire, _acquire_body}, 2_000
    assert_receive {:finding_emit_started, body}, 2_000
    assert body["event_id"] == event_id

    assert_receive {:lease_release, release_body, persisted_before_release?}, 2_000
    assert release_body == %{"lease_id" => lease_id, "release_reason" => "normal"}
    refute persisted_before_release?, "timed-out runtime task must not advance the cursor"

    state = :sys.get_state(pid)
    refute state.running?
    assert DateTime.compare(state.cursor, prior) == :eq
    refute File.exists?(shadow_path)

    GenServer.stop(pid)
  end
end
