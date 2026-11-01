from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
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


def test_drift_stops_before_any_output_or_fts(tmp_path, monkeypatch):
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
            return {"relkind": b"r", "relrowsecurity": False}

    connection = Connection()
    row = {"id": "a", "summary": "Target", "details": "evidence", "tags": ["agent-adoption"]}
    monkeypatch.setattr(kg, "enumerate_universe", AsyncMock(return_value=([row], kg.canonical([row]))))
    monkeypatch.setattr(kg, "pinned_backend", lambda _: pytest.fail("must stop before FTS"))
    with pytest.raises(ValueError, match="corpus drift"):
        asyncio.run(kg.operate(connection, {"mode": "evaluate", "corpus_sha256": "stale"}, tmp_path))
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
