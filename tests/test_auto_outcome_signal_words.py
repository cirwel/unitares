"""Auto-emitted outcome labels must match whole-word signals, not substrings.

A bare substring test let "unresolved" match "resolved" and "unblocked" match
"blocked", so a check-in reporting open problems was recorded as a completed
task (and one reporting progress as a failure). These tests drive the real
``_post_update_auto_outcome`` with a mocked DB.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.updates import phases


def _ctx(text: str, complexity: float = 0.6) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agent-under-test",
        response_text=text,
        complexity=complexity,
        confidence=None,
        arguments={"client_session_id": "sess-1"},
        metrics_dict={},
        outcome_event_id=None,
    )


async def _emitted_types(text: str, complexity: float = 0.6) -> list[str]:
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="outcome-1")
    with patch("src.db.get_db", return_value=db):
        await phases._post_update_auto_outcome(_ctx(text, complexity))
    return [c.kwargs["outcome_type"] for c in db.record_outcome_event.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        # The status-line shape that produced the false label.
        "Status: 76 unresolved (3 high, 73 medium), 23 confirmed, 192 dismissed",
        "3 unresolved",
        "Unfinished: two items remain",
    ],
)
async def test_negated_completion_words_emit_nothing(text):
    assert await _emitted_types(text) == []


@pytest.mark.asyncio
async def test_unblocked_is_not_a_failure():
    assert await _emitted_types("Pipeline unblocked, continuing") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["Fixed the parser", "All 12 findings resolved", "Deployed to staging."],
)
async def test_completion_words_still_emit_task_completed(text):
    assert await _emitted_types(text) == ["task_completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["Build failed on step 3", "Two errors in the log", "Service crashed twice", "Blocked on review"],
)
async def test_failure_words_and_inflections_still_emit_task_failed(text):
    assert await _emitted_types(text) == ["task_failed"]


@pytest.mark.asyncio
async def test_low_complexity_still_emits_nothing():
    assert await _emitted_types("Fixed the parser", complexity=0.1) == []
