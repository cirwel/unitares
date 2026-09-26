"""get_metrics reads the reading out of the check_working_state envelope.

``get_metrics`` calls the ``check_working_state`` alias, whose response is
the server's experience envelope: the reading in ``state_summary``, the
verdict and action in ``action_summary``, and the canonical payload under
``raw_governance`` only for ``verbosity="standard"`` or ``"full"``. Neither
shape has a top-level ``metrics`` key, and until this fix that was the only
key ``MetricsResult`` read, so ``MetricsResult.metrics`` was ``{}`` on every
call.

The fixtures below are real producer output, trimmed to the keys the parser
can read: ``get_governance_metrics_data`` run on a real monitor and wrapped by
the server's own envelope builder (``tests/helpers/metrics_producer.py`` in
the server tree, 2026-09-26). ``tests/test_sdk_metrics_envelope_roundtrip.py``
in the server tree runs the same parser over the live builder's output, so a
drift in either shape fails there.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from unitares_sdk._metrics_fields import resolve_metrics_fields
from unitares_sdk.client import GovernanceClient
from unitares_sdk.models import MetricsResult
from unitares_sdk.sync_client import SyncGovernanceClient

# The default read (verbosity="minimal"), thirty check-ins in.
MINIMAL = {
    "success": True,
    "tool": "check_working_state",
    "state_summary": {
        "value": "safe",
        "meaning": "Behavioral assessment: low risk.",
        "decision_action": "proceed",
        "status": "🟢 healthy",
        "primary_eisv_source": "behavioral",
        "E": 0.8377105593793863,
        "I": 0.8011825707806318,
        "S": 0.29627661439983566,
        "V": 0.033185,
        "coherence": {
            "value": 0.4818973803075639,
            "status": "⚪ legacy control feedback (not health-rated)",
            "source": "legacy_tanh_v",
            "role": "ode_control_feedback",
        },
        "risk_score": 0.0,
    },
    "action_summary": {
        "action": "proceed",
        "verdict": "safe",
        "risk_score": 0.0,
        "verdict_confidence": "non_provisional",
        "evidence_basis": "behavioral",
    },
}

# verbosity="full": state_summary names the verdict "verdict", and
# raw_governance carries the canonical payload, whose verdict is a string.
FULL = {
    "success": True,
    "tool": "check_working_state",
    "state_summary": {
        "verdict": "safe",
        "status": "healthy",
        "primary_eisv_source": "behavioral",
        "E": 0.8377351168477144,
        "I": 0.8180916629331794,
        "S": 0.28551624230926437,
        "V": 0.019008364448592236,
        "coherence": 0.4819456655018422,
        "risk_score": 0.0,
    },
    "action_summary": {
        "action": "proceed",
        "verdict": "safe",
        "risk_score": 0.0,
        "verdict_confidence": "non_provisional",
        "evidence_basis": "behavioral",
    },
    "raw_governance": {
        "E": 0.8377351168477144,
        "I": 0.8180916629331794,
        "S": 0.28551624230926437,
        "V": 0.019008364448592236,
        "coherence": 0.4819456655018422,
        "risk_score": 0.0,
        "verdict": "safe",
        "primary_eisv_source": "behavioral",
        "last_decision_action": "proceed",
        "status": "healthy",
    },
}

# A paused agent: the verdict is still "safe"; the action is what paused.
PAUSED = {
    "success": True,
    "tool": "check_working_state",
    "state_summary": {
        "value": "safe",
        "meaning": "Behavioral assessment: low risk.",
        "decision_action": "pause",
        "status": "🟢 healthy",
        "primary_eisv_source": "behavioral",
        "E": 0.8588239500349637,
        "I": 0.7549149954350558,
        "S": 0.43980580210628856,
        "V": 0.099938,
        "coherence": {
            "value": 0.49356752788698693,
            "status": "⚪ legacy control feedback (not health-rated)",
            "source": "legacy_tanh_v",
            "role": "ode_control_feedback",
        },
        "risk_score": 0.1,
    },
    "action_summary": {
        "action": "pause",
        "verdict": "safe",
        "risk_score": 0.1,
        "verdict_confidence": "non_provisional",
        "evidence_basis": "behavioral",
    },
}

# Before the first check-in: E/I/S/V are the controller's fallback, and the
# pending coherence badge carries a null value.
UNINITIALIZED = {
    "success": True,
    "tool": "check_working_state",
    "state_summary": {
        "value": "uninitialized",
        "meaning": "Agent has no recorded state yet.",
        "status": "⚪ uninitialized",
        "primary_eisv_source": "ode_fallback",
        "E": 0.7,
        "I": 0.8,
        "S": 0.2,
        "V": 0.0,
        "coherence": {
            "value": None,
            "status": "⚪ pending (first check-in required)",
            "source": "legacy_tanh_v",
            "role": "ode_control_feedback",
        },
        "verdict_provisional": True,
    },
    "action_summary": {
        "action": "uninitialized",
        "verdict": "uninitialized",
        "verdict_confidence": "provisional",
        "evidence_basis": "ode_fallback",
    },
}

UNBOUND = {
    "success": True,
    "tool": "check_working_state",
    "state_summary": {
        "value": "unbound",
        "meaning": "No caller-proven identity on this call.",
        "status": "⚪ unbound",
    },
    "action_summary": {
        "action": "unbound",
        "verdict": "unbound",
        "verdict_confidence": "unspecified",
    },
}

# A direct get_governance_metrics caller at the default tier: every value is
# badged with its label or threshold.
CANONICAL_MINIMAL = {
    "success": True,
    "E": {"value": 0.8377105593793863, "label": "Energy"},
    "I": {"value": 0.8011825707806318, "label": "Information Integrity"},
    "S": {"value": 0.29627661439983566, "label": "Entropy"},
    "V": {"value": 0.033185, "label": "Valence"},
    "coherence": {"value": 0.4818973803075639, "source": "legacy_tanh_v"},
    "risk_score": {"value": 0.0, "threshold": 0.45},
    "verdict": {"value": "safe", "decision_action": "proceed"},
    "primary_eisv_source": "behavioral",
    "status": "🟢 healthy",
}


def _via_async(raw: dict) -> MetricsResult:
    import asyncio

    client = GovernanceClient(timeout=5.0, retry_delay=0.01)
    client.call_tool = AsyncMock(return_value=deepcopy(raw))
    result = asyncio.run(client.get_metrics())
    assert client.call_tool.call_args[0][0] == "check_working_state"
    return result


def _via_sync(raw: dict) -> MetricsResult:
    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: deepcopy(raw)
    return client.get_metrics()


CLIENTS = [pytest.param(_via_async, id="async"), pytest.param(_via_sync, id="sync")]


@pytest.mark.parametrize("read", CLIENTS)
def test_default_envelope_yields_the_reading(read):
    result = read(MINIMAL)
    summary = MINIMAL["state_summary"]
    assert result.metrics == {
        "E": summary["E"],
        "I": summary["I"],
        "S": summary["S"],
        "V": summary["V"],
        "coherence": summary["coherence"]["value"],
        "risk_score": 0.0,
        "verdict": "safe",
        "primary_eisv_source": "behavioral",
    }
    assert result.verdict == "safe"
    assert result.action == "proceed"
    assert result.coherence == summary["coherence"]["value"]
    assert result.risk == 0.0


@pytest.mark.parametrize("read", CLIENTS)
def test_full_envelope_reads_the_canonical_payload_first(read):
    """raw_governance outranks state_summary; the copies are made to disagree."""
    raw = deepcopy(FULL)
    raw["state_summary"]["E"] = 0.01
    raw["state_summary"]["coherence"] = 0.99
    raw["action_summary"]["action"] = "pause"

    result = read(raw)

    assert result.metrics["E"] == FULL["raw_governance"]["E"]
    assert result.coherence == FULL["raw_governance"]["coherence"]
    assert result.action == "proceed"
    assert result.verdict == "safe"


@pytest.mark.parametrize("read", CLIENTS)
def test_a_pause_is_the_action_not_the_verdict(read):
    result = read(PAUSED)
    assert result.action == "pause"
    assert result.verdict == "safe"
    assert result.risk == 0.1


@pytest.mark.parametrize("read", CLIENTS)
def test_uninitialized_read_keeps_the_fallback_marked_as_fallback(read):
    result = read(UNINITIALIZED)
    assert result.verdict == "uninitialized"
    assert result.action == "uninitialized"
    assert result.coherence is None
    assert "coherence" not in result.metrics
    assert result.metrics["primary_eisv_source"] == "ode_fallback"
    assert result.metrics["E"] == 0.7


@pytest.mark.parametrize("read", CLIENTS)
def test_unbound_read_has_no_reading(read):
    result = read(UNBOUND)
    assert result.metrics == {"verdict": "unbound"}
    assert result.action == "unbound"
    assert result.coherence is None and result.risk is None


def test_direct_canonical_payload_is_read_through_its_badges():
    fields = resolve_metrics_fields(CANONICAL_MINIMAL)
    assert fields["metrics"]["E"] == CANONICAL_MINIMAL["E"]["value"]
    assert fields["coherence"] == CANONICAL_MINIMAL["coherence"]["value"]
    assert fields["risk"] == 0.0
    assert fields["verdict"] == "safe"
    assert fields["action"] == "proceed"


def test_a_response_with_its_own_metrics_dict_keeps_it():
    """Backward compatible: a shape that did carry ``metrics`` is not replaced."""
    fields = resolve_metrics_fields(
        {"success": True, "metrics": {"coherence": 0.5, "risk_score": 0.2}}
    )
    assert fields["metrics"] == {"coherence": 0.5, "risk_score": 0.2}
    assert fields["coherence"] == 0.5
    assert fields["risk"] == 0.2
