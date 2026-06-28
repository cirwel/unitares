"""Shared resolution → exogenous outcome builder + Sentinel adjudication wiring.

Pins the precision semantics (confirmed=good, fp-dismissal=bad, other-dismissal
=not-bad) and that the Sentinel CLI path attributes the external-truth outcome to
Sentinel's OWN UUID so the handler snapshots Sentinel's EISV — the second
baselined-resident channel for the residual falsifiability test.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agents.common.resolution_outcome import build_resolution_outcome_args


def test_confirmed_is_good_outcome():
    a = build_resolution_outcome_args("sentinel_finding", "confirmed", "fp123", "uuid-1")
    assert a["agent_id"] == "uuid-1"
    assert a["outcome_type"] == "sentinel_finding_confirmed"
    assert a["is_bad"] is False
    assert a["verification_source"] == "external_signal"
    assert a["detail"] == {"fingerprint": "fp123", "resolution": "confirmed", "reason": ""}


def test_false_positive_dismissal_is_bad():
    a = build_resolution_outcome_args("sentinel_finding", "dismissed", "fp123", "uuid-1", reason="fp")
    assert a["outcome_type"] == "sentinel_finding_dismissed"
    assert a["is_bad"] is True
    assert a["detail"]["reason"] == "fp"


def test_non_fp_dismissal_is_not_bad():
    for reason in ("out_of_scope", "wont_fix", "dup", "stale", "unclear", None):
        a = build_resolution_outcome_args("sentinel_finding", "dismissed", "fp", "uuid", reason=reason)
        assert a["is_bad"] is False, reason
        assert a["outcome_type"] == "sentinel_finding_dismissed"


def test_finding_kind_parameterizes_outcome_type():
    w = build_resolution_outcome_args("watcher_finding", "confirmed", "fp", "u")
    assert w["outcome_type"] == "watcher_finding_confirmed"
    s = build_resolution_outcome_args("sentinel_finding", "dismissed", "fp", "u", reason="fp")
    assert s["outcome_type"] == "sentinel_finding_dismissed"


@pytest.mark.asyncio
async def test_adjudicate_finding_posts_outcome_attributed_to_sentinel():
    from agents.sentinel.agent import adjudicate_finding

    agent = SimpleNamespace(agent_uuid="sentinel-uuid", _ensure_identity=AsyncMock())
    client = SimpleNamespace(call_tool=AsyncMock())

    args = await adjudicate_finding(agent, client, "dismissed", "fp-xyz", reason="fp")

    agent._ensure_identity.assert_awaited_once_with(client)
    client.call_tool.assert_awaited_once()
    tool, payload = client.call_tool.await_args.args
    assert tool == "outcome_event"
    assert payload["agent_id"] == "sentinel-uuid"
    assert payload["outcome_type"] == "sentinel_finding_dismissed"
    assert payload["is_bad"] is True
    assert payload["verification_source"] == "external_signal"
    assert args == payload


@pytest.mark.asyncio
async def test_adjudicate_finding_refuses_without_identity():
    from agents.sentinel.agent import adjudicate_finding

    agent = SimpleNamespace(agent_uuid=None, _ensure_identity=AsyncMock())
    client = SimpleNamespace(call_tool=AsyncMock())

    with pytest.raises(RuntimeError):
        await adjudicate_finding(agent, client, "confirmed", "fp", None)
    client.call_tool.assert_not_awaited()
