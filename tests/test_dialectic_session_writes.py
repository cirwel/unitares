"""Every Python-initiated dialectic session write is recorded as an attempt/response pair.

`dialectic_session_write` rows are what the Wave 3 collision report counts to
decide whether its reading is complete: an attempt without a matching response
makes the reading inconclusive. These tests pin the pairing at every
chokepoint -- the `src.dialectic_db` write helpers and the BEAM client's
writers -- for each way a write can end: written, not written, BEAM already
terminal, a non-OK answer, no answer, an exception, and a cancellation.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import dialectic_db
from src import dialectic_session_writes as sw
from src.mcp_handlers.dialectic import beam_resolve_client as brc


@pytest.fixture
def captured(monkeypatch):
    rows = []

    async def fake_emit(details):
        rows.append(details)

    monkeypatch.setattr(sw, "_emit", fake_emit)
    return rows


def _pair(rows):
    assert [r["stage"] for r in rows] == ["attempt", "response"], rows
    attempt, response = rows
    assert attempt["attempt_id"] and attempt["attempt_id"] == response["attempt_id"]
    for key in ("session_id", "kind", "via", "site", "requested"):
        assert attempt[key] == response[key], key
    return attempt, response


def _fake_httpx(status_code, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x"
    resp.json.return_value = body
    client_cm = MagicMock()
    client_cm.__enter__.return_value.post.return_value = resp
    client_cm.__exit__.return_value = False
    fake = MagicMock()
    fake.Client.return_value = client_cm
    return fake


def _enable(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")


# --- the recorder itself ---------------------------------------------------


class TestRecorder:
    @pytest.mark.asyncio
    async def test_a_normal_exit_emits_the_response(self, captured):
        async with sw.record_session_write(kind="phase", session_id="s1",
                                           requested="antithesis") as rec:
            rec.respond(outcome="written")
        attempt, response = _pair(captured)
        assert attempt["via"] == "python" and attempt["site"] is None
        assert response["outcome"] == "written"

    @pytest.mark.asyncio
    async def test_an_exception_emits_an_error_response_and_propagates(self, captured):
        with pytest.raises(RuntimeError):
            async with sw.record_session_write(kind="phase", session_id="s1"):
                raise RuntimeError("pool gone")
        _attempt, response = _pair(captured)
        assert response["outcome"] == "error"
        assert response["error"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_a_cancellation_still_gets_its_response(self, captured):
        """Scheduled, not awaited mid-cancellation -- but it lands."""
        entered = asyncio.Event()

        async def write():
            async with sw.record_session_write(kind="resolve", session_id="s1"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(write())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)
        _attempt, response = _pair(captured)
        assert response["outcome"] == "interrupted"

    @pytest.mark.asyncio
    async def test_via_and_site_come_from_the_callers_scope(self, captured):
        with sw.session_write_via("python_fallback", site="thesis"):
            async with sw.record_session_write(kind="phase", session_id="s1") as rec:
                rec.respond(outcome="written")
        attempt, _ = _pair(captured)
        assert (attempt["via"], attempt["site"]) == ("python_fallback", "thesis")

    @pytest.mark.asyncio
    async def test_an_attempt_id_scope_is_used(self, captured):
        with sw.attempt_id_scope("att-1"):
            async with sw.record_session_write(kind="status", session_id="s1") as rec:
                rec.respond(outcome="written")
        assert {r["attempt_id"] for r in captured} == {"att-1"}

    @pytest.mark.asyncio
    async def test_the_real_emitter_is_fail_soft(self):
        with patch("src.audit_db.append_audit_event_async",
                   AsyncMock(side_effect=RuntimeError("audit down"))):
            async with sw.record_session_write(kind="phase", session_id="s1") as rec:
                rec.respond(outcome="written")  # must not raise

    @pytest.mark.asyncio
    async def test_the_real_emitter_writes_one_event_name(self):
        seen = []

        async def fake_append(entry):
            seen.append(entry)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            async with sw.record_session_write(kind="phase", session_id="s1") as rec:
                rec.respond(outcome="written")
        assert {e["event_type"] for e in seen} == {"dialectic_session_write"}
        assert all(e["session_id"] == "s1" for e in seen)


# --- the Python chokepoints ------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch):
    db = MagicMock()
    for name in ("create_session", "update_session_phase", "reopen_session",
                 "update_session_reviewer", "update_session_status",
                 "mark_awaiting_facilitation", "update_session_awaiting_facilitation",
                 "resolve_session"):
        setattr(db, name, AsyncMock(return_value=True))
    monkeypatch.setattr(dialectic_db, "get_dialectic_db", AsyncMock(return_value=db))
    return db


class TestPythonChokepoints:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("call,kind,requested", [
        (lambda: dialectic_db.update_session_phase_async("s1", "antithesis"), "phase", "antithesis"),
        (lambda: dialectic_db.reopen_session_async("s1", "antithesis"), "reopen", "antithesis"),
        (lambda: dialectic_db.update_session_reviewer_async("s1", "r2"), "reviewer", "r2"),
        (lambda: dialectic_db.update_session_status_async("s1", "failed"), "status", "failed"),
        (lambda: dialectic_db.mark_awaiting_facilitation_async("s1"), "facilitation", True),
        (lambda: dialectic_db.update_session_awaiting_facilitation_async("s1", False),
         "facilitation", False),
        (lambda: dialectic_db.resolve_session_async("s1", {"reason": "r"}, "resolved"),
         "resolve", "resolved"),
        (lambda: dialectic_db.create_session_async(session_id="s1"), "create", None),
    ])
    async def test_every_write_helper_records_a_pair(self, captured, fake_db, call, kind,
                                                     requested):
        assert await call() is True
        attempt, response = _pair(captured)
        assert attempt["kind"] == kind
        assert attempt["session_id"] == "s1"
        assert attempt["requested"] == requested
        assert response["outcome"] == "written"

    @pytest.mark.asyncio
    async def test_a_refused_write_is_not_written_and_names_the_winner(self, captured, fake_db):
        async def refuse(session_id, status, winner=None):
            winner.update(winner_status="failed", winner_reason="liveness_timeout")
            return False

        fake_db.update_session_status = AsyncMock(side_effect=refuse)
        winner = {}
        assert await dialectic_db.update_session_status_async("s1", "failed",
                                                              winner=winner) is False
        _attempt, response = _pair(captured)
        assert response["outcome"] == "not_written"
        assert response["winner_status"] == "failed"
        assert response["winner_reason"] == "liveness_timeout"

    @pytest.mark.asyncio
    async def test_a_raising_write_records_an_error_response(self, captured, fake_db):
        fake_db.update_session_phase = AsyncMock(side_effect=ConnectionError("gone"))
        with pytest.raises(ConnectionError):
            await dialectic_db.update_session_phase_async("s1", "antithesis")
        _attempt, response = _pair(captured)
        assert response["outcome"] == "error"


# --- the BEAM client -------------------------------------------------------

READ = "src.dialectic_db.get_session_terminal_state_async"


class TestBeamClient:
    @pytest.mark.asyncio
    async def test_already_terminal_is_recorded_with_the_rows_reason(self, monkeypatch,
                                                                     captured):
        _enable(monkeypatch)
        body = {"ok": True, "status": "failed", "saga_id": None,
                "origin": "already_terminal", "protocol_version": "1"}
        read = AsyncMock(return_value={"status": "failed", "reason": None})
        with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}), patch(READ, read):
            out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})

        assert out == body, "the resolve's answer is unchanged"
        attempt, response = _pair(captured)
        assert (attempt["kind"], attempt["via"], attempt["requested"]) == (
            "resolve", "beam", "resolved")
        assert response["outcome"] == "already_terminal"
        assert response["origin"] == "already_terminal"
        assert response["reported_status"] == "failed"
        assert response["saga_id"] is None
        assert response["terminal_reason"] is None
        assert response["terminal_reason_read"] is True

    @pytest.mark.asyncio
    async def test_a_reason_read_failure_is_recorded_as_unread(self, monkeypatch, captured):
        _enable(monkeypatch)
        body = {"ok": True, "status": "failed", "saga_id": None, "origin": "already_terminal"}
        with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}), \
             patch(READ, AsyncMock(side_effect=RuntimeError("pool gone"))):
            assert await brc.beam_resolve("s1", "p", "r", {}) == body
        _attempt, response = _pair(captured)
        assert response["terminal_reason_read"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", ["new", "idempotent"])
    async def test_a_claimed_saga_is_written_with_its_id(self, monkeypatch, captured, origin):
        _enable(monkeypatch)
        body = {"ok": True, "status": "resolved", "saga_id": "sg-1", "origin": origin}
        with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}):
            await brc.beam_resolve("s1", "p", "r", {})
        _attempt, response = _pair(captured)
        assert (response["outcome"], response["origin"], response["saga_id"]) == (
            "written", origin, "sg-1")

    @pytest.mark.asyncio
    async def test_a_non_ok_answer_is_not_written(self, monkeypatch, captured):
        _enable(monkeypatch)
        with patch.dict(sys.modules, {"httpx": _fake_httpx(409, {"ok": False,
                                                                  "error": "saga_in_flight"})}):
            assert await brc.beam_resolve("s1", "p", "r", {}, status="failed") is None
        attempt, response = _pair(captured)
        assert attempt["requested"] == "failed"
        assert (response["outcome"], response["http_status"], response["error"]) == (
            "not_written", 409, "saga_in_flight")

    @pytest.mark.asyncio
    async def test_an_exception_produces_the_response_record(self, monkeypatch, captured):
        _enable(monkeypatch)
        fake = MagicMock()
        fake.Client.side_effect = ConnectionError("refused")
        with patch.dict(sys.modules, {"httpx": fake}):
            assert await brc.beam_resolve("s1", "p", "r", {}) is None
        _attempt, response = _pair(captured)
        assert (response["outcome"], response["error"]) == ("no_response", "ConnectionError")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call,kind,requested", [
        (lambda: brc.beam_update_phase("s1", "antithesis"), "phase", "antithesis"),
        (lambda: brc.beam_update_reviewer("s1", "r2"), "reviewer", "r2"),
        (lambda: brc.beam_create_session("s1", "p"), "create", None),
    ])
    async def test_the_phase_reviewer_and_create_posts_are_recorded(
        self, monkeypatch, captured, call, kind, requested
    ):
        _enable(monkeypatch)
        with patch.dict(sys.modules, {"httpx": _fake_httpx(200, {"ok": True})}):
            assert await call() == {"ok": True}
        attempt, response = _pair(captured)
        assert (attempt["kind"], attempt["via"], attempt["requested"]) == (kind, "beam",
                                                                           requested)
        # BEAM's update_phase answers OK for a terminal no-op too.
        expected = "accepted_effect_unknown" if kind == "phase" else "written"
        assert response["outcome"] == expected

    @pytest.mark.asyncio
    async def test_no_request_means_no_record(self, monkeypatch, captured):
        monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
        assert await brc.beam_resolve("s1", "p", "r", {}) is None
        assert await brc.beam_update_phase("s1", "antithesis") is None
        assert captured == []

    @pytest.mark.asyncio
    async def test_a_failed_emit_never_stops_the_request(self, monkeypatch):
        _enable(monkeypatch)
        fake = _fake_httpx(200, {"ok": True, "status": "resolved", "saga_id": "x",
                                 "origin": "new"})
        with patch.dict(sys.modules, {"httpx": fake}), \
             patch("src.audit_db.append_audit_event_async",
                   AsyncMock(side_effect=RuntimeError("audit down"))):
            out = await brc.beam_resolve("s1", "p", "r", {})
        assert out["origin"] == "new"
        fake.Client.return_value.__enter__.return_value.post.assert_called_once()


def test_origin_string_matches_the_beam_atom():
    """The client keys on the string Jason produces from `:already_terminal`."""
    from pathlib import Path

    saga = (Path(__file__).resolve().parents[1]
            / "elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex")
    assert f"origin: :{brc.ALREADY_TERMINAL_ORIGIN}" in saga.read_text(encoding="utf-8")


def test_every_beam_fallback_python_write_is_attributed():
    """Each `if <beam answer> is None:` Python write runs under
    `session_write_via("python_fallback", ...)`, so the report can tell a
    fallback from a Python-only path."""
    from pathlib import Path

    pkg = Path(brc.__file__).parent
    for name, expected in (("handlers.py", 8), ("session.py", 2)):
        text = (pkg / name).read_text(encoding="utf-8")
        assert text.count('session_write_via("python_fallback"') == expected, name


@pytest.mark.asyncio
async def test_the_sweepers_own_row_and_its_session_write_pair_share_an_attempt_id(
    captured, fake_db, monkeypatch
):
    """`dialectic_guarded_write` (the sweeper's analytic row) and the chokepoint's
    attempt/response pair describe one write, and join on attempt_id."""
    from src.mcp_handlers.dialectic import auto_resolve as ar

    guarded = []

    async def fake_guarded(**row):
        guarded.append(row)

    monkeypatch.setattr(ar, "emit_guarded_write", fake_guarded)
    monkeypatch.setattr(ar, "probe_saga_since_async", AsyncMock(return_value={}))
    counts = ar._CycleCounts(cycle_id="c1")
    with sw.session_write_via("sweeper"):
        ok = await ar._attempt_guarded_write(
            counts, [], attempted="reap_failed",
            session={"session_id": "s1", "paused_agent_id": "p"},
            write=lambda winner: dialectic_db.update_session_status_async(
                "s1", "failed", winner=winner),
            decision_read_ts=None, early_check_ts=None,
        )
    assert ok is True
    attempt, response = _pair(captured)
    assert attempt["via"] == "sweeper" and attempt["kind"] == "status"
    assert guarded[0]["attempt_id"] == attempt["attempt_id"]



class TestDecisionTimeAndEmitFailures:
    @pytest.mark.asyncio
    async def test_a_write_carries_the_time_this_task_read_the_session(self, captured):
        from datetime import datetime, timezone

        read_at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        sw.note_session_read(["s1", "s2"], read_at)
        async with sw.record_session_write(kind="phase", session_id="s1") as rec:
            rec.respond(outcome="written")
        async with sw.record_session_write(kind="phase", session_id="s3") as rec:
            rec.respond(outcome="written")
        assert captured[0]["decision_ts"] == read_at
        assert captured[2]["decision_ts"] is None, "never read in this task"

    @pytest.mark.asyncio
    async def test_a_db_read_notes_the_decision_time(self, monkeypatch):
        from datetime import datetime, timezone

        pool = MagicMock()
        pool._closed = False
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"session_id": "s9", "status": "active"})
        conn.fetch = AsyncMock(return_value=[])
        acm = AsyncMock()
        acm.__aenter__ = AsyncMock(return_value=conn)
        acm.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = acm
        db = dialectic_db.DialecticDB(pool=pool)
        db._initialized = True
        before = datetime.now(timezone.utc)
        await db.get_session("s9")
        noted = sw.decision_ts_for("s9")
        assert noted is not None and noted >= before

    @pytest.mark.asyncio
    async def test_session_write_emit_failures_are_counted(self):
        sw.take_emit_failures()
        with patch("src.audit_db.append_audit_event_async", AsyncMock(return_value=False)):
            async with sw.record_session_write(kind="phase", session_id="s1") as rec:
                rec.respond(outcome="written")
        assert sw.take_emit_failures() == 2
        assert sw.take_emit_failures() == 0



def test_an_emit_failure_is_appended_to_the_durable_ledger(tmp_path, monkeypatch):
    """It survives the process: one fsynced JSON line per failure."""
    import json as _json

    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(sw.EMIT_FAILURE_LEDGER_ENV, str(ledger))
    sw.take_emit_failures()
    sw.record_emit_failure("dialectic_guarded_write", "s1", 7)
    sw.record_emit_failure("dialectic_session_write")
    lines = [_json.loads(x) for x in ledger.read_text().splitlines()]
    assert [x["event_type"] for x in lines] == ["dialectic_guarded_write",
                                                "dialectic_session_write"]
    assert lines[0]["session_id"] == "s1" and lines[0]["cycle_seq"] == 7
    assert all(x["process_boot_id"] == sw.PROCESS_BOOT_ID for x in lines)
    assert sw.take_emit_failures() == 2


def test_a_ledger_that_cannot_be_written_never_raises(monkeypatch):
    monkeypatch.setenv(sw.EMIT_FAILURE_LEDGER_ENV, "/proc/definitely/not/writable/x.jsonl")
    sw.record_emit_failure("dialectic_session_write")  # must not raise
    sw.take_emit_failures()


def test_the_default_ledger_lives_under_data_dialectic(monkeypatch):
    monkeypatch.delenv(sw.EMIT_FAILURE_LEDGER_ENV, raising=False)
    assert sw.emit_failure_ledger_path().endswith(
        "data/dialectic/instrument_emit_failures.jsonl")



class TestNoOpsAreNotWrites:
    @pytest.mark.asyncio
    async def test_an_idempotent_resolve_is_a_no_op(self, captured, fake_db):
        async def idempotent(session_id, resolution, status, detail=None):
            detail.update(written=False, existing_status="resolved")
            return True

        fake_db.resolve_session = AsyncMock(side_effect=idempotent)
        assert await dialectic_db.resolve_session_async("s1", {}, "resolved") is True
        _attempt, response = _pair(captured)
        assert response["outcome"] == "no_op"
        assert response["existing_status"] == "resolved"

    @pytest.mark.asyncio
    async def test_a_duplicate_create_is_not_written(self, captured, fake_db):
        fake_db.create_session = AsyncMock(
            return_value={"session_id": "s1", "created": False, "error": "already_exists"})
        await dialectic_db.create_session_async(session_id="s1")
        _attempt, response = _pair(captured)
        assert response["outcome"] == "not_written"



@pytest.mark.asyncio
async def test_a_duplicate_beam_create_is_not_written(monkeypatch, captured):
    _enable(monkeypatch)
    body = {"ok": True, "created": False}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}):
        assert await brc.beam_create_session("s1", "p") == body
    _attempt, response = _pair(captured)
    assert response["outcome"] == "not_written"


@pytest.mark.asyncio
async def test_a_stalled_audit_sink_does_not_block_the_session_write(monkeypatch, fake_db):
    """The attempt record is awaited before the write; a stall there is bounded
    and counted as a failed emit, and the write still runs."""
    monkeypatch.setattr(sw, "EMIT_TIMEOUT_S", 0.05)
    sw.take_emit_failures()

    async def stall(entry):
        await asyncio.Event().wait()

    with patch("src.audit_db.append_audit_event_async", side_effect=stall):
        assert await asyncio.wait_for(
            dialectic_db.update_session_phase_async("s1", "antithesis"), timeout=5) is True
    fake_db.update_session_phase.assert_awaited_once()
    assert sw.take_emit_failures() == 2


def test_the_first_cycle_row_creates_the_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "sub" / "ledger.jsonl"
    monkeypatch.setenv(sw.EMIT_FAILURE_LEDGER_ENV, str(ledger))
    sw.ensure_emit_failure_ledger()
    assert ledger.exists() and ledger.read_text() == ""
