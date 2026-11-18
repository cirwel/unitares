"""The complete export package carries the accountability record.

Before this, ``export(action='file', complete_package=true)`` held agent
metadata and the EISV monitor's history, and nothing of the record itself:
no checkpoints, no authored findings, no outcomes. It also could not be
written at all when the estimator's state was unreadable. The package now has
a ``record`` layer read straight from storage, and the EISV history is an
optional layer beside it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.introspection import export as export_mod

AGENT = "1111aaaa-1111-4111-8111-1111aaaa1111"

RECORD = {
    "checkpoints": {"items": [{"checkpoint_id": "c1", "seq": 1}], "returned": 1, "truncated": False},
    "findings": {"items": [], "returned": 0, "truncated": False},
    "outcomes": {"items": [], "returned": 0, "truncated": False},
}


def _server(tmp_path, monitor_raises: bool = False):
    server = MagicMock()
    server.project_root = str(tmp_path)
    server.agent_metadata = {}
    server.load_monitor_state.return_value = None
    if monitor_raises:
        server.get_or_create_monitor.side_effect = RuntimeError("estimator down")
    else:
        monitor = MagicMock()
        monitor.state.V_history = []
        monitor.export_history.return_value = json.dumps({"E_history": []})
        server.get_or_create_monitor.return_value = monitor
    return server


async def _run(tmp_path, arguments, *, monitor_raises=False, record=RECORD, record_raises=False):
    reader = (
        AsyncMock(side_effect=RuntimeError("db down")) if record_raises
        else AsyncMock(return_value=record)
    )
    db = MagicMock()
    with patch.object(export_mod, "mcp_server", _server(tmp_path, monitor_raises)), \
         patch.object(export_mod, "require_registered_agent", return_value=(AGENT, None)), \
         patch.object(export_mod, "_read_agent_record", reader), \
         patch("src.agent_monitor_state.ensure_hydrated", AsyncMock()), \
         patch("src.db.get_db", return_value=db):
        result = await export_mod.handle_export_to_file(arguments)
    return json.loads(result[0].text), reader


@pytest.mark.asyncio
async def test_complete_package_includes_the_record_layer(tmp_path):
    body, reader = await _run(tmp_path, {"complete_package": True})

    assert body["success"] is True, body
    assert body["layers_included"] == ["metadata", "record", "history", "validation"]
    assert reader.await_args.args[1:] == (AGENT, export_mod._RECORD_LIMIT)
    package = json.loads(open(body["file_path"]).read())
    assert package["layers"]["record"] == RECORD
    assert "history_error" not in package and "record_error" not in package


@pytest.mark.asyncio
async def test_record_is_exported_when_the_estimator_cannot_be_read(tmp_path):
    body, _ = await _run(tmp_path, {"complete_package": True}, monitor_raises=True)

    assert body["success"] is True, body
    package = json.loads(open(body["file_path"]).read())
    assert package["layers"]["record"] == RECORD
    assert package["layers"]["history"] == {}
    assert package["history_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_record_read_failure_is_reported_not_fatal(tmp_path):
    body, _ = await _run(tmp_path, {"complete_package": True}, record_raises=True)

    assert body["success"] is True, body
    package = json.loads(open(body["file_path"]).read())
    assert package["layers"]["record"] is None
    assert package["record_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_history_only_export_says_why_when_the_estimator_is_unreadable(tmp_path):
    body, _ = await _run(tmp_path, {}, monitor_raises=True)

    assert body["success"] is False
    assert "complete_package=true" in json.dumps(body)


def _db(identity=True, checkpoints=(), findings=(), outcomes=()):
    db = MagicMock()
    db.get_identity = AsyncMock(
        return_value=MagicMock(identity_id=7) if identity else None
    )
    db.list_identity_checkpoints = AsyncMock(return_value=list(checkpoints))
    db.kg_query = AsyncMock(return_value=list(findings))
    db.list_agent_outcomes = AsyncMock(return_value=list(outcomes))
    return db


EISV_NONE = {f"eisv_{k}": None for k in ("e", "i", "s", "v", "phi", "verdict", "coherence", "regime")}


@pytest.mark.asyncio
async def test_read_agent_record_shapes_each_family():
    ts = datetime(2026, 11, 18, tzinfo=timezone.utc)
    checkpoints = [
        {"checkpoint_id": "c2", "seq": 2, "recorded_at": ts, "provenance": '{"tier": "strong"}'},
        {"checkpoint_id": "c1", "seq": 1, "recorded_at": ts, "provenance": None},
    ]
    findings = [{"id": "f1", "summary": "s", "created_at": ts, "search_vector": "'s':1"}]
    outcomes = [
        dict({"outcome_id": "o1", "ts": ts, "outcome_type": "test_passed", "detail": '{"k": 1}'},
             **dict(EISV_NONE, eisv_e=0.7, eisv_verdict="safe")),
        dict({"outcome_id": "o2", "ts": ts, "outcome_type": "task_failed", "detail": None}, **EISV_NONE),
    ]
    db = _db(checkpoints=checkpoints, findings=findings, outcomes=outcomes)

    record = await export_mod._read_agent_record(db, AGENT, 1)

    # limit+1 rows are read so truncation is known without a count query.
    db.list_identity_checkpoints.assert_awaited_once_with(7, limit=2)
    db.kg_query.assert_awaited_once_with(agent_id=AGENT, limit=2)
    db.list_agent_outcomes.assert_awaited_once_with(AGENT, limit=2)
    assert record["checkpoints"]["returned"] == 1
    assert record["checkpoints"]["truncated"] is True
    cp = record["checkpoints"]["items"][0]
    assert cp["recorded_at"] == ts.isoformat()
    assert cp["provenance"] == {"tier": "strong"}
    finding = record["findings"]["items"][0]
    assert "search_vector" not in finding and finding["created_at"] == ts.isoformat()
    assert record["findings"]["truncated"] is False
    outcome = record["outcomes"]["items"][0]
    assert outcome["detail"] == {"k": 1}
    assert not any(k.startswith("eisv_") for k in outcome)
    assert outcome["eisv"]["e"] == 0.7 and outcome["eisv"]["verdict"] == "safe"


@pytest.mark.asyncio
async def test_outcome_without_estimator_values_has_no_eisv_block():
    db = _db(outcomes=[dict({"outcome_id": "o", "ts": None, "detail": None}, **EISV_NONE)])

    record = await export_mod._read_agent_record(db, AGENT, 10)

    assert record["outcomes"]["items"][0]["eisv"] is None


@pytest.mark.asyncio
async def test_agent_without_a_durable_identity_has_no_checkpoints():
    db = _db(identity=False)

    record = await export_mod._read_agent_record(db, AGENT, 10)

    db.list_identity_checkpoints.assert_not_awaited()
    assert record["checkpoints"] == {"items": [], "returned": 0, "truncated": False}
