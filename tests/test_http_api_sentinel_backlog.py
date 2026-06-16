"""Tests for GET /v1/sentinel/backlog — durable Sentinel finding backlog.

The endpoint reads the durable audit.events store (where findings already
persist via broadcaster._persist_event), filtered to sentinel finding event
types. These tests mock the audit query so they need no live DB.
"""

import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import src.audit_db as audit_db
from src.http_api import http_sentinel_backlog


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/v1/sentinel/backlog", http_sentinel_backlog, methods=["GET"])])
    return TestClient(app)


def _audit_row(severity, finding_type="verdict_shift", vclass="ENT", ts="2026-06-16T16:00:00+00:00"):
    """Shape one audit.events row as query_audit_events_async returns it."""
    return {
        "timestamp": ts,
        "agent_id": "Sentinel",
        "event_type": "sentinel_finding",
        "confidence": 1.0,
        "event_id": 42,
        "details": {
            "severity": severity,
            "finding_type": finding_type,
            "violation_class": vclass,
            "message": f"{finding_type} fired",
            "fingerprint": f"fp-{finding_type}-{severity}",
            "agent_name": "Sentinel",
        },
    }


def _patch_query(monkeypatch, rows):
    captured = {}

    async def _fake_query(**kwargs):
        captured.update(kwargs)
        return rows

    monkeypatch.setattr(audit_db, "query_audit_events_async", _fake_query)
    return captured


def test_default_filters_to_high_and_critical(client, monkeypatch):
    rows = [
        _audit_row("high", "coordinated_degradation", "CON"),
        _audit_row("medium", "entropy_outlier", "ENT"),
        _audit_row("critical", "verdict_shift", "ENT"),
        _audit_row("info", "correlated_events", "BEH"),
    ]
    captured = _patch_query(monkeypatch, rows)

    r = client.get("/v1/sentinel/backlog")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    severities = {f["severity"] for f in body["findings"]}
    assert severities == {"high", "critical"}
    assert body["count"] == 2
    # Default queries the sentinel finding event types over a window.
    assert set(captured["event_types"]) == {"sentinel_finding", "sentinel_alarm_finding"}
    assert captured["order"] == "desc"


def test_severity_all_returns_every_severity(client, monkeypatch):
    rows = [_audit_row("high"), _audit_row("medium"), _audit_row("info")]
    _patch_query(monkeypatch, rows)

    r = client.get("/v1/sentinel/backlog?severity=all")
    body = r.json()
    assert body["count"] == 3
    assert body["severity"] == "all"


def test_severity_pin_one_value(client, monkeypatch):
    rows = [_audit_row("high"), _audit_row("medium"), _audit_row("critical")]
    _patch_query(monkeypatch, rows)

    r = client.get("/v1/sentinel/backlog?severity=medium")
    body = r.json()
    assert body["count"] == 1
    assert body["findings"][0]["severity"] == "medium"


def test_row_shaping_maps_details_fields(client, monkeypatch):
    _patch_query(monkeypatch, [_audit_row("high", "coordinated_degradation", "CON")])

    r = client.get("/v1/sentinel/backlog")
    f = r.json()["findings"][0]
    assert f["finding_type"] == "coordinated_degradation"
    assert f["violation_class"] == "CON"
    assert f["message"] == "coordinated_degradation fired"
    assert f["fingerprint"] == "fp-coordinated_degradation-high"
    assert f["agent_name"] == "Sentinel"
    assert f["agent_id"] == "Sentinel"
    assert f["event_id"] == 42


def test_limit_is_capped_after_severity_filter(client, monkeypatch):
    # 5 high rows; limit=2 must yield exactly 2 after filtering.
    rows = [_audit_row("high") for _ in range(5)]
    captured = _patch_query(monkeypatch, rows)

    r = client.get("/v1/sentinel/backlog?limit=2")
    body = r.json()
    assert body["count"] == 2
    # Over-fetches beyond the requested limit so the post-filter cap can fill.
    assert captured["limit"] >= 2


def test_alarm_kind_fallback_for_finding_type(client, monkeypatch):
    row = _audit_row("high")
    del row["details"]["finding_type"]
    row["details"]["alarm_kind"] = "forced_release"
    _patch_query(monkeypatch, [row])

    r = client.get("/v1/sentinel/backlog")
    assert r.json()["findings"][0]["finding_type"] == "forced_release"


def test_query_failure_returns_500_with_empty_list(client, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_db, "query_audit_events_async", _boom)

    r = client.get("/v1/sentinel/backlog")
    assert r.status_code == 500
    assert r.json()["findings"] == []
