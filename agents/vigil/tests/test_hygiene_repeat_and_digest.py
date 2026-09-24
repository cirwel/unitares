"""Vigil hygiene output that reaches someone, once.

Measured 2026-09-24, over 167 logged cycles:

- the stale-opens sweep listed the same 2 KG entries in 167 of 167 cycles,
  so the check-in and the log repeated identical lines 48 times a day;
- Groundskeeper reported 78 archive candidates, "0 archivable now", and none
  of Vigil's 91 KG notes in 30 days drew a detail read. Candidates only ever
  left the queue through the 90-day auto-archive.

These tests pin the fix: an unchanged stale set is re-reported at most daily,
the operator gets one archive-candidate digest a week on the finding channel
(never a KG note, never an archive), and the digest names a command that
lists the candidates and archives them only on --apply.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from unitares_sdk.models import AuditResult

from agents.common.findings import DEDUPED, DELIVERED, FAILED
from agents.vigil import agent as vigil

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
STALE = [
    {"id": "2026-05-01T00:00:00.000001", "summary": "old open", "last_activity_days": 146},
    {"id": "2026-06-01T00:00:00.000001", "summary": "older open", "last_activity_days": 115},
]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep these tests out of the operator's real log and state file."""
    monkeypatch.setattr(vigil, "LOG_FILE", tmp_path / "vigil-test.log", raising=False)
    monkeypatch.setenv("VIGIL_LOG_FILE", str(tmp_path / "vigil-test.log"))


# --- stale-opens: report a set once, then only when it changes or daily ----


def test_first_report_lists_the_set():
    lines, state = vigil.stale_opens_report(STALE, {}, NOW)
    assert lines[0].startswith("hygiene: 2 stale opens")
    assert sum(line.startswith("stale_open:") for line in lines) == 2
    assert state["hygiene_stale_opens_reported_at"] == NOW.isoformat()


def test_unchanged_set_inside_a_day_is_silent():
    _, state = vigil.stale_opens_report(STALE, {}, NOW)
    lines, state2 = vigil.stale_opens_report(STALE, state, NOW + timedelta(minutes=30))
    assert lines == []
    # The clock is not reset by a silent cycle, or the set would never recur.
    assert state2["hygiene_stale_opens_reported_at"] == NOW.isoformat()


def test_unchanged_set_recurs_after_a_day():
    _, state = vigil.stale_opens_report(STALE, {}, NOW)
    lines, _ = vigil.stale_opens_report(STALE, state, NOW + timedelta(hours=25))
    assert lines and lines[0].startswith("hygiene: 2 stale opens")


def test_a_changed_set_reports_at_once():
    _, state = vigil.stale_opens_report(STALE, {}, NOW)
    changed = STALE + [{"id": "2026-07-01T00:00:00.000001", "last_activity_days": 85}]
    lines, _ = vigil.stale_opens_report(changed, state, NOW + timedelta(minutes=30))
    assert lines and lines[0].startswith("hygiene: 3 stale opens")


def test_empty_sweep_reports_nothing():
    lines, state = vigil.stale_opens_report([], {}, NOW)
    assert lines == []
    assert state["hygiene_stale_open_ids"] == []


# --- weekly digest ------------------------------------------------------------


def test_digest_due_weekly():
    assert vigil.archive_digest_due({}, NOW)
    recent = {"archive_digest_sent_at": (NOW - timedelta(days=1)).isoformat()}
    assert not vigil.archive_digest_due(recent, NOW)
    old = {"archive_digest_sent_at": (NOW - timedelta(days=8)).isoformat()}
    assert vigil.archive_digest_due(old, NOW)


def test_digest_is_a_low_finding_naming_the_review_command(monkeypatch):
    sent: list[dict] = []

    def fake_post(**kwargs):
        sent.append(kwargs)
        return DELIVERED

    monkeypatch.setattr(vigil, "post_finding_result", fake_post)
    assert vigil.post_archive_digest(78, 2, 90, NOW) == DELIVERED
    vigil.post_archive_digest(78, 2, 90, NOW + timedelta(days=7))

    first = sent[0]
    assert first["event_type"] == "vigil_finding"
    assert first["severity"] == "low"
    assert "78 archive candidate" in first["message"]
    assert vigil.ARCHIVE_REVIEW_COMMAND in first["message"]
    assert first["extra"]["finding_type"] == "kg_archive_candidates"
    # A new week is a new finding, not a dedup of last week's.
    assert sent[0]["fingerprint"] != sent[1]["fingerprint"]


# --- the cycle wires both -----------------------------------------------------


def _cycle_agent(tmp_path, monkeypatch, posted: list, outcome: str = DELIVERED):
    agent = vigil.VigilAgent(mcp_url="http://localhost:8767/mcp/", with_hygiene=True)
    agent.state_file = tmp_path / "vigil_state.json"
    monkeypatch.setattr(vigil, "run_health_checks", AsyncMock(return_value=[]))
    monkeypatch.setattr(vigil, "maybe_recompute_watcher_floor", lambda **_: False)
    agent._read_sentinel_findings = AsyncMock(return_value=[])
    agent._run_groundskeeper = AsyncMock(return_value={
        "audit_run": True, "stale_found": 80, "archive_candidates": 78,
        "archive_eligible": 2, "archived": 0, "errors": [],
    })
    agent._run_stale_opens_sweep = AsyncMock(return_value=list(STALE))
    agent._run_aged_candidate_archive = AsyncMock(return_value={"archived": 0})

    def fake_post(**kwargs):
        posted.append(kwargs)
        return outcome

    monkeypatch.setattr(vigil, "post_finding_result", fake_post)
    return agent


@pytest.mark.asyncio
async def test_two_cycles_report_the_set_and_the_digest_once(tmp_path, monkeypatch):
    posted: list[dict] = []
    agent = _cycle_agent(tmp_path, monkeypatch, posted)

    first = await agent._run_cycle_inner(AsyncMock())
    agent.save_state(agent._cycle_state)
    second = await agent._run_cycle_inner(AsyncMock())

    assert "stale_open:" in first.summary
    assert "stale_open:" not in second.summary
    assert len(posted) == 1, "the digest must go out once a week, not per cycle"
    assert agent._cycle_state["hygiene_stale_opens"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,retries", [(FAILED, True), (DEDUPED, False)])
async def test_undelivered_digest_is_retried(tmp_path, monkeypatch, outcome, retries):
    """FAILED means nobody was told, so recording it as sent would silence
    the retry for a week. DEDUPED means governance holds it — that counts."""
    posted: list[dict] = []
    agent = _cycle_agent(tmp_path, monkeypatch, posted, outcome=outcome)

    await agent._run_cycle_inner(AsyncMock())
    agent.save_state(agent._cycle_state)
    await agent._run_cycle_inner(AsyncMock())

    assert len(posted) == (2 if retries else 1)


# --- the operator review path -------------------------------------------------


class _FakeClient:
    def __init__(self, entries):
        self.audit_knowledge = AsyncMock(return_value=AuditResult(
            success=True,
            audit={"buckets": {"candidate_for_archive": len(entries)}, "top_stale": entries},
        ))
        self.call_tool = AsyncMock(return_value={"success": True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


CANDIDATES = [
    {"id": "a", "bucket": "candidate_for_archive", "last_activity_days": 40, "activity_score": 0},
    {"id": "b", "bucket": "candidate_for_archive", "last_activity_days": 35, "activity_score": 2},
    {"id": "c", "bucket": "stale", "last_activity_days": 50, "activity_score": 0},
]


def _review_agent(monkeypatch, fake):
    monkeypatch.setattr(vigil, "GovernanceClient", lambda **_: fake)
    agent = vigil.VigilAgent(mcp_url="http://localhost:8767/mcp/")
    agent._ensure_identity = AsyncMock()
    return agent


def _archived_ids(fake) -> list[str]:
    return [c.args[1]["discovery_id"] for c in fake.call_tool.call_args_list]


@pytest.mark.asyncio
async def test_review_lists_without_archiving(monkeypatch, capsys):
    fake = _FakeClient(CANDIDATES)
    agent = _review_agent(monkeypatch, fake)

    assert await agent.run_archive_review() == 0

    out = capsys.readouterr().out
    assert "2 archive candidate(s)" in out  # the stale-bucket entry is not one
    fake.call_tool.assert_not_called()
    agent._ensure_identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_archives_only_inactive_candidates(monkeypatch):
    fake = _FakeClient(CANDIDATES)
    agent = _review_agent(monkeypatch, fake)

    assert await agent.run_archive_review(apply=True) == 0
    assert _archived_ids(fake) == ["a"]


@pytest.mark.asyncio
async def test_apply_with_ids_archives_exactly_those_candidates(monkeypatch):
    fake = _FakeClient(CANDIDATES)
    agent = _review_agent(monkeypatch, fake)

    await agent.run_archive_review(apply=True, ids=["b", "c"])
    # "c" is not a candidate, so naming it archives nothing.
    assert _archived_ids(fake) == ["b"]
