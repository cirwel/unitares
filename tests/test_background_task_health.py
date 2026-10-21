"""Background-loop failures are logged once per distinct error and recorded.

These loops used to log a failed run at DEBUG, which the server never writes,
so a loop could fail on every run with nothing on record (concept extraction
did for months, KG 2026-10-20T03:44:57.244104+00:00). The record has to
WARN on the first failure and on a changed error, stay quiet while the same
error repeats every tick, log recovery, and expose what it saw to the health
check and the doctor.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import background_task_health as health
from src import background_tasks
from src.services.runtime_queries import NO_DATA_YET

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "unitares_doctor.py"


@pytest.fixture(autouse=True)
def _clean_registry():
    health.reset_for_tests()
    background_tasks._silence_close_pending.clear()
    yield
    health.reset_for_tests()
    background_tasks._silence_close_pending.clear()


@pytest.fixture
def log():
    return MagicMock(spec=logging.Logger)


def test_first_failure_warns_and_repeats_stay_quiet(log):
    health.record_failure("matview_refresh", RuntimeError("relation locked"), log=log)
    health.record_failure("matview_refresh", RuntimeError("relation locked"), log=log)
    health.record_failure("matview_refresh", RuntimeError("relation locked"), log=log)

    assert log.warning.call_count == 1
    assert log.debug.call_count == 2
    entry = health.snapshot()["matview_refresh"]
    assert entry["status"] == "failing"
    assert entry["consecutive_failures"] == 3
    assert entry["last_error"] == "RuntimeError: relation locked"
    assert entry["last_success_at"] is None


def test_changed_error_warns_again_but_changed_numbers_do_not(log):
    health.record_failure("silence_check", ValueError("row 17 missing"), log=log)
    health.record_failure("silence_check", ValueError("row 42 missing"), log=log)
    assert log.warning.call_count == 1  # same error, different id

    health.record_failure("silence_check", TypeError("can't compare"), log=log)
    assert log.warning.call_count == 2  # a different error


def test_recovery_logs_once_and_clears_the_streak(log):
    health.record_failure("partition_maintenance", RuntimeError("boom"), log=log)
    health.record_success("partition_maintenance", log=log)
    health.record_success("partition_maintenance", log=log)

    assert log.info.call_count == 1
    entry = health.snapshot()["partition_maintenance"]
    assert entry["status"] == "ok"
    assert entry["consecutive_failures"] == 0
    assert entry["last_success_at"] is not None
    # The last error stays on record after recovery.
    assert entry["last_error"] == "RuntimeError: boom"

    health.record_failure("partition_maintenance", RuntimeError("boom"), log=log)
    assert log.warning.call_count == 2  # first failure of a new streak warns


def test_error_text_is_bounded(log):
    health.record_failure("matview_refresh", RuntimeError("x" * 5000), log=log)
    assert len(health.snapshot()["matview_refresh"]["last_error"]) == health.MAX_ERROR_CHARS


def test_health_check_states(log):
    assert health.health_check()["status"] == NO_DATA_YET

    health.record_success("matview_refresh", log=log)
    check = health.health_check()
    assert check["status"] == "healthy"
    assert check["failing"] == []

    health.record_failure("silence_check", RuntimeError("down"), log=log)
    check = health.health_check()
    assert check["status"] == "warning"
    assert check["failing"] == ["silence_check"]
    assert "silence_check" in check["warning"]


# --- the loops record their runs ---------------------------------------------


class _Stop(Exception):
    pass


def _db_with(conn):
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.acquire.return_value = acquire
    return db


async def _one_pass(coro_fn, monkeypatch, conn):
    import src.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: _db_with(conn))
    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > 1:  # the startup delay, then stop after one pass
            raise _Stop

    monkeypatch.setattr(background_tasks.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        await coro_fn()


@pytest.mark.asyncio
async def test_matview_refresh_failure_is_recorded(monkeypatch):
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("cannot refresh concurrently"))
    await _one_pass(background_tasks.periodic_matview_refresh, monkeypatch, conn)
    entry = health.snapshot()["matview_refresh"]
    assert entry["status"] == "failing"
    assert "cannot refresh concurrently" in entry["last_error"]


@pytest.mark.asyncio
async def test_partition_maintenance_success_is_recorded(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value='{"events_next": "exists"}')
    await _one_pass(background_tasks.periodic_partition_maintenance, monkeypatch, conn)
    assert health.snapshot()["partition_maintenance"]["status"] == "ok"


@pytest.mark.asyncio
async def test_silence_loop_records_both_halves(monkeypatch):
    monkeypatch.setattr(
        background_tasks, "_silence_check_iteration", AsyncMock(side_effect=KeyError("label"))
    )
    monkeypatch.setattr(background_tasks, "_resident_pause_check_iteration", AsyncMock())
    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _Stop

    monkeypatch.setattr(background_tasks.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        await background_tasks.check_agent_silence()
    snap = health.snapshot()
    assert snap["silence_check"]["status"] == "failing"
    assert snap["resident_pause_check"]["status"] == "ok"


@pytest.mark.asyncio
async def test_silence_finding_write_failure_is_recorded(monkeypatch):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", AsyncMock(side_effect=ConnectionError("kg down")))
    meta = MagicMock(label=None)
    await background_tasks._log_resident_silence_finding("agent-1", meta, 45.0, 300)
    assert health.snapshot()["silence_finding_write"]["status"] == "failing"


@pytest.mark.asyncio
async def test_silence_finding_close_rejected_update_is_a_failure(monkeypatch):
    import src.knowledge_graph as kg
    from datetime import datetime, timezone

    row = MagicMock(id="d1", tags=["resident-silence", "auto-filed"], agent_id="agent-1", details="")
    graph = MagicMock()
    graph.query = AsyncMock(return_value=[row])
    graph.update_discovery = AsyncMock(return_value=False)
    monkeypatch.setattr(kg, "get_knowledge_graph", AsyncMock(return_value=graph))

    landed = await background_tasks._close_resident_silence_findings(
        "agent-1", datetime.now(timezone.utc)
    )
    assert landed is False
    entry = health.snapshot()["silence_finding_close"]
    assert entry["status"] == "failing"
    assert "rejected" in entry["last_error"]



@pytest.mark.asyncio
async def test_silence_finding_close_recovers_when_nothing_is_left_to_close(monkeypatch):
    import src.knowledge_graph as kg
    from datetime import datetime, timezone

    monkeypatch.setattr(kg, "get_knowledge_graph", AsyncMock(side_effect=ConnectionError("kg down")))
    assert await background_tasks._close_resident_silence_findings(
        "agent-1", datetime.now(timezone.utc)
    ) is False
    assert health.snapshot()["silence_finding_close"]["status"] == "failing"

    # The finding was resolved elsewhere meanwhile: the lookup succeeds and
    # finds nothing. That is a successful run, and it must clear the failure,
    # because the caller stops checking this agent after a True.
    graph = MagicMock()
    graph.query = AsyncMock(return_value=[])
    monkeypatch.setattr(kg, "get_knowledge_graph", AsyncMock(return_value=graph))
    assert await background_tasks._close_resident_silence_findings(
        "agent-1", datetime.now(timezone.utc)
    ) is True
    assert health.snapshot()["silence_finding_close"]["status"] == "ok"


async def _one_rotation_pass(monkeypatch, tmp_path):
    """Run periodic_server_log_rotation for exactly one pass over a temp log dir."""
    import asyncio

    log_dir = tmp_path / "data" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "mcp_server.log").write_text("x" * 64)
    monkeypatch.setattr(background_tasks, "__file__", str(tmp_path / "src" / "background_tasks.py"))
    # First sleep is the startup delay; the second ends the loop after one pass.
    monkeypatch.setattr(
        background_tasks.asyncio, "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    with pytest.raises(asyncio.CancelledError):
        await background_tasks.periodic_server_log_rotation(max_size_mb=0)
    return log_dir


@pytest.mark.asyncio
async def test_server_log_rotation_failure_is_recorded(monkeypatch, tmp_path):
    def broken_gzip_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(background_tasks.gzip, "open", broken_gzip_open)
    await _one_rotation_pass(monkeypatch, tmp_path)
    entry = health.snapshot()["server_log_rotation"]
    assert entry["status"] == "failing"
    assert "mcp_server.log" in entry["last_error"] and "disk full" in entry["last_error"]


@pytest.mark.asyncio
async def test_server_log_rotation_success_is_recorded(monkeypatch, tmp_path):
    log_dir = await _one_rotation_pass(monkeypatch, tmp_path)
    assert health.snapshot()["server_log_rotation"]["status"] == "ok"
    assert (log_dir / "mcp_server.log").read_text() == ""
    assert list((log_dir / "archive").glob("mcp_server_*.log.gz"))


@pytest.mark.asyncio
async def test_one_agents_success_does_not_clear_anothers_close_failure(monkeypatch):
    import src.knowledge_graph as kg
    from datetime import datetime, timezone

    row = MagicMock(id="d1", tags=["resident-silence", "auto-filed"], agent_id="agent-a", details="")

    async def query(**kwargs):
        return [row] if kwargs.get("agent_id") == "agent-a" else []

    graph = MagicMock()
    graph.query = AsyncMock(side_effect=query)
    graph.update_discovery = AsyncMock(return_value=False)
    monkeypatch.setattr(kg, "get_knowledge_graph", AsyncMock(return_value=graph))
    now = datetime.now(timezone.utc)

    assert await background_tasks._close_resident_silence_findings("agent-a", now) is False
    # Agent B has nothing to close; agent A's resolve is still pending.
    assert await background_tasks._close_resident_silence_findings("agent-b", now) is True
    entry = health.snapshot()["silence_finding_close"]
    assert entry["status"] == "failing"
    assert "agent-a" in entry["last_error"]

    graph.update_discovery = AsyncMock(return_value=True)
    assert await background_tasks._close_resident_silence_findings("agent-a", now) is True
    assert health.snapshot()["silence_finding_close"]["status"] == "ok"

# --- doctor --------------------------------------------------------------------


@pytest.fixture
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, doctor, payload=None, error=None):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = dict(request.header_items())
        if error is not None:
            raise error
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)
    return seen


def _payload(tasks):
    return {"checks": {"background_tasks": {"status": "x", "tasks": tasks}}}


def test_doctor_warns_on_a_failing_task(doctor, monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    _serve(monkeypatch, doctor, _payload({
        "matview_refresh": {"status": "ok"},
        "silence_check": {"status": "failing", "consecutive_failures": 4,
                          "last_success_at": None, "last_error": "KeyError: 'label'"},
    }))
    result = doctor.check_background_task_failures()
    assert result.status == doctor.Status.WARN
    assert "silence_check" in result.message
    assert "KeyError" in result.detail


def test_doctor_passes_when_every_task_succeeded(doctor, monkeypatch):
    _serve(monkeypatch, doctor, _payload({"matview_refresh": {"status": "ok"}}))
    assert doctor.check_background_task_failures().status == doctor.Status.PASS


def test_doctor_reports_no_runs_as_info(doctor, monkeypatch):
    _serve(monkeypatch, doctor, _payload({}))
    assert doctor.check_background_task_failures().status == doctor.Status.INFO


def test_doctor_skips_when_refused_and_sends_a_visible_token(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "tok-a,tok-b")
    seen = _serve(monkeypatch, doctor, error=urllib.error.HTTPError(
        doctor.HTTP_DEEP_HEALTH_URL, 401, "unauthorized", {}, None))
    result = doctor.check_background_task_failures()
    assert result.status == doctor.Status.SKIP
    assert seen["headers"].get("Authorization") == "Bearer tok-a"


def test_doctor_skips_an_older_server_without_the_record(doctor, monkeypatch):
    _serve(monkeypatch, doctor, {"checks": {}})
    assert doctor.check_background_task_failures().status == doctor.Status.SKIP


def test_doctor_skips_when_server_unreachable(doctor, monkeypatch):
    _serve(monkeypatch, doctor, error=urllib.error.URLError("refused"))
    assert doctor.check_background_task_failures().status == doctor.Status.SKIP


def test_doctor_does_not_pass_on_a_stale_snapshot(doctor, monkeypatch):
    payload = _payload({"matview_refresh": {"status": "ok"}})
    payload["_cache"] = {"stale": True, "produced_at": "2026-10-20T00:00:00Z", "age_seconds": 900.0}
    _serve(monkeypatch, doctor, payload)
    result = doctor.check_background_task_failures()
    assert result.status == doctor.Status.WARN
    assert "stale" in result.message
    assert "2026-10-20T00:00:00Z" in result.detail


def test_doctor_names_staleness_beside_real_failures(doctor, monkeypatch):
    payload = _payload({"silence_check": {"status": "failing", "consecutive_failures": 1,
                                          "last_success_at": None, "last_error": "boom"}})
    payload["_cache"] = {"stale": True, "produced_at": "t0", "age_seconds": 600.0}
    _serve(monkeypatch, doctor, payload)
    result = doctor.check_background_task_failures()
    assert result.status == doctor.Status.WARN
    assert "silence_check" in result.message
    assert result.detail.startswith("deep-health snapshot is stale")


def test_doctor_passes_on_a_fresh_snapshot(doctor, monkeypatch):
    payload = _payload({"matview_refresh": {"status": "ok"}})
    payload["_cache"] = {"stale": False, "produced_at": "t0", "age_seconds": 5.0}
    _serve(monkeypatch, doctor, payload)
    assert doctor.check_background_task_failures().status == doctor.Status.PASS
