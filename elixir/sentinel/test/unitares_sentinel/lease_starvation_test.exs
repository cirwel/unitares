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
    # {"high","critical"} (src/http_api.py:1824). Anything lower is invisible to
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

  test "a resumed episode carries its sticky blocker, so the remedy survives a restart" do
    # The sidecar used to persist the clock and the ladder but NOT the blocker.
    # A resident restarted mid-episode therefore resumed with last_conflict: nil
    # and lost the one fact the remedy sentence is built from.
    path = tmp_state_path()

    tracker(state_path: path)
    |> block(held_by_other_scope(), 0)
    |> block(unavailable_scope(), 30)

    resumed = tracker(state_path: path, now: at(45))

    assert resumed.lease_blocked_last_conflict[:blocking_lease_id] == @lease_id
    assert resumed.lease_blocked_last_conflict[:held_by_uuid] == @holder_uuid

    finding =
      resumed
      |> block(unavailable_scope(), 50)
      |> LeaseStarvation.finding(at(60), 1)

    assert finding.extra.blocking_lease_id == @lease_id
    assert finding.summary =~ "/v1/lease/force-release"
  end

  test "a resumed episode with no persisted blocker never claims the plane named none" do
    # The safety net for the case persistence cannot cover: a sidecar that is
    # absent, unreadable, or from a schema version that predates the blocker
    # (hand-written here). The resumed episode then legitimately has no blocker —
    # but "this process observed none" must NOT be rendered as "the lease plane
    # reported NO blocking lease at any point in this episode, so there is
    # nothing to force-release", which reads as license to stop looking while an
    # immortal lease is still holding the surface. This fires in exactly the
    # scenario the persistence exists for: first blocked tick after the restart
    # is one of the service_unavailable bursts, with a rung already due.
    path = tmp_state_path()

    File.write!(
      path,
      Jason.encode!(%{
        "surface_id" => @surface,
        "resident" => "ForcedReleasePoller",
        "blocked_since" => DateTime.to_iso8601(t0()),
        "last_blocked_at" => DateTime.to_iso8601(at(30)),
        "last_emitted_multiple" => 0
      })
    )

    resumed = tracker(state_path: path, now: at(45))
    assert resumed.lease_blocked_since == t0()
    assert resumed.lease_blocked_last_conflict == nil

    finding =
      resumed
      |> block(unavailable_scope(), 50)
      |> LeaseStarvation.finding(at(60), 1)

    refute finding.summary =~ "The lease plane reported NO blocking lease"
    refute finding.summary =~ "at any point in this episode"
    assert finding.summary =~ "observed by THIS PROCESS"
    assert finding.summary =~ "LEASE_PLANE_BEARER_TOKEN"
  end

  test "a sidecar written by a different surface or resident is not resumed" do
    # slug/1 collapses every non-alphanumeric run to "_", so distinct surfaces
    # can derive the SAME filename. The payload has always carried surface_id and
    # resident and nothing read them; they are the guard against putting one
    # resident's outage clock on another's ladder.
    path = tmp_state_path()

    tracker(state_path: path) |> block(held_by_other_scope(), 0)

    other_surface =
      tracker(state_path: path, surface_id: "resident:/sentinel_fleet_emit", now: at(30))

    other_resident = tracker(state_path: path, resident: "FleetFindingEmitter", now: at(30))

    assert other_surface.lease_blocked_since == nil
    assert other_resident.lease_blocked_since == nil

    # Same writer: still resumes.
    assert tracker(state_path: path, now: at(30)).lease_blocked_since == t0()
  end

  # ---- required surface --------------------------------------------------

  test "new/1 refuses to build a tracker without a real surface_id" do
    # Both the sidecar path and the finding fingerprint are keyed on the surface,
    # so a shared default silently makes two residents one writer on one file
    # whose outages dedup into each other.
    assert_raise KeyError, fn ->
      LeaseStarvation.new(resident: "ForcedReleasePoller", state_path: false)
    end

    # `Keyword.fetch!` alone would not be enough: both call sites read the
    # surface out of `:lease_opts`, where a missing key yields nil.
    assert_raise ArgumentError, fn ->
      LeaseStarvation.new(resident: "ForcedReleasePoller", surface_id: nil, state_path: false)
    end

    assert_raise ArgumentError, fn ->
      LeaseStarvation.new(resident: "ForcedReleasePoller", surface_id: "", state_path: false)
    end
  end

  # ---- closure delivery --------------------------------------------------

  test "a lost closure keeps the episode instead of destroying the only record of it" do
    # The closure POST used to be fire-and-forget, followed unconditionally by
    # discard_persisted + reset: a closure lost to a transport error left the
    # tracker with no memory that an episode had ever existed. gov-MCP's
    # jetsam-kill 502 window is *correlated* with the lease plane freeing up, so
    # the loss lands exactly when closures are emitted.
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :closure_attempted)
      {:error, :econnrefused}
    end

    tracker =
      tracker(state_path: path)
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(
        emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
      )

    tracker = LeaseStarvation.clear(tracker, emit_opts(failing_post, now: at(120)))

    assert_receive :closure_attempted
    assert File.exists?(path)
    assert tracker.lease_blocked_pending_closure.attempts == 1
    # The EPISODE is over even though the closure is not delivered: a later
    # blocked tick must start a fresh one, not resume this one.
    assert tracker.lease_blocked_since == nil

    tracker = LeaseStarvation.clear(tracker, emit_opts(collecting_post(parent), now: at(180)))

    assert_receive {:finding_posted, closure}
    assert closure["finding_type"] == "sentinel_lease_starvation_cleared"
    # Frozen when the episode ended, not stretched by the retry delay.
    assert closure["message"] =~ "after 2m dark"
    assert tracker.lease_blocked_pending_closure == nil
    refute File.exists?(path)
  end

  test "closure retries are bounded rather than looping against a down gov-MCP" do
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :closure_attempted)
      {:error, :econnrefused}
    end

    tracker =
      tracker(state_path: path)
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(
        emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
      )
      |> LeaseStarvation.clear(emit_opts(failing_post, now: at(120)))

    assert_receive :closure_attempted

    tracker =
      Enum.reduce(1..4, tracker, fn n, acc ->
        acc = LeaseStarvation.clear(acc, emit_opts(failing_post, now: at(120 + n * 30)))
        assert_receive :closure_attempted
        acc
      end)

    assert tracker.lease_blocked_pending_closure.attempts == 5

    # Next granted tick gives up: an unbounded retry against a plane that is down
    # would outlive the incident it describes.
    tracker = LeaseStarvation.clear(tracker, emit_opts(failing_post, now: at(400)))

    refute_receive :closure_attempted, 50
    assert tracker.lease_blocked_pending_closure == nil
    refute File.exists?(path)
  end

  test "a closure owed when the operator turns findings off is dropped, not retried forever" do
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :closure_attempted)
      {:error, :econnrefused}
    end

    tracker =
      tracker(state_path: path)
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(
        emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
      )
      |> LeaseStarvation.clear(emit_opts(failing_post, now: at(120)))

    assert_receive :closure_attempted

    tracker =
      LeaseStarvation.clear(
        tracker,
        emit_opts(failing_post, now: at(150), emit_findings?: false)
      )

    refute_receive :closure_attempted, 50
    assert tracker.lease_blocked_pending_closure == nil
    refute File.exists?(path)
  end

  test "a later NON-alerting episode does not destroy the closure owed by an earlier one" do
    # Executed repro of the regression the closure-retry mechanism introduced.
    # `settle_closure(tracker, nil, _)` meant "THIS episode owes nothing" and was
    # implemented as `forget_episode/1` — "NO closure is owed at all" — so a
    # single sub-threshold blip between the loss and the retry silently dropped
    # the debt with zero POSTs attempted, leaving episode 1's `high` finding open
    # in the backlog forever. The live data has 62 service_unavailable ticks
    # interleaved among 1,683 held_by_other ones, so step 3 is normal traffic.
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :closure_attempted)
      {:error, :econnrefused}
    end

    # 1. Episode 1 passes the threshold and emits a high sentinel_lease_starved.
    tracker =
      tracker(state_path: path)
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(
        emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
      )

    assert tracker.lease_blocked_last_emitted_multiple == 1

    # 2. Granted tick: the closure POST is lost to gov-MCP's 502 window.
    tracker = LeaseStarvation.clear(tracker, emit_opts(failing_post, now: at(120)))
    assert_receive :closure_attempted
    assert tracker.lease_blocked_pending_closure.attempts == 1

    # 3. ONE blocked tick — a single service_unavailable blip, far under the
    #    threshold. Episode 2 opens with last_emitted_multiple == 0.
    tracker = block(tracker, unavailable_scope(), 150)
    assert tracker.lease_blocked_since == at(150)
    assert tracker.lease_blocked_last_emitted_multiple == 0
    # The debt survives the new episode opening on top of it, on disk as well as
    # in memory — a restart here must not forgive it either.
    assert tracker.lease_blocked_pending_closure.attempts == 1
    assert File.exists?(path)
    assert Jason.decode!(File.read!(path))["state"] == "open"
    assert Jason.decode!(File.read!(path))["owed_closure"]["attempts"] == 1

    # 4. Next granted tick. Episode 2 owes nothing, but episode 1 still does.
    tracker = LeaseStarvation.clear(tracker, emit_opts(collecting_post(parent), now: at(180)))

    assert_receive {:finding_posted, closure}
    assert closure["finding_type"] == "sentinel_lease_starvation_cleared"
    # It closes EPISODE 1 — frozen at the moment episode 1 ended (at(120) - t0),
    # not stretched by the intervening episode.
    assert closure["message"] =~ "after 2m dark"
    assert closure["blocked_since"] == DateTime.to_iso8601(t0())

    assert tracker.lease_blocked_pending_closure == nil
    refute File.exists?(path)
  end

  test "a closure owed by episode 1 is still delivered when episode 2 also owes one" do
    # The supersede rule is only defensible if the older debt gets its own
    # attempt FIRST. Two closures, two POSTs, in one granted tick.
    parent = self()

    delivered_post = fn _url, body, _headers, _timeout_ms ->
      send(parent, {:finding_posted, body})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    failing_post = fn _url, _body, _headers, _timeout_ms -> {:error, :econnrefused} end

    ok_post = fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end

    tracker =
      tracker(state_path: false)
      |> block(held_by_other_scope(), 0)
      |> LeaseStarvation.maybe_emit(emit_opts(ok_post, now: at(60)))
      |> LeaseStarvation.clear(emit_opts(failing_post, now: at(120)))

    assert tracker.lease_blocked_pending_closure.attempts == 1

    # Episode 2: alerts in its own right, then clears while gov-MCP is back.
    tracker =
      tracker
      |> block(held_by_other_scope(), 200)
      |> LeaseStarvation.maybe_emit(emit_opts(ok_post, now: at(260)))
      |> LeaseStarvation.clear(emit_opts(delivered_post, now: at(300)))

    assert_receive {:finding_posted, first}
    assert_receive {:finding_posted, second}

    assert first["blocked_since"] == DateTime.to_iso8601(t0())
    assert second["blocked_since"] == DateTime.to_iso8601(at(200))
    assert tracker.lease_blocked_pending_closure == nil
  end

  test "a restart after a lost closure starts a FRESH episode instead of fabricating an outage" do
    # Executed repro. The retained sidecar used to carry no "episode ended"
    # marker, so `load_episode/5` — which only ever asked "was the last blocked
    # tick recent?" — resumed a CLOSED episode across a restart. The resumed
    # episode kept blocked_since, kept its ladder position, and the next blocked
    # tick emitted an escalation claiming a contiguous outage spanning an
    # interval during which the surface had been GRANTED. That is exactly the
    # fabricated outage the moduledoc's "Known gap" section commits to never
    # producing, and it contradicted this file's own in-memory invariant below
    # ("the EPISODE is over ... a later blocked tick must start a fresh one").
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms ->
      send(parent, :closure_attempted)
      {:error, :econnrefused}
    end

    # Blocked t=0..t=100, alerting at rung 1; granted at t=110; closure lost.
    tracker(state_path: path)
    |> block(held_by_other_scope(), 0)
    |> block(held_by_other_scope(), 50)
    |> block(held_by_other_scope(), 100)
    |> LeaseStarvation.maybe_emit(
      emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(100))
    )
    |> LeaseStarvation.clear(emit_opts(failing_post, now: at(110)))

    assert_receive :closure_attempted
    assert File.exists?(path)
    assert Jason.decode!(File.read!(path))["state"] == "closing"

    # Restart at t=130 — jetsam or `launchctl kickstart -k`, the exact trigger
    # the persistence was built for. The gap from the last BLOCKED tick (t=100)
    # is 30s, well inside the 60s threshold, so the old resume test passed and
    # the closed episode came back to life.
    resumed = tracker(state_path: path, now: at(130))

    assert resumed.lease_blocked_since == nil
    assert resumed.lease_blocked_last_emitted_multiple == 0
    # Only the debt survives, with its attempt budget intact.
    assert resumed.lease_blocked_pending_closure.attempts == 1

    # A later blocked tick opens a FRESH episode: rung 1 at one threshold, an
    # honest duration, and no rungs skipped. Resuming would have put this at
    # rung 4 (~(280+60)/60) claiming "refused for 5m" across 3 granted minutes.
    fresh = block(resumed, held_by_other_scope(), 280)

    assert fresh.lease_blocked_since == at(280)
    assert LeaseStarvation.due_multiple(fresh, at(340)) == 1

    finding = LeaseStarvation.finding(fresh, at(340), 1)
    assert finding.extra.escalation_multiple == 1
    assert finding.extra.blocked_seconds == 60
    assert finding.extra.blocked_since == DateTime.to_iso8601(at(280))
    assert finding.summary =~ "for 1m"
  end

  test "a closure owed across a restart is delivered on the next granted tick, frozen" do
    # The comment on the retained sidecar used to CLAIM this ("a resident
    # restarted mid-retry still resumes the episode and closes it on its next
    # granted tick") while the in-memory debt died with the process. What
    # actually happened was a SECOND closure with an inflated duration spanning
    # the granted interval and blocked_ticks_this_process: 0. Now the debt itself
    # is persisted, so the claim is implemented rather than asserted.
    parent = self()
    path = tmp_state_path()

    failing_post = fn _url, _body, _headers, _timeout_ms -> {:error, :econnrefused} end

    tracker(state_path: path)
    |> block(held_by_other_scope(), 0)
    |> block(held_by_other_scope(), 50)
    |> block(held_by_other_scope(), 100)
    |> LeaseStarvation.maybe_emit(
      emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(100))
    )
    |> LeaseStarvation.clear(emit_opts(failing_post, now: at(110)))

    resumed = tracker(state_path: path, now: at(130))

    resumed =
      LeaseStarvation.clear(resumed, emit_opts(collecting_post(parent), now: at(140)))

    assert_receive {:finding_posted, closure}
    assert closure["finding_type"] == "sentinel_lease_starvation_cleared"
    # 110s, the episode's real duration — NOT 140s, which would span the granted
    # interval and the restart.
    assert closure["blocked_seconds"] == 110
    assert closure["message"] =~ "after 1m dark"
    # The ticks belong to the ended episode, not to this fresh process.
    assert closure["blocked_ticks_this_process"] == 3
    assert closure["message"] =~ "3 blocked ticks"
    assert closure["lease_outcome_counts"] == %{"held_by_other" => 3}

    assert resumed.lease_blocked_pending_closure == nil
    refute File.exists?(path)
  end

  test "a closure debt whose attempt budget was already spent is not resumed" do
    # Bound 1 of 3 now survives a restart; before, crashing reset the budget.
    path = tmp_state_path()

    File.write!(
      path,
      Jason.encode!(%{
        "schema_version" => 2,
        "surface_id" => @surface,
        "resident" => "ForcedReleasePoller",
        "state" => "closing",
        "owed_closure" => %{
          "blocked_since" => DateTime.to_iso8601(t0()),
          "ended_at" => DateTime.to_iso8601(at(110)),
          "blocked_ticks" => 3,
          "outcome_counts" => %{"held_by_other" => 3},
          "attempts" => 5
        }
      })
    )

    assert tracker(state_path: path, now: at(130)).lease_blocked_pending_closure == nil
  end

  test "a closure debt for an episode that ended long ago is not posted as news" do
    # Bound 2 of 3: staleness measured against ended_at, mirroring the open
    # episode's resume window. A sidecar found after a long downtime describes a
    # recovery nobody is still waiting to hear about.
    path = tmp_state_path()

    File.write!(
      path,
      Jason.encode!(%{
        "schema_version" => 2,
        "surface_id" => @surface,
        "resident" => "ForcedReleasePoller",
        "state" => "closing",
        "owed_closure" => %{
          "blocked_since" => DateTime.to_iso8601(t0()),
          "ended_at" => DateTime.to_iso8601(at(110)),
          "blocked_ticks" => 3,
          "outcome_counts" => %{"held_by_other" => 3},
          "attempts" => 1
        }
      })
    )

    assert tracker(state_path: path, now: at(10_000)).lease_blocked_pending_closure == nil
  end

  # ---- sidecar schema compatibility --------------------------------------

  test "sidecars written by HEAD and by the pre-state format both load as open episodes" do
    # Both shapes exist on real disks and neither carries a version key. Reading
    # them as "open" is not a guess: before the explicit state existed the
    # sidecar was deleted unconditionally the moment an episode ended, so every
    # versionless file on disk IS an in-progress episode.
    head_era = tmp_state_path()

    # 154b0a6d: no schema_version, no last_conflict.
    File.write!(
      head_era,
      Jason.encode!(%{
        "surface_id" => @surface,
        "resident" => "ForcedReleasePoller",
        "blocked_since" => DateTime.to_iso8601(t0()),
        "last_blocked_at" => DateTime.to_iso8601(at(30)),
        "last_emitted_multiple" => 1
      })
    )

    from_head = tracker(state_path: head_era, now: at(45))

    assert from_head.lease_blocked_since == t0()
    assert from_head.lease_blocked_last_emitted_multiple == 1
    assert from_head.lease_blocked_last_conflict == nil
    assert from_head.lease_blocked_pending_closure == nil

    # The intermediate format: same, plus the sticky blocker, still no version.
    pre_state = tmp_state_path()

    File.write!(
      pre_state,
      Jason.encode!(%{
        "surface_id" => @surface,
        "resident" => "ForcedReleasePoller",
        "blocked_since" => DateTime.to_iso8601(t0()),
        "last_blocked_at" => DateTime.to_iso8601(at(30)),
        "last_emitted_multiple" => 2,
        "last_conflict" => %{
          "blocking_lease_id" => @lease_id,
          "held_by_uuid" => @holder_uuid
        }
      })
    )

    from_pre_state = tracker(state_path: pre_state, now: at(45))

    assert from_pre_state.lease_blocked_since == t0()
    assert from_pre_state.lease_blocked_last_emitted_multiple == 2
    assert from_pre_state.lease_blocked_last_conflict[:blocking_lease_id] == @lease_id
  end

  test "an unreadable or unknown-version sidecar degrades to a fresh tracker, never a crash" do
    # `new/1` runs inside `init/1`. Anything that raises here is a supervisor
    # restart loop, so every malformed shape has to land on "start fresh".
    for content <- [
          # A schema version this build does not know: do not interpret its
          # fields, and in particular do not assume an absent "state" means open.
          Jason.encode!(%{
            "schema_version" => 99,
            "surface_id" => @surface,
            "resident" => "ForcedReleasePoller",
            "blocked_since" => DateTime.to_iso8601(t0()),
            "last_blocked_at" => DateTime.to_iso8601(at(30)),
            "last_emitted_multiple" => 1
          }),
          # v2 with a state value that is neither open nor closing.
          Jason.encode!(%{
            "schema_version" => 2,
            "surface_id" => @surface,
            "resident" => "ForcedReleasePoller",
            "state" => "wedged"
          }),
          # v2 "closing" with a corrupt debt.
          Jason.encode!(%{
            "schema_version" => 2,
            "surface_id" => @surface,
            "resident" => "ForcedReleasePoller",
            "state" => "closing",
            "owed_closure" => %{"blocked_since" => "not-a-timestamp", "attempts" => "many"}
          }),
          # Valid JSON, wrong shape.
          Jason.encode!([1, 2, 3]),
          # Not JSON at all (truncated write, disk corruption).
          "{\"surface_id\": ",
          ""
        ] do
      path = tmp_state_path()
      File.write!(path, content)

      loaded = tracker(state_path: path, now: at(45))

      assert loaded.lease_blocked_since == nil, "should not resume from: #{inspect(content)}"
      assert loaded.lease_blocked_pending_closure == nil
      assert loaded.lease_blocked_last_emitted_multiple == 0
    end
  end

  test "the sidecar names the episode phase explicitly rather than leaving it inferred" do
    # The root cause was that "episode ended, closure owed" had no representation
    # — it was the conjunction of "file still there" and an in-memory field, and
    # those two halves disagreed in both directions. Pin the on-disk contract.
    parent = self()
    path = tmp_state_path()

    tracker = tracker(state_path: path) |> block(held_by_other_scope(), 0)

    open = Jason.decode!(File.read!(path))
    assert open["schema_version"] == 2
    assert open["state"] == "open"
    refute Map.has_key?(open, "owed_closure")

    tracker =
      LeaseStarvation.maybe_emit(
        tracker,
        emit_opts(fn _u, _b, _h, _t -> {:ok, 200, ~s({"success":true})} end, now: at(60))
      )

    LeaseStarvation.clear(
      tracker,
      emit_opts(fn _u, _b, _h, _t -> {:error, :econnrefused} end, now: at(120))
    )

    closing = Jason.decode!(File.read!(path))
    assert closing["state"] == "closing"
    # No live clock is left lying around for a reader to restart.
    refute Map.has_key?(closing, "blocked_since")
    refute Map.has_key?(closing, "last_blocked_at")
    assert closing["owed_closure"]["ended_at"] == DateTime.to_iso8601(at(120))
    assert closing["owed_closure"]["attempts"] == 1

    refute_receive {:finding_posted, _}, 50
    _ = parent
  end

  # ---- surface_id defaulting ---------------------------------------------

  test "put_default_surface_id guards an explicit nil, not merely an omitted key" do
    # `Keyword.put_new/3` keys on presence, so `[surface_id: nil]` sailed past it
    # into `Keyword.fetch!/2` at the call sites and raised inside `init/1`.
    default = "resident:/sentinel_cycle"

    assert LeaseStarvation.put_default_surface_id([], default)[:surface_id] == default

    assert LeaseStarvation.put_default_surface_id([surface_id: nil], default)[:surface_id] ==
             default

    assert LeaseStarvation.put_default_surface_id([surface_id: ""], default)[:surface_id] ==
             default

    # A real value is never overwritten.
    assert LeaseStarvation.put_default_surface_id([surface_id: "resident:/other"], default)[
             :surface_id
           ] == "resident:/other"

    # Other options survive untouched.
    opts = LeaseStarvation.put_default_surface_id([surface_id: nil, bearer_token: "t"], default)
    assert opts[:bearer_token] == "t"
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
