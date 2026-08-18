"""Reviewer verdicts must record which model produced them.

The gap this closes: provenance was computed on every review and written only
into the check-in's ``response_text``, which is not persisted (3 of 30,063
agent_state rows in 30 days; 0 of 4.18M audit events carried the reviewer's
audit line). So 194 live antithesis/synthesis rows over 90 days recorded
neither the model nor whether a fallback fired.

That distinction is not cosmetic: replaying 14 real theses (2026-08-18) put
local-model verdicts 36-50% apart from the deployed codex reviewer's, so a
selected-host verdict and a degraded-fallback verdict are materially different
objects.

``signature`` is NOT an available slot — it is the protocol's HMAC attestation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch

from agents.dialectic_reviewer import reviewer as r

FULL_PROVENANCE = {
    "backend": "external",
    "host_id": "external:generativelanguage.googleapis.com",
    "model_requested": "some-model-id",
    "model_used": "served-002",
    "models_used": ["served-002"],
    "tokens_used": 321,
    "cost_usd": 0.004,
    "latency_ms": 1234,
    "finish_reason": "stop",
    "fallback_from": None,
    "warnings": [],
}


# --------------------------------------------------------------------------- #
# The payload itself
# --------------------------------------------------------------------------- #
def test_persisted_provenance_keeps_the_attribution_fields():
    stored = r._provenance_for_message(FULL_PROVENANCE, degraded=False)
    assert stored["backend"] == "external"
    assert stored["host_id"] == "external:generativelanguage.googleapis.com"
    assert stored["model_used"] == "served-002"
    assert stored["models_used"] == ["served-002"]
    assert stored["tokens_used"] == 321
    assert stored["degraded"] is False


def test_fallback_is_recorded_when_one_fired():
    provenance = {
        "backend": "ollama",
        "host_id": "ollama:local",
        "models_used": ["gemma4:latest"],
        "fallback_from": "codex:host-adapter",
        "warnings": ["Codex backend unavailable or returned no verdict"],
    }
    stored = r._provenance_for_message(provenance, degraded=True)
    assert stored["fallback_from"] == "codex:host-adapter"
    assert stored["degraded"] is True
    # A degraded verdict must be distinguishable from a selected-host one.
    assert stored != r._provenance_for_message(FULL_PROVENANCE, degraded=False)


def test_unknown_provenance_fields_are_not_persisted():
    """Allowlist, not denylist — a future backend must not be able to leak a
    field into the governance ledger by adding it to its provenance dict."""
    stored = r._provenance_for_message(
        {**FULL_PROVENANCE, "api_key": "sk-secret", "raw_response": "..."},
        degraded=False,
    )
    assert "api_key" not in stored
    assert "raw_response" not in stored
    assert "sk-secret" not in json.dumps(stored)


def test_none_valued_fields_are_dropped_not_stored_as_null():
    stored = r._provenance_for_message(FULL_PROVENANCE, degraded=False)
    assert "fallback_from" not in stored  # it was None on this path


def test_payload_is_json_serializable():
    """It lands in a jsonb column; a non-serializable value would fail the write
    at submit time, i.e. lose the verdict rather than just the provenance."""
    json.dumps(r._provenance_for_message(FULL_PROVENANCE, degraded=False))


# --------------------------------------------------------------------------- #
# The submission actually carries it
# --------------------------------------------------------------------------- #
def _run_reviewer_capturing_calls(provenance, verdict_text):
    """Drive run() far enough to capture the antithesis submission."""
    calls = []

    class FakeClient:
        agent_uuid = "reviewer-uuid"

        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def onboard(self, *a, **kw):
            return {"uuid": self.agent_uuid}

        async def call_tool(self, name, args):
            calls.append((name, args))
            return {"success": True}

        async def checkin(self, **kw):
            return {"success": True}

    thesis = r.Thesis(session_id="s1", root_cause="rc", reasoning="why")

    async def fake_obtain(prompt):
        r._record_reviewer_provenance(provenance)
        return verdict_text

    # unitares_sdk is imported lazily inside run(); stub it so the test needs
    # no SDK install and no network.
    sdk = ModuleType("unitares_sdk")
    sdk_client = ModuleType("unitares_sdk.client")
    sdk_client.GovernanceClient = FakeClient

    with patch.object(r, "obtain_reviewer_text", side_effect=fake_obtain):
        with patch.object(r, "continue_after_disagreement", new=AsyncMock()):
            with patch.dict(
                sys.modules,
                {"unitares_sdk": sdk, "unitares_sdk.client": sdk_client},
            ):
                asyncio.run(r.run(thesis, "http://localhost:8767", None))
    return calls


def test_antithesis_submission_carries_reviewer_backend():
    calls = _run_reviewer_capturing_calls(
        FULL_PROVENANCE, '{"agrees": false, "reasoning": "no"}'
    )
    antithesis = [args for name, args in calls if args.get("action") == "antithesis"]
    assert antithesis, "reviewer submitted no antithesis"
    stored = antithesis[0]["observed_metrics"]["reviewer_backend"]
    assert stored["backend"] == "external"
    assert stored["model_used"] == "served-002"


def test_antithesis_provenance_survives_a_degraded_fallback():
    degraded_provenance = {
        "backend": "ollama",
        "host_id": "ollama:local",
        "models_used": ["gemma4:latest"],
        "fallback_from": "codex:host-adapter",
    }
    calls = _run_reviewer_capturing_calls(
        degraded_provenance, "not json at all"  # forces a degraded verdict
    )
    antithesis = [args for name, args in calls if args.get("action") == "antithesis"]
    stored = antithesis[0]["observed_metrics"]["reviewer_backend"]
    assert stored["fallback_from"] == "codex:host-adapter"
    assert stored["degraded"] is True


def test_submission_does_not_touch_signature():
    """signature is the protocol's HMAC attestation, not a provenance slot."""
    calls = _run_reviewer_capturing_calls(
        FULL_PROVENANCE, '{"agrees": true, "reasoning": "ok", "proposed_conditions": ["c"]}'
    )
    for _, args in calls:
        assert "signature" not in args
