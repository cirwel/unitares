"""check_working_state says whether writes are held; sync_state says no bare
"healthy" (AX dogfood 2026-10-16, items 4 and 10).

Item 4: a fresh identity's metrics read answered `uninitialized` and sent the
agent to a write to learn whether it was paused. The read now carries
`write_gate`, read from the predicate the two write gates apply, and the
envelope lifts its answer into action_summary.

Item 10: a provisional check-in returned `status: healthy` beside "not a
validated all-clear". The friendly state_summary drops the all-clear word;
the canonical payload keeps it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from src.mcp_handlers.support.pause_ttl import describe_write_gate
from tests.helpers.metrics_producer import real_metrics_payload


def _iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


# ---------------------------------------------------------------------------
# describe_write_gate mirrors the gates
# ---------------------------------------------------------------------------


def test_no_record_and_active_identities_are_not_held():
    assert describe_write_gate(None) == {"paused": False, "writes_held": False}
    active = SimpleNamespace(status="active", paused_at=None)
    assert describe_write_gate(active) == {"paused": False, "writes_held": False}


def test_live_pause_holds_writes_and_names_its_expiry():
    meta = SimpleNamespace(status="paused", paused_at=_iso(-60))
    gate = describe_write_gate(meta)
    assert gate["paused"] is True
    assert gate["writes_held"] is True
    assert "refused, not queued" in gate["held"]
    assert gate["expires_at"]
    # Read-only: describing the gate never expires the pause.
    assert meta.status == "paused"


def test_stale_pause_is_reported_but_not_held_and_not_expired_by_the_read():
    from config.governance_config import GovernanceConfig

    stale = _iso(-(int(GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS) + 60))
    meta = SimpleNamespace(status="paused", paused_at=stale)
    gate = describe_write_gate(meta)
    assert gate == {
        "paused": True,
        "writes_held": False,
        "note": (
            "The pause is past its expiry: the next check-in or new "
            "shared-memory entry lifts it."
        ),
    }
    assert meta.status == "paused" and meta.paused_at == stale


def test_archived_holds_shared_memory_writes_only():
    gate = describe_write_gate(SimpleNamespace(status="archived", paused_at=None))
    assert gate["paused"] is False
    assert gate["writes_held"] is True
    assert "check-in reactivates" in gate["held"]


@pytest.mark.parametrize(
    "status, paused_offset",
    [("active", None), ("paused", -60), ("archived", None), ("waiting_input", None)],
)
def test_description_agrees_with_check_agent_can_operate(status, paused_offset):
    """The shared-memory gate refuses exactly when the description says held."""
    from src.mcp_handlers.support.agent_auth import check_agent_can_operate

    paused_at = _iso(paused_offset) if paused_offset is not None else None
    meta = SimpleNamespace(status=status, paused_at=paused_at)
    expected_held = describe_write_gate(meta)["writes_held"]
    server = SimpleNamespace(agent_metadata={"agent-1": meta})
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
        refused = check_agent_can_operate("agent-1") is not None
    assert refused is expected_held


# ---------------------------------------------------------------------------
# The metrics read carries it, and the envelope answers in action_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verbosity", ["minimal", "standard", "full"])
async def test_every_metrics_tier_carries_write_gate(verbosity):
    payload, _ = await real_metrics_payload({"verbosity": verbosity}, check_ins=2)
    assert payload["write_gate"] == {"paused": False, "writes_held": False}


@pytest.mark.asyncio
async def test_fresh_identity_read_answers_paused_and_held_without_a_write():
    payload, validated = await real_metrics_payload(check_ins=0, recent_decisions=[])
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated
    )
    summary = env["action_summary"]
    assert summary["action"] == "uninitialized"
    assert summary["paused"] is False
    assert summary["writes_held"] is False


@pytest.mark.asyncio
async def test_paused_identity_read_says_writes_are_held():
    payload, validated = await real_metrics_payload(
        check_ins=2, status="paused", recent_decisions=["proceed", "pause"]
    )
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated
    )
    summary = env["action_summary"]
    assert summary["paused"] is True
    assert summary["writes_held"] is True
    assert "refused, not queued" in summary["held"]


# ---------------------------------------------------------------------------
# No bare "healthy" on the check-in's friendly surface
# ---------------------------------------------------------------------------


def _provisional_check_in(status: str) -> dict:
    return {
        "success": True,
        "status": status,
        "health_status": status,
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk (26.5%)",
            "margin": "settling",
        },
        "metrics": {
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.265,
            "verdict": {
                "value": "safe",
                "evidence": {"grade": "provisional", "basis": "ode_fallback"},
            },
            "health_status": status,
        },
        "risk_attribution": {"primary_driver": "phi_cold_start"},
    }


@pytest.mark.parametrize("mode", ["auto", "full"])
def test_provisional_check_in_drops_the_all_clear_word(mode):
    source = _provisional_check_in("healthy")
    formatted = format_response(deepcopy(source), {"response_mode": mode})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": mode}
    )
    state = env["state_summary"]
    assert "status" not in state and "health_status" not in state
    if mode == "full":
        assert "not a validated all-clear" in env["verdict_caveat"]
        # The canonical payload is untouched.
        assert env["raw_governance"]["status"] == "healthy"


def test_non_all_clear_bands_stay():
    source = _provisional_check_in("moderate")
    formatted = format_response(deepcopy(source), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "auto"}
    )
    assert env["state_summary"]["status"] == "moderate"
