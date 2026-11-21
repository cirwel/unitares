"""Tests for POST /api/findings — external finding ingestion."""

import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.event_detector import event_detector
from src.http_api import http_record_finding


@pytest.fixture(autouse=True)
def clear_events():
    event_detector.clear_events()
    event_detector._recent_fingerprints.clear()
    event_detector._change_tokens.clear()
    event_detector._event_counter = 0
    yield
    event_detector.clear_events()
    event_detector._recent_fingerprints.clear()
    event_detector._change_tokens.clear()
    event_detector._event_counter = 0


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    """Use the local trusted-network path without configuring a token."""
    for name in (
        "UNITARES_HTTP_API_TOKEN",
        "UNITARES_HTTP_AGENT_TOKENS",
        "UNITARES_MCP_BEARER_TOKENS",
        "UNITARES_REST_STRICT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/api/findings", http_record_finding, methods=["POST"])])
    return TestClient(app, client=("127.0.0.1", 50000))


def test_accepts_valid_finding(client):
    payload = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "agent_id": "sentinel-01",
        "agent_name": "Sentinel",
        "fingerprint": "abcd1234",
    }
    r = client.post("/api/findings", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["event"]["event_id"] == 1
    assert body["event"]["type"] == "sentinel_finding"
    assert body["deduped"] is False


def test_deduped_finding_returns_success_but_marked(client):
    payload = {
        "type": "sentinel_finding", "severity": "high", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "dedup-me",
    }
    r1 = client.post("/api/findings", json=payload)
    r2 = client.post("/api/findings", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["deduped"] is False
    assert r2.json()["deduped"] is True
    assert r2.json()["event"] is None


def test_rejects_missing_required_fields(client):
    r = client.post("/api/findings", json={"type": "x"})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_rejects_invalid_severity(client):
    r = client.post("/api/findings", json={
        "type": "x", "severity": "BOGUS", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "fp",
    })
    assert r.status_code == 400


def test_rejects_invalid_type_prefix(client):
    r = client.post("/api/findings", json={
        "type": "verdict_change", "severity": "info", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "fp",
    })
    assert r.status_code == 400


def test_accepted_finding_calls_broadcaster_for_persistence(client, monkeypatch):
    """Accepted findings must reach broadcaster.broadcast_event so _persist_event
    writes them to audit.events. Without this, a server restart empties the
    in-memory ring buffer and all findings are lost.
    Regression for phase-2 fix (KG discovery 2026-04-25T10:49:00.729859)."""
    from src.broadcaster import broadcaster_instance

    broadcast_calls = []

    async def _capture(event_type, agent_id=None, payload=None):
        broadcast_calls.append({"event_type": event_type, "agent_id": agent_id})

    monkeypatch.setattr(broadcaster_instance, "broadcast_event", _capture)

    payload = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "agent_id": "sentinel-01",
        "agent_name": "Sentinel",
        "fingerprint": "persist-regression-fp",
    }
    r = client.post("/api/findings", json=payload)
    assert r.status_code == 200
    assert r.json()["deduped"] is False

    assert len(broadcast_calls) == 1, "broadcast_event must fire once per accepted finding"
    assert broadcast_calls[0]["event_type"] == "sentinel_finding"
    assert broadcast_calls[0]["agent_id"] == "sentinel-01"

    # Simulate restart: clear the in-memory ring buffer
    event_detector.clear_events()
    assert event_detector._recent_events == []
    # The finding survives in audit.events because broadcast_event → _persist_event was called.


def test_deduped_finding_skips_broadcaster(client, monkeypatch):
    """Deduped findings must not trigger a redundant broadcast_event call."""
    from src.broadcaster import broadcaster_instance

    broadcast_calls = []

    async def _capture(event_type, agent_id=None, payload=None):
        broadcast_calls.append(event_type)

    monkeypatch.setattr(broadcaster_instance, "broadcast_event", _capture)

    payload = {
        "type": "sentinel_finding", "severity": "high", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "dedup-broadcast-fp",
    }
    client.post("/api/findings", json=payload)
    client.post("/api/findings", json=payload)  # deduped

    assert len(broadcast_calls) == 1  # only the first accepted posting


# ---------------------------------------------------------------------------
# Evidence at ingest (bridge-dispatch proposal §4, PR #1450)
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

FR_LEASE = "17546f52-370d-4274-9d5b-0c233f09590c"


def _fr_payload(**over):
    p = {
        "type": "sentinel_alarm_finding",
        "severity": "high",
        "message": f"forced release: resident:/sentinel_cycle (lease {FR_LEASE})",
        "agent_id": "sentinel-01",
        "agent_name": "Sentinel",
        "fingerprint": "fr-ingest-1",
        "lease_id": FR_LEASE,
        "surface_id": "resident:/sentinel_cycle",
        "ts": "2026-07-31T21:08:05Z",
    }
    p.update(over)
    return p


def _lease_row():
    return {
        "lease_id": FR_LEASE,
        "surface_id": "resident:/sentinel_cycle",
        "release_reason": "forced",
        "holder_kind": "local_beam",
        "holder_pid_null": True,
        "original_ttl_s": 300,
        "held_s": 4350.0,
    }


def test_forced_release_ingest_attaches_server_computed_evidence(client):
    with patch("src.http_routes.findings._fetch_lease_rows",
               AsyncMock(return_value={FR_LEASE: _lease_row()})):
        r = client.post("/api/findings", json=_fr_payload())
    assert r.status_code == 200
    ev = r.json()["event"]["evidence"]
    assert ev["assessment"] == "event_recorded"
    assert ev["held_x_ttl"] == 14.5
    # ingest-time latency: "now" minus the lease event ts — just assert presence
    assert ev["report_latency_s"] >= 0


def test_client_supplied_evidence_is_stripped_and_recomputed(client):
    spoofed = _fr_payload(fingerprint="fr-ingest-2",
                          evidence={"assessment": "event_recorded", "forged": True})
    with patch("src.http_routes.findings._fetch_lease_rows", AsyncMock(return_value={})):
        r = client.post("/api/findings", json=spoofed)
    ev = r.json()["event"]["evidence"]
    assert "forged" not in ev
    assert ev["assessment"] == "no_lease_row"  # server truth, not the forgery


def test_evidence_failure_never_blocks_ingest(client):
    with patch("src.http_routes.findings._fetch_lease_rows",
               AsyncMock(side_effect=RuntimeError("db down"))):
        r = client.post("/api/findings", json=_fr_payload(fingerprint="fr-ingest-3"))
    assert r.status_code == 200
    assert r.json()["event"]["evidence"]["assessment"] == "check_error"


def test_non_forced_release_findings_are_untouched(client):
    with patch("src.http_routes.findings._fetch_lease_rows", AsyncMock()) as fetch:
        r = client.post("/api/findings", json={
            "type": "sentinel_finding", "severity": "high",
            "message": "fleet coherence dipped", "agent_id": "a",
            "agent_name": "Sentinel", "fingerprint": "plain-1",
        })
    assert r.status_code == 200
    assert "evidence" not in r.json()["event"]
    fetch.assert_not_called()


# --- fingerprint bound ------------------------------------------------------
# Ingest used to accept any length while /v1/sentinel/model-adjudicate refused
# over 256, so a long fingerprint was stored, queued, judged, then refused.
# Over-long ones are normalized (never rejected: producers post best-effort
# and a 400 would lose the finding silently).

def _finding(fp, **extra):
    return {"type": "sentinel_alarm_finding", "severity": "high", "message": "m",
            "agent_id": "sentinel", "agent_name": "Sentinel", "fingerprint": fp, **extra}


def test_over_long_fingerprint_is_normalized_not_rejected(client):
    import hashlib

    from src.http_routes.findings import _FINGERPRINT_MAX_CHARS
    long_fp = "lease:" + "x" * 400
    r = client.post("/api/findings", json=_finding(long_fp))
    assert r.status_code == 200 and r.json()["success"] is True
    event = r.json()["event"]
    assert event["fingerprint"] == "sha256:" + hashlib.sha256(long_fp.encode()).hexdigest()
    assert len(event["fingerprint"]) <= _FINGERPRINT_MAX_CHARS
    assert event["fingerprint_original"] == long_fp


def test_normalization_is_deterministic_so_dedup_still_works(client):
    long_fp = "y" * 1000
    first = client.post("/api/findings", json=_finding(long_fp)).json()
    second = client.post("/api/findings", json=_finding(long_fp)).json()
    assert first["deduped"] is False and second["deduped"] is True


def test_fingerprint_at_the_bound_is_untouched(client):
    fp = "z" * 256
    event = client.post("/api/findings", json=_finding(fp)).json()["event"]
    assert event["fingerprint"] == fp
    assert "fingerprint_original" not in event


def test_client_cannot_supply_fingerprint_original(client):
    event = client.post("/api/findings",
                        json=_finding("short", fingerprint_original="forged")).json()["event"]
    assert "fingerprint_original" not in event


def test_kept_original_is_itself_capped(client):
    from src.http_routes.findings import _FINGERPRINT_ORIGINAL_MAX_CHARS
    event = client.post("/api/findings", json=_finding("q" * 100_000)).json()["event"]
    assert len(event["fingerprint_original"]) == _FINGERPRINT_ORIGINAL_MAX_CHARS


# ---------------------------------------------------------------------------
# Server-verified actor provenance and `findings` agent token scope
# ---------------------------------------------------------------------------

VIGIL_UUID = "11111111-2222-4333-8444-555555555555"
OTHER_UUID = "99999999-8888-4777-8666-555555555555"
VIGIL_FINDINGS_TOKEN = "f" * 64


def _agent_token_entry(agent_uuid: str, token: str, scopes: str | None = None) -> str:
    import hashlib

    base = f"{agent_uuid}:sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    return f"{base}:{scopes}" if scopes is not None else base


def _client_for_ip(ip: str) -> TestClient:
    from src.http_api import http_events

    app = Starlette(
        routes=[
            Route("/api/findings", http_record_finding, methods=["POST"]),
            Route("/api/events", http_events, methods=["GET"]),
        ]
    )
    return TestClient(app, client=(ip, 50000))


def test_strips_caller_forged_provenance_on_trusted_network(client):
    spoofed = _finding(
        "prov-strip-1",
        type="vigil_finding",
        agent_id="vigil",
        agent_name="Vigil",
        provenance_verified=True,
        verified_actor_agent_uuid=VIGIL_UUID,
        provenance_source="server_findings_token",
        provenance_mac="0" * 64,
    )
    r = client.post("/api/findings", json=spoofed)
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["provenance_verified"] is False
    assert ev["verified_actor_agent_uuid"] is None
    assert "provenance_source" not in ev
    assert "provenance_mac" not in ev


def test_shared_static_tokens_record_unverified_provenance(monkeypatch):
    public_client = _client_for_ip("203.0.113.9")
    spoofed = _finding(
        "prov-shared-http",
        type="vigil_finding",
        agent_id="vigil",
        agent_name="Vigil",
        provenance_verified=True,
        verified_actor_agent_uuid=VIGIL_UUID,
    )
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "shared-local-secret")
    r_local = public_client.post(
        "/api/findings",
        json=spoofed,
        headers={"Authorization": "Bearer shared-local-secret"},
    )
    assert r_local.status_code == 200
    ev_local = r_local.json()["event"]
    assert ev_local["provenance_verified"] is False
    assert ev_local["verified_actor_agent_uuid"] is None

    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "shared-mcp-secret")
    r_mcp = public_client.post(
        "/api/findings",
        json={**spoofed, "fingerprint": "prov-shared-mcp"},
        headers={"Authorization": "Bearer shared-mcp-secret"},
    )
    assert r_mcp.status_code == 200
    ev_mcp = r_mcp.json()["event"]
    assert ev_mcp["provenance_verified"] is False
    assert ev_mcp["verified_actor_agent_uuid"] is None


def test_shared_static_secret_reused_in_agent_tokens_never_verifies_provenance(monkeypatch):
    """A shared static token must never be elevated into per-agent provenance."""
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", VIGIL_FINDINGS_TOKEN)
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
    )
    public_client = _client_for_ip("203.0.113.9")
    r = public_client.post(
        "/api/findings",
        json=_finding(
            "prov-reused-shared",
            provenance_verified=True,
            verified_actor_agent_uuid=VIGIL_UUID,
        ),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["provenance_verified"] is False
    assert ev["verified_actor_agent_uuid"] is None


@pytest.mark.parametrize("mode", ["loopback", "public", "strict"])
def test_findings_scoped_agent_token_sets_verified_provenance(monkeypatch, mode):
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID.upper(), VIGIL_FINDINGS_TOKEN, "findings"),
    )
    if mode == "strict":
        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "distinct-mcp-secret")
    ip = "127.0.0.1" if mode == "loopback" else "203.0.113.9"
    c = _client_for_ip(ip)
    r = c.post(
        "/api/findings",
        json=_finding(
            f"prov-verified-{mode}",
            type="vigil_finding",
            agent_id="vigil",
            agent_name="Vigil",
            provenance_verified=False,
            verified_actor_agent_uuid=OTHER_UUID,
        ),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["provenance_verified"] is True
    assert ev["verified_actor_agent_uuid"] == VIGIL_UUID
    assert ev["agent_id"] == "vigil"


@pytest.mark.parametrize(
    "scopes",
    [None, "checkin", "outcome", "knowledge_store", "checkin+outcome+knowledge_store"],
)
@pytest.mark.parametrize("mode", ["public", "strict_loopback"])
def test_non_findings_agent_tokens_are_refused_with_403_on_untrusted_or_strict(
    monkeypatch, scopes, mode
):
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, scopes),
    )
    if mode == "strict_loopback":
        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "distinct-mcp-secret")
        ip = "127.0.0.1"
    else:
        ip = "203.0.113.9"
    c = _client_for_ip(ip)
    r = c.post(
        "/api/findings",
        json=_finding(f"prov-refused-{scopes}-{mode}"),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False
    assert body["error_type"] == "AgentTokenScope"
    assert event_detector.get_recent_events() == []


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5"])
def test_trusted_network_resident_with_checkin_token_falls_back_unverified(
    monkeypatch, ip
):
    """Trusted-network callers carrying a non-findings agent token stay unblocked but unverified."""
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "checkin"),
    )
    c = _client_for_ip(ip)
    r = c.post(
        "/api/findings",
        json=_finding(
            f"prov-trusted-checkin-{ip}",
            provenance_verified=True,
            verified_actor_agent_uuid=VIGIL_UUID,
        ),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["provenance_verified"] is False
    assert ev["verified_actor_agent_uuid"] is None


@pytest.mark.parametrize(
    "extra_header",
    [("X-Unitares-Operator", "op-secret"), ("X-Agent-Id", OTHER_UUID)],
)
def test_findings_token_refuses_operator_and_x_agent_id_headers(monkeypatch, extra_header):
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
    )
    c = _client_for_ip("203.0.113.9")
    r = c.post(
        "/api/findings",
        json=_finding(f"prov-hdr-{extra_header[0]}"),
        headers={
            "Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}",
            extra_header[0]: extra_header[1],
        },
    )
    assert r.status_code == 403
    assert r.json()["error_type"] == "AgentTokenScope"

    # On trusted local network, forbidden headers still never yield verified provenance.
    local_c = _client_for_ip("127.0.0.1")
    r_local = local_c.post(
        "/api/findings",
        json=_finding(f"prov-hdr-local-{extra_header[0]}"),
        headers={
            "Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}",
            extra_header[0]: extra_header[1],
        },
    )
    assert r_local.status_code == 200
    ev_local = r_local.json()["event"]
    assert ev_local["provenance_verified"] is False
    assert ev_local["verified_actor_agent_uuid"] is None


@pytest.mark.parametrize("with_change_token", [False, True])
def test_unverified_and_other_actor_posts_cannot_dedup_verified_actor(
    monkeypatch, with_change_token
):
    """Unverified or different-actor posts must never shadow a verified actor's finding."""
    other_findings_token = "e" * 64
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        ",".join(
            [
                _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
                _agent_token_entry(OTHER_UUID, other_findings_token, "findings"),
            ]
        ),
    )
    c = _client_for_ip("127.0.0.1")
    fp = f"vigil-predictable-fp-{with_change_token}"
    base_extra = {"type": "vigil_finding", "agent_id": "vigil", "agent_name": "Vigil"}
    if with_change_token:
        base_extra["change_token"] = "open:1000000.0"

    # 1. Unverified caller posts first with the predictable Vigil fingerprint
    r_unverified = c.post("/api/findings", json=_finding(fp, **base_extra))
    assert r_unverified.status_code == 200
    assert r_unverified.json()["deduped"] is False
    assert r_unverified.json()["event"]["provenance_verified"] is False

    # 2. Another verified actor posts with the same fingerprint
    r_other = c.post(
        "/api/findings",
        json=_finding(fp, **base_extra),
        headers={"Authorization": f"Bearer {other_findings_token}"},
    )
    assert r_other.status_code == 200
    assert r_other.json()["deduped"] is False
    assert r_other.json()["event"]["provenance_verified"] is True
    assert r_other.json()["event"]["verified_actor_agent_uuid"] == OTHER_UUID

    # 3. Vigil's real findings-scoped post is NOT deduped by either prior post
    r_vigil = c.post(
        "/api/findings",
        json=_finding(fp, **base_extra),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r_vigil.status_code == 200
    body_vigil = r_vigil.json()
    assert body_vigil["deduped"] is False
    assert body_vigil["event"]["provenance_verified"] is True
    assert body_vigil["event"]["verified_actor_agent_uuid"] == VIGIL_UUID

    # 4. A repeat post by Vigil itself IS still deduped cleanly
    r_vigil_dup = c.post(
        "/api/findings",
        json=_finding(fp, **base_extra),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r_vigil_dup.status_code == 200
    assert r_vigil_dup.json()["deduped"] is True
    assert r_vigil_dup.json()["event"] is None


def test_unknown_bearer_on_public_or_strict_is_401(monkeypatch):
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
    )
    public_client = _client_for_ip("203.0.113.9")
    r = public_client.post(
        "/api/findings",
        json=_finding("prov-wrong-tok"),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401

    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "real-mcp-secret")
    loopback_client = _client_for_ip("127.0.0.1")
    r_strict = loopback_client.post(
        "/api/findings",
        json=_finding("prov-wrong-tok-strict"),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r_strict.status_code == 401


def test_malformed_agent_tokens_env_fails_closed(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_AGENT_TOKENS", "not-a-valid-entry")
    public_client = _client_for_ip("203.0.113.9")
    assert (
        public_client.post(
            "/api/findings",
            json=_finding("prov-bad-env-public"),
            headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
        ).status_code
        == 401
    )
    loopback_client = _client_for_ip("127.0.0.1")
    r_local = loopback_client.post(
        "/api/findings",
        json=_finding(
            "prov-bad-env-local",
            provenance_verified=True,
            verified_actor_agent_uuid=VIGIL_UUID,
        ),
        headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
    )
    assert r_local.status_code == 200
    ev = r_local.json()["event"]
    assert ev["provenance_verified"] is False
    assert ev["verified_actor_agent_uuid"] is None


def test_provenance_preserved_top_level_in_rest_events_and_ws_broadcast(monkeypatch):
    """GET /api/events (ring + audit-DB backfill) and WS broadcast keep top-level provenance."""
    from src.broadcaster import broadcaster_instance

    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
    )

    ws_sent: list[dict] = []
    persisted_rows: list[dict] = []

    async def _capture_ws(data: dict):
        ws_sent.append(dict(data))

    async def _capture_persist(audit_event: dict):
        persisted_rows.append(
            {
                "event_id": f"db-{len(persisted_rows) + 1}",
                "event_type": audit_event["event_type"],
                "agent_id": audit_event["agent_id"],
                "timestamp": audit_event["timestamp"],
                "details": audit_event["details"],
            }
        )

    monkeypatch.setattr(broadcaster_instance, "_send_to_clients", _capture_ws)

    c = _client_for_ip("127.0.0.1")
    with patch("src.audit_db.append_audit_event_async", new=_capture_persist):
        r = c.post(
            "/api/findings",
            json=_finding(
                "prov-e2e-stream",
                type="vigil_finding",
                agent_id="vigil",
                agent_name="Vigil",
                provenance_verified=False,
                verified_actor_agent_uuid=OTHER_UUID,
            ),
            headers={"Authorization": f"Bearer {VIGIL_FINDINGS_TOKEN}"},
        )
    assert r.status_code == 200

    # 1. POST response and WebSocket payload preserve top-level provenance keys
    #    and never leak the internal audit-DB provenance_mac.
    assert "provenance_mac" not in r.json()["event"]
    assert len(ws_sent) == 1
    assert type(ws_sent[0]["provenance_verified"]) is bool
    assert ws_sent[0]["provenance_verified"] is True
    assert ws_sent[0]["verified_actor_agent_uuid"] == VIGIL_UUID
    assert "provenance_mac" not in ws_sent[0]

    # 2. REST /api/events (in-memory ring) preserves top-level provenance keys
    #    without exposing provenance_mac.
    with patch("src.audit_db.query_audit_events_async", new=AsyncMock(return_value=[])):
        r_events = c.get("/api/events?since=0&limit=10")
    assert r_events.status_code == 200
    events = r_events.json()["events"]
    assert len(events) == 1
    assert type(events[0]["provenance_verified"]) is bool
    assert events[0]["provenance_verified"] is True
    assert events[0]["verified_actor_agent_uuid"] == VIGIL_UUID
    assert "provenance_mac" not in events[0]

    # 3. REST /api/events (audit-DB backfill after restart) trusts provenance ONLY
    #    when provenance_source, provenance_verified=True, canonical UUID, and
    #    HMAC provenance_mac all verify, and never exposes provenance_mac in JSON.
    assert len(persisted_rows) == 1
    assert persisted_rows[0]["details"]["provenance_source"] == "server_findings_token"
    assert isinstance(persisted_rows[0]["details"].get("provenance_mac"), str)
    assert len(persisted_rows[0]["details"]["provenance_mac"]) == 64

    tampered_row = {
        **persisted_rows[0],
        "event_id": "db-tampered-message",
        "details": {
            **persisted_rows[0]["details"],
            "message": "tampered after signing",
        },
    }
    db_rows = [
        persisted_rows[0],
        {
            "event_id": "db-forged-no-mac",
            "event_type": "vigil_finding",
            "agent_id": "vigil",
            "timestamp": "2026-11-21T01:59:45+00:00",
            "details": {
                "type": "vigil_finding",
                "severity": "warning",
                "message": "pre-deploy forged row with all 3 public strings but no MAC",
                "agent_name": "Vigil",
                "provenance_source": "server_findings_token",
                "provenance_verified": True,
                "verified_actor_agent_uuid": VIGIL_UUID,
            },
        },
        {
            "event_id": "db-forged-bad-mac",
            "event_type": "vigil_finding",
            "agent_id": "vigil",
            "timestamp": "2026-11-21T01:59:30+00:00",
            "details": {
                "type": "vigil_finding",
                "severity": "warning",
                "message": "forged row with wrong MAC",
                "agent_name": "Vigil",
                "provenance_source": "server_findings_token",
                "provenance_verified": True,
                "verified_actor_agent_uuid": VIGIL_UUID,
                "provenance_mac": "0" * 64,
            },
        },
        tampered_row,
        {
            "event_id": "db-missing-actor",
            "event_type": "vigil_finding",
            "agent_id": "vigil",
            "timestamp": "2026-11-21T01:59:15+00:00",
            "details": {
                "type": "vigil_finding",
                "severity": "warning",
                "message": "mismatched row with None actor",
                "agent_name": "Vigil",
                "provenance_source": "server_findings_token",
                "provenance_verified": True,
                "verified_actor_agent_uuid": None,
                "provenance_mac": persisted_rows[0]["details"]["provenance_mac"],
            },
        },
        {
            "event_id": "db-spoofed-nonbool",
            "event_type": "vigil_finding",
            "agent_id": "vigil",
            "timestamp": "2026-11-21T01:59:00+00:00",
            "details": {
                "type": "vigil_finding",
                "severity": "warning",
                "message": "spoofed",
                "agent_name": "Vigil",
                "provenance_source": "server_findings_token",
                "provenance_verified": "true",
                "verified_actor_agent_uuid": 12345,
                "provenance_mac": persisted_rows[0]["details"]["provenance_mac"],
            },
        },
    ]
    event_detector.clear_events()
    with patch("src.audit_db.query_audit_events_async", new=AsyncMock(return_value=db_rows)):
        r_db = c.get("/api/events?limit=10")
    assert r_db.status_code == 200
    by_id = {e["event_id"]: e for e in r_db.json()["events"]}
    assert type(by_id["db-1"]["provenance_verified"]) is bool
    assert by_id["db-1"]["provenance_verified"] is True
    assert by_id["db-1"]["verified_actor_agent_uuid"] == VIGIL_UUID
    assert "provenance_mac" not in by_id["db-1"]
    for bad_id in (
        "db-forged-no-mac",
        "db-forged-bad-mac",
        "db-tampered-message",
        "db-missing-actor",
        "db-spoofed-nonbool",
    ):
        assert by_id[bad_id]["provenance_verified"] is False
        assert by_id[bad_id]["verified_actor_agent_uuid"] is None
        assert "provenance_mac" not in by_id[bad_id]

    # 4. Per-actor MAC isolation & rotation properties:
    #    - Rotating/adding another agent's token does NOT invalidate Vigil's backfill.
    #    - Listing a second Vigil token during rotation keeps the old row valid.
    #    - Revoking Vigil's old token immediately fails the old row closed.
    other_tok = "b" * 64
    vigil_rotated_tok = "c" * 64
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        ",".join(
            [
                _agent_token_entry(VIGIL_UUID, VIGIL_FINDINGS_TOKEN, "findings"),
                _agent_token_entry(VIGIL_UUID, vigil_rotated_tok, "findings"),
                _agent_token_entry(OTHER_UUID, other_tok, "findings"),
            ]
        ),
    )
    # Cross-actor MAC forgery: OTHER_UUID cannot sign a row claiming VIGIL_UUID.
    from src.http_agent_tokens import findings_mac_key_for_bearer
    from src.http_routes.findings import compute_finding_provenance_mac

    other_key = findings_mac_key_for_bearer(other_tok)
    assert other_key is not None
    cross_actor_row = {
        **persisted_rows[0],
        "event_id": "db-cross-actor-mac",
        "details": {
            **persisted_rows[0]["details"],
            "provenance_mac": compute_finding_provenance_mac(
                mac_key=other_key,
                provenance_source="server_findings_token",
                verified_actor_agent_uuid=VIGIL_UUID,
                agent_id="vigil",
                event_type="vigil_finding",
                message=persisted_rows[0]["details"]["message"],
            ),
        },
    }
    with patch(
        "src.audit_db.query_audit_events_async",
        new=AsyncMock(return_value=[persisted_rows[0], cross_actor_row]),
    ):
        r_rot = c.get("/api/events?limit=10")
    by_rot = {e["event_id"]: e for e in r_rot.json()["events"]}
    assert by_rot["db-1"]["provenance_verified"] is True
    assert by_rot["db-1"]["verified_actor_agent_uuid"] == VIGIL_UUID
    assert by_rot["db-cross-actor-mac"]["provenance_verified"] is False
    assert by_rot["db-cross-actor-mac"]["verified_actor_agent_uuid"] is None

    # After revoking the old Vigil token (keeping only the new rotated token),
    # rows signed by the revoked token fail closed on backfill.
    monkeypatch.setenv(
        "UNITARES_HTTP_AGENT_TOKENS",
        _agent_token_entry(VIGIL_UUID, vigil_rotated_tok, "findings"),
    )
    with patch(
        "src.audit_db.query_audit_events_async",
        new=AsyncMock(return_value=[persisted_rows[0]]),
    ):
        r_revoked = c.get("/api/events?limit=10")
    revoked_ev = r_revoked.json()["events"][0]
    assert revoked_ev["provenance_verified"] is False
    assert revoked_ev["verified_actor_agent_uuid"] is None
