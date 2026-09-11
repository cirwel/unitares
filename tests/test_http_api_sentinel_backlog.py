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
import src.http_api as http_api
from agents.sentinel.forced_release_alarm import ForcedReleaseAlarm
from src.http_api import http_sentinel_backlog


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/v1/sentinel/backlog", http_sentinel_backlog, methods=["GET"])])
    return TestClient(app, client=("127.0.0.1", 50000))


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


def test_groups_repeated_fingerprint_as_one_incident_with_timeline(client, monkeypatch):
    newest = _audit_row(
        "high", "sentinel_lease_starved", "BEH", "2026-08-01T02:00:00+00:00"
    )
    oldest = _audit_row(
        "high", "sentinel_lease_starved", "BEH", "2026-08-01T01:00:00+00:00"
    )
    for event_id, row, rung in ((102, newest, 2), (101, oldest, 1)):
        row["event_id"] = event_id
        row["details"].update(
            {
                "fingerprint": "lease-episode-fp",
                "change_token": f"episode|{rung}",
                "escalation_multiple": rung,
                "blocked_seconds": rung * 900,
                "surface_id": "resident:/sentinel_cycle",
            }
        )
    _patch_query(monkeypatch, [newest, oldest])

    body = client.get("/v1/sentinel/backlog").json()

    assert body["count"] == 2
    assert body["unresolved_count"] == 2
    assert body["incident_count"] == 1
    incident = body["incidents"][0]
    assert incident["fingerprint"] == "lease-episode-fp"
    assert incident["occurrence_count"] == 2
    assert incident["first_seen"] == "2026-08-01T01:00:00+00:00"
    assert incident["last_seen"] == "2026-08-01T02:00:00+00:00"
    assert [entry["change_token"] for entry in incident["timeline"]] == [
        "episode|2",
        "episode|1",
    ]
    assert [entry["escalation_multiple"] for entry in incident["timeline"]] == [2, 1]
    assert [entry["blocked_seconds"] for entry in incident["timeline"]] == [1800, 900]


def test_forced_release_receipts_are_separate_from_unresolved_findings(client, monkeypatch):
    lease_1 = "17546f52-370d-4274-9d5b-0c233f09590c"
    lease_0 = "a2d89680-0f3a-48e5-b51a-50684f70bfd9"
    event_1 = "5bb82bc0-5692-420b-a52a-c16b3efe0188"
    event_0 = "6c22eb79-25ed-489c-9664-fd7f04d0e8d6"
    explicit = _audit_row("high", "ad_hoc", "BEH")
    explicit["details"].update(
        {
            "message": f"forced release: resident:/sentinel_cycle (lease {lease_1})",
            "fingerprint": f"forced_release:ad_hoc:{event_1}",
            "event_id": event_1,
            "lease_id": lease_1,
            "surface_id": "resident:/sentinel_cycle",
            "record_kind": "action_receipt",
            "requires_adjudication": False,
        }
    )
    legacy = _audit_row("high", "ad_hoc", "BEH", "2026-06-16T15:00:00+00:00")
    legacy["event_id"] = 41
    legacy["details"].update(
        {
            "message": f"forced release: resident:/sentinel_cycle (lease {lease_0})",
            "fingerprint": f"forced_release:ad_hoc:{event_0}",
            "event_id": event_0,
            "lease_id": lease_0,
            "surface_id": "resident:/sentinel_cycle",
        }
    )
    _patch_query(monkeypatch, [explicit, legacy])

    async def _lease_rows(_lease_ids):
        return {
            lease_id: {
                "lease_id": lease_id,
                "surface_id": "resident:/sentinel_cycle",
                "release_reason": "forced",
                "original_ttl_s": 300,
                "held_s": 600,
                "holder_pid_null": True,
            }
            for lease_id in (lease_0, lease_1)
        }

    monkeypatch.setattr(http_api, "_fetch_lease_rows", _lease_rows)

    async def _authority(_event_ids, _deprecation_ids):
        return {
            "forced_events": {
                event_1: {
                    **_lease_rows_for_event(event_1, lease_1),
                },
                event_0: {
                    **_lease_rows_for_event(event_0, lease_0),
                },
            },
            "deprecations": {},
        }

    monkeypatch.setattr(
        http_api, "_fetch_sentinel_receipt_authority", _authority, raising=False
    )

    body = client.get("/v1/sentinel/backlog").json()

    # Legacy findings/count remain a raw-record compatibility view.
    assert body["count"] == 2
    assert len(body["findings"]) == 2
    assert body["unresolved_count"] == 0
    assert body["incident_count"] == 0
    assert body["receipt_count"] == 2
    assert {item["record_kind"] for item in body["action_receipts"]} == {
        "action_receipt"
    }


def _lease_rows_for_event(event_id, lease_id):
    return {
        "event_id": event_id,
        "event_type": "forced",
        "event_lease_id": lease_id,
        "event_surface_id": "resident:/sentinel_cycle",
        "lease_id": lease_id,
        "lease_surface_id": "resident:/sentinel_cycle",
        "release_reason": "forced",
        "original_ttl_s": 300,
        "held_s": 600,
        "holder_pid_null": True,
    }


def test_historical_lease_row_without_matching_event_cannot_suppress_adjudication(
    client, monkeypatch
):
    event_id = "5bb82bc0-5692-420b-a52a-c16b3efe0188"
    lease_id = "17546f52-370d-4274-9d5b-0c233f09590c"
    spoofed = _audit_row("high", "ad_hoc", "BEH")
    spoofed["details"].update(
        {
            "message": f"forced release: resident:/sentinel_cycle (lease {lease_id})",
            "fingerprint": f"forced_release:ad_hoc:{event_id}",
            "event_id": event_id,
            "lease_id": lease_id,
            "surface_id": "resident:/sentinel_cycle",
            "record_kind": "action_receipt",
            "requires_adjudication": False,
        }
    )
    _patch_query(monkeypatch, [spoofed])

    async def _lease_rows(_lease_ids):
        return {lease_id: {**_lease_rows_for_event(event_id, lease_id), "surface_id": "resident:/sentinel_cycle"}}

    async def _no_matching_event(_event_ids, _deprecation_ids):
        return {"forced_events": {}, "deprecations": {}}

    monkeypatch.setattr(http_api, "_fetch_lease_rows", _lease_rows)
    monkeypatch.setattr(
        http_api,
        "_fetch_sentinel_receipt_authority",
        _no_matching_event,
        raising=False,
    )

    body = client.get("/v1/sentinel/backlog").json()

    assert body["unresolved_count"] == 1
    assert body["receipt_count"] == 0


def test_completed_deprecation_sweep_is_an_authoritatively_verified_receipt(
    client, monkeypatch
):
    deprecation_id = "a609cbba-3a7d-47fb-b8d6-43cab7423ea4"
    row = _audit_row("medium", "deprecation_batch", "BEH")
    row["details"].update(
        {
            "message": "deprecation sweep complete: kind=file count=3",
            "fingerprint": f"forced_release:deprecation_batch:{deprecation_id}",
            "deprecation_id": deprecation_id,
            "kind": "file",
            "count": 3,
            "record_kind": "action_receipt",
            "requires_adjudication": False,
        }
    )
    _patch_query(monkeypatch, [row])

    async def _authority(_event_ids, _deprecation_ids):
        return {
            "forced_events": {},
            "deprecations": {
                deprecation_id: {
                    "deprecation_id": deprecation_id,
                    "surface_kind": "file",
                    "sweep_completed_at": "2026-08-01T00:00:00+00:00",
                    "event_count": 3,
                }
            },
        }

    monkeypatch.setattr(
        http_api, "_fetch_sentinel_receipt_authority", _authority, raising=False
    )

    body = client.get("/v1/sentinel/backlog?severity=all").json()

    assert body["unresolved_count"] == 0
    assert body["receipt_count"] == 1


def test_sender_cannot_opt_an_arbitrary_finding_out_of_adjudication(client, monkeypatch):
    spoofed = _audit_row("high", "coordinated_degradation", "CON")
    spoofed["details"].update(
        {"record_kind": "action_receipt", "requires_adjudication": False}
    )
    _patch_query(monkeypatch, [spoofed])

    body = client.get("/v1/sentinel/backlog").json()

    assert body["unresolved_count"] == 1
    assert body["receipt_count"] == 0
    assert body["unresolved_findings"][0]["requires_adjudication"] is True


def test_limit_is_capped_after_severity_filter(client, monkeypatch):
    # 5 high rows; limit=2 must yield exactly 2 after filtering.
    rows = [_audit_row("high") for _ in range(5)]
    captured = _patch_query(monkeypatch, rows)

    r = client.get("/v1/sentinel/backlog?limit=2")
    body = r.json()
    assert body["count"] == 2
    assert body["unresolved_count"] == 5
    assert body["results_truncated"] is True
    # Over-fetches beyond the requested limit so the post-filter cap can fill.
    assert captured["limit"] >= 2


def test_scan_cap_marks_incident_timeline_incomplete(client, monkeypatch):
    rows = [_audit_row("high") for _ in range(4)]
    _patch_query(monkeypatch, rows)
    monkeypatch.setattr(http_api, "_SENTINEL_EVENT_SCAN_LIMIT", 3)

    body = client.get("/v1/sentinel/backlog?limit=1").json()

    assert body["scan_truncated"] is True
    assert body["incidents"][0]["timeline_complete"] is False


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


def test_forced_release_alarm_fifth_positional_argument_remains_extra():
    extra = {"surface_id": "resident:/sentinel_cycle"}

    alarm = ForcedReleaseAlarm("conflict_batch", "medium", "summary", "fp", extra)

    assert alarm.extra == extra
    assert alarm.record_kind == "finding"
    assert alarm.requires_adjudication is True
