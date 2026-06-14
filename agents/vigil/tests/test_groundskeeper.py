"""
Tests for Vigil's groundskeeper duties and change detection.

All MCP calls are mocked — no live server required.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load vigil_agent module from its new location via importlib
project_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(project_root))

module_path = project_root / "agents" / "vigil" / "agent.py"
spec = importlib.util.spec_from_file_location("vigil_agent", module_path)
assert spec and spec.loader
_hb_module = importlib.util.module_from_spec(spec)
sys.modules["vigil_agent"] = _hb_module
spec.loader.exec_module(_hb_module)

from vigil_agent import (
    VigilAgent,
    detect_changes,
)

from unitares_sdk.models import (
    AuditResult,
    CleanupResult,
    NoteResult,
)

# Redirect log output to a temp file so tests don't pollute Vigil's production log
_hb_module.LOG_FILE = Path(tempfile.gettempdir()) / "unitares-heartbeat-test.log"


# =============================================================================
# Test helpers
# =============================================================================

def _make_agent(with_audit: bool = True) -> VigilAgent:
    """Create a VigilAgent with mocked identity."""
    agent = VigilAgent(
        mcp_url="http://localhost:8767/mcp/",
        with_audit=with_audit,
    )
    agent.client_session_id = "test-session-id"
    return agent


def _make_mock_client(
    audit_result=None,
    cleanup_result=None,
):
    """Create a mock GovernanceClient for groundskeeper tests."""
    client = AsyncMock()

    client.audit_knowledge = AsyncMock(return_value=audit_result or AuditResult(
        success=True,
        audit={"buckets": {"healthy": 5, "aging": 2, "stale": 1, "candidate_for_archive": 0}},
    ))
    client.cleanup_knowledge = AsyncMock(return_value=cleanup_result or CleanupResult(
        success=True, cleaned=0,
    ))
    client.leave_note = AsyncMock(return_value=NoteResult(success=True))

    return client


# =============================================================================
# Tests: _run_groundskeeper
# =============================================================================

class TestRunGroundskeeper:
    """Tests for the groundskeeper method."""

    @pytest.mark.asyncio
    async def test_groundskeeper_calls_audit(self):
        """Groundskeeper should call audit_knowledge."""
        agent = _make_agent()
        client = _make_mock_client()
        result = await agent._run_groundskeeper(client)

        client.audit_knowledge.assert_called_once()
        assert result["audit_run"] is True

    @pytest.mark.asyncio
    async def test_groundskeeper_triggers_cleanup_on_candidates(self):
        """When audit finds archive candidates, cleanup should be triggered."""
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(
                success=True,
                audit={"buckets": {"healthy": 2, "aging": 1, "stale": 1, "candidate_for_archive": 3}},
            ),
            cleanup_result=CleanupResult(success=True, cleaned=3),
        )
        result = await agent._run_groundskeeper(client)

        client.cleanup_knowledge.assert_called_once()
        assert result["archived"] == 3
        assert result["stale_found"] == 4  # 1 stale + 3 candidate

    @pytest.mark.asyncio
    async def test_groundskeeper_skips_cleanup_when_no_candidates(self):
        """When no archive candidates, cleanup should not be called."""
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(
                success=True,
                audit={"buckets": {"healthy": 5, "aging": 0, "stale": 0, "candidate_for_archive": 0}},
            ),
        )
        await agent._run_groundskeeper(client)
        client.cleanup_knowledge.assert_not_called()

    @pytest.mark.asyncio
    async def test_groundskeeper_does_not_sweep_orphans(self):
        """Groundskeeper no longer calls archive_orphan_agents.

        Regression against the 2026-04-19 aggressive-sweep fix: the auto-sweep
        was hiding initializing-agent bugs. Operators invoke the MCP tool
        manually if they want a sweep.
        """
        agent = _make_agent()
        client = _make_mock_client()
        result = await agent._run_groundskeeper(client)
        # Client no longer has archive_orphan_agents called on it. We use a
        # spec-less AsyncMock so attribute access wouldn't error — assert via
        # the result shape instead.
        assert "orphans_archived" not in result

    @pytest.mark.asyncio
    async def test_groundskeeper_leaves_note(self):
        """Groundskeeper should leave a summary note with correct tags."""
        agent = _make_agent()
        client = _make_mock_client()
        await agent._run_groundskeeper(client)

        client.leave_note.assert_called_once()
        call_kwargs = client.leave_note.call_args.kwargs
        assert "groundskeeper" in call_kwargs["tags"]
        assert "vigil" in call_kwargs["tags"]
        assert "ephemeral" in call_kwargs["tags"]

    @pytest.mark.asyncio
    async def test_groundskeeper_suppresses_note_when_unchanged(self):
        """When stale/archived counts match prev_state, skip leave_note.

        Regression for the KG spam where Vigil posted an identical
        'Groundskeeper: 134 stale, 4 archived' note every 30 minutes
        because the audit/cleanup mismatch keeps the numbers pinned.
        """
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(
                success=True,
                audit={"buckets": {"healthy": 2, "stale": 1, "candidate_for_archive": 3}},
            ),
            cleanup_result=CleanupResult(success=True, cleaned=3),
        )
        prev = {"groundskeeper_stale": 4, "groundskeeper_archived": 3}
        result = await agent._run_groundskeeper(client, prev_state=prev)

        client.leave_note.assert_not_called()
        assert result["note_suppressed"] is True

    @pytest.mark.asyncio
    async def test_groundskeeper_suppresses_when_only_archived_differs(self):
        """Backlog flat, archived oscillating between 0 and 1 — must suppress.

        Regression: the two-key (stale, archived) dedup let oscillation
        through. Observed pattern in production was 4 stale / 0 archived
        flip-flopping with 4 stale / 1 archived every 30 minutes, producing
        a fresh KG row each tick. The persistent backlog is 4 in both
        states; archived is per-cycle progress, not state. Now we compare
        stale_found only.
        """
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(
                success=True,
                audit={"buckets": {"healthy": 2, "stale": 1, "candidate_for_archive": 3}},
            ),
            cleanup_result=CleanupResult(success=True, cleaned=0),
        )
        prev = {"groundskeeper_stale": 4, "groundskeeper_archived": 1}
        result = await agent._run_groundskeeper(client, prev_state=prev)

        client.leave_note.assert_not_called()
        assert result["note_suppressed"] is True

    @pytest.mark.asyncio
    async def test_groundskeeper_posts_note_on_change(self):
        """When numbers differ from prev_state, leave_note must fire."""
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(
                success=True,
                audit={"buckets": {"healthy": 2, "stale": 1, "candidate_for_archive": 3}},
            ),
            cleanup_result=CleanupResult(success=True, cleaned=3),
        )
        prev = {"groundskeeper_stale": 100, "groundskeeper_archived": 0}
        await agent._run_groundskeeper(client, prev_state=prev)
        client.leave_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_groundskeeper_posts_note_on_first_cycle(self):
        """No prev_state (cold start) must still post the inaugural note."""
        agent = _make_agent()
        client = _make_mock_client()
        await agent._run_groundskeeper(client, prev_state=None)
        client.leave_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_groundskeeper_handles_audit_failure(self):
        """Gracefully handles audit tool failure."""
        agent = _make_agent()
        client = _make_mock_client(
            audit_result=AuditResult(success=False, results=[]),
        )
        result = await agent._run_groundskeeper(client)

        assert result["audit_run"] is False
        assert len(result["errors"]) > 0


# =============================================================================
# Tests: _run_aged_candidate_archive (KG hygiene v2 — act-on-candidates)
# =============================================================================

def _audit_with_top_stale(top_stale: List[Dict[str, Any]]) -> AuditResult:
    return AuditResult(
        success=True,
        audit={
            "buckets": {
                "healthy": 0, "aging": 0,
                "stale": 0, "candidate_for_archive": len(top_stale),
            },
            "top_stale": top_stale,
        },
    )


class TestRunAgedCandidateArchive:
    """Tests for the auto-archive bridge between audit and lifecycle cleanup."""

    @pytest.mark.asyncio
    async def test_skipped_when_hygiene_off(self):
        """Default state: hygiene off → no audit, no archive calls."""
        agent = _make_agent()
        agent.with_hygiene = False
        client = AsyncMock()
        result = await agent._run_aged_candidate_archive(client)
        assert result["auto_archive_run"] is False
        assert result["archived"] == 0
        client.audit_knowledge.assert_not_called()
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_archives_aged_candidates(self):
        """With hygiene on: aged candidate_for_archive entries get archived."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "2025-01-01T00:00:00", "bucket": "candidate_for_archive", "last_activity_days": 120},
            {"id": "2025-01-02T00:00:00", "bucket": "candidate_for_archive", "last_activity_days": 100},
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["auto_archive_run"] is True
        assert result["archived"] == 2
        assert result["candidates_seen"] == 2
        assert client.call_tool.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_below_threshold(self):
        """Entries with age below threshold (default 90d) are not touched."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "x", "bucket": "candidate_for_archive", "last_activity_days": 60},
            {"id": "y", "bucket": "candidate_for_archive", "last_activity_days": 89},
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["candidates_seen"] == 0
        assert result["archived"] == 0
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_other_buckets(self):
        """Stale/aging/healthy entries are not touched even if very old."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "stale-x", "bucket": "stale", "last_activity_days": 200},
            {"id": "aging-y", "bucket": "aging", "last_activity_days": 200},
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["candidates_seen"] == 0
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_caps_at_max_per_cycle(self, monkeypatch):
        """Cap respected: only N entries archived per cycle even if more eligible.

        Mock returns exactly ``max_per_cycle * 3`` entries — that's the
        ``top_n`` the audit is called with, so this matches the production
        contract (server respects top_n; we just slice harder).
        """
        monkeypatch.setenv("VIGIL_AUTO_ARCHIVE_MAX_PER_CYCLE", "3")
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": f"e{i}", "bucket": "candidate_for_archive", "last_activity_days": 120, "activity_score": 0}
            for i in range(9)
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["candidates_seen"] == 3
        assert result["archived"] == 3
        assert client.call_tool.call_count == 3

    @pytest.mark.asyncio
    async def test_high_sev_falls_back_to_closed(self):
        """High-severity rejection on archive triggers retry with status=closed."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "hi-sev", "bucket": "candidate_for_archive", "last_activity_days": 120},
        ]))
        client.call_tool = AsyncMock(side_effect=[
            {"success": False, "error": "Permission denied: Cannot set status 'archived' on high-severity discovery 'hi-sev'."},
            {"success": True},
        ])
        result = await agent._run_aged_candidate_archive(client)
        assert result["archived"] == 1
        assert client.call_tool.call_count == 2
        # Second call must be the closed fallback
        second_call_args = client.call_tool.call_args_list[1]
        assert second_call_args.args[1]["status"] == "closed"

    @pytest.mark.asyncio
    async def test_per_entry_failure_does_not_poison_rest(self):
        """A failure on one entry doesn't stop the rest of the batch."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "ok-1", "bucket": "candidate_for_archive", "last_activity_days": 120},
            {"id": "boom", "bucket": "candidate_for_archive", "last_activity_days": 120},
            {"id": "ok-2", "bucket": "candidate_for_archive", "last_activity_days": 120},
        ]))
        client.call_tool = AsyncMock(side_effect=[
            {"success": True},
            Exception("transient"),
            {"success": True},
        ])
        result = await agent._run_aged_candidate_archive(client)
        assert result["archived"] == 2
        assert any("boom" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_audit_failure_returns_clean_summary(self):
        """Audit failure surfaces in errors but doesn't raise."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=AuditResult(success=False))
        client.call_tool = AsyncMock()
        result = await agent._run_aged_candidate_archive(client)
        assert result["archived"] == 0
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_entries_with_related_links(self):
        """Defense-in-depth: entries with related_to (activity_score>0) are not archived.

        The bucket classifier in _score_discovery only checks responses_from
        for the healthy guard, not related_to. An entry that's heavily
        cross-linked but never replied to can land in candidate_for_archive
        despite being load-bearing. We re-check activity_score here.
        """
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "isolated", "bucket": "candidate_for_archive", "last_activity_days": 120, "activity_score": 0},
            {"id": "linked", "bucket": "candidate_for_archive", "last_activity_days": 120, "activity_score": 3},
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["candidates_seen"] == 1
        assert client.call_tool.call_count == 1
        archived_id = client.call_tool.call_args.args[1]["discovery_id"]
        assert archived_id == "isolated"

    @pytest.mark.asyncio
    async def test_high_sev_uses_error_code_first(self):
        """Prefer the structured error_code over substring matching."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "hi-sev", "bucket": "candidate_for_archive", "last_activity_days": 120, "activity_score": 0},
        ]))
        client.call_tool = AsyncMock(side_effect=[
            # Server returns a refactored error message without "high-severity"
            # but still emits the structured error_code. Old substring check
            # would miss this.
            {"success": False, "error": "Permission denied: severity-locked.", "error_code": "PERMISSION_DENIED"},
            {"success": True},
        ])
        result = await agent._run_aged_candidate_archive(client)
        assert result["archived"] == 1
        assert client.call_tool.call_count == 2
        assert client.call_tool.call_args_list[1].args[1]["status"] == "closed"

    @pytest.mark.asyncio
    async def test_non_dict_response_does_not_crash(self):
        """Server returning non-dict (None, primitive) is reported, not raised."""
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "weird", "bucket": "candidate_for_archive", "last_activity_days": 120, "activity_score": 0},
        ]))
        client.call_tool = AsyncMock(return_value=None)
        result = await agent._run_aged_candidate_archive(client)
        assert result["archived"] == 0
        assert any("non-dict" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_age_threshold_env_override(self, monkeypatch):
        """VIGIL_AUTO_ARCHIVE_AGE_DAYS=45 should make 60-day entries eligible."""
        monkeypatch.setenv("VIGIL_AUTO_ARCHIVE_AGE_DAYS", "45")
        agent = _make_agent()
        agent.with_hygiene = True
        client = AsyncMock()
        client.audit_knowledge = AsyncMock(return_value=_audit_with_top_stale([
            {"id": "e1", "bucket": "candidate_for_archive", "last_activity_days": 60, "activity_score": 0},
            {"id": "e2", "bucket": "candidate_for_archive", "last_activity_days": 40, "activity_score": 0},
        ]))
        client.call_tool = AsyncMock(return_value={"success": True})
        result = await agent._run_aged_candidate_archive(client)
        assert result["candidates_seen"] == 1
        assert client.call_tool.call_args.args[1]["discovery_id"] == "e1"


# =============================================================================
# Tests: watcher --scan-commits wiring
# =============================================================================

class TestGroundskeeperScanCommits:
    """Watcher --scan-commits is called each cycle and exceptions don't propagate."""

    @pytest.mark.asyncio
    async def test_scan_commits_argv(self, monkeypatch):
        """subprocess.run must be called with --scan-commits in argv."""
        calls = []

        class _FakeResult:
            returncode = 0
            stdout = "resolved 0"
            stderr = ""

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return _FakeResult()

        monkeypatch.setattr(_hb_module.subprocess, "run", _fake_run)

        agent = _make_agent()
        client = _make_mock_client()
        await agent._run_groundskeeper(client)

        scan_calls = [c for c in calls if "--scan-commits" in c]
        assert len(scan_calls) == 1
        assert any("watcher" in str(a) and "agent.py" in str(a) for a in scan_calls[0])

    @pytest.mark.asyncio
    async def test_scan_commits_exception_does_not_propagate(self, monkeypatch):
        """A crash in subprocess.run must not raise out of _run_groundskeeper."""

        def _raise(*args, **kwargs):
            raise RuntimeError("watcher exploded")

        monkeypatch.setattr(_hb_module.subprocess, "run", _raise)

        agent = _make_agent()
        client = _make_mock_client()
        result = await agent._run_groundskeeper(client)
        assert isinstance(result, dict)


# =============================================================================
# Tests: with_audit flag
# =============================================================================

class TestWithAuditFlag:
    """Tests for the --no-audit CLI flag."""

    def test_default_with_audit_true(self):
        """By default, with_audit should be True."""
        agent = VigilAgent()
        assert agent.with_audit is True

    def test_with_audit_false(self):
        """with_audit=False should be settable."""
        agent = VigilAgent(with_audit=False)
        assert agent.with_audit is False


# =============================================================================
# Tests: detect_changes with groundskeeper state
# =============================================================================

class TestDetectChangesGroundskeeper:
    """Tests for change detection with groundskeeper staleness tracking."""

    def test_stale_spike_generates_note(self):
        prev = {"groundskeeper_stale": 5}
        curr = {"groundskeeper_stale": 20}
        changes = detect_changes(prev, curr)
        gk_changes = [c for c in changes if "groundskeeper" in c.get("tags", [])]
        assert len(gk_changes) == 1
        assert "spike" in gk_changes[0]["summary"].lower()

    def test_stale_stable_no_note(self):
        prev = {"groundskeeper_stale": 5}
        curr = {"groundskeeper_stale": 8}
        changes = detect_changes(prev, curr)
        gk_changes = [c for c in changes if "groundskeeper" in c.get("tags", [])]
        assert len(gk_changes) == 0

    def test_stale_decrease_no_note(self):
        prev = {"groundskeeper_stale": 20}
        curr = {"groundskeeper_stale": 5}
        changes = detect_changes(prev, curr)
        gk_changes = [c for c in changes if "groundskeeper" in c.get("tags", [])]
        assert len(gk_changes) == 0

    def test_no_previous_stale_no_note(self):
        prev = {}
        curr = {"groundskeeper_stale": 15}
        changes = detect_changes(prev, curr)
        gk_changes = [c for c in changes if "groundskeeper" in c.get("tags", [])]
        assert len(gk_changes) == 1  # 15 > 0 + 10


class TestDetectChangesArbitraryService:
    """Health transitions are detected for ANY monitored service, not just a
    hardcoded governance/lumen pair — so a deployment's own health-check
    plugins (e.g. a 'redis' or 'gateway' check) get outage/recovery notes."""

    def test_custom_service_outage_note(self):
        prev = {"redis_healthy": True}
        curr = {"redis_healthy": False, "redis_detail": "Redis: UNREACHABLE"}
        changes = detect_changes(prev, curr)
        outage = [c for c in changes if "redis" in c.get("tags", [])]
        assert len(outage) == 1
        assert "outage" in outage[0]["tags"]
        assert "down" in outage[0]["summary"].lower()

    def test_custom_service_recovery_note(self):
        prev = {"redis_healthy": False}
        curr = {"redis_healthy": True}
        changes = detect_changes(prev, curr)
        recovery = [c for c in changes if "recovery" in c.get("tags", [])]
        assert len(recovery) == 1
        assert "redis" in recovery[0]["tags"]

    def test_custom_service_sustained_outage_note(self):
        prev = {"redis_healthy": False, "redis_down_streak": 2}
        curr = {"redis_healthy": False, "redis_down_streak": 3}
        changes = detect_changes(prev, curr)
        sustained = [c for c in changes if "sustained" in c.get("tags", [])]
        assert len(sustained) == 1
        assert "redis" in sustained[0]["tags"]
        assert "consecutive" in sustained[0]["summary"].lower()

    def test_lumen_sustained_note_unchanged(self):
        """Regression: the canonical Lumen sustained-outage note still reads
        exactly as before the generalization."""
        prev = {"lumen_healthy": False, "lumen_down_streak": 2}
        curr = {"lumen_healthy": False, "lumen_down_streak": 3}
        changes = detect_changes(prev, curr)
        sustained = [c for c in changes if "sustained" in c.get("tags", [])]
        assert len(sustained) == 1
        assert sustained[0]["summary"] == (
            "Lumen unreachable for 3 consecutive cycles (~2h)"
        )


# =============================================================================
# Tests: uptime tracking
# =============================================================================

class TestUptimeTracking:
    def test_counters_increment_from_zero(self):
        prev = {}
        total = prev.get("total_cycles", 0) + 1
        gov_up = prev.get("gov_up_cycles", 0) + 1
        lumen_up = prev.get("lumen_up_cycles", 0) + 0
        assert total == 1
        assert gov_up == 1
        assert lumen_up == 0

    def test_counters_accumulate(self):
        prev = {"total_cycles": 100, "gov_up_cycles": 98, "lumen_up_cycles": 90}
        total = prev.get("total_cycles", 0) + 1
        gov_up = prev.get("gov_up_cycles", 0) + 1
        lumen_up = prev.get("lumen_up_cycles", 0) + 1
        assert total == 101
        assert gov_up == 99
        assert lumen_up == 91

    def test_uptime_percentage_calculation(self):
        state = {"total_cycles": 200, "gov_up_cycles": 198, "lumen_up_cycles": 180}
        gov_pct = state["gov_up_cycles"] / state["total_cycles"]
        lumen_pct = state["lumen_up_cycles"] / state["total_cycles"]
        assert gov_pct == pytest.approx(0.99)
        assert lumen_pct == pytest.approx(0.90)
