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


def _ctx(
    text: str,
    complexity: float = 0.6,
    epistemic_class: str = "agent_report",
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agent-under-test",
        response_text=text,
        complexity=complexity,
        confidence=None,
        epistemic_class=epistemic_class,
        arguments={"client_session_id": "sess-1"},
        metrics_dict={},
        outcome_event_id=None,
    )


async def _emitted_types(
    text: str,
    complexity: float = 0.6,
    epistemic_class: str = "agent_report",
) -> list[str]:
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="outcome-1")
    with patch("src.db.get_db", return_value=db):
        await phases._post_update_auto_outcome(_ctx(text, complexity, epistemic_class))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,epistemic_class",
    [
        ("12 tool calls completed: read_file, edit_file, bash", "agent_report"),
        ("1 tool call completed: rg", "agent_report"),
        ("All checks completed and verified.", "substrate_observation"),
        ("Task failed with error.", "substrate_interpretation"),
    ],
)
async def test_automatic_and_rollup_checkins_emit_no_auto_outcomes(text, epistemic_class):
    """#2418 Fix 2: non-authored check-ins and runtime tool-count rollups never emit task_completed/task_failed."""
    assert await _emitted_types(text, complexity=0.7, epistemic_class=epistemic_class) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Just wrote a deliberately contradictory critical-severity finding claiming "
    "the system is both healthy and broken. Testing knowledge integrity and "
    "whether governance reacts to adversarial self-reporting.",
    "No errors occurred; all checks passed.",
    "No crashes and errors occurred",
    "No errors, crashes or regressions occurred.",
    "Neither deployed, fixed nor merged the patch.",
    "The report says v3.1 failed.",
    "If v3.1 is deployed, everything is fine.",
    "If the build fails,\ndeployment is blocked.",
    "The report says:\nFixed the parser.",
    '"Build\nfailed on step 3"',
    "> Build failed on step 3",
    "Zero completed tasks",
    "0 tasks completed",
    "Tests: 12 passed, 0 failed.",
    "0 completed, 3 pending.",
    "'The parser's broken'",
    "‘The parser’s broken’",
    "The build may have failed",
    "The patch will be deployed tomorrow",
    "I'll have deployed the patch tomorrow",
    "I expect the service to have crashed",
    "0 errors and zero crashes",
    "Errors: 0",
    "Parser was not fixed",
    "Never deployed to staging",
    "The service isn't broken",
    "The service isn’t broken",
    "Not blocked on review anymore",
    'Investigating why "Build failed on step 3" appears in old logs',
    "The example says 'Fixed the parser'",
    "```\nBuild failed\n```",
    "`task failed`",
    "“Service crashed twice”",
    "‘Build failed’",
    "Checking whether the service crashed",
    "If the build failed, deployment is blocked",
    "Did the build fail?",
    "Build failed?",
    "I am not claiming the task failed",
    "I fixed the parser, but deployment failed",
    "Fixed the parser. Build failed on step 3.",
    "System is both resolved and broken",
    "The errorless, fixedwidth report is ready",
])
async def test_non_assertions_and_mixed_results_abstain(text):
    assert await _emitted_types(text) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [
    ("No errors occurred, but deployment failed", "task_failed"),
    ("Parser was not fixed; deployment failed", "task_failed"),
    ("Completed the task with no errors", None),  # conservative single-clause abstention
    ("Completed the task; no errors occurred", "task_completed"),
    ("I deployed `backend` to staging", "task_completed"),
    ("The deployment on worker 0 failed.", "task_failed"),
    ("The deployment on worker 1 failed.", "task_failed"),
    ("Version 3.0 deployed successfully", "task_completed"),
    ("Version 3.1 deployed successfully", "task_completed"),
    ("Fixed bug 0 in the tracker", "task_completed"),
    ("Tests passed (0 failed). Deployed the patch.", "task_completed"),
    ("> Build failed on step 3\n\nDeployed the patch.", "task_completed"),
    ("If the build failed, deployment would stop. Deployed to staging.", "task_completed"),
])
async def test_independent_result_clauses(text, expected):
    assert await _emitted_types(text) == ([expected] if expected else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [
    ("Fixed the parser", "task_completed"),
    ("Build failed on step 3", "task_failed"),
])
async def test_guarded_labels_keep_weak_provenance_and_complexity_boundary(text, expected):
    from src.auto_outcome_labels import AUTO_OUTCOME_RULE_VERSION

    assert await _emitted_types(text, complexity=0.299) == []
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="outcome-1")
    with patch("src.db.get_db", return_value=db):
        await phases._post_update_auto_outcome(_ctx(text, complexity=0.3))
    event = db.record_outcome_event.call_args.kwargs
    assert event["outcome_type"] == expected
    assert event["verification_source"] == "agent_reported_tool_result"
    assert event["detail"]["auto_outcome_rule_version"] == AUTO_OUTCOME_RULE_VERSION
