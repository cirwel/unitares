#!/usr/bin/env python3
"""Explicitly reviewed, read-only KG database-component feasibility operations.

Plan capture first; review it before execute. Curate three queries from that
private capture, plan evaluation separately, and review that exact plan too.
No MCP, model, identity creation, audit append, retry, or application startup.
Run with python -I -S -B; preparation never connects to PostgreSQL.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import sysconfig
import time
from uuid import uuid4

# -S excludes startup hooks; add only this checkout to load the pure helpers.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Capture helper source directly: -B alone would still read timestamp-valid pyc.
_helper = REPO / "scripts/eval/kg_canary_runtime.py"
_spec = importlib.util.spec_from_file_location("scripts.eval.kg_canary_runtime", _helper)
rt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rt
_helper_bytes = _helper.read_bytes()
exec(compile(_helper_bytes, str(_helper), "exec"), rt.__dict__)

SCHEMA = "unitares.kg-snapshot-feasibility.v1"
PREDICATE = "tags && ARRAY['agent-adoption']::text[] AND status IS DISTINCT FROM 'archived' AND status IS DISTINCT FROM 'cold'"
MAX_RECORDS, MAX_BYTES = 200, 4 * 1024 * 1024
SETTINGS = {
    "default_transaction_read_only": "on", "search_path": "pg_catalog",
    "timezone": "UTC", "client_encoding": "UTF8", "row_security": "off",
    "statement_timeout": "10000", "lock_timeout": "2000",
    "idle_in_transaction_session_timeout": "30000",
    "application_name": "kg-readonly-feasibility-v1",
}
CLAIMS = {name: False for name in ("behavioral_evidence", "efficacy", "adoption", "scored_run_authorized", "v0_rescued")}


def encode_record(value):
    """Canonical JSON without rounding PostgreSQL JSONB numeric metadata."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + encode_record(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(encode_record(v) for v in value) + "]"
    if isinstance(value, Decimal):
        rt.require(value.is_finite(), "nonfinite record number")
        return str(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def canonical(records):
    rt.require(0 < len(records) <= MAX_RECORDS, "empty/overflow universe")
    ids = [row.get("id") for row in records]
    rt.require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids), "missing/duplicate IDs")
    data = b"".join((encode_record(row) + "\n").encode() for row in sorted(records, key=lambda r: r["id"]))
    rt.require(len(data) <= MAX_BYTES, "canonical corpus byte overflow")
    return data


def projection(records):
    result = []
    for row in records:
        rt.require(isinstance(row.get("summary"), str) and row["summary"].strip(), "invalid summary")
        rt.require(isinstance(row.get("details"), (str, type(None))) and isinstance(row.get("tags"), list)
                   and all(isinstance(t, str) for t in row["tags"]), "invalid details/tags")
        text = (row.get("details") or "") + "\n" + " ".join(row["tags"])
        rt.require(bool(text.strip()), "empty projection")
        result.append({"source_id": row["id"], "title": row["summary"], "text": text})
    return result


def write_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    rt.sync_dir(path.parent)


def read_corpus(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        rt.require(info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600, "corpus must be private")
        data = stream.read(MAX_BYTES + 1)
    rt.require(len(data) <= MAX_BYTES, "corpus byte overflow")
    records = [json.loads(line, parse_float=Decimal) for line in data.splitlines()]
    rt.require(canonical(records) == data, "corpus is not canonical")
    projection(records)
    return records, data


def validate_tasks(tasks, ids):
    rt.require(isinstance(tasks, list) and len(tasks) == 3, "exactly three frozen queries required")
    for task in tasks:
        rt.require(set(task) == {"question", "query", "material_source_ids", "objective_key"}, "unexpected task fields")
        rt.require(all(isinstance(task[k], str) and task[k].strip() for k in ("question", "query", "objective_key")), "empty task field")
        rt.require(len(task["query"]) <= 500, "query too long")
        sources = task["material_source_ids"]
        rt.require(isinstance(sources, list) and sources and len(set(sources)) == len(sources)
                   and set(sources) <= ids, "invalid material sources")
    rt.require(len({t["query"] for t in tasks}) == 3, "queries must be distinct")
    return tasks


def plan(artifacts, config, corpus=None, tasks_path=None):
    rt.private_dir(artifacts)
    db = rt.db_environment(config)
    write_bytes(artifacts / "db-binding.key", os.urandom(32))
    mode = "evaluate" if corpus else "capture"
    tasks, corpus_hash, projected_hash = [], None, None
    if corpus:
        records, data = read_corpus(corpus)
        tasks = validate_tasks(rt.load(tasks_path)["tasks"], {r["id"] for r in records})
        corpus_hash = hashlib.sha256(data).hexdigest()
        projected_hash = hashlib.sha256(json.dumps(projection(records), sort_keys=True).encode()).hexdigest()
    rt.require(bool(corpus) == bool(tasks_path), "evaluation needs both corpus and tasks")
    site = Path(sysconfig.get_path("purelib")).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    files = set(rt.code_files(REPO)) | set(rt.code_files(site)) | set(rt.code_files(stdlib, exclude_site=True))
    files.add(Path(sys.executable).resolve())
    framework = Path(sys.base_prefix) / "Python"
    if framework.is_file():
        files.add(framework.resolve())
    registry = Path.home() / ".local/state/unitares/experiments/kg-operation-reservations"
    registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    rt.private_dir(registry)
    operation_id = str(uuid4())
    document = {
        "schema": SCHEMA, "mode": mode, "operation_id": operation_id,
        "reservation": str(registry / (operation_id + ".json")), "artifacts": str(artifacts),
        "repo": str(REPO), "site": str(site), "stdlib_path": list(sys.path),
        "python": str(Path(sys.executable).resolve()), "python_version": sys.version,
        "files": {str(p): rt.digest(p) for p in sorted(files)},
        "db_binding": rt.db_binding(artifacts, db), "db_config": str(config.resolve()),
        "predicate": PREDICATE, "codec": "canonical_kg_universe_v1",
        "max_records": MAX_RECORDS, "max_canonical_bytes": MAX_BYTES, "max_sql_json_bytes": MAX_BYTES,
        "server_settings": SETTINGS, "transaction": "read-only repeatable-read",
        "transaction_timeout_s": 45, "automatic_retries": 0,
        "fts_queries": len(tasks), "lexical_queries": len(tasks), "operator": "OR", "k": 5,
        "corpus_sha256": corpus_hash, "projection_sha256": projected_hash, "tasks": tasks,
        "claims": CLAIMS,
    }
    rt.save_new(artifacts / "plan.json", document)
    return {"plan_sha256": rt.digest(artifacts / "plan.json"), "mode": mode}


def pinned_backend(connection):
    from src.db.mixins.knowledge_graph import KnowledgeGraphMixin

    class HeldConnection(KnowledgeGraphMixin):
        @asynccontextmanager
        async def acquire(self):
            yield connection

    return HeldConnection()


async def enumerate_universe(connection):
    # Check size before transferring full documents. Both queries share the RR snapshot.
    sizes = await connection.fetch(f"SELECT id, octet_length(to_jsonb(d)::text) AS bytes FROM knowledge.discoveries d WHERE {PREDICATE} ORDER BY id LIMIT 201")
    rt.require(0 < len(sizes) <= MAX_RECORDS, "empty/overflow universe")
    rt.require(sum(row["bytes"] for row in sizes) <= MAX_BYTES, "SQL corpus byte overflow")
    rows = await connection.fetch(f"SELECT to_jsonb(d) AS document FROM knowledge.discoveries d WHERE {PREDICATE} ORDER BY id LIMIT 201")
    rt.require(all(isinstance(row["document"], str) for row in rows), "raw JSONB text codec required")
    records = [json.loads(row["document"], parse_float=Decimal) for row in rows]
    data = canonical(records)
    rt.require(len(records) == len(sizes), "enumeration changed inside transaction")
    projection(records)
    return records, data


async def operate(connection, document, artifacts):
    from scripts.eval.kg_agent_adoption import lexical_search

    async with connection.transaction(isolation="repeatable_read", readonly=True):
        rt.require(await connection.fetchval("SHOW transaction_read_only") == "on", "transaction is writable")
        rt.require(await connection.fetchval("SHOW transaction_isolation") == "repeatable read", "wrong isolation")
        relation = await connection.fetchrow("SELECT relkind, relrowsecurity FROM pg_catalog.pg_class WHERE oid='knowledge.discoveries'::regclass")
        rt.require(relation and relation["relkind"] == b"r" and not relation["relrowsecurity"], "expected unfiltered physical table")
        records, data = await enumerate_universe(connection)
        fingerprint = hashlib.sha256(data).hexdigest()
        projected = projection(records)
        if document["mode"] == "evaluate":
            rt.require(fingerprint == document["corpus_sha256"], "corpus drift; enrollment stopped")
            rt.require(hashlib.sha256(json.dumps(projected, sort_keys=True).encode()).hexdigest() == document["projection_sha256"], "projection drift")
            validate_tasks(document["tasks"], {r["id"] for r in records})
        # Durable corpus precedes any evaluative query; invalid/overflow capture is never published.
        write_bytes(artifacts / "corpus.jsonl", data)
        result = {"schema": SCHEMA, "mode": document["mode"], "records": len(records), "bytes": len(data),
                  "corpus_sha256": fingerprint, "claims": CLAIMS, "queries": [],
                  "postgres_version": await connection.fetchval("SHOW server_version"),
                  "backend_pid": await connection.fetchval("SELECT pg_backend_pid()"),
                  "transaction_snapshot": str(await connection.fetchval("SELECT pg_current_snapshot()")),
                  "ordering_caveat": "Native rank/created_at ties have no final ID tie-breaker; returned order is retained."}
        backend = pinned_backend(connection)
        for index, task in enumerate(document["tasks"]):
            for arm in ("native_fts", "lexical_bm25"):
                start = time.monotonic()
                if arm == "native_fts":
                    rows = await backend.kg_full_text_search(task["query"], limit=5, operator="OR", tags=["agent-adoption"],
                                                            filters={"exclude_archived": True, "exclude_cold": True})
                    ids = [row["id"] for row in rows]
                    scores = [float(row["rank"]) for row in rows]
                else:
                    rows = lexical_search(projected, task["query"], limit=5)
                    ids, scores = [r["source_id"] for r in rows], [r["score"] for r in rows]
                rt.require(set(ids) <= {r["id"] for r in records}, "retrieval escaped captured universe")
                result["queries"].append({"step": index + 1, "arm": arm, "ids": ids, "scores": scores,
                                          "latency_ms": round((time.monotonic() - start) * 1000, 3),
                                          "material_ranks": {i: ids.index(i) + 1 if i in ids else None for i in task["material_source_ids"]}})
                rt.save_new(artifacts / f"query-{index + 1}-{arm}.json", result["queries"][-1])
        rt.save_new(artifacts / "result.json", result)
        return {"mode": document["mode"], "records": len(records), "corpus_sha256": fingerprint, "queries": len(result["queries"])}


async def connect_and_operate(db, document, artifacts):
    import asyncpg

    # One connection, no pool/application initialization and no reconnect path.
    async with asyncio.timeout(45):
        connection = await asyncpg.connect(db["DB_POSTGRES_URL"], timeout=10, server_settings=SETTINGS,
                                          passfile=str(artifacts / "disabled.pgpass"))
        try:
            return await operate(connection, document, artifacts)
        finally:
            await connection.close(timeout=3)


def validate_contract(document):
    rt.require(document["predicate"] == PREDICATE and document["server_settings"] == SETTINGS
               and document["max_records"] == MAX_RECORDS
               and document["max_canonical_bytes"] == document["max_sql_json_bytes"] == MAX_BYTES
               and document["transaction_timeout_s"] == 45 and document["transaction"] == "read-only repeatable-read"
               and document["codec"] == "canonical_kg_universe_v1"
               and document["operator"] == "OR" and document["k"] == 5 and document["automatic_retries"] == 0
               and document["claims"] == CLAIMS and document["fts_queries"] == document["lexical_queries"] == len(document["tasks"])
               and len(document["tasks"]) == (3 if document["mode"] == "evaluate" else 0), "operation contract changed")


def execute(plan_path, expected_sha, approval_path):
    document = rt.load(plan_path, expected_digest=expected_sha)
    rt.require(document["schema"] == SCHEMA and document["mode"] in ("capture", "evaluate"), "wrong operation")
    artifacts = Path(document["artifacts"])
    rt.private_dir(artifacts)
    rt.require(plan_path.resolve() == artifacts / "plan.json", "plan moved from reviewed artifact location")
    approval = rt.load(approval_path)
    rt.require(approval.get("approved") is True and approval.get("plan_sha256") == expected_sha
               and approval.get("scope") == "read_only_kg_" + document["mode"]
               and approval.get("review_session") and approval.get("operator_authorization_ref"), "exact independent approval required")
    rt.require(Path(sys.executable).resolve() == Path(document["python"]) and sys.version == document["python_version"], "interpreter changed")
    rt.require(document["repo"] == str(REPO), "checkout changed")
    rt.require(hashlib.sha256(_helper_bytes).hexdigest() == document["files"].get(str(_helper)), "loaded helper differs from sealed source")
    for path, expected in document["files"].items():
        rt.require(rt.digest(path) == expected, "sealed runtime changed")
    db = rt.db_environment(Path(document["db_config"]))
    rt.require(rt.db_binding(artifacts, db) == document["db_binding"], "DB target changed")
    validate_contract(document)
    rt.require(not (artifacts / "disabled.pgpass").exists(), "disabled passfile path must not exist")
    reservation = Path(document["reservation"])
    rt.private_dir(reservation.parent)
    rt.save_new(reservation, {"plan_sha256": expected_sha, "approval": approval, "automatic_retries": 0})
    # Clear ambient credentials and host bindings before any application import.
    os.environ.clear()
    os.environ.update(PYTHON_DOTENV_DISABLED="1", PATH=os.defpath, LANG="en_US.UTF-8")
    sys.path[:] = [*document["stdlib_path"], document["site"]]
    finder = rt.OriginFinder(document["files"])
    sys.meta_path[:] = [importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter, finder]
    try:
        result = asyncio.run(connect_and_operate(db, document, artifacts))
    except BaseException as exc:
        rt.save_new(artifacts / "outcome.json", {"status": "failed_no_retry", "exception_type": type(exc).__name__})
        raise
    rt.save_new(artifacts / "outcome.json", {"status": "completed", **result})
    return result


def main(argv=None):
    rt.require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "requires python -I -S -B")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    preparation = sub.add_parser("plan")
    preparation.add_argument("--artifacts", type=Path, required=True)
    preparation.add_argument("--db-config", type=Path, required=True)
    preparation.add_argument("--corpus", type=Path)
    preparation.add_argument("--tasks", type=Path)
    execution = sub.add_parser("execute")
    execution.add_argument("--plan", type=Path, required=True)
    execution.add_argument("--plan-sha256", required=True)
    execution.add_argument("--approval", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "plan":
        result = plan(args.artifacts.resolve(), args.db_config.resolve(), args.corpus, args.tasks)
    else:
        result = execute(args.plan.resolve(), args.plan_sha256, args.approval.resolve())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
