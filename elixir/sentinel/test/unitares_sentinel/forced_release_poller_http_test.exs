defmodule UnitaresSentinel.ForcedReleasePollerHttpTest do
  use ExUnit.Case, async: false

  alias UnitaresSentinel.{BearerClient, CycleState, Findings, ForcedReleasePoller}

  setup do
    BearerClient.clear_preference_cache()

    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_poller_http_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    state_file = Path.join(tmpdir, ".sentinel_state")
    Application.put_env(:unitares_sentinel, :state_file_path, state_file)

    on_exit(fn ->
      BearerClient.clear_preference_cache()
      Application.delete_env(:unitares_sentinel, :state_file_path)
      Application.delete_env(:unitares_sentinel, :query_transport)
      File.rm_rf!(tmpdir)
    end)

    {:ok, state_file: state_file, shadow_file: state_file <> ".beam"}
  end

  test "HTTP tick fetches all three alarm classes, preserves Python +00:00 conflict fingerprint, and advances cursor",
       ctx do
    parent = self()
    prior = ~U[2026-11-12 10:00:00Z]

    http_get = fn url, headers, _timeout_ms ->
      send(parent, {:alarm_get, url, headers})

      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "polled_at" => "2026-11-12T10:05:00+00:00",
         "since" => "2026-11-12T10:00:00+00:00",
         "ad_hoc" => [
           %{
             "event_id" => "evt-1",
             "ts" => "2026-11-12T10:01:00+00:00",
             "lease_id" => "lease-1",
             "surface_id" => "dialectic:/s1",
             "surface_kind" => "dialectic"
           }
         ],
         "deprecation_batches" => [
           %{
             "deprecation_id" => "dep-1",
             "surface_kind" => "resident",
             "sweep_completed_at" => "2026-11-12T10:02:00+00:00",
             "event_count" => 3,
             "first_ts" => "2026-11-12T10:01:30+00:00",
             "last_ts" => "2026-11-12T10:02:00+00:00"
           }
         ],
         "conflict_batches" => [
           %{
             "surface_id" => "resident:/contended",
             "surface_kind" => "resident",
             "event_count" => 4,
             "first_ts" => "2026-11-12T10:01:00+00:00",
             "last_ts" => "2026-11-12T10:04:15.123456+00:00"
           }
         ]
       })}
    end

    env = fn
      "UNITARES_MCP_BEARER_TOKEN" -> "mcp-remote"
      _ -> nil
    end

    {alarms, new_cursor} =
      ForcedReleasePoller.tick(
        prior_cursor: prior,
        base_url: "https://gov.example",
        env: env,
        http_get: http_get,
        persist: true,
        state_path: ctx.shadow_file
      )

    assert_received {:alarm_get, url, hdrs}
    assert url == "https://gov.example/v1/lease-plane/alarm-events?since=2026-11-12T10%3A00%3A00Z"
    assert {"Authorization", "Bearer mcp-remote"} in hdrs

    assert Enum.map(alarms, & &1.kind) |> Enum.sort() == [
             "ad_hoc",
             "conflict_batch",
             "deprecation_batch"
           ]

    conflict = Enum.find(alarms, &(&1.kind == "conflict_batch"))

    assert conflict.fingerprint ==
             "forced_release:conflict_batch:resident:/contended:2026-11-12T10:04:15.123456+00:00"

    assert DateTime.compare(new_cursor, ~U[2026-11-12 10:04:15.123456Z]) == :eq
    assert CycleState.get_last_event_ts(CycleState.load(shadow: ctx.shadow_file)) != nil
  end

  test "HTTP poll failure or malformed row preserves §B6 cursor and never writes shadow state",
       ctx do
    prior = ~U[2026-11-12 10:00:00Z]

    assert {[], ^prior} =
             ForcedReleasePoller.tick(
               prior_cursor: prior,
               base_url: "https://gov.example",
               http_get: fn _u, _h, _t -> {:ok, 503, ~s({"error":"down"})} end,
               persist: true,
               state_path: ctx.shadow_file
             )

    assert {[], ^prior} =
             ForcedReleasePoller.tick(
               prior_cursor: prior,
               base_url: "https://gov.example",
               http_get: fn _u, _h, _t ->
                 {:ok, 200,
                  Jason.encode!(%{
                    "success" => true,
                    "ad_hoc" => [%{"event_id" => "bad", "ts" => "not-a-date", "surface_id" => "x", "surface_kind" => "y"}],
                    "deprecation_batches" => [],
                    "conflict_batches" => []
                  })}
               end,
               persist: true,
               state_path: ctx.shadow_file
             )

    refute File.exists?(ctx.shadow_file)
  end

  test "GenServer emits poller_blind after 3 failed HTTP polls, retries until delivered, and resets on recovery" do
    parent = self()
    poll_attempt = :counters.new(1, [])
    post_attempt = :counters.new(1, [])

    # Polls 1..4 fail; poll 5 succeeds (empty rows).
    http_get = fn _url, _headers, _timeout_ms ->
      n = :counters.get(poll_attempt, 1) + 1
      :counters.put(poll_attempt, 1, n)
      send(parent, {:poll, n})

      if n <= 4 do
        {:error, :econnrefused}
      else
        {:ok, 200,
         Jason.encode!(%{
           "success" => true,
           "ad_hoc" => [],
           "deprecation_batches" => [],
           "conflict_batches" => []
         })}
      end
    end

    # First blind-alarm POST (on poll #3) fails; retry on poll #4 succeeds!
    http_post = fn _url, body, _headers, _timeout_ms ->
      m = :counters.get(post_attempt, 1) + 1
      :counters.put(post_attempt, 1, m)
      send(parent, {:blind_post, m, body})

      if m == 1 do
        {:error, :timeout}
      else
        {:ok, 200, ~s({"success":true,"deduped":false})}
      end
    end

    {:ok, pid} =
      GenServer.start(
        ForcedReleasePoller,
        [
          transport: :http,
          http_opts: [base_url: "https://gov.example", http_get: http_get],
          interval_ms: 60_000,
          initial_delay_ms: 60_000,
          jitter_ms: 0,
          lease_advisory: false,
          lease_blocked_state_path: false,
          emit_findings: true,
          findings_opts: [http_post: http_post]
        ]
      )

    # Tick 1 & 2: below threshold, no blind alarm posted.
    send(pid, :tick)
    assert_receive {:poll, 1}, 1_000
    send(pid, :tick)
    assert_receive {:poll, 2}, 1_000
    refute_receive {:blind_post, _, _}, 30

    # Tick 3: hits threshold (3x), attempts blind alarm POST (#1 fails).
    send(pid, :tick)
    assert_receive {:poll, 3}, 1_000
    assert_receive {:blind_post, 1, body1}, 1_000
    assert body1["type"] == "sentinel_alarm_finding"
    assert body1["alarm_kind"] == "poller_blind"
    assert body1["severity"] == "high"
    assert body1["consecutive_failures"] == 3

    assert body1["fingerprint"] ==
             Findings.compute_fingerprint([
               "forced_release",
               "poller_blind",
               "https://gov.example"
             ])

    # Tick 4: still failing (4x), retries blind alarm POST (#2 lands!).
    send(pid, :tick)
    assert_receive {:poll, 4}, 1_000
    assert_receive {:blind_post, 2, body2}, 1_000
    assert body2["consecutive_failures"] == 4

    # Tick 5: poll recovers -> resets failure streak & blind_alarm_delivered?.
    send(pid, :tick)
    assert_receive {:poll, 5}, 1_000
    Process.sleep(25)

    state = :sys.get_state(pid)
    assert state.consecutive_failures == 0
    assert state.blind_alarm_delivered? == false

    GenServer.stop(pid)
  end
end
