"""Tests for LeasePlaneClient.propose_file_write — the consumer side of governed
file writes (route a write through the plane instead of writing directly)."""

from __future__ import annotations

from unitares_sdk.lease_plane.client import LeasePlaneClient, LeasePlaneClientConfig


def _client(fake_transport):
    return LeasePlaneClient(
        LeasePlaneClientConfig(bearer_token="tok"), transport=fake_transport
    )


def test_propose_file_write_builds_the_governed_envelope():
    captured: dict = {}

    def fake_transport(request):
        captured["req"] = request
        return {
            "ok": True,
            "effect_id": "e-1",
            "custody_mode": "execute",
            "result": {"bytes_written": 3},
            "protocol_version": "v1.0",
        }

    resp = _client(fake_transport).propose_file_write(
        path="/tmp/governed.txt",
        content="abc",
        proposer_uuid="11111111-1111-1111-1111-111111111111",
        continuity_token="ctok",
        session_id="sess-1",
        ttl_s=300,
        idempotency_key="idem-1",
    )

    assert resp["ok"] is True
    assert resp["effect_id"] == "e-1"

    req = captured["req"]
    assert req.method == "POST"
    assert req.url.endswith("/v1/effects")
    assert req.headers["Authorization"] == "Bearer tok"

    body = req.json_body
    assert body["effect_type"] == "file_write"
    assert body["custody_mode"] == "execute"
    assert body["surface"] == "file:///tmp/governed.txt"
    assert body["required_leases"] == [
        {"surface": "file:///tmp/governed.txt", "ttl_s": 300}
    ]
    assert body["payload"] == {"path": "/tmp/governed.txt", "content": "abc"}
    # proposer identity is nested (matches the server's validate(); a flat
    # proposer_agent_uuid would arrive as nil and crash before the veto).
    assert body["proposer"] == {
        "agent_uuid": "11111111-1111-1111-1111-111111111111",
        "continuity_token": "ctok",
    }
    assert body["provenance"] == {"session_id": "sess-1"}
    assert body["idempotency_key"] == "idem-1"


def test_propose_file_write_strips_file_scheme_and_autogenerates_idempotency():
    captured: dict = {}

    def fake_transport(request):
        captured["req"] = request
        return {"ok": True, "effect_id": "e", "protocol_version": "v1.0"}

    _client(fake_transport).propose_file_write(
        path="file:///tmp/x",
        content="y",
        proposer_uuid="u",
        continuity_token="c",
        session_id="s",
        encoding="base64",
    )

    body = captured["req"].json_body
    # the scheme is stripped for the filesystem path but kept for the surface
    assert body["payload"]["path"] == "/tmp/x"
    assert body["payload"]["encoding"] == "base64"
    assert body["surface"] == "file:///tmp/x"
    # no idempotency_key supplied -> a stable-prefixed one is generated
    assert body["idempotency_key"].startswith("fw-")


def test_propose_file_write_surfaces_server_error_envelope():
    def fake_transport(request):
        return {
            "ok": False,
            "error": "governance_blocked",
            "reason": "vetoed",
            "protocol_version": "v1.0",
        }

    resp = _client(fake_transport).propose_file_write(
        path="/tmp/x",
        content="y",
        proposer_uuid="u",
        continuity_token="c",
        session_id="s",
    )
    # the caller can see the governance verdict; nothing was committed
    assert resp["ok"] is False
    assert resp["error"] == "governance_blocked"
