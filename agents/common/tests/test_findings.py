"""Tests for the shared post_finding helper used by Sentinel/Vigil/Watcher."""

from __future__ import annotations

import pytest

from agents.common.findings import compute_change_token, compute_fingerprint, post_finding


def test_compute_fingerprint_is_stable():
    fp1 = compute_fingerprint(["sentinel", "coordinated_degradation", "BEH", "sentinel-01"])
    fp2 = compute_fingerprint(["sentinel", "coordinated_degradation", "BEH", "sentinel-01"])
    assert fp1 == fp2
    assert len(fp1) == 16


def test_compute_fingerprint_differs_on_input():
    fp1 = compute_fingerprint(["sentinel", "a"])
    fp2 = compute_fingerprint(["sentinel", "b"])
    assert fp1 != fp2


def test_compute_change_token_is_stable_and_condition_scoped():
    base = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "extra": {"violation_class": "BEH"},
    }
    same = dict(base)
    changed = {**base, "severity": "critical"}

    assert compute_change_token(base) == compute_change_token(same)
    assert compute_change_token(base) != compute_change_token(changed)
    assert len(compute_change_token(base)) == 16


def test_compute_change_token_handles_non_jsonish_context():
    parts = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "extra": {1: ("a", "b"), "set": {"x", "y"}},
    }

    assert compute_change_token(parts) == compute_change_token(parts)
    assert len(compute_change_token(parts)) == 16


def test_post_finding_success(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):  # noqa: A002
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "deduped": False, "event": {"event_id": 1}}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    ok = post_finding(
        event_type="sentinel_finding",
        severity="high",
        message="fleet coherence dipped",
        agent_id="sentinel-01",
        agent_name="Sentinel",
        fingerprint="abcd1234",
        extra={"violation_class": "BEH"},
    )
    assert ok is True
    assert len(calls) == 1
    body = calls[0]["json"]
    assert body["type"] == "sentinel_finding"
    assert body["violation_class"] == "BEH"
    assert body["fingerprint"] == "abcd1234"
    assert body["change_token"] == compute_change_token({
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "extra": {"violation_class": "BEH"},
    })
    assert calls[0]["url"].endswith("/api/findings")


def test_post_finding_accepts_explicit_change_token(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):  # noqa: A002
        captured["body"] = json

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "deduped": False}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    post_finding(
        event_type="sentinel_finding",
        severity="high",
        message="fleet coherence dipped",
        agent_id="sentinel-01",
        agent_name="Sentinel",
        fingerprint="abcd1234",
        change_token="condition-v2",
        extra={"change_token": "legacy-extra-token", "violation_class": "BEH"},
    )

    assert captured["body"]["change_token"] == "condition-v2"
    assert captured["body"]["violation_class"] == "BEH"


def test_post_finding_preserves_legacy_extra_change_token(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):  # noqa: A002
        captured["body"] = json

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "deduped": False}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    post_finding(
        event_type="watcher_finding",
        severity="critical",
        message="persistent finding",
        agent_id="watcher",
        agent_name="Watcher",
        fingerprint="fp",
        extra={"change_token": "legacy-token", "pattern": "P005"},
    )

    assert captured["body"]["change_token"] == "legacy-token"
    assert captured["body"]["pattern"] == "P005"


def test_post_finding_swallows_network_errors(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("agents.common.findings._httpx_post", boom)
    # Must NOT raise — posting findings is best-effort, never blocks the agent
    assert post_finding(
        event_type="vigil_finding", severity="critical", message="gov down",
        agent_id="vigil", agent_name="Vigil", fingerprint="fp",
    ) is False


def test_post_finding_respects_env_token(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):  # noqa: A002
        calls.append(headers)

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "secret-token-xyz")
    post_finding(
        event_type="watcher_finding", severity="high", message="m",
        agent_id="watcher", agent_name="Watcher", fingerprint="fp",
    )
    assert calls[0].get("Authorization") == "Bearer secret-token-xyz"


def test_extra_cannot_overwrite_required_fields(monkeypatch):
    """`extra` must never clobber the 6 required body fields."""
    captured = {}

    def fake_post(url, json, headers, timeout):  # noqa: A002
        captured["body"] = json

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "deduped": False}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    post_finding(
        event_type="watcher_finding",
        severity="high",
        message="real",
        agent_id="w",
        agent_name="W",
        fingerprint="real-fp",
        extra={"fingerprint": "spoofed", "context": "passthrough"},
    )
    # The real fingerprint wins — extra cannot shadow required fields
    assert captured["body"]["fingerprint"] == "real-fp"
    # Non-conflicting extras pass through unchanged
    assert captured["body"]["context"] == "passthrough"


def test_malformed_json_response_returns_false(monkeypatch):
    """If resp.json() raises, post_finding returns False without re-raising."""

    def fake_post(url, json, headers, timeout):  # noqa: A002
        class FakeResp:
            status_code = 200

            def json(self):
                raise ValueError("not JSON")

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    assert post_finding(
        event_type="sentinel_finding", severity="info", message="m",
        agent_id="a", agent_name="A", fingerprint="fp",
    ) is False


def test_non_200_status_returns_false(monkeypatch):
    """Server-side rejection (400, 401, 500) surfaces as False, not an exception."""

    def fake_post(url, json, headers, timeout):  # noqa: A002
        class FakeResp:
            status_code = 400

            def json(self):
                return {"success": False, "error": "rejected"}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    assert post_finding(
        event_type="vigil_finding", severity="critical", message="m",
        agent_id="v", agent_name="V", fingerprint="fp",
    ) is False


def test_compute_fingerprint_format_is_locked():
    """Lock the pipe-joined SHA-256 16-hex-prefix format against silent refactor.

    This exists because Watcher already stores fingerprints in this format on
    disk (agents/watcher/findings.jsonl); any change to the format would
    break cross-agent dedup against Watcher's existing findings.
    """
    import hashlib

    expected = hashlib.sha256("sentinel|coord|BEH".encode()).hexdigest()[:16]
    actual = compute_fingerprint(["sentinel", "coord", "BEH"])
    assert actual == expected
    # And all-lowercase hex (hexdigest() guarantees this, but pin it)
    assert actual == actual.lower()
    assert len(actual) == 16
    assert all(c in "0123456789abcdef" for c in actual)


# --- Wave 3 §3.2 retry-on-503 (§14 prereq PR #10) ---


def _resp(status_code: int, body: dict | None = None, headers: dict | None = None):
    class FakeResp:
        pass

    resp = FakeResp()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json = lambda: body if body is not None else {}
    return resp


def test_post_finding_retries_once_on_503(monkeypatch):
    sleeps = []
    monkeypatch.setattr("agents.common.findings.time.sleep", sleeps.append)
    responses = [
        _resp(503, headers={"Retry-After": "2"}),
        _resp(200, {"success": True, "deduped": False}),
    ]
    calls = []

    def fake_post(url, json, headers, timeout):  # noqa: A002
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    ok = post_finding(
        event_type="sentinel_finding", severity="high", message="m",
        agent_id="sentinel-01", agent_name="Sentinel", fingerprint="fp",
    )
    assert ok is True
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_post_finding_503_twice_returns_false(monkeypatch):
    monkeypatch.setattr("agents.common.findings.time.sleep", lambda s: None)
    body = {
        "ok": False,
        "error": "governance_temporarily_unavailable",
        "retry_after_seconds": 5,
    }

    def fake_post(url, json, headers, timeout):  # noqa: A002
        return _resp(503, body)

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_post)
    # Still never raises — best-effort contract holds through cutover.
    assert post_finding(
        event_type="vigil_finding", severity="critical", message="m",
        agent_id="vigil", agent_name="Vigil", fingerprint="fp",
    ) is False


def test_post_finding_503_sleep_is_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr("agents.common.findings.time.sleep", sleeps.append)
    responses = [
        _resp(503, {"retry_after_seconds": 600}),
        _resp(200, {"success": True, "deduped": False}),
    ]
    monkeypatch.setattr(
        "agents.common.findings._httpx_post",
        lambda url, json, headers, timeout: responses.pop(0),
    )
    post_finding(
        event_type="sentinel_finding", severity="low", message="m",
        agent_id="s", agent_name="S", fingerprint="fp",
    )
    # Hot-path cap: never parks an agent cycle longer than 5s.
    assert sleeps == [5.0]
