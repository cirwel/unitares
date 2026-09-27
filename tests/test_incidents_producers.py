"""/api/incidents says how each incident type is produced.

anomaly_detected is written only when a caller runs detect_anomalies. The
dashboard's Anomalies card did that on every refresh until #2494 removed it,
after which the feed can go empty and read as a quiet fleet. The response now
names each producer's cadence and newest row, so an empty list is readable.
"""

import json

import pytest
from starlette.requests import Request


def _request(query=b""):
    return Request({
        "type": "http", "method": "GET", "path": "/api/incidents",
        "headers": [], "query_string": query, "client": ("127.0.0.1", 50000),
    })


@pytest.fixture
def audit(monkeypatch):
    rows = {
        "anomaly_detected": [],
        "stuck_detected": [{"timestamp": "2026-09-24T13:04:15+00:00", "event_type": "stuck_detected"}],
    }

    async def fake_query(event_type=None, order="desc", limit=200):
        return list(rows.get(event_type, []))

    import src.audit_db as audit_db
    monkeypatch.setattr(audit_db, "query_audit_events_async", fake_query)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    return rows


@pytest.mark.asyncio
async def test_an_empty_anomaly_feed_is_undetermined_not_quiet(audit):
    """Review round 1 on #2535: a run that found nothing and no run at all
    leave the same empty feed, so the response must not pick one."""
    from src.http_routes.overview import http_incidents

    body = json.loads((await http_incidents(_request())).body)

    anomalies = body["producers"]["anomaly_detected"]
    assert anomalies["cadence"] == "on_demand"
    assert anomalies["newest_at"] is None
    # Review rounds 1-2 on #2535: never a list of causes; any list implies
    # the unlisted states (a failed run, a dropped audit write) were ruled out.
    assert anomalies["absence_means"].startswith("undetermined")
    assert stuck_absence(body).startswith("undetermined")
    stuck = body["producers"]["stuck_detected"]
    assert stuck["cadence"] == "scheduled"
    assert stuck["newest_at"] == "2026-09-24T13:04:15+00:00"
    assert body["count"] == 1


def stuck_absence(body):
    return body["producers"]["stuck_detected"]["absence_means"]


@pytest.mark.asyncio
async def test_a_single_type_query_describes_only_that_type(audit):
    from src.http_routes.overview import http_incidents

    body = json.loads((await http_incidents(_request(b"type=anomaly_detected"))).body)

    assert set(body["producers"]) == {"anomaly_detected"}
