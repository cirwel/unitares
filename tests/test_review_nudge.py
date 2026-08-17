"""In-flow review nudge (adoption increment 2, #1685).

Covers the trigger predicate (off by default, condition set, warmup), the
FAIL-CLOSED session dedup (no Redis / error / repeat => no nudge — repetition
trains readers to ignore the channel), the mirror-mode signal line, and the
envelope's next_action suggestion.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.updates.enrichments import (
    _REVIEW_NUDGE_CONF_CEILING,
    _REVIEW_NUDGE_COMPLEXITY_FLOOR,
    _review_nudge_novel,
    _review_nudge_trigger,
)


def _ctx(
    confidence=None,
    complexity=0.5,
    total_updates=10,
    sub_action=None,
    agent_uuid="agent-uuid-1",
):
    response_data = {}
    if sub_action is not None:
        response_data["decision"] = {"sub_action": sub_action}
    return SimpleNamespace(
        confidence=confidence,
        complexity=complexity,
        response_data=response_data,
        meta=SimpleNamespace(total_updates=total_updates),
        agent_uuid=agent_uuid,
    )


# ─── trigger predicate ──────────────────────────────────────────────────


def test_flag_off_by_default_never_triggers(monkeypatch):
    monkeypatch.delenv("UNITARES_REVIEW_NUDGE", raising=False)
    assert _review_nudge_trigger(_ctx(confidence=0.1)) is None


def test_low_confidence_triggers(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    ctx = _ctx(confidence=_REVIEW_NUDGE_CONF_CEILING)
    assert _review_nudge_trigger(ctx) == "low_confidence"


def test_high_complexity_triggers(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    ctx = _ctx(complexity=_REVIEW_NUDGE_COMPLEXITY_FLOOR)
    assert _review_nudge_trigger(ctx) == "high_complexity"


def test_guide_verdict_triggers(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    assert _review_nudge_trigger(_ctx(sub_action="guide")) == "guide_verdict"


def test_healthy_steady_state_does_not_trigger(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    ctx = _ctx(confidence=0.9, complexity=0.5, sub_action="approve")
    assert _review_nudge_trigger(ctx) is None


def test_warmup_suppresses(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    assert _review_nudge_trigger(_ctx(confidence=0.1, total_updates=3)) is None


def test_absent_confidence_is_not_low_confidence(monkeypatch):
    monkeypatch.setenv("UNITARES_REVIEW_NUDGE", "1")
    assert _review_nudge_trigger(_ctx(confidence=None, complexity=0.5)) is None


# ─── session dedup: FAIL-CLOSED ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_novel_true_on_first_set():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=redis)):
        assert await _review_nudge_novel(_ctx()) is True
    kwargs = redis.set.call_args.kwargs
    assert kwargs.get("nx") is True and kwargs.get("ex")


@pytest.mark.asyncio
async def test_repeat_in_session_is_suppressed():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=None)  # NX miss: key already present
    with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=redis)):
        assert await _review_nudge_novel(_ctx()) is False


@pytest.mark.asyncio
async def test_no_redis_fails_closed():
    with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=None)):
        assert await _review_nudge_novel(_ctx()) is False


@pytest.mark.asyncio
async def test_redis_error_fails_closed():
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=RuntimeError("down"))
    with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=redis)):
        assert await _review_nudge_novel(_ctx()) is False


# ─── surfacing: mirror line + envelope next_action ──────────────────────


def test_mirror_mode_renders_nudge_line():
    from src.mcp_handlers.response_formatter import _format_mirror

    data = {
        "metrics": {"phi": 0.1, "coherence": 0.5, "risk_score": 0.1},
        "review_suggested": {"trigger": "low_confidence"},
    }
    result = _format_mirror(data, saved_trust_tier=None)
    assert any("request_review" in line for line in result["mirror"])


def test_mirror_mode_without_nudge_has_no_line():
    from src.mcp_handlers.response_formatter import _format_mirror

    data = {"metrics": {"phi": 0.1, "coherence": 0.5, "risk_score": 0.1}}
    result = _format_mirror(data, saved_trust_tier=None)
    assert not any("request_review" in line for line in result["mirror"])


def test_envelope_appends_nudge_to_next_action():
    from src.mcp_handlers.middleware.envelope_step import build_experience_envelope

    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "review_suggested": {"trigger": "low_confidence"},
    }
    envelope = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "request_review" in envelope["next_action"]
    assert "low confidence" in envelope["next_action"]


def test_envelope_next_action_unchanged_without_nudge():
    from src.mcp_handlers.middleware.envelope_step import build_experience_envelope

    payload = {"success": True, "decision": {"action": "proceed"}}
    envelope = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "request_review" not in envelope["next_action"]
