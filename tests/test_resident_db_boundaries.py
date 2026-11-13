"""Boundary ratchet and HTTP contract tests for resident DB/Core decoupling.

Enforces `docs/proposals/active/federation-component-boundaries-v0.md` §2.3 / §6
across Python client-side residents (`agents/{chronicler,sentinel,vigil,watcher,steward,common}`)
and verifies strict-mode authentication, error isolation, and validation on the
read-only Core `/v1/*` telemetry routes that replaced direct PostgreSQL reads.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

REPO_ROOT = Path(__file__).resolve().parent.parent

CLIENT_RESIDENT_DIRS = (
    "agents/chronicler",
    "agents/sentinel",
    "agents/vigil",
    "agents/watcher",
    "agents/steward",
    "agents/common",
)

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "src",
        "governance_core",
        "asyncpg",
        "psycopg2",
        "psycopg",
    }
)

FORBIDDEN_SERVER_SCRIPTS = (
    "retrieval_eval.py",
    "evaluate_phase_b_promotion.py",
)


def _iter_client_resident_py_files() -> list[Path]:
    """Return client-side `.py` files under `agents/*`, excluding server-loaded
    `routes.py` route packs and test directories."""
    files: list[Path] = []
    for rel_dir in CLIENT_RESIDENT_DIRS:
        base = REPO_ROOT / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel_parts = path.relative_to(base).parts
            if "tests" in rel_parts:
                continue
            if path.name == "routes.py":
                # `agents/*/routes.py` are server-side `UNITARES_ROUTE_PACKS`
                # loaded in-process by Core (`src/http_routes/packs.py`).
                continue
            files.append(path)
    return files


class TestResidentBoundaryRatchet:
    """Ratchet B1 (no `src.*` / `governance_core.*`) and B2 (no direct DB driver
    or server-local DB script subprocess) across Python client residents."""

    def test_no_forbidden_imports_in_client_residents(self):
        violations: list[str] = []
        files = _iter_client_resident_py_files()
        assert files, "expected client resident .py files to scan"

        for path in files:
            rel = path.relative_to(REPO_ROOT)
            tree = ast.parse(path.read_text(), filename=str(rel))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if root in FORBIDDEN_IMPORT_ROOTS:
                            violations.append(f"{rel}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    if root in FORBIDDEN_IMPORT_ROOTS:
                        violations.append(f"{rel}:{node.lineno} from {node.module} import ...")

        assert violations == [], (
            "Client-side resident modules must not import server internals or DB drivers:\n"
            + "\n".join(violations)
        )

    def test_no_server_db_script_launches_in_client_residents(self):
        def _docstring_nodes(tree: ast.AST) -> set[int]:
            ids: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ) and node.body:
                    first = node.body[0]
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        ids.add(id(first.value))
            return ids

        violations: list[str] = []
        for path in _iter_client_resident_py_files():
            rel = path.relative_to(REPO_ROOT)
            tree = ast.parse(path.read_text(), filename=str(rel))
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    for script_name in FORBIDDEN_SERVER_SCRIPTS:
                        if script_name in node.value:
                            violations.append(
                                f"{rel}:{node.lineno} string constant references {script_name!r}"
                            )

        assert violations == [], (
            "Client-side residents must call Core HTTP/REST routes rather than "
            "spawning server-local DB scripts:\n" + "\n".join(violations)
        )


def _build_telemetry_app() -> Starlette:
    from src.http_routes.lease_plane_telemetry import (
        http_get_lease_alarm_events,
        http_get_lease_phase_b,
    )
    from src.http_routes.metrics_api import http_get_metrics_snapshot

    return Starlette(
        routes=[
            Route("/v1/metrics/snapshot", http_get_metrics_snapshot, methods=["GET"]),
            Route(
                "/v1/lease-plane/alarm-events",
                http_get_lease_alarm_events,
                methods=["GET"],
            ),
            Route(
                "/v1/lease-plane/phase-b/{surface_kind}",
                http_get_lease_phase_b,
                methods=["GET"],
            ),
        ]
    )


class TestNewRoutesStrictAuth:
    """Under `UNITARES_REST_STRICT=1`, all three new `/v1/*` routes must enforce
    `UNITARES_MCP_BEARER_TOKENS` and fail closed even on loopback."""

    ROUTES = (
        "/v1/metrics/snapshot",
        "/v1/lease-plane/alarm-events",
        "/v1/lease-plane/phase-b/dialectic",
    )

    @pytest.mark.asyncio
    async def test_strict_mode_rejects_unauthenticated_wrong_and_legacy_tokens(
        self, monkeypatch
    ):
        from src import lease_plane_phase_b, lease_plane_telemetry
        from src.fleet_metrics import snapshot as snapshot_mod
        from src.lease_plane_phase_b import CriterionResult, EvaluationReport

        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "mcp-strict-valid")
        monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "legacy-http-only")

        async def _fake_snapshot(**_kw):
            return {
                "generated_at": "2026-11-12T00:00:00+00:00",
                "window": "7d",
                "metrics": {"agents.active.7d": 5.0},
                "finding_counts_7d": {},
                "errors": {},
            }

        async def _fake_alarms(*, since=None):
            return {
                "polled_at": "2026-11-12T00:00:00+00:00",
                "since": since.isoformat() if since else None,
                "ad_hoc": [],
                "deprecation_batches": [],
                "conflict_batches": [],
            }

        async def _fake_phase_b(*, surface_kind, window_days, accept_drill_evidence):
            return EvaluationReport(
                surface_kind=surface_kind,
                evaluated_at="2026-11-12T00:00:00+00:00",
                window_days=window_days,
                criteria=[
                    CriterionResult(
                        number=1,
                        name="zero_lease_error",
                        status="FAIL",
                        detail="insufficient window",
                    )
                ],
                promotable=False,
            )

        monkeypatch.setattr(snapshot_mod, "collect_metrics_snapshot", _fake_snapshot)
        monkeypatch.setattr(lease_plane_telemetry, "fetch_lease_alarm_events", _fake_alarms)
        monkeypatch.setattr(lease_plane_phase_b, "evaluate_phase_b_async", _fake_phase_b)

        app = _build_telemetry_app()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8767") as client:
            for route in self.ROUTES:
                r_none = await client.get(route)
                assert r_none.status_code == 401, f"{route} allowed unauthenticated request"

                r_wrong = await client.get(
                    route, headers={"Authorization": "Bearer wrong-token"}
                )
                assert r_wrong.status_code == 401, f"{route} allowed wrong bearer token"

                r_legacy = await client.get(
                    route, headers={"Authorization": "Bearer legacy-http-only"}
                )
                assert r_legacy.status_code == 401, (
                    f"{route} accepted UNITARES_HTTP_API_TOKEN under UNITARES_REST_STRICT=1"
                )

                r_valid = await client.get(
                    route, headers={"Authorization": "Bearer mcp-strict-valid"}
                )
                assert r_valid.status_code == 200, (
                    f"{route} rejected valid UNITARES_MCP_BEARER_TOKENS: {r_valid.text}"
                )

    @pytest.mark.asyncio
    async def test_strict_mode_fails_closed_when_mcp_bearer_allowlist_empty(
        self, monkeypatch
    ):
        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
        monkeypatch.delenv("UNITARES_MCP_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "legacy-http-only")

        app = _build_telemetry_app()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8767") as client:
            for route in self.ROUTES:
                r = await client.get(
                    route, headers={"Authorization": "Bearer legacy-http-only"}
                )
                assert r.status_code == 401, f"{route} did not fail closed under strict mode"


class TestMetricsSnapshotIsolation:
    """Per-query savepoints keep one broken SQL query from blanking the snapshot."""

    @pytest.mark.asyncio
    async def test_per_query_savepoint_isolates_single_broken_metric(self):
        from src.fleet_metrics.snapshot import collect_snapshot_from_conn

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self, **_kw):
                yield

            async def execute(self, _sql: str):
                return None

            async def fetchval(self, sql: str):
                if sql == "SELECT now()":
                    return datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
                if "knowledge.discoveries" in sql:
                    raise RuntimeError("AGE graph temporary error")
                return 7

            async def fetch(self, _sql: str):
                return [
                    {"event_type": "sentinel_finding", "cnt": 4},
                    {"event_type": "sentinel_alarm_finding", "cnt": 2},
                ]

        snap = await collect_snapshot_from_conn(_FakeConn())
        assert snap["metrics"]["kg.entries.count"] is None
        assert "kg.entries.count" in snap["errors"]
        assert snap["metrics"]["agents.active.7d"] == 7.0
        assert snap["metrics"]["checkins.7d"] == 7.0
        assert snap["finding_counts_7d"] == {
            "sentinel_finding": 4,
            "sentinel_alarm_finding": 2,
        }


class TestLeasePlaneRoutesValidation:
    """Input validation for `since`, `surface_kind`, `window_days`, and `promotable=False`."""

    @pytest.mark.asyncio
    async def test_alarm_events_rejects_naive_or_malformed_since(self, monkeypatch):
        from src import lease_plane_telemetry

        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "valid-tok")

        async def _fake_alarms(*, since=None):
            return {
                "polled_at": "2026-11-12T00:00:00+00:00",
                "since": since.isoformat() if since else None,
                "ad_hoc": [],
                "deprecation_batches": [],
                "conflict_batches": [],
            }

        monkeypatch.setattr(lease_plane_telemetry, "fetch_lease_alarm_events", _fake_alarms)
        app = _build_telemetry_app()
        headers = {"Authorization": "Bearer valid-tok"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r_naive = await client.get(
                "/v1/lease-plane/alarm-events",
                params={"since": "2026-05-05T09:00:00"},
                headers=headers,
            )
            assert r_naive.status_code == 400

            r_bad = await client.get(
                "/v1/lease-plane/alarm-events",
                params={"since": "not-an-iso-date"},
                headers=headers,
            )
            assert r_bad.status_code == 400

            r_z = await client.get(
                "/v1/lease-plane/alarm-events",
                params={"since": "2026-05-05T09:00:00Z"},
                headers=headers,
            )
            assert r_z.status_code == 200
            assert r_z.json()["since"] == "2026-05-05T09:00:00+00:00"

    @pytest.mark.asyncio
    async def test_phase_b_route_validates_surface_kind_and_window_days(self, monkeypatch):
        from src import lease_plane_phase_b
        from src.lease_plane_phase_b import (
            CriterionResult,
            EvaluationReport,
            UnknownSurfaceKindError,
        )

        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "valid-tok")

        async def _fake_eval(*, surface_kind, window_days, accept_drill_evidence):
            if surface_kind not in {"dialectic", "resident"}:
                raise UnknownSurfaceKindError(f"unknown surface_kind {surface_kind!r}")
            return EvaluationReport(
                surface_kind=surface_kind,
                evaluated_at="2026-11-12T00:00:00+00:00",
                window_days=window_days,
                criteria=[
                    CriterionResult(
                        number=1,
                        name="zero_lease_error",
                        status="FAIL",
                        detail="window incomplete",
                    )
                ],
                promotable=False,
            )

        monkeypatch.setattr(lease_plane_phase_b, "evaluate_phase_b_async", _fake_eval)
        app = _build_telemetry_app()
        headers = {"Authorization": "Bearer valid-tok"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r_kind = await client.get("/v1/lease-plane/phase-b/bogus", headers=headers)
            assert r_kind.status_code == 400

            r_zero = await client.get(
                "/v1/lease-plane/phase-b/dialectic",
                params={"window_days": "0"},
                headers=headers,
            )
            assert r_zero.status_code == 400

            r_nan = await client.get(
                "/v1/lease-plane/phase-b/dialectic",
                params={"window_days": "abc"},
                headers=headers,
            )
            assert r_nan.status_code == 400

            r_ok = await client.get(
                "/v1/lease-plane/phase-b/dialectic",
                params={"window_days": "14"},
                headers=headers,
            )
            assert r_ok.status_code == 200
            body = r_ok.json()
            assert body["success"] is True
            assert body["promotable"] is False
            assert body["criteria"][0]["status"] == "FAIL"


class TestSentinelBlindPoller:
    """Repeated `/v1/lease-plane/alarm-events` failures surface a high-severity
    `poller_blind` finding without advancing the event cursor."""

    @pytest.mark.asyncio
    async def test_three_consecutive_failures_emit_poller_blind_once_then_recover(
        self, monkeypatch
    ):
        from agents.sentinel import agent as sentinel_mod

        agent = sentinel_mod.SentinelAgent.__new__(sentinel_mod.SentinelAgent)
        agent.mcp_url = "http://127.0.0.1:8767/mcp/"
        agent.agent_uuid = "sentinel-test-uuid"

        persisted: dict[str, Any] = {
            "forced_release_alarm": {
                "last_event_ts": "2026-11-12T00:00:00+00:00",
            }
        }
        agent.load_state = lambda: json.loads(json.dumps(persisted))
        agent.save_state = lambda s: (persisted.clear(), persisted.update(json.loads(json.dumps(s))))

        emitted: list[dict[str, Any]] = []
        monkeypatch.setattr(
            sentinel_mod,
            "post_finding",
            lambda **kw: emitted.append(kw),
        )
        monkeypatch.setattr(sentinel_mod, "notify", lambda *_a, **_kw: None)

        def _fail_poll(_api_url, _cursor):
            raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr(sentinel_mod, "_poll_sync_forced_release", _fail_poll)

        # Failures 1 and 2: recorded, cursor untouched, no blind-poller alarm yet.
        await agent._emit_forced_release_alarms()
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 2
        assert persisted["forced_release_alarm"]["last_event_ts"] == "2026-11-12T00:00:00+00:00"
        assert emitted == []

        # Failure 3: hits _BLIND_POLLER_THRESHOLD -> emits high-severity poller_blind!
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 3
        assert len(emitted) == 1
        assert emitted[0]["severity"] == "high"
        assert emitted[0]["extra"]["alarm_kind"] == "poller_blind"

        # Failure 4: does not spam duplicate poller_blind every cycle.
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 4
        assert len(emitted) == 1

        # Recovery on next poll resets consecutive_failures to 0.
        new_ts = datetime(2026, 11, 12, 1, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            sentinel_mod,
            "_poll_sync_forced_release",
            lambda _api_url, _cursor: ([], new_ts),
        )
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 0
        assert persisted["forced_release_alarm"]["last_event_ts"] == new_ts.isoformat()


class TestVigilRemoteRetrievalEval:
    """Parity between `agents.vigil.remote_eval` and `scripts.eval.metrics`."""

    @pytest.mark.parametrize(
        "ranked,relevant",
        [
            (["a", "b", "c"], {"a"}),
            (["x", "a", "b", "c"], {"a", "c"}),
            (["x", "y", "z"], {"a", "b"}),
            ([], {"a"}),
            (["a", "b"], set()),
        ],
    )
    def test_ranking_math_matches_canonical_eval_metrics(
        self, ranked: list[str], relevant: set[str]
    ):
        from agents.vigil import remote_eval
        from scripts.eval import metrics as server_metrics

        for k in (1, 3, 10, 20):
            assert remote_eval.ndcg_at_k(ranked, relevant, k) == pytest.approx(
                server_metrics.ndcg_at_k(ranked, relevant, k), abs=1e-12
            )
            assert remote_eval.recall_at_k(ranked, relevant, k) == pytest.approx(
                server_metrics.recall_at_k(ranked, relevant, k), abs=1e-12
            )
        assert remote_eval.mrr(ranked, relevant) == pytest.approx(
            server_metrics.mrr(ranked, relevant), abs=1e-12
        )

    def test_run_remote_retrieval_eval_produces_vigil_compatible_shape(self, tmp_path: Path):
        from agents.vigil.agent import _normalize_eval_metrics
        from agents.vigil.remote_eval import run_remote_retrieval_eval

        labels = tmp_path / "labels.json"
        labels.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pairs": [
                        {"query": "q_hit", "relevant_ids": ["doc-1"]},
                        {"query": "q_miss", "relevant_ids": ["doc-2"]},
                    ],
                }
            )
        )

        def _fake_search(query: str, limit: int) -> dict[str, Any]:
            assert limit == 20
            if query == "q_hit":
                return {"success": True, "discoveries": [{"id": "doc-1"}, {"id": "other"}]}
            return {"success": True, "discoveries": [{"id": "other"}]}

        report = run_remote_retrieval_eval(labels_path=labels, search_fn=_fake_search)
        assert report["corpus"] == {
            "path": "labels.json",
            "pair_count": 2,
            "scored_pair_count": 2,
            "schema_version": 1,
        }
        assert report["aggregate"]["flat_miss_count"] == 1
        assert report["aggregate"]["flat_miss_rate"] == 0.5

        flat = _normalize_eval_metrics(report)
        assert flat["nDCG@10"] == pytest.approx(0.5, abs=1e-6)
        assert flat["Recall@20"] == pytest.approx(0.5, abs=1e-6)
        assert flat["MRR"] == pytest.approx(0.5, abs=1e-6)
        assert "latency_p50" in flat
        assert "latency_p95" in flat


class TestServerTelemetryPostgresRoundTrip:
    """Verify the new server-owned `asyncpg` snapshot and Phase-B queries execute
    against the real test PostgreSQL schema without SQL or parameter type errors."""

    @pytest.mark.asyncio
    async def test_snapshot_and_phase_b_queries_execute_against_test_db(self):
        asyncpg = pytest.importorskip("asyncpg")
        from tests.test_db_utils import (
            TEST_DB_URL,
            can_connect_to_test_db,
            ensure_test_database_schema,
        )

        if not can_connect_to_test_db():
            pytest.skip("governance_test database not available")

        from src.fleet_metrics.snapshot import collect_snapshot_from_conn
        from src.lease_plane_phase_b import KNOWN_SURFACE_KINDS, evaluate_phase_b_from_conn

        await ensure_test_database_schema()
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            snap = await collect_snapshot_from_conn(conn)
            assert snap["errors"] == {}, f"snapshot SQL errors: {snap['errors']}"
            assert set(snap["metrics"].keys()) == {
                "agents.active.7d",
                "kg.entries.count",
                "checkins.7d",
                "governance.coherence.mean.7d",
                "governance.risk.mean.7d",
                "governance.guide.7d",
                "governance.pause.7d",
            }
            assert isinstance(snap["finding_counts_7d"], dict)

            for sk in ("dialectic", "file"):
                assert sk in KNOWN_SURFACE_KINDS
                report = await evaluate_phase_b_from_conn(conn, surface_kind=sk, window_days=14)
                assert report.surface_kind == sk
                assert len(report.criteria) == 6
        finally:
            await conn.close()
