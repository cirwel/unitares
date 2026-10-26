"""dialectic(action='get') serves the server-captured pause evidence.

A reviewer started by a launch profile takes only the session id and reads its
thesis and evidence from this record (inference-hosts-config-v0 §2.5,
agents/dialectic_reviewer/reviewer.py thesis_from_session). The last test runs
the server's fast read into the reviewer's parser, so the two stay in step.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.dialectic_reviewer.reviewer import (
    PAUSE_EVIDENCE_SCHEMA,
    thesis_from_session,
)
from src.mcp_handlers.dialectic.handlers import _PAUSE_EVIDENCE_SCHEMA

PAUSED = "11111111-1111-4111-8111-111111111111"
EVIDENCE = {
    "schema": _PAUSE_EVIDENCE_SCHEMA,
    "evidence_status": "captured",
    "risk_score": 0.71,
    "pause_effect_required": True,
    "pause_token": "2026-10-26T10:00:00+00:00",
}


def _db(row, messages=()):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=list(messages))
    pool = AsyncMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    db = AsyncMock()
    db._pool = pool
    db._ensure_pool = AsyncMock()
    return db


def _row(state):
    return {
        "session_id": "s-evidence",
        "phase": "antithesis",
        "status": "active",
        "session_type": "review",
        "paused_agent_id": PAUSED,
        "reviewer_agent_id": None,
        "topic": "Paused on risk",
        "created_at": datetime.now(timezone.utc),
        "resolution_json": None,
        "reason": "risk over threshold",
        "trigger_source": "governance",
        "awaiting_facilitation": False,
        "synthesis_round": 0,
        "max_synthesis_rounds": 5,
        "paused_agent_state_json": state,
    }


def _thesis_message():
    return {
        "message_type": "thesis",
        "agent_id": PAUSED,
        "timestamp": datetime.now(timezone.utc),
        "reasoning": "the spike was a test fixture",
        "root_cause": "test load",
        "proposed_conditions": json.dumps(["rerun the suite"]),
        "concerns": None,
        "agrees": None,
        "observed_metrics": None,
    }


async def _fast_get(row, messages=()):
    from src.mcp_handlers.dialectic.session import load_session_as_dict

    with patch("src.dialectic_db.get_dialectic_db", new=AsyncMock(return_value=_db(row, messages))):
        return await load_session_as_dict(row["session_id"])


def test_the_reviewer_reads_the_server_schema():
    assert PAUSE_EVIDENCE_SCHEMA == _PAUSE_EVIDENCE_SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [EVIDENCE, json.dumps(EVIDENCE)])
async def test_fast_get_serves_the_stored_evidence(stored):
    result = await _fast_get(_row(stored))
    assert result["paused_agent_state"] == EVIDENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "not json", "[1, 2]", 7])
async def test_unreadable_evidence_reads_as_empty(stored):
    assert (await _fast_get(_row(stored)))["paused_agent_state"] == {}


@pytest.mark.asyncio
async def test_the_reviewer_builds_its_thesis_from_the_fast_get():
    result = await _fast_get(_row(json.dumps(EVIDENCE)), [_thesis_message()])
    thesis, parent = thesis_from_session("s-evidence", result)
    assert parent == PAUSED
    assert thesis.root_cause == "test load"
    assert thesis.proposed_conditions == ["rerun the suite"]
    assert thesis.reasoning == "the spike was a test fixture"
    assert thesis.situation == "Topic: Paused on risk\nReason: risk over threshold"
    assert thesis.paused_agent_state == EVIDENCE
