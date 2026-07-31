defmodule UnitaresSentinel.ForcedReleasePollerStarvationTest do
  @moduledoc """
  Lease-starvation self-reporting for the 30s cycle poller (2026-07-31
  immortal-lease incident).

  Deliberately NOT `@moduletag :db`. The lease-enforcement-blocked branch
  returns before `await_runtime_tick/1` ever reaches Postgrex, so none of this
  needs a database — and `test_helper.exs` excludes `:db` when governance_test
  is unreachable, which would hide these tests on a DB-less CI box. Putting them
  in either existing poller-GenServer file would have inherited that tag.

  Hermeticity (PR #1410 discipline): the lease plane and /api/findings are
  separate injected stubs tagged `{:lease_acquire, _}` and
  `{:finding_posted, _}`; no assertion counts total POSTs; ticks are driven by
  explicit `send(pid, :tick)` with the scheduler parked a minute out; and
  `lease_blocked_state_path: false` keeps the sidecar file off the filesystem.
  """

  use ExUnit.Case, async: false

  alias UnitaresSentinel.ForcedReleasePoller

  @blocking_lease_id "b583498a-51a8-4fc4-8e69-8796423f7491"
  @holder_uuid "58eb2d62-c24a-8cfc-33e1-1366a6a80604"

  defp blocked_lease_post(parent) do
    fn url, body, _headers, _timeout_ms ->
      if String.ends_with?(url, "/v1/lease/acquire") do
        send(parent, {:lease_acquire, body})

        {:ok, 409,
         Jason.encode!(%{
           ok: false,
           error: "held_by_other",
           held_by_uuid: @holder_uuid,
           blocking_lease_id: @blocking_lease_id
         })}
      else
        send(parent, {:unexpected_release, body})
        {:ok, 200, ~s({"ok":true})}
      end
    end
  end

  defp finding_post(parent) do
    fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end
  end

  defp start_poller(parent, prefix, opts) do
    {:ok, pid} =
      ForcedReleasePoller.start_link(
        Keyword.merge(
          [
            name: :"test_poller_#{prefix}_#{System.unique_integer([:positive])}",
            db: :"unused_db_#{System.unique_integer([:positive])}",
            initial_delay_ms: 60_000,
            interval_ms: 60_000,
            jitter_ms: 0,
            lease_advisory: true,
            lease_opts: [
              base_url: "http://lease.test",
              bearer_token: "test-token",
              enforced_surface_kinds: MapSet.new(["resident"]),
              http_post: blocked_lease_post(parent)
            ],
            emit_findings: true,
            findings_opts: [agent_id: "sentinel-test", http_post: finding_post(parent)],
            lease_blocked_state_path: false
          ],
          opts
        )
      )

    on_exit(fn -> if Process.alive?(pid), do: GenServer.stop(pid) end)
    pid
  end

  # Backdate the episode rather than sleeping past a real threshold: the ladder
  # trips on elapsed seconds, so this IS the whole condition.
  defp backdate(pid, seconds) do
    :sys.replace_state(pid, fn state ->
      %{state | lease_blocked_since: DateTime.add(DateTime.utc_now(), -seconds, :second)}
    end)
  end

  test "poller stays silent below the lease-blocked alert threshold" do
    parent = self()
    pid = start_poller(parent, "silent", lease_blocked_alert_after_seconds: 86_400)

    send(pid, :tick)
    assert_receive {:lease_acquire, body}, 1_000
    assert body["surface_id"] == "resident:/sentinel_cycle"
    refute_receive {:finding_posted, _}, 200

    assert :sys.get_state(pid).lease_blocked_streak == 1
  end

  test "poller emits a lease-starvation self finding once the episode passes the threshold" do
    parent = self()
    pid = start_poller(parent, "starved", lease_blocked_alert_after_seconds: 60)

    send(pid, :tick)
    assert_receive {:lease_acquire, _}, 1_000
    refute_receive {:finding_posted, _}, 100

    backdate(pid, 61)
    send(pid, :tick)

    assert_receive {:finding_posted, finding}, 1_000
    assert finding["type"] == "sentinel_finding"
    assert finding["finding_type"] == "sentinel_lease_starved"
    assert finding["severity"] == "high"
    assert finding["resident"] == "ForcedReleasePoller"
    # The poller passes no :surface_id in :lease_opts, so the surface can only
    # reach the finding via enforce_scope/3 stamping it into the conflict.
    assert finding["surface_id"] == "resident:/sentinel_cycle"
    assert finding["message"] =~ "resident:/sentinel_cycle"
    assert finding["blocking_lease_id"] == @blocking_lease_id
    assert finding["message"] =~ "/v1/lease/force-release"
  end

  test "poller re-escalates at 2x and stays silent between ladder rungs" do
    parent = self()
    pid = start_poller(parent, "ladder", lease_blocked_alert_after_seconds: 60)

    send(pid, :tick)
    backdate(pid, 61)
    send(pid, :tick)
    assert_receive {:finding_posted, rung_1}, 1_000

    # Still rung 1: silent.
    backdate(pid, 90)
    send(pid, :tick)
    refute_receive {:finding_posted, _}, 200

    backdate(pid, 121)
    send(pid, :tick)
    assert_receive {:finding_posted, rung_2}, 1_000

    assert rung_1["change_token"] != rung_2["change_token"]
    assert :sys.get_state(pid).lease_blocked_last_emitted_multiple == 2
  end

  test "a lost findings POST leaves the rung due so the next tick retries it" do
    # gov-MCP being unreachable is correlated with residents starving, so the
    # densest, most valuable early alerts are the ones a fire-and-forget design
    # would silently drop.
    parent = self()

    flaky_post = fn _url, body, _headers, _timeout_ms ->
      case Process.get(:findings_attempts, 0) do
        0 ->
          Process.put(:findings_attempts, 1)
          send(parent, :finding_post_failed)
          {:error, :econnrefused}

        _ ->
          send(parent, {:finding_posted, body})
          {:ok, 200, ~s({"success":true,"deduped":false})}
      end
    end

    pid =
      start_poller(parent, "retry",
        lease_blocked_alert_after_seconds: 60,
        findings_opts: [agent_id: "sentinel-test", http_post: flaky_post]
      )

    send(pid, :tick)
    backdate(pid, 61)
    send(pid, :tick)

    assert_receive :finding_post_failed, 1_000
    assert :sys.get_state(pid).lease_blocked_last_emitted_multiple == 0

    # Same rung, next tick: retried and delivered.
    backdate(pid, 62)
    send(pid, :tick)

    assert_receive {:finding_posted, finding}, 1_000
    assert finding["finding_type"] == "sentinel_lease_starved"
    assert :sys.get_state(pid).lease_blocked_last_emitted_multiple == 1
  end

  test "poller emits nothing when findings emission is disabled" do
    parent = self()

    pid =
      start_poller(parent, "quiet",
        lease_blocked_alert_after_seconds: 60,
        emit_findings: false
      )

    send(pid, :tick)
    backdate(pid, 3_600)
    send(pid, :tick)

    assert_receive {:lease_acquire, _}, 1_000
    refute_receive {:finding_posted, _}, 200
  end
end
