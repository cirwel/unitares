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
        import importlib

        snapshot_mod = importlib.import_module("src.fleet_metrics.snapshot")
        lease_plane_telemetry = importlib.import_module("src.lease_plane_telemetry")
        lease_plane_phase_b = importlib.import_module("src.lease_plane_phase_b")
        CriterionResult = lease_plane_phase_b.CriterionResult
        EvaluationReport = lease_plane_phase_b.EvaluationReport

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
        import importlib

        lease_plane_telemetry = importlib.import_module("src.lease_plane_telemetry")

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
        import importlib

        lease_plane_phase_b = importlib.import_module("src.lease_plane_phase_b")
        CriterionResult = lease_plane_phase_b.CriterionResult
        EvaluationReport = lease_plane_phase_b.EvaluationReport
        UnknownSurfaceKindError = lease_plane_phase_b.UnknownSurfaceKindError

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
    """Repeated `/v1/lease-plane/alarm-events` failures notify locally once and
    retry `post_finding_result` until Core accepts/dedupes the finding."""

    @pytest.mark.asyncio
    async def test_blind_poller_retries_until_delivered_notifies_once_then_recovers(
        self, monkeypatch
    ):
        from agents.common.findings import DELIVERED, FAILED
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
        call_threads: list[int] = []
        outcomes = [FAILED, DELIVERED]
        import threading

        loop_thread_id = threading.get_ident()

        def _fake_post_finding_result(**kw):
            call_threads.append(threading.get_ident())
            emitted.append(kw)
            return outcomes.pop(0) if outcomes else DELIVERED

        notifications: list[tuple[str, str]] = []
        monkeypatch.setattr(sentinel_mod, "post_finding_result", _fake_post_finding_result)
        monkeypatch.setattr(
            sentinel_mod, "notify", lambda title, msg: notifications.append((title, msg))
        )

        def _fail_poll(_api_url, _cursor):
            raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr(sentinel_mod, "_poll_sync_forced_release", _fail_poll)

        # Failures 1 and 2: recorded, cursor untouched, no blind-poller alarm yet.
        await agent._emit_forced_release_alarms()
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 2
        assert persisted["forced_release_alarm"]["last_event_ts"] == "2026-11-12T00:00:00+00:00"
        assert emitted == []
        assert notifications == []

        # Failure 3: hits _BLIND_POLLER_THRESHOLD -> notifies locally once;
        # first post_finding_result fails (e.g. Core unreachable/401), so not marked delivered!
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 3
        assert len(notifications) == 1
        assert len(emitted) == 1
        assert emitted[0]["severity"] == "high"
        assert emitted[0]["extra"]["alarm_kind"] == "poller_blind"
        assert not persisted["forced_release_alarm"].get("blind_alarm_delivered", False)

        # Failure 4: does NOT re-page local notify, DOES retry post_finding_result -> DELIVERED!
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 4
        assert len(notifications) == 1
        assert len(emitted) == 2
        assert persisted["forced_release_alarm"]["blind_alarm_delivered"] is True

        # Failure 5: already delivered + notified -> neither re-fires.
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 5
        assert len(notifications) == 1
        assert len(emitted) == 2
        assert len(call_threads) == 2
        assert all(tid != loop_thread_id for tid in call_threads), (
            "blind-poller post_finding_result retries must run in executor off the event loop"
        )

        # Recovery on next poll resets consecutive_failures and delivery flags.
        new_ts = datetime(2026, 11, 12, 1, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            sentinel_mod,
            "_poll_sync_forced_release",
            lambda _api_url, _cursor: ([], new_ts),
        )
        await agent._emit_forced_release_alarms()
        assert persisted["forced_release_alarm"]["consecutive_failures"] == 0
        assert "blind_alarm_delivered" not in persisted["forced_release_alarm"]
        assert "blind_alarm_notified" not in persisted["forced_release_alarm"]
        assert persisted["forced_release_alarm"]["last_event_ts"] == new_ts.isoformat()


class TestBearerFallbackAndUrlNormalization:
    """Covers `UNITARES_REST_STRICT=0` 401 bearer fallback + winner caching,
    reverse-proxy path-prefix preservation, and `UNITARES_METRICS_URL` scoping."""

    def test_bearer_fallback_supports_both_strict_and_loosened_postures_and_caches_winner(
        self, monkeypatch
    ):
        from agents.common.http_client import (
            clear_bearer_preference_cache,
            send_with_bearer_fallback,
        )

        clear_bearer_preference_cache()

        # Client has BOTH tokens set and NO local UNITARES_REST_STRICT hint.
        monkeypatch.delenv("UNITARES_REST_STRICT", raising=False)
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", "mcp-tok")
        monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "http-tok")

        seen_auth: list[str] = []
        try:
            # 1) Remote strict Core (`UNITARES_REST_STRICT=1` -> accepts only `mcp-tok`):
            # non-loopback URL sends `mcp-tok` on attempt 1 with zero 401s and never
            # sends `http-tok`.
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda req: (
                        seen_auth.append(req.headers.get("authorization", ""))
                        or (
                            httpx.Response(200, json={"ok": True})
                            if req.headers.get("authorization") == "Bearer mcp-tok"
                            else httpx.Response(401, json={"error": "Unauthorized"})
                        )
                    )
                )
            ) as strict_client:
                r_strict = send_with_bearer_fallback(
                    lambda hdrs: strict_client.get(
                        "https://gov.example/unitares/v1/probe", headers=hdrs
                    ),
                    base_url="https://gov.example/unitares",
                )
                assert r_strict.status_code == 200
                assert seen_auth == ["Bearer mcp-tok"]

            # 2) Remote loosened Core (`UNITARES_REST_STRICT=0` -> accepts only `http-tok`):
            # tries `mcp-tok` first, retries once on 401 with `http-tok`, and caches
            # `http-tok` for subsequent calls in-process.
            seen_auth.clear()
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda req: (
                        seen_auth.append(req.headers.get("authorization", ""))
                        or (
                            httpx.Response(200, json={"ok": True})
                            if req.headers.get("authorization") == "Bearer http-tok"
                            else httpx.Response(401, json={"error": "Unauthorized"})
                        )
                    )
                )
            ) as loose_client:
                r1 = send_with_bearer_fallback(
                    lambda hdrs: loose_client.get(
                        "https://loose.example/v1/probe", headers=hdrs
                    ),
                    base_url="https://loose.example",
                )
                assert r1.status_code == 200
                assert seen_auth == ["Bearer mcp-tok", "Bearer http-tok"]

                r2 = send_with_bearer_fallback(
                    lambda hdrs: loose_client.get(
                        "https://loose.example/v1/probe", headers=hdrs
                    ),
                    base_url="https://loose.example",
                )
                assert r2.status_code == 200
                assert seen_auth == [
                    "Bearer mcp-tok",
                    "Bearer http-tok",
                    "Bearer http-tok",
                ]
        finally:
            clear_bearer_preference_cache()

    @pytest.mark.asyncio
    async def test_async_bearer_fallback_against_real_access_gate(self, monkeypatch):
        from starlette.responses import JSONResponse
        from agents.common.http_client import (
            async_send_with_bearer_fallback,
            clear_bearer_preference_cache,
        )
        from src.http_routes import access

        clear_bearer_preference_cache()
        calls = 0

        async def _endpoint(request):
            nonlocal calls
            calls += 1
            with patch.dict(
                os.environ,
                {
                    "UNITARES_MCP_BEARER_TOKENS": "mcp-tok",
                    "UNITARES_REST_STRICT": "1",
                    "UNITARES_HTTP_API_TOKEN": "http-tok",
                },
                clear=False,
            ):
                if not access._check_http_auth(request, http_api_token="http-tok"):
                    return access._http_unauthorized()
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/probe", _endpoint)])
        monkeypatch.delenv("UNITARES_REST_STRICT", raising=False)
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", "mcp-tok")
        monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "http-tok")

        try:
            transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 54321))
            # Remote strict host succeeds on first try with `mcp-tok`.
            async with httpx.AsyncClient(
                transport=transport, base_url="https://gov.example"
            ) as remote_client:
                r_remote = await async_send_with_bearer_fallback(
                    lambda hdrs: remote_client.get("/v1/probe", headers=hdrs),
                    base_url="https://gov.example",
                )
                assert r_remote.status_code == 200
                assert calls == 1

            # Loopback host against strict Core retries once on 401 (`http-tok` -> `mcp-tok`)
            # and caches `mcp-tok` for the second call.
            calls = 0
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost:8767"
            ) as local_client:
                r1 = await async_send_with_bearer_fallback(
                    lambda hdrs: local_client.get("/v1/probe", headers=hdrs),
                    base_url="http://localhost:8767",
                )
                assert r1.status_code == 200
                assert calls == 2

                r2 = await async_send_with_bearer_fallback(
                    lambda hdrs: local_client.get("/v1/probe", headers=hdrs),
                    base_url="http://localhost:8767",
                )
                assert r2.status_code == 200
                assert calls == 3
        finally:
            clear_bearer_preference_cache()

    def test_url_normalization_preserves_reverse_proxy_prefix_and_scopes_metrics_url(
        self, monkeypatch
    ):
        from agents.chronicler import scrapers
        from agents.common.http_client import resolve_governance_base_url

        assert resolve_governance_base_url("https://host/unitares") == "https://host/unitares"
        assert resolve_governance_base_url("https://host/unitares/") == "https://host/unitares"
        assert resolve_governance_base_url("https://host/unitares/mcp/") == "https://host/unitares"
        assert (
            resolve_governance_base_url("https://host/unitares/v1/tools/call")
            == "https://host/unitares"
        )
        assert (
            resolve_governance_base_url("https://host/unitares/api/findings")
            == "https://host/unitares"
        )

        # UNITARES_METRICS_URL must NOT hijack Sentinel/Vigil's governance base URL...
        monkeypatch.delenv("GOV_REST_URL", raising=False)
        monkeypatch.setenv("UNITARES_METRICS_URL", "https://metrics-host/unitares")
        monkeypatch.setenv("MCP_SERVER_URL", "https://core-host:8767/mcp/")
        assert resolve_governance_base_url() == "https://core-host:8767"

        # ...while Chronicler's snapshot fetcher DOES honor UNITARES_METRICS_URL + prefix!
        scrapers.clear_snapshot_cache()
        seen_urls: list[str] = []

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def get(self, url, **_kw):
                seen_urls.append(url)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "metrics": {"agents.active.7d": 3.0},
                        "finding_counts_7d": {},
                        "errors": {},
                    },
                )

        with patch("agents.chronicler.scrapers.httpx.Client", return_value=_FakeClient()):
            snap = scrapers._fetch_metrics_snapshot()
        assert seen_urls == ["https://metrics-host/unitares/v1/metrics/snapshot"]
        assert snap["metrics"]["agents.active.7d"] == 3.0
        scrapers.clear_snapshot_cache()


class TestSitrepExactWindowAndUrl:
    """SitrepGenerator honors configured `mcp_url` and queries exact `since`."""

    @pytest.mark.asyncio
    async def test_sitrep_honors_mcp_url_and_exact_fractional_or_long_window(
        self, monkeypatch
    ):
        import importlib
        from agents.sentinel.agent import FleetState, SitrepGenerator
        from src.http_routes.telemetry import http_lifecycle_recent

        audit_db = importlib.import_module("src.audit_db")
        captured_queries: list[dict[str, Any]] = []

        async def _fake_query_audit(**kw):
            captured_queries.append(kw)
            return []

        monkeypatch.setattr(audit_db, "query_audit_events_async", _fake_query_audit)
        monkeypatch.delenv("GOV_REST_URL", raising=False)
        monkeypatch.delenv("MCP_SERVER_URL", raising=False)
        monkeypatch.setenv("UNITARES_METRICS_URL", "http://wrong-metrics-host:9999")
        monkeypatch.setenv("UNITARES_REST_STRICT", "1")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "strict-tok")
        monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", "strict-tok")

        app = Starlette(routes=[Route("/v1/lifecycle/recent", http_lifecycle_recent)])
        seen_request_urls: list[str] = []

        real_async_client = httpx.AsyncClient

        def _patched_async_client(*_a, **kw):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
            client = real_async_client(transport=transport, timeout=kw.get("timeout", 15.0))
            orig_get = client.get

            async def _recording_get(url, *args, **kwargs):
                seen_request_urls.append(str(url))
                return await orig_get(url, *args, **kwargs)

            client.get = _recording_get  # type: ignore[method-assign]
            return client

        fleet = FleetState()
        sitrep = SitrepGenerator(fleet, mcp_url="http://core-host:8767/mcp/")

        with patch("httpx.AsyncClient", side_effect=_patched_async_client):
            before = datetime.now(timezone.utc)
            out_short = await sitrep.generate(hours=0.25)
            out_long = await sitrep.generate(hours=200.0)

        assert "unavailable" not in out_short
        assert "unavailable" not in out_long
        assert seen_request_urls == [
            "http://core-host:8767/v1/lifecycle/recent",
            "http://core-host:8767/v1/lifecycle/recent",
        ]
        assert len(captured_queries) == 2

        since_short = datetime.fromisoformat(captured_queries[0]["start_time"])
        delta_short_min = (before - since_short).total_seconds() / 60.0
        assert 14.0 <= delta_short_min <= 16.0, f"expected ~15m window, got {delta_short_min}m"

        since_long = datetime.fromisoformat(captured_queries[1]["start_time"])
        delta_long_h = (before - since_long).total_seconds() / 3600.0
        assert 199.5 <= delta_long_h <= 200.5, f"expected ~200h window, got {delta_long_h}h"


class TestVigilRemoteRetrievalEval:
    """Parity between `agents.vigil.remote_eval` and `scripts.eval.metrics`,
    plus Core-sourced ranker config tag selection."""

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
        from agents.vigil.agent import _derive_eval_config_tag, _normalize_eval_metrics
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
                return {
                    "success": True,
                    "search_mode_used": "hybrid_rrf",
                    "discoveries": [{"id": "doc-1"}, {"id": "other"}],
                }
            return {
                "success": True,
                "search_mode_used": "hybrid_rrf",
                "discoveries": [{"id": "other"}],
            }

        report = run_remote_retrieval_eval(
            labels_path=labels,
            search_fn=_fake_search,
            config_fn=lambda: {
                "embedding_model": "bge-m3",
                "hybrid": True,
                "graph_expand": False,
                "rerank": False,
            },
        )
        assert report["corpus"] == {
            "path": "labels.json",
            "pair_count": 2,
            "scored_pair_count": 2,
            "schema_version": 1,
        }
        assert report["config"]["hybrid"] is True
        assert report["config"]["rerank"] is False
        assert report["config"]["embedding_model"] == "bge-m3"
        assert report["config"]["config_source"] == "core"
        assert report["config"]["search_modes_observed"] == ["hybrid_rrf"]
        assert _derive_eval_config_tag(report["config"]) == "hybrid_rrf"
        assert report["aggregate"]["flat_miss_count"] == 1
        assert report["aggregate"]["flat_miss_rate"] == 0.5

        flat = _normalize_eval_metrics(report)
        assert flat["nDCG@10"] == pytest.approx(0.5, abs=1e-6)
        assert flat["Recall@20"] == pytest.approx(0.5, abs=1e-6)
        assert flat["MRR"] == pytest.approx(0.5, abs=1e-6)
        assert "latency_p50" in flat
        assert "latency_p95" in flat

    @pytest.mark.asyncio
    async def test_vigil_eval_step_picks_core_hybrid_baseline_when_local_env_unset(
        self, tmp_path: Path, monkeypatch
    ):
        from agents.vigil.agent import VigilAgent

        for k in (
            "UNITARES_EMBEDDING_MODEL",
            "UNITARES_ENABLE_HYBRID",
            "UNITARES_ENABLE_GRAPH_EXPANSION",
            "UNITARES_ENABLE_RELATED_EXPANSION",
            "UNITARES_ENABLE_RERANKER",
        ):
            monkeypatch.delenv(k, raising=False)

        (tmp_path / "baseline_2026-04-20_hybrid_rrf.json").write_text(
            json.dumps({"aggregate": {"ndcg@10": {"mean": 0.80}}})
        )
        (tmp_path / "baseline_2026-04-20_default.json").write_text(
            json.dumps({"aggregate": {"ndcg@10": {"mean": 0.40}}})
        )

        observed_urls: list[str | None] = []

        def _fake_eval(base_url: str | None = None) -> dict[str, Any]:
            observed_urls.append(base_url)
            return {
                "config": {
                    "embedding_model": "bge-m3",
                    "hybrid": True,
                    "graph_expand": False,
                    "rerank": False,
                    "config_source": "core",
                },
                "aggregate": {"ndcg@10": {"mean": 0.78}},
            }

        vigil = VigilAgent(mcp_url="http://core-host:8767/mcp/", with_eval=True)
        with (
            patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path),
            patch("agents.vigil.agent._run_eval_subprocess", side_effect=_fake_eval),
        ):
            result = await vigil._run_eval_step()

        assert observed_urls == ["http://core-host:8767/mcp/"]
        assert result["ran"] is True
        assert result["config_tag"] == "hybrid_rrf"
        assert result["baseline"] == "baseline_2026-04-20_hybrid_rrf.json"
        assert result["regression"] is False

    @pytest.mark.asyncio
    async def test_health_check_retrieval_config_round_trips_through_remote_eval_and_503_fallback(
        self, tmp_path: Path, monkeypatch
    ):
        """End-to-end contract: `get_health_check_data` populates
        `checks.knowledge_graph.retrieval_config` in both lite and full modes,
        `run_remote_retrieval_eval` parses that exact wire shape over HTTP, and
        when `/health/deep` returns 503 during warmup, observed
        `search_mode_used='hybrid_rrf_graph'` still preserves `graph_expand=True`
        (`hybrid_graph`)."""
        from unittest.mock import AsyncMock, MagicMock
        from agents.vigil.agent import _derive_eval_config_tag
        from agents.vigil.remote_eval import run_remote_retrieval_eval
        from src.services.runtime_queries import get_health_check_data

        monkeypatch.setenv("UNITARES_EMBEDDING_MODEL", "bge-m3")
        monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
        monkeypatch.setenv("UNITARES_ENABLE_RELATED_EXPANSION", "1")
        monkeypatch.setenv("UNITARES_ENABLE_RERANKER", "0")

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True
        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with (
            patch("src.calibration.calibration_checker", mock_cal),
            patch("src.telemetry.telemetry_collector", MagicMock()),
            patch("src.audit_log.audit_logger", mock_audit),
            patch("src.db.get_db", return_value=mock_db),
            patch(
                "src.calibration_db.calibration_health_check_async",
                new_callable=AsyncMock,
                return_value={"status": "healthy", "backend": "postgres"},
            ),
            patch(
                "src.audit_db.audit_health_check_async",
                new_callable=AsyncMock,
                return_value={"status": "healthy", "backend": "postgres"},
            ),
            patch("src.cache.is_redis_available", return_value=False),
            patch("src.embeddings.embeddings_available", return_value=True),
        ):
            full_health = await get_health_check_data({"lite": False})
            lite_health = await get_health_check_data({"lite": True})

        expected_cfg = {
            "embedding_model": "bge-m3",
            "hybrid": True,
            "graph_expand": True,
            "rerank": False,
        }
        assert full_health["checks"]["knowledge_graph"]["retrieval_config"] == expected_cfg
        assert lite_health["checks"]["knowledge_graph"]["retrieval_config"] == expected_cfg

        labels = tmp_path / "labels.json"
        labels.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pairs": [{"query": "q_graph", "relevant_ids": ["doc-1"]}],
                }
            )
        )

        # Clear Vigil's local env so only Core's response can select `hybrid_graph`.
        for k in (
            "UNITARES_EMBEDDING_MODEL",
            "UNITARES_ENABLE_HYBRID",
            "UNITARES_ENABLE_GRAPH_EXPANSION",
            "UNITARES_ENABLE_RELATED_EXPANSION",
            "UNITARES_ENABLE_RERANKER",
        ):
            monkeypatch.delenv(k, raising=False)

        real_client_cls = httpx.Client

        # Case A: `/health/deep` returns 200 with the real `full_health` payload.
        def _transport_200(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/health/deep"):
                return httpx.Response(200, json=full_health)
            if req.url.path.endswith("/v1/tools/call"):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": {
                            "success": True,
                            "search_mode_used": "hybrid_rrf_graph",
                            "discoveries": [{"id": "doc-1"}],
                        },
                    },
                )
            return httpx.Response(404)

        with patch(
            "agents.vigil.remote_eval.httpx.Client",
            side_effect=lambda **kw: real_client_cls(
                transport=httpx.MockTransport(_transport_200), **kw
            ),
        ):
            report_200 = run_remote_retrieval_eval(
                labels_path=labels,
                base_url="https://gov.example/mcp/",
                api_token="tok",
            )

        assert report_200["config"]["config_source"] == "core"
        assert report_200["config"]["embedding_model"] == "bge-m3"
        assert report_200["config"]["graph_expand"] is True
        assert _derive_eval_config_tag(report_200["config"]) == "hybrid_graph"

        # Case B: `/health/deep` stays 503 throughout warmup -> observed
        # `search_mode_used="hybrid_rrf_graph"` STILL sets `graph_expand=True`
        # and selects `hybrid_graph` rather than `hybrid_rrf`!
        def _transport_503(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/health/deep"):
                return httpx.Response(
                    503,
                    json={"status": "unavailable", "retry_after_seconds": 5},
                )
            if req.url.path.endswith("/v1/tools/call"):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": {
                            "success": True,
                            "search_mode_used": "hybrid_rrf_graph",
                            "discoveries": [{"id": "doc-1"}],
                        },
                    },
                )
            return httpx.Response(404)

        with patch(
            "agents.vigil.remote_eval.httpx.Client",
            side_effect=lambda **kw: real_client_cls(
                transport=httpx.MockTransport(_transport_503), **kw
            ),
        ):
            report_503 = run_remote_retrieval_eval(
                labels_path=labels,
                base_url="https://gov.example/mcp/",
                api_token="tok",
            )

        assert report_503["config"]["config_source"] == "observed_search_mode"
        assert report_503["config"]["hybrid"] is True
        assert report_503["config"]["graph_expand"] is True
        assert _derive_eval_config_tag(report_503["config"]) == "hybrid_graph"


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
