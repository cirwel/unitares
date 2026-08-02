"""post_finding_result must distinguish "governance knows" from "nobody was told".

``post_finding`` collapses dedup and hard failure onto the same False. That is
fine for a caller asking "did I add something new", and wrong for a caller
deciding whether it may record having alerted. deploy_drift_doctor made exactly
that mistake and posted zero findings across its entire life while writing
last_alert every cycle (verified 2026-08-01: no deploy_drift row in
audit.events, ever).
"""
from __future__ import annotations

import pytest

from agents.common.findings import (
    DEDUPED, DELIVERED, FAILED, REACHED_GOVERNANCE,
    post_finding, post_finding_result,
)

PAYLOAD = dict(
    event_type="test_finding", severity="low", message="m",
    agent_id="a", agent_name="a", fingerprint="fp",
)


class FakeResp:
    def __init__(self, status_code=200, body=None, raises=False):
        self.status_code = status_code
        self._body = body if body is not None else {"success": True}
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("resp,expected", [
    (FakeResp(200, {"success": True}), DELIVERED),
    (FakeResp(200, {"success": True, "deduped": True}), DEDUPED),
    (FakeResp(200, {"success": False}), FAILED),
    (FakeResp(500), FAILED),
    (FakeResp(200, raises=True), FAILED),
])
def test_outcomes_are_distinguished(monkeypatch, resp, expected):
    monkeypatch.setattr("agents.common.findings._httpx_post",
                        lambda *a, **k: resp)
    assert post_finding_result(**PAYLOAD) == expected


def test_network_error_is_failed_not_silent(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("governance down")
    monkeypatch.setattr("agents.common.findings._httpx_post", boom)
    assert post_finding_result(**PAYLOAD) == FAILED


def test_never_raises_on_network_error(monkeypatch):
    """Called from agent-cycle hot paths; an outage must not crash a resident."""
    def boom(*a, **k):
        raise ConnectionError("governance down")
    monkeypatch.setattr("agents.common.findings._httpx_post", boom)
    assert post_finding(**PAYLOAD) is False


@pytest.mark.parametrize("resp,expected", [
    (FakeResp(200, {"success": True}), True),
    (FakeResp(200, {"success": True, "deduped": True}), False),
    (FakeResp(500), False),
])
def test_bool_wrapper_keeps_its_old_contract(monkeypatch, resp, expected):
    """sentinel/vigil/watcher/dogfood still get "did I add something new"."""
    monkeypatch.setattr("agents.common.findings._httpx_post",
                        lambda *a, **k: resp)
    assert post_finding(**PAYLOAD) is expected


def test_reached_governance_excludes_failure():
    assert DELIVERED in REACHED_GOVERNANCE
    assert DEDUPED in REACHED_GOVERNANCE
    assert FAILED not in REACHED_GOVERNANCE
