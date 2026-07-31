defmodule UnitaresSentinel.LeaseStarvationTest do
  @moduledoc """
  Pure-logic bindings for the lease-starvation self-finding (2026-07-31
  immortal-lease incident: 5,703 consecutive "tick skipped by lease
  enforcement" warnings, zero alerts, every liveness signal healthy).

  Hermetic by construction: no network (findings POSTs go through an injected
  `:http_post`), no DB, and `state_path: false` disables the sidecar file except
  in the two persistence tests, which use their own tmpdir.
  """

  use ExUnit.Case, async: true

  alias UnitaresSentinel.{Findings, LeaseStarvation}

  @lease_id "b583498a-51a8-4fc4-8e69-8796423f7491"
  @holder_uuid "788992bb-4b9c-2306-cb83-0b83091d71b9"
  @surface "resident:/sentinel_cycle"

  defp t0, do: ~U[2026-07-31 21:11:03Z]

  defp at(seconds), do: DateTime.add(t0(), seconds, :second)

  defp tracker(opts \\ []) do
    LeaseStarvation.new(
      Keyword.merge(
        [
          resident: "ForcedReleasePoller",
          surface_id: @surface,
          alert_after_seconds: 60,
          state_path: false
        ],
        opts
      )
    )
  end

  defp held_by_other_scope do
    %{
      outcome: :enforcement_blocked,
      lease_id: nil,
      conflict: %{
        blocked_outcome: :held_by_other,
        surface_id: @surface,
        blocking_lease_id: @lease_id,
        held_by_uuid: @holder_uuid,
        expires_at: "2026-07-31T21:16:03Z"
      }
    }
  end

  defp unavailable_scope do
    %{
      outcome: :enforcement_blocked,
      lease_id: nil,
      conflict: %{blocked_outcome: :service_unavailable, surface_id: @surface}
    }
  end

  defp block(tracker, scope, seconds) do
    LeaseStarvation.record_blocked(tracker, scope, at(seconds))
  end

  defp collecting_post(parent) do
    fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end
  end

  defp emit_opts(http_post, extra \\ []) do
    Keyword.merge(
      [findings_opts: [agent_id: "sentinel-test", http_post: http_post]],
      extra
    )
  end

  # ---- counter -----------------------------------------------------------

  test "record_blocked stamps the episode start on the first blocked tick and holds it steady" do
    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> block(held_by_other_scope(), 30)
      |> block(held_by_other_scope(), 60)

    assert tracker.lease_blocked_streak == 3
    assert tracker.lease_blocked_since == t0()
    assert tracker.lease_blocked_last_blocked_at == at(60)
    assert tracker.lease_blocked_outcome_counts == %{held_by_other: 3}
  end

  test "reset clears the streak, the episode start and the sticky blocker" do
    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.reset()

    assert tracker.lease_blocked_streak == 0
    assert tracker.lease_blocked_since == nil
    assert tracker.lease_blocked_last_conflict == nil
    assert tracker.lease_blocked_outcome_counts == %{}
    assert tracker.lease_blocked_last_emitted_multiple == 0
  end

  test "record_blocked adopts the surface the lease plane actually refused" do
    # ForcedReleasePoller passes no :lease_opts, so `enforce_scope/3` stamping
    # :surface_id is its only channel for naming the surface in the finding.
    tracker =
      tracker(surface_id: "resident:/placeholder")
      |> block(held_by_other_scope(), 0)

    assert tracker.lease_blocked_surface_id == @surface
  end

  # ---- escalation ladder -------------------------------------------------

  test "due_multiple is 0 below the threshold and 1 at it" do
    tracker = block(tracker(), held_by_other_scope(), 0)

    assert LeaseStarvation.due_multiple(tracker, at(0)) == 0
    assert LeaseStarvation.due_multiple(tracker, at(59)) == 0
    assert LeaseStarvation.due_multiple(tracker, at(60)) == 1
  end

  test "due_multiple escalates on power-of-two multiples and holds between them" do
    tracker = block(tracker(), held_by_other_scope(), 0)

    for {elapsed, expected} <- [
          {60, 1},
          {119, 1},
          {120, 2},
          {239, 2},
          {240, 4},
          {479, 4},
          {480, 8},
          {959, 8},
          {960, 16}
        ] do
      assert LeaseStarvation.due_multiple(tracker, at(elapsed)) == expected,
             "elapsed=#{elapsed}s should be rung #{expected}"
    end
  end

  test "due_multiple settles to a fixed 16x cadence past the backoff cap" do
    tracker = block(tracker(), held_by_other_scope(), 0)

    # ratio 16..31 -> 16, 32..47 -> 32, 48 -> 48. The floor is the point: the
    # poller ran a 7h41m episode overnight; an alert that fires once and goes
    # quiet is indistinguishable from a resolved outage.
    for {elapsed, expected} <- [
          {960, 16},
          {60 * 31, 16},
          {60 * 32, 32},
          {60 * 47, 32},
          {60 * 48, 48}
        ] do
      assert LeaseStarvation.due_multiple(tracker, at(elapsed)) == expected
    end
  end

  test "due_multiple is 0 when no episode is open" do
    assert LeaseStarvation.due_multiple(tracker(), at(10_000)) == 0
  end

  # ---- finding shape -----------------------------------------------------

  test "finding names the surface, the blocking lease id and the force-release remedy" do
    tracker = block(tracker(), held_by_other_scope(), 0)
    finding = LeaseStarvation.finding(tracker, at(720), 1)

    assert finding.type == "sentinel_lease_starved"
    # `high` is forced, not stylistic: _SENTINEL_BACKLOG_DEFAULT_SEVERITIES =
    # {"high","critical"} (src/http_api.py:1822). Anything lower is invisible to
    # the operator's default "what did I miss?" query.
    assert finding.severity == "high"
    assert finding.violation_class == "BEH"
    assert finding.extra.self_observation == true
    assert finding.extra.blocking_lease_id == @lease_id
    assert finding.extra.held_by_uuid == @holder_uuid
    assert finding.extra.surface_id == @surface

    assert finding.summary =~ @surface
    assert finding.summary =~ "LEASE-STARVED"
    assert finding.summary =~ "12m"
    assert finding.summary =~ "/v1/lease/force-release"
    assert finding.summary =~ ~s|"lease_id": "#{@lease_id}"|
    assert finding.summary =~ "immortal lease"
  end

  test "finding omits the force-release remedy when no blocking lease was ever reported" do
    # enforce_scope/3 collapses service_unavailable, permission_denied,
    # schema_invalid, client_error AND a missing LEASE_PLANE_BEARER_TOKEN into
    # :enforcement_blocked. Suggesting a force-release in those cases sends the
    # operator hunting a lease that does not exist.
    tracker = block(tracker(), unavailable_scope(), 0)
    finding = LeaseStarvation.finding(tracker, at(720), 1)

    refute finding.summary =~ "/v1/lease/force-release"
    assert finding.summary =~ "nothing to force-release"
    assert finding.summary =~ "LEASE_PLANE_BEARER_TOKEN"
    assert finding.summary =~ "service_unavailable=1"
    refute Map.has_key?(finding.extra, :blocking_lease_id)
  end

  test "a service_unavailable tick on the escalation boundary still names the episode's blocker" do
    # Live data: 62 service_unavailable ticks (Finch transport timeouts) arrive
    # in bursts of four interleaved among 1,683 held_by_other ticks, and the
    # emitter's FIRST blocked tick of the 2026-07-31 episode was one of them.
    # Rendering the remedy from whichever tick happens to land on a ladder rung
    # is a coin flip that can actively mislead — worse than saying nothing.
    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> block(held_by_other_scope(), 30)
      |> block(unavailable_scope(), 60)

    finding = LeaseStarvation.finding(tracker, at(60), 1)

    assert finding.extra.blocking_lease_id == @lease_id
    assert finding.summary =~ "force-release"
    assert finding.summary =~ "held_by_other=2"
    assert finding.summary =~ "service_unavailable=1"
  end

  test "change_token is stable within a rung and distinct across rungs and episodes" do
    tracker = block(tracker(), held_by_other_scope(), 0)

    rung_1 = LeaseStarvation.finding(tracker, at(60), 1).change_token
    rung_1_again = LeaseStarvation.finding(tracker, at(90), 1).change_token
    rung_2 = LeaseStarvation.finding(tracker, at(120), 2).change_token

    assert rung_1 == rung_1_again
    assert rung_1 != rung_2

    # A later, unrelated episode must never be permanently suppressed by an
    # earlier one: reset/1 nils the episode start, so the new episode's rung-1
    # token cannot collide with any prior episode's.
    next_episode =
      tracker
      |> LeaseStarvation.reset()
      |> block(held_by_other_scope(), 100_000)

    assert LeaseStarvation.finding(next_episode, at(100_060), 1).change_token != rung_1
  end

  test "findings fingerprint per lease surface even when agent_id is identical" do
    # The emitter carries the anchor agent_uuid; the poller, started as a bare
    # module atom, falls back to the "sentinel" literal. Without a
    # surface-keyed fingerprint the two residents' outages can dedup into one.
    poller = block(tracker(), held_by_other_scope(), 0)

    emitter =
      tracker(resident: "FleetFindingEmitter", surface_id: "resident:/sentinel_fleet_emit")
      |> block(
        %{
          outcome: :enforcement_blocked,
          lease_id: nil,
          conflict: %{
            blocked_outcome: :held_by_other,
            surface_id: "resident:/sentinel_fleet_emit"
          }
        },
        0
      )

    poller_body =
      Findings.finding_body(LeaseStarvation.finding(poller, at(60), 1), agent_id: "same-agent")

    emitter_body =
      Findings.finding_body(LeaseStarvation.finding(emitter, at(60), 1), agent_id: "same-agent")

    assert poller_body["fingerprint"] != emitter_body["fingerprint"]
    assert poller_body["type"] == "sentinel_finding"
    assert poller_body["finding_type"] == "sentinel_lease_starved"
    assert is_binary(poller_body["change_token"])
  end

  # ---- emission ----------------------------------------------------------

  test "maybe_emit posts nothing below the threshold and one finding at it" do
    parent = self()
    opts = emit_opts(collecting_post(parent))

    tracker = block(tracker(), held_by_other_scope(), 0)

    tracker = LeaseStarvation.maybe_emit(tracker, Keyword.put(opts, :now, at(59)))
    refute_receive {:finding_posted, _}, 50
    assert tracker.lease_blocked_last_emitted_multiple == 0

    tracker = LeaseStarvation.maybe_emit(tracker, Keyword.put(opts, :now, at(60)))
    assert_receive {:finding_posted, body}
    assert body["finding_type"] == "sentinel_lease_starved"
    assert tracker.lease_blocked_last_emitted_multiple == 1

    # Same rung: silent.
    LeaseStarvation.maybe_emit(tracker, Keyword.put(opts, :now, at(90)))
    refute_receive {:finding_posted, _}, 50
  end

  test "maybe_emit respects an operator who turned findings off" do
    parent = self()

    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(
        emit_opts(collecting_post(parent), now: at(720), emit_findings?: false)
      )

    refute_receive {:finding_posted, _}, 50
    assert tracker.lease_blocked_last_emitted_multiple == 0
  end

  test "a lost findings POST does not burn its rung — the next tick retries it" do
    # gov-MCP being unreachable (jetsam 502 window) is CORRELATED with residents
    # starving, so a fire-and-forget emitter would drop exactly the dense early
    # alerts that carry the value.
    parent = self()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :post_attempted)
      {:error, :econnrefused}
    end

    tracker = block(tracker(), held_by_other_scope(), 0)

    tracker =
      LeaseStarvation.maybe_emit(tracker, emit_opts(failing_post, now: at(60)))

    assert_receive :post_attempted
    assert tracker.lease_blocked_last_emitted_multiple == 0

    tracker =
      LeaseStarvation.maybe_emit(tracker, emit_opts(collecting_post(parent), now: at(90)))

    assert_receive {:finding_posted, body}
    assert body["finding_type"] == "sentinel_lease_starved"
    assert tracker.lease_blocked_last_emitted_multiple == 1
  end

  test "a deduped findings POST counts as delivered and is not retried" do
    # Without this the retry-until-delivered rule becomes a storm against a
    # server that will keep answering deduped: true.
    parent = self()

    deduped_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :post_attempted)
      {:ok, 200, ~s({"success":true,"deduped":true})}
    end

    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(emit_opts(deduped_post, now: at(60)))

    assert_receive :post_attempted
    assert tracker.lease_blocked_last_emitted_multiple == 1

    LeaseStarvation.maybe_emit(tracker, emit_opts(deduped_post, now: at(90)))
    refute_receive :post_attempted, 50
  end

  # ---- closure -----------------------------------------------------------

  test "clear emits an info closure finding when the episode had alerted" do
    parent = self()
    opts = emit_opts(collecting_post(parent))

    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(Keyword.put(opts, :now, at(60)))

    assert_receive {:finding_posted, _starved}

    tracker = LeaseStarvation.clear(tracker, Keyword.put(opts, :now, at(120)))

    assert_receive {:finding_posted, cleared}
    assert cleared["finding_type"] == "sentinel_lease_starvation_cleared"
    assert cleared["severity"] == "info"
    assert cleared["message"] =~ "CLEARED"
    assert cleared["message"] =~ @surface
    assert String.ends_with?(cleared["change_token"], "|cleared")

    assert tracker.lease_blocked_since == nil
    assert tracker.lease_blocked_streak == 0
  end

  test "clear is silent for an episode that never reached the alert threshold" do
    parent = self()
    opts = emit_opts(collecting_post(parent))

    tracker =
      tracker()
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.clear(Keyword.put(opts, :now, at(30)))

    refute_receive {:finding_posted, _}, 50
    assert tracker.lease_blocked_since == nil
  end

  test "clear on a tracker with no open episode does nothing at all" do
    parent = self()
    tracker = tracker()

    assert LeaseStarvation.clear(tracker, emit_opts(collecting_post(parent), now: at(0))) ==
             tracker

    refute_receive {:finding_posted, _}, 50
  end

  # ---- restart survival --------------------------------------------------

  test "a persisted episode is resumed after a restart inside the resume window" do
    # KeepAlive is true with a 30s ThrottleInterval and the poller's first tick
    # lands 1s after boot. In-memory-only state means a crash loop stays silent
    # forever while fully dark, and an operator running `launchctl kickstart -k`
    # because "sentinel looks stuck" buys another threshold of silence at
    # exactly the moment someone is looking.
    path = tmp_state_path()

    tracker(state_path: path)
    |> block(held_by_other_scope(), 0)
    |> LeaseStarvation.maybe_emit(
      emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
    )

    # Restart gap measured from the last blocked tick (at(0)), well inside the
    # 60s threshold this tracker was built with.
    resumed = tracker(state_path: path, now: at(45))

    assert resumed.lease_blocked_since == t0()
    assert resumed.lease_blocked_last_emitted_multiple == 1
    # The streak is per-process and honestly restarts at zero; the episode
    # duration, which is what the ladder trips on, does not.
    assert resumed.lease_blocked_streak == 0
    assert LeaseStarvation.due_multiple(resumed, at(120)) == 2
  end

  test "a stale persisted episode is discarded rather than claiming a phantom outage" do
    path = tmp_state_path()

    tracker(state_path: path) |> block(held_by_other_scope(), 0)

    # Gap longer than the alert threshold: a fresh episode would reach the
    # threshold in the same time anyway, and resuming would make the very first
    # blocked tick claim an outage that never happened.
    resumed = tracker(state_path: path, now: at(10_000))

    assert resumed.lease_blocked_since == nil
    assert resumed.lease_blocked_last_emitted_multiple == 0
  end

  test "clear discards the persisted episode" do
    path = tmp_state_path()

    tracker(state_path: path)
    |> block(held_by_other_scope(), 0)
    |> LeaseStarvation.clear(now: at(30), emit_findings?: false)

    refute File.exists?(path)
  end

  defp tmp_state_path do
    dir =
      Path.join(
        System.tmp_dir!(),
        "unitares_sentinel_starvation_#{System.unique_integer([:positive])}"
      )

    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)
    Path.join(dir, "episode.json")
  end
end
