"""Tests for GET /v1/eisv/recent — backfill endpoint for dashboard chart."""

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_eisv_recent
from src import http_api


def _make_event(agent_id, e_val, ts="2026-04-22T00:00:00+00:00"):
    return {
        "type": "eisv_update",
        "agent_id": agent_id,
        "agent_name": agent_id,
        "timestamp": ts,
        "eisv": {"E": e_val, "I": 0.5, "S": 0.1, "V": 0.0},
        "coherence": 0.5,
    }


def _client():
    app = Starlette(routes=[Route("/v1/eisv/recent", http_eisv_recent, methods=["GET"])])
    # Loopback peer: the route is auth-gated, and the local posture's
    # trusted-network bypass is what lets these shape tests exercise the
    # handler rather than the gate. Same construction as the already-gated
    # /v1/runtime/activity tests. Gate behavior itself is pinned in
    # tests/test_http_route_auth_parity.py.
    return TestClient(app, client=("127.0.0.1", 50000))


def test_returns_eisv_events_in_chronological_order():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_make_event("a", 0.1))
    http_api.broadcaster_instance.event_history.append(_make_event("b", 0.2))
    http_api.broadcaster_instance.event_history.append(_make_event("c", 0.3))

    r = _client().get("/v1/eisv/recent")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "eisv_recent"
    assert body["count"] == 3
    assert [e["agent_id"] for e in body["events"]] == ["a", "b", "c"]


def test_filters_out_non_eisv_events():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_make_event("a", 0.1))
    http_api.broadcaster_instance.event_history.append({"type": "lifecycle_paused", "agent_id": "x"})
    http_api.broadcaster_instance.event_history.append(_make_event("b", 0.2))

    body = _client().get("/v1/eisv/recent").json()
    assert body["count"] == 2
    assert [e["agent_id"] for e in body["events"]] == ["a", "b"]


def test_limit_parameter_is_honored_and_clamped():
    http_api.broadcaster_instance.event_history.clear()
    for i in range(10):
        http_api.broadcaster_instance.event_history.append(_make_event(f"a{i}", i / 10))

    body = _client().get("/v1/eisv/recent?limit=3").json()
    assert body["count"] == 3
    # Most recent three, in order
    assert [e["agent_id"] for e in body["events"]] == ["a7", "a8", "a9"]


def test_limit_is_clamped_to_max():
    http_api.broadcaster_instance.event_history.clear()
    body = _client().get("/v1/eisv/recent?limit=999999").json()
    # Clamped internally; with an empty buffer just returns []
    assert body["count"] == 0
    assert body["events"] == []


def test_invalid_limit_falls_back_to_default():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_make_event("a", 0.1))
    body = _client().get("/v1/eisv/recent?limit=abc").json()
    assert body["count"] == 1


# --- fields=compact projection ------------------------------------------------
# The dashboard polls this endpoint every 10 seconds and reads only
# eisv/coherence/risk/timestamp plus the measurement-source tag. The full event
# measured ~6.3 KB live on 2026-08-28 (decision ~1.9 KB, eisv_telemetry ~1.9 KB,
# metrics ~1.3 KB), so a tick moved ~194 KB to draw twenty points. Compact is
# opt-in: the default shape must stay byte-identical for WebSocket clients.

def _fat_event(agent_id="a"):
    return {
        "type": "eisv_update",
        "agent_id": agent_id,
        "agent_name": "agent-" + agent_id,
        "timestamp": "2026-08-28T00:00:00+00:00",
        "eisv": {"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.1},
        "coherence": 0.48,
        "risk": 0.31,
        "eisv_telemetry": {
            "measurement_source": "behavioral",
            "behavioral_confidence": 0.8,
            "afferents_recorded": True,
            "afferent_source": "physical",
            "afferent_source_field": "body_anima",
            "afferent_count": 4,
            "afferent_valid_count": 4,
            "afferent_truncated": False,
            "afferent_keys": ["clarity", "presence", "stability", "warmth"],
            "afferent_policy_applied": False,
            "missing_inputs": [],
            "enforcement_requested": False,
            "enforcement_applied": False,
            "derivation": {"kind": "noise", "steps": ["x"] * 50},
        },
        "metrics": {"primary_eisv_source": "behavioral", "E": 0.6, "lambda1": 0.2},
        "decision": {"action": "guide", "reason": "x" * 400},
        "drift_trends": {"E": [0.1] * 20},
        "inputs": {"complexity": 0.5},
        "risk_reason": "some prose",
    }


def test_default_shape_is_unchanged_for_existing_consumers():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_fat_event())

    body = _client().get("/v1/eisv/recent").json()
    assert body["fields"] == "full"
    assert body["events"][0] == _fat_event()


def test_compact_keeps_every_field_the_chart_reads():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_fat_event())

    body = _client().get("/v1/eisv/recent?fields=compact").json()
    assert body["fields"] == "compact"
    event = body["events"][0]
    for key in ("type", "timestamp", "agent_id", "agent_name", "eisv", "coherence", "risk"):
        assert key in event, key
    assert event["eisv"] == {"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.1}
    assert event["risk"] == 0.31
    # The measurement-lane view reads these two and nothing else off them.
    assert event["eisv_telemetry"]["measurement_source"] == "behavioral"
    assert event["eisv_telemetry"]["behavioral_confidence"] == 0.8
    assert event["eisv_telemetry"]["afferent_count"] == 4
    assert event["eisv_telemetry"]["afferent_valid_count"] == 4
    assert event["eisv_telemetry"]["afferent_truncated"] is False
    assert event["eisv_telemetry"]["afferent_keys"] == [
        "clarity",
        "presence",
        "stability",
        "warmth",
    ]
    assert event["eisv_telemetry"]["afferent_policy_applied"] is False
    assert event["metrics"] == {"primary_eisv_source": "behavioral"}


def test_compact_drops_the_payload_nothing_reads():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_fat_event())

    event = _client().get("/v1/eisv/recent?fields=compact").json()["events"][0]
    for key in ("drift_trends", "inputs", "risk_reason"):
        assert key not in event, f"{key} has zero consumers and must not be polled"
    # The verdict is read (the Overview's recent-check-ins feed); the ~1.9 KB
    # of reasoning around it is not.
    assert event["decision"] == {"action": "guide"}
    # Telemetry is whitelisted, so a large diagnostic sub-object goes too.
    assert "derivation" not in event["eisv_telemetry"]
    # And the projection must actually be smaller, not merely reshaped.
    import json
    assert len(json.dumps(event)) < len(json.dumps(_fat_event())) / 2


def test_compact_is_a_strict_subset_so_one_parser_handles_both():
    """A compact event must never carry a key the full event lacks, and must
    omit absent keys rather than emitting nulls — otherwise a client would
    need two code paths and `if (e.risk)` would behave differently."""
    http_api.broadcaster_instance.event_history.clear()
    lean = {"type": "eisv_update", "timestamp": "2026-08-28T00:00:00+00:00",
            "eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.4}}
    http_api.broadcaster_instance.event_history.append(dict(lean))

    event = _client().get("/v1/eisv/recent?fields=compact").json()["events"][0]
    assert set(event).issubset(set(lean))
    assert "risk" not in event and "coherence" not in event


def test_unknown_fields_value_falls_back_to_the_full_shape():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_fat_event())

    body = _client().get("/v1/eisv/recent?fields=nonsense").json()
    assert body["fields"] == "full"
    assert "decision" in body["events"][0]


# --- /api/activity coverage ---------------------------------------------------

def test_activity_coverage_starts_at_process_start_inside_the_window():
    import time
    from src.broadcaster import EISVBroadcaster

    b = EISVBroadcaster()
    b.started_at = time.time() - 600  # restarted ten minutes ago
    assert abs(b.activity_coverage_start(60) - b.started_at) < 1


def test_activity_coverage_is_the_window_when_history_is_older():
    import time
    from src.broadcaster import EISVBroadcaster

    b = EISVBroadcaster()
    b.started_at = time.time() - 7200
    assert abs(b.activity_coverage_start(60) - (time.time() - 3600)) < 1


def test_activity_coverage_moves_up_when_the_ring_is_full():
    import time
    from src.broadcaster import EISVBroadcaster

    b = EISVBroadcaster()
    b.started_at = time.time() - 7200
    oldest = time.time() - 300
    for i in range(b.activity_history.maxlen):
        b.activity_history.append((oldest + i * 0.1, "proceed"))
    assert abs(b.activity_coverage_start(60) - oldest) < 1


# --- /v1/eisv/agents: one row per agent, folded server-side ------------------

def _agents_client():
    from src.http_api import http_eisv_agents
    app = Starlette(routes=[Route("/v1/eisv/agents", http_eisv_agents, methods=["GET"])])
    return TestClient(app, client=("127.0.0.1", 50000))


def test_agents_returns_latest_event_per_agent_newest_first():
    h = http_api.broadcaster_instance.event_history
    h.clear()
    h.append(_make_event("a", 0.1, ts="2026-09-27T00:00:01+00:00"))
    h.append(_make_event("b", 0.2, ts="2026-09-27T00:00:02+00:00"))
    h.append({"type": "lifecycle_paused", "agent_id": "a"})
    h.append(dict(_fat_event("a"), timestamp="2026-09-27T00:00:03+00:00"))

    body = _agents_client().get("/v1/eisv/agents").json()
    assert [r["agent_id"] for r in body["agents"]] == ["a", "b"]
    a = body["agents"][0]
    assert a["checkins"] == 2 and a["agent_name"] == "agent-a"
    assert a["decision"] == {"action": "guide"}  # compact projection, not the full event
    assert "drift_trends" not in a
    assert isinstance(body["coverage_start"], float)


def test_agents_is_empty_not_an_error_on_a_fresh_server():
    http_api.broadcaster_instance.event_history.clear()
    body = _agents_client().get("/v1/eisv/agents").json()
    assert body["count"] == 0 and body["agents"] == []


# --- verdict = sub_action when present (review #2502) ------------------------

def test_guided_checkin_counts_as_guide_not_proceed():
    """A guided check-in is decision.action="proceed", sub_action="guide" — the
    rule record_agent_state persists by. Reading `action` alone made the
    Overview's guide count zero while the fleet produced thousands."""
    import asyncio
    from src.broadcaster import EISVBroadcaster

    b = EISVBroadcaster()
    for decision in ({"action": "proceed", "sub_action": "guide"}, {"action": "proceed"},
                     {"action": "proceed", "sub_action": "risk_pause"}, {"action": "approve"}):
        asyncio.run(b.broadcast({"type": "eisv_update", "decision": decision}))
    totals = {"proceed": 0, "guide": 0, "pause": 0}
    for bucket in b.get_activity_buckets(60, 5):
        for k in totals:
            totals[k] += bucket[k]
    assert totals == {"proceed": 2, "guide": 1, "pause": 1}


def test_compact_keeps_sub_action_with_action():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(
        dict(_fat_event(), decision={"action": "proceed", "sub_action": "guide", "reason": "x" * 400}))
    event = _client().get("/v1/eisv/recent?fields=compact").json()["events"][0]
    assert event["decision"] == {"action": "proceed", "sub_action": "guide"}


def test_activity_totals_cover_the_whole_window_not_just_aligned_buckets():
    """Buckets start at an aligned boundary up to one bucket inside the window;
    a check-in 58 minutes ago is in the hour but may sit before the first
    bucket. The exact totals must still count it."""
    import time
    from src.broadcaster import EISVBroadcaster

    b = EISVBroadcaster()
    b.activity_history.append((time.time() - 58 * 60, "proceed"))
    b.activity_history.append((time.time() - 61 * 60, "proceed"))  # outside
    b.activity_history.append((time.time() - 60, "risk_pause"))
    assert b.activity_totals(60) == {"proceed": 1, "guide": 0, "pause": 1}
