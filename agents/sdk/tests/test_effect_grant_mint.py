"""propose_file_write effect-binding mint wiring (#1252 item 3).

The mint is best-effort by default (``auto``): today, with every binding flag
off server-side, the mint endpoint answers 501 and the proposed envelope must
be byte-identical to the pre-binding shape — that inertness is what makes the
producer wiring safe to land ahead of enforcement.
"""

from __future__ import annotations

from unitares_sdk.lease_plane.canonical import canonical_payload_sha256
from unitares_sdk.lease_plane.client import LeasePlaneClient, LeasePlaneClientConfig


class RecordingTransport:
    """Routes by URL: gov-mcp mint vs lease-plane effects."""

    def __init__(self, mint_response):
        self.mint_response = mint_response
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.endswith("/v1/effect-grant"):
            if isinstance(self.mint_response, Exception):
                raise self.mint_response
            return self.mint_response
        return {"ok": True, "effect_id": "e-1", "protocol_version": "v0.1"}

    def mint_requests(self):
        return [r for r in self.requests if r.url.endswith("/v1/effect-grant")]

    def effect_requests(self):
        return [r for r in self.requests if r.url.endswith("/v1/effects")]


def _client(transport):
    return LeasePlaneClient(
        LeasePlaneClientConfig(governance_url="http://gov.test:8767"),
        transport=transport,
    )


def _propose(client, **kwargs):
    return client.propose_file_write(
        path="/tmp/x.txt",
        content="aGVsbG8=",
        proposer_uuid="00000000-0000-0000-0000-000000000001",
        continuity_token="v1.tok",
        session_id="s-1",
        idempotency_key="fw-fixed",
        **kwargs,
    )


def test_auto_attaches_grant_when_minted():
    transport = RecordingTransport({"ok": True, "grant": "gnt.v1.abc.def"})
    result = _propose(_client(transport))
    assert result["ok"] is True

    (mint,) = transport.mint_requests()
    expected_sha = canonical_payload_sha256({"path": "/tmp/x.txt", "content": "aGVsbG8="})
    assert mint.json_body["payload_sha256"] == expected_sha
    assert mint.json_body["idempotency_key"] == "fw-fixed"
    assert mint.json_body["custody_mode"] == "execute"
    assert mint.json_body["surface"] == "file:///tmp/x.txt"
    assert mint.json_body["proposer_continuity_token"] == "v1.tok"

    (effect,) = transport.effect_requests()
    assert effect.json_body["proposer"]["effect_grant"] == "gnt.v1.abc.def"
    assert effect.json_body["idempotency_key"] == "fw-fixed"


def test_auto_proposes_grantless_on_mint_501():
    transport = RecordingTransport({"ok": False, "error": "binding_not_enabled"})
    result = _propose(_client(transport))
    assert result["ok"] is True

    (effect,) = transport.effect_requests()
    # Byte-compat with the pre-binding envelope: no effect_grant key at all.
    assert "effect_grant" not in effect.json_body["proposer"]


def test_auto_proposes_grantless_on_transport_error():
    transport = RecordingTransport(ConnectionError("gov down"))
    result = _propose(_client(transport))
    assert result["ok"] is True
    (effect,) = transport.effect_requests()
    assert "effect_grant" not in effect.json_body["proposer"]


def test_require_fails_without_proposing():
    transport = RecordingTransport({"ok": False, "error": "tier_recert_failed"})
    result = _propose(_client(transport), effect_binding="require")
    assert result["ok"] is False
    assert result["error"] == "effect_grant_mint_failed"
    assert transport.effect_requests() == []  # no side effects


def test_off_never_mints():
    transport = RecordingTransport({"ok": True, "grant": "gnt.v1.abc.def"})
    result = _propose(_client(transport), effect_binding="off")
    assert result["ok"] is True
    assert transport.mint_requests() == []
    (effect,) = transport.effect_requests()
    assert "effect_grant" not in effect.json_body["proposer"]


def test_invalid_mode_is_schema_invalid():
    transport = RecordingTransport({"ok": True, "grant": "g"})
    result = _propose(_client(transport), effect_binding="always")
    assert result["ok"] is False
    assert result["error"] == "schema_invalid"
    assert transport.requests == []


def test_mint_hits_governance_url_not_lease_plane():
    transport = RecordingTransport({"ok": True, "grant": "gnt.v1.abc.def"})
    _propose(_client(transport))
    (mint,) = transport.mint_requests()
    assert mint.url == "http://gov.test:8767/v1/effect-grant"

def test_auto_mints_for_multiline_text_content():
    """Regression (#1075 activation attempt, 2026-07-02): real file content is
    multi-line text — the floor writer proposes pretty-printed JSON — and the
    canonical form must admit the short-escape controls, or the sole standing
    producer can never mint and every enforced propose vetoes binding_absent."""
    transport = RecordingTransport({"ok": True, "grant": "gnt.v1.abc.def"})
    content = '{\n  "a": 1,\n\t"b": "x"\r\n}\n'
    result = _client(transport).propose_file_write(
        path="/tmp/floor.json",
        content=content,
        proposer_uuid="00000000-0000-0000-0000-000000000001",
        continuity_token="v1.tok",
        session_id="s-1",
        idempotency_key="fw-multiline",
    )
    assert result["ok"] is True

    (mint,) = transport.mint_requests()
    expected_sha = canonical_payload_sha256({"path": "/tmp/floor.json", "content": content})
    assert mint.json_body["payload_sha256"] == expected_sha

    (effect,) = transport.effect_requests()
    assert effect.json_body["proposer"]["effect_grant"] == "gnt.v1.abc.def"
