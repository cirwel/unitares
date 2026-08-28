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
    return TestClient(app)


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
        "timestamp": "2026-08-28T00:00:00+00:00",
        "eisv": {"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.1},
        "coherence": 0.48,
        "risk": 0.31,
        "eisv_telemetry": {
            "measurement_source": "behavioral",
            "behavioral_confidence": 0.8,
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
    for key in ("type", "timestamp", "agent_id", "eisv", "coherence", "risk"):
        assert key in event, key
    assert event["eisv"] == {"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.1}
    assert event["risk"] == 0.31
    # The measurement-lane view reads these two and nothing else off them.
    assert event["eisv_telemetry"]["measurement_source"] == "behavioral"
    assert event["eisv_telemetry"]["behavioral_confidence"] == 0.8
    assert event["metrics"] == {"primary_eisv_source": "behavioral"}


def test_compact_drops_the_payload_nothing_reads():
    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append(_fat_event())

    event = _client().get("/v1/eisv/recent?fields=compact").json()["events"][0]
    for key in ("decision", "drift_trends", "inputs", "risk_reason"):
        assert key not in event, f"{key} has zero consumers and must not be polled"
    # Telemetry is whitelisted, so a large diagnostic sub-object goes too.
    assert "derivation" not in event["eisv_telemetry"]
    # And the projection must actually be smaller, not merely reshaped.
    import json
    assert len(json.dumps(event)) < len(json.dumps(_fat_event())) / 3


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
