from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.identity.s22_h5_comparison import (
    AGENT_STATE_S22_SQL,
    KG_S22_SQL,
    assess_s22_h5_coverage,
    collect_s22_h5_entries,
    normalize_s22_h5_entry,
    normalize_s22_harness,
)


class _RecordLike:
    def __init__(self, data):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


def _row(harness: str, comparison_key: str = "h5-bounded-task", **extra):
    context = {
        "schema": "s22.write_context.v1",
        "context_source": "process_agent_update",
        "harness_type": harness,
        "comparison_key": comparison_key,
        "task_label": "H5 bounded task",
        "transport": "mcp-stdio",
        "tool_surface": ["terminal", "mcp:unitares"],
        **extra,
    }
    return {
        "entry_id": f"{harness}-{comparison_key}",
        "source": "agent_state",
        "agent_id": f"agent-{harness}",
        "recorded_at": datetime(2026, 5, 6, tzinfo=timezone.utc),
        "s22_context": context,
    }


def test_normalize_s22_harness_aliases():
    assert normalize_s22_harness("claude_code") == "claude-code"
    assert normalize_s22_harness("Claude Code") == "claude-code"
    assert normalize_s22_harness("openai-codex") == "codex-cli"
    assert normalize_s22_harness("hermes-agent") == "hermes"


def test_normalize_entry_reads_state_json_context():
    entry = normalize_s22_h5_entry({
        "entry_id": "state-1",
        "source": "agent_state",
        "agent_id": "agent-1",
        "state_json": {
            "action": "proceed",
            "provenance_context": {
                "schema": "s22.write_context.v1",
                "context_source": "process_agent_update",
                "harness_type": "codex_cli",
                "comparison_key": "h5-task",
                "model_provider": "openai",
            },
        },
    })

    assert entry is not None
    assert entry.canonical_harness == "codex-cli"
    assert entry.comparison_key == "h5-task"
    assert entry.task_outcome == "proceed"
    assert entry.is_comparable is True


def test_normalize_entry_accepts_asyncpg_record_like_rows():
    entry = normalize_s22_h5_entry(_RecordLike(_row("codex-cli")))

    assert entry is not None
    assert entry.canonical_harness == "codex-cli"
    assert entry.comparison_key == "h5-bounded-task"
    assert entry.is_comparable is True


def test_assess_s22_h5_coverage_complete_for_shared_key():
    assessment = assess_s22_h5_coverage([
        _row("hermes"),
        _row("claude_code"),
        _row("codex-cli"),
    ])

    assert assessment["decision"] == "complete"
    assert assessment["reason"] == "shared_comparison_key_covers_required_harnesses"
    assert assessment["complete_comparison_keys"] == ["h5-bounded-task"]
    assert assessment["missing_comparable_harnesses"] == []


def test_assess_s22_h5_coverage_requires_shared_key():
    assessment = assess_s22_h5_coverage([
        _row("hermes", "task-a"),
        _row("claude-code", "task-b"),
        _row("codex-cli", "task-c"),
    ])

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "no_shared_comparison_key"
    assert assessment["missing_comparable_harnesses"] == []
    assert {item["comparison_key"] for item in assessment["comparison_sets"]} == {
        "task-a",
        "task-b",
        "task-c",
    }


def test_assess_s22_h5_coverage_reports_missing_harness():
    assessment = assess_s22_h5_coverage([
        _row("hermes"),
        _row("codex-cli"),
    ])

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "missing_required_harness_entries"
    assert assessment["missing_comparable_harnesses"] == ["claude-code"]


def test_assess_s22_h5_coverage_rejects_unsituated_entry():
    row = _row("hermes")
    row["s22_context"].pop("transport")
    row["s22_context"].pop("tool_surface")

    assessment = assess_s22_h5_coverage([row])

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "no_comparable_entries"
    assert assessment["non_comparable_entries"][0]["canonical_harness"] == "hermes"


def test_kg_query_avoids_optional_discovery_confidence_column():
    assert "d.confidence" not in KG_S22_SQL


@pytest.mark.asyncio
async def test_collect_s22_h5_entries_reads_state_and_kg_sources():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [_row("codex-cli")],
        [_row("hermes")],
    ])
    acquire = AsyncMock()
    acquire.__aenter__.return_value = conn
    acquire.__aexit__.return_value = False
    db = MagicMock()
    db.acquire.return_value = acquire

    entries = await collect_s22_h5_entries(db=db, limit_per_source=25)

    assert [entry.canonical_harness for entry in entries] == ["codex-cli", "hermes"]
    assert conn.fetch.await_args_list[0].args == (AGENT_STATE_S22_SQL, 25)
    assert conn.fetch.await_args_list[1].args == (KG_S22_SQL, 25)
