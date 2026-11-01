from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock
import json

import pytest

from scripts.eval import kg_snapshot_feasibility as kg


def test_full_universe_codec_and_projection_keep_distractors():
    rows = [{"id": "b", "summary": "Other", "details": "", "tags": ["agent-adoption"], "search_vector": "'other':1"},
            {"id": "a", "summary": "Target", "details": None, "tags": ["agent-adoption"], "provenance": {"v": 1}}]
    data = kg.canonical(rows)
    assert [json.loads(line)["id"] for line in data.splitlines()] == ["a", "b"]
    assert b'"search_vector"' in data and b'"provenance"' in data
    assert len(kg.projection(rows)) == 2
    with pytest.raises(ValueError, match="duplicate"):
        kg.canonical([rows[0], rows[0]])
    with pytest.raises(ValueError, match="Out of range"):
        kg.canonical([dict(rows[0], invalid=float("nan"))])
    with pytest.raises(ValueError, match="overflow"):
        kg.canonical(rows * 101)
    with pytest.raises(ValueError, match="overflow"):
        kg.canonical([dict(rows[0], details="x" * kg.MAX_BYTES)])


def test_task_count_and_material_sources_are_frozen():
    task = {"question": "Which?", "query": "term", "objective_key": "answer", "material_source_ids": ["a"]}
    with pytest.raises(ValueError, match="exactly three"):
        kg.validate_tasks([task], {"a"})
    tasks = [dict(task, query=f"term {i}") for i in range(3)]
    assert kg.validate_tasks(tasks, {"a"}) == tasks
    with pytest.raises(ValueError, match="material sources"):
        kg.validate_tasks(tasks, {"b"})


def test_numeric_metadata_changes_cannot_round_to_same_fingerprint(tmp_path):
    def row(number):
        return {"id": "a", "summary": "Source", "details": "", "tags": ["agent-adoption"],
                "provenance": {"precise": Decimal(number)}}
    first = kg.canonical([row("0.123456789012345678901")])
    second = kg.canonical([row("0.123456789012345678902")])
    assert first != second
    path = tmp_path / "corpus.jsonl"
    kg.write_bytes(path, first)
    records, data = kg.read_corpus(path)
    assert data == first
    assert records[0]["provenance"]["precise"] == Decimal("0.123456789012345678901")


def test_fts_adapter_acquires_exact_held_connection_and_filters_before_limit():
    async def scenario():
        connection = AsyncMock()
        connection.fetch.return_value = []
        backend = kg.pinned_backend(connection)
        async with backend.acquire() as acquired:
            assert acquired is connection
        await backend.kg_full_text_search("alpha dependency", limit=5, operator="OR", tags=["agent-adoption"],
                                          filters={"exclude_archived": True, "exclude_cold": True})
        sql, *params = connection.fetch.call_args.args
        assert "status IS DISTINCT FROM 'archived'" in sql
        assert "status IS DISTINCT FROM 'cold'" in sql
        assert sql.index("tags &&") < sql.index("LIMIT")
        assert params == ["alpha OR dependency", ["agent-adoption"], 5]
    asyncio.run(scenario())


@pytest.mark.parametrize("column_write", [False, True])
def test_drift_or_column_write_privilege_stops_before_any_output_or_fts(tmp_path, monkeypatch, column_write):
    class Connection:
        def __init__(self):
            self.calls = []

        @asynccontextmanager
        async def transaction(self, **kwargs):
            self.calls.append(kwargs)
            yield self

        async def fetchval(self, sql):
            return "on" if "read_only" in sql else "repeatable read"

        async def fetchrow(self, sql):
            if "relkind" in sql:
                return {"relkind": b"r", "relrowsecurity": False}
            return {"can_select": True, "direct_login": True, "can_write_columns": column_write,
                    **{key: False for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication",
                                              "rolbypassrls", "memberships", "can_write", "owns_table")}}

    connection = Connection()
    row = {"id": "a", "summary": "Target", "details": "evidence", "tags": ["agent-adoption"]}
    monkeypatch.setattr(kg, "enumerate_universe", AsyncMock(return_value=([row], kg.canonical([row]))))
    monkeypatch.setattr(kg, "pinned_backend", lambda _: pytest.fail("must stop before FTS"))
    with pytest.raises(ValueError, match="SELECT-only" if column_write else "corpus drift"):
        asyncio.run(kg.operate(connection, {"mode": "evaluate", "corpus_sha256": "stale"}, tmp_path))
    assert kg.enumerate_universe.await_count == (0 if column_write else 1)
    assert connection.calls == [{"isolation": "repeatable_read", "readonly": True}]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,value", [
    ("max_records", 1), ("max_canonical_bytes", 1), ("max_sql_json_bytes", 1),
    ("transaction_timeout_s", 1), ("transaction", "read committed"), ("codec", "other"),
    ("operator", "AND"), ("k", 1), ("automatic_retries", 1), ("fts_queries", 1),
])
def test_every_advertised_operation_bound_must_match(field, value):
    document = {"predicate": kg.PREDICATE, "server_settings": kg.SETTINGS,
                "max_records": kg.MAX_RECORDS, "max_canonical_bytes": kg.MAX_BYTES,
                "max_sql_json_bytes": kg.MAX_BYTES, "transaction_timeout_s": 45,
                "transaction": "read-only repeatable-read", "codec": "canonical_kg_universe_v1",
                "operator": "OR", "k": 5, "automatic_retries": 0, "claims": kg.CLAIMS,
                "fts_queries": 0, "lexical_queries": 0, "tasks": [], "mode": "capture"}
    kg.validate_contract(document)
    document[field] = value
    with pytest.raises(ValueError, match="operation contract changed"):
        kg.validate_contract(document)


def test_trusted_launcher_verifies_both_sources_before_entrypoint_execution(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts/eval"
    scripts.mkdir(parents=True)
    marker = tmp_path / "entered"
    entry = scripts / "kg_snapshot_feasibility.py"
    helper = scripts / "kg_canary_runtime.py"
    helper.write_bytes(b"# verified helper source\n")
    entry.write_text(f"from pathlib import Path\nassert _VERIFIED_HELPER_BYTES == {helper.read_bytes()!r}\nPath({str(marker)!r}).write_text('verified')\n")
    plan = tmp_path / "plan.json"
    kg.rt.save_new(plan, {"repo": str(repo), "files": {str(p): kg.rt.digest(p) for p in (entry, helper)}})
    launcher = Path(kg.__file__).with_name("run_kg_snapshot.py")
    command = [sys.executable, "-I", "-S", "-B", str(launcher), str(plan), kg.rt.digest(plan), "unused-approval", "unused-approval-sha"]
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert marker.read_text() == "verified"
    marker.unlink()
    helper.write_bytes(b"raise AssertionError('unreviewed helper')\n")
    result = subprocess.run(command, capture_output=True)
    assert result.returncode != 0 and b"changed before execution" in result.stderr
    assert not marker.exists()
    changed = plan.read_bytes() + b" "
    plan.write_bytes(changed)
    assert hashlib.sha256(changed).hexdigest() != command[-3]
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_truthy_approval_strings_cannot_substitute_for_independent_operation_resolution():
    document = {"mode": "capture", "operation_id": "operation-1"}
    approval = {"approved": True, "plan_sha256": "digest", "scope": "read_only_kg_capture",
                "operator_authorization_ref": "inert-test", "review_session": "review-1"}
    with pytest.raises(ValueError, match="durable independent governance"):
        kg.validate_approval(approval, document, "digest")
    record = {"success": True, "phase": "resolved", "status": "resolved", "session_id": "review-1",
              "reviewer": "reviewer", "paused_agent": "author",
              "reviewed_work_authorship": {"reviewed_work_author_agent_ids": ["author"]},
              "resolution": {"action": "resume", "conditions": ["APPROVE_CAPTURE sha256=digest operation_id=operation-1"]}}
    approval["governance_record"] = record
    kg.validate_approval(approval, document, "digest")
    record["reviewer"] = "author"
    with pytest.raises(ValueError, match="durable independent governance"):
        kg.validate_approval(approval, document, "digest")
    record["reviewer"] = "reviewer"
    with pytest.raises(ValueError, match="durable independent governance"):
        kg.validate_approval(approval, dict(document, operation_id="copied-operation"), "digest")
