"""Tests for Vigil's Sentinel-coordination arc.

Vigil reads high-severity Sentinel notes from the KG at the start of each
cycle and either references them in its check-in or forces a groundskeeper
pass, depending on the finding type. These tests pin the behavior.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(project_root))

module_path = project_root / "agents" / "vigil" / "agent.py"
spec = importlib.util.spec_from_file_location("vigil_agent", module_path)
assert spec and spec.loader
_hb_module = importlib.util.module_from_spec(spec)
sys.modules["vigil_agent"] = _hb_module
spec.loader.exec_module(_hb_module)

from vigil_agent import VigilAgent, _filter_sentinel_findings, _SENTINEL_AUDIT_TRIGGERS

from unitares_sdk.models import (
    ArchiveResult,
    AuditResult,
    CleanupResult,
    NoteResult,
    SearchResult,
)

_hb_module.LOG_FILE = Path(tempfile.gettempdir()) / "unitares-heartbeat-test.log"


# =============================================================================
# Pure filter tests
# =============================================================================

class TestFilterSentinelFindings:
    def test_extracts_sentinel_high_notes(self):
        results = [
            {
                "id": "d1",
                "summary": "[Sentinel] coordinated drop",
                "tags": ["sentinel", "coordinated_coherence_drop", "high"],
                "created_at": "2026-04-14T10:00:00+00:00",
            },
        ]
        out = _filter_sentinel_findings(results, since_iso=None)
        assert len(out) == 1
        assert out[0]["type"] == "coordinated_coherence_drop"
        assert out[0]["id"] == "d1"

    def test_drops_notes_older_than_since_iso(self):
        results = [
            {
                "id": "old",
                "summary": "stale",
                "tags": ["sentinel", "verdict_shift", "high"],
                "created_at": "2026-04-14T09:00:00+00:00",
            },
            {
                "id": "new",
                "summary": "fresh",
                "tags": ["sentinel", "verdict_shift", "high"],
                "created_at": "2026-04-14T11:00:00+00:00",
            },
        ]
        out = _filter_sentinel_findings(results, since_iso="2026-04-14T10:00:00+00:00")
        assert [f["id"] for f in out] == ["new"]

    def test_drops_non_sentinel_and_non_high(self):
        results = [
            # Missing "sentinel" tag
            {
                "id": "a",
                "tags": ["vigil", "groundskeeper", "audit"],
                "created_at": "2026-04-14T10:00:00+00:00",
            },
            # Missing "high" tag
            {
                "id": "b",
                "tags": ["sentinel", "fleet_entropy_outlier"],
                "created_at": "2026-04-14T10:00:00+00:00",
            },
            # Valid
            {
                "id": "c",
                "summary": "ok",
                "tags": ["sentinel", "correlated_events", "high"],
                "created_at": "2026-04-14T10:00:00+00:00",
            },
        ]
        out = _filter_sentinel_findings(results, since_iso=None)
        assert [f["id"] for f in out] == ["c"]

    def test_handles_missing_created_at_gracefully(self):
        results = [{
            "id": "n",
            "summary": "no ts",
            "tags": ["sentinel", "verdict_shift", "high"],
        }]
        # No created_at and no since_iso → keep it
        out = _filter_sentinel_findings(results, since_iso=None)
        assert len(out) == 1
        # No created_at but since_iso set → still keep (can't prove it's old)
        out = _filter_sentinel_findings(results, since_iso="2026-04-14T10:00:00+00:00")
        assert len(out) == 1


# =============================================================================
# Read-from-KG tests
# =============================================================================

def _make_agent(with_audit: bool = True) -> VigilAgent:
    agent = VigilAgent(mcp_url="http://localhost:8767/mcp/", with_audit=with_audit)
    agent.client_session_id = "test-session-id"
    return agent


def _mock_search_result(results):
    return SearchResult(success=True, results=results)


class TestReadSentinelFindings:
    @pytest.mark.asyncio
    async def test_returns_filtered_findings(self):
        agent = _make_agent()
        client = MagicMock()
        client.search_knowledge = AsyncMock(return_value=_mock_search_result([
            {
                "id": "x",
                "summary": "fleet drop",
                "tags": ["sentinel", "coordinated_coherence_drop", "high"],
                "created_at": "2026-04-14T11:00:00+00:00",
            },
        ]))

        out = await agent._read_sentinel_findings(
            client, since_iso="2026-04-14T10:00:00+00:00"
        )
        assert len(out) == 1
        assert out[0]["type"] == "coordinated_coherence_drop"
        client.search_knowledge.assert_awaited_once_with(
            query="sentinel", tags=["sentinel"], limit=10, semantic=False,
        )

    @pytest.mark.asyncio
    async def test_returns_empty_on_search_error(self):
        agent = _make_agent()
        client = MagicMock()
        client.search_knowledge = AsyncMock(side_effect=RuntimeError("kg down"))

        out = await agent._read_sentinel_findings(client, since_iso=None)
        assert out == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_unsuccessful_result(self):
        agent = _make_agent()
        client = MagicMock()
        client.search_knowledge = AsyncMock(
            return_value=SearchResult(success=False, error="no index")
        )
        out = await agent._read_sentinel_findings(client, since_iso=None)
        assert out == []


# =============================================================================
# End-to-end cycle tests
# =============================================================================

def _full_mock_client(search_results=None):
    """Client with enough surface to run a full Vigil cycle in tests."""
    client = MagicMock()
    client.search_knowledge = AsyncMock(return_value=_mock_search_result(
        search_results or []
    ))
    client.audit_knowledge = AsyncMock(return_value=AuditResult(
        success=True,
        results=[{"buckets": {"healthy": 5, "aging": 2, "stale": 0, "candidate_for_archive": 0}}],
    ))
    client.cleanup_knowledge = AsyncMock(return_value=CleanupResult(success=True, cleaned=0))
    client.archive_orphan_agents = AsyncMock(return_value=ArchiveResult(success=True, archived=0))
    client.leave_note = AsyncMock(return_value=NoteResult(success=True))
    return client


def _patch_health_checks(monkeypatch):
    """Stub network health checks so run_cycle doesn't try real HTTP.

    With the registry refactor, health probes go through run_health_checks
    (imported into agent.py). Returning [] means no checks ran — defaults in
    _collect_health_state treat governance and lumen as healthy.
    """
    async def _no_checks(prev_state=None):
        return []
    monkeypatch.setattr(_hb_module, "run_health_checks", _no_checks)


class TestSentinelTriggerNamesMatchEmittedTypes:
    """Vigil's audit-trigger set must contain types Sentinel actually emits.

    Discovery 2026-04-25T10:49:00 documented production divergence: Vigil watched for
    `verdict_distribution_shift` / `correlated_governance_events`, but Sentinel emits
    `verdict_shift` / `correlated_events` (agents/sentinel/agent.py:249,266). The
    short names are also the canonical taxonomy entries (src/violation_taxonomy.yaml).
    Tests previously passed because their fixtures used the long names too — the
    integration-level mismatch was invisible.
    """

    def test_verdict_shift_triggers_audit(self):
        assert "verdict_shift" in _SENTINEL_AUDIT_TRIGGERS, (
            "Sentinel emits 'verdict_shift' (sentinel/agent.py:249) — Vigil must trigger "
            "audit on it; sentinel_force_audit otherwise never fires in production."
        )

    def test_correlated_events_triggers_audit(self):
        assert "correlated_events" in _SENTINEL_AUDIT_TRIGGERS, (
            "Sentinel emits 'correlated_events' (sentinel/agent.py:266) — Vigil must "
            "trigger audit on it."
        )

    def test_old_long_names_are_not_in_trigger_set(self):
        # Catches regressions back to the divergent vocabulary.
        assert "verdict_distribution_shift" not in _SENTINEL_AUDIT_TRIGGERS
        assert "correlated_governance_events" not in _SENTINEL_AUDIT_TRIGGERS


class TestRunCycleCoordination:
    @pytest.mark.asyncio
    async def test_audit_triggering_finding_forces_groundskeeper(self, monkeypatch):
        _patch_health_checks(monkeypatch)

        agent = _make_agent(with_audit=False)  # audit OFF by default
        agent.load_state = lambda: {"cycle_time": "2026-04-14T10:00:00+00:00"}

        client = _full_mock_client(search_results=[{
            "id": "shift1",
            "summary": "10% reject rate",
            "tags": ["sentinel", "verdict_shift", "high"],
            "created_at": "2026-04-14T11:00:00+00:00",
        }])

        result = await agent.run_cycle(client)

        assert result is not None
        # Groundskeeper ran despite with_audit=False
        client.audit_knowledge.assert_awaited()
        # Finding and the forced-audit marker both appear in the check-in summary
        assert "Sentinel/verdict_shift" in result.summary
        assert "Groundskeeper forced by Sentinel coordination" in result.summary

    @pytest.mark.asyncio
    async def test_non_audit_finding_references_but_does_not_force(self, monkeypatch):
        _patch_health_checks(monkeypatch)

        agent = _make_agent(with_audit=False)
        agent.load_state = lambda: {"cycle_time": "2026-04-14T10:00:00+00:00"}

        client = _full_mock_client(search_results=[{
            "id": "drop1",
            "summary": "coherence fell 0.2 across 3 agents",
            "tags": ["sentinel", "coordinated_coherence_drop", "high"],
            "created_at": "2026-04-14T11:00:00+00:00",
        }])

        result = await agent.run_cycle(client)

        # Coherence drop does NOT force an audit — groundskeeper stays off
        client.audit_knowledge.assert_not_awaited()
        # But the finding IS referenced in the check-in so the chain is auditable
        assert "Sentinel/coordinated_coherence_drop" in result.summary

    @pytest.mark.asyncio
    async def test_no_sentinel_findings_preserves_normal_cycle(self, monkeypatch):
        _patch_health_checks(monkeypatch)

        agent = _make_agent(with_audit=True)
        agent.load_state = lambda: {"cycle_time": "2026-04-14T10:00:00+00:00"}

        client = _full_mock_client(search_results=[])

        result = await agent.run_cycle(client)

        assert result is not None
        assert "Sentinel/" not in result.summary
        # Groundskeeper runs as normal when with_audit=True
        client.audit_knowledge.assert_awaited()

    @pytest.mark.asyncio
    async def test_non_audit_cycle_preserves_groundskeeper_counts(self, monkeypatch):
        """Non-audit cycles must carry over prev_state's groundskeeper counts.

        Regression for the dedupe failure across audit-gated cycles: a non-audit
        cycle was overwriting groundskeeper_stale/archived with 0, so the next
        audit cycle compared against 0 and the "unchanged" check fired even when
        the real counts hadn't moved — duplicate KG note every time.
        """
        _patch_health_checks(monkeypatch)

        agent = _make_agent(with_audit=False)
        agent.load_state = lambda: {
            "cycle_time": "2026-04-14T10:00:00+00:00",
            "groundskeeper_stale": 5,
            "groundskeeper_archived": 2,
        }

        client = _full_mock_client(search_results=[])
        await agent.run_cycle(client)

        # Audit didn't run this cycle
        client.audit_knowledge.assert_not_awaited()
        # Counts carry over from prev_state, not zeroed
        assert agent._cycle_state["groundskeeper_stale"] == 5
        assert agent._cycle_state["groundskeeper_archived"] == 2

    @pytest.mark.asyncio
    async def test_broken_search_does_not_break_cycle(self, monkeypatch):
        _patch_health_checks(monkeypatch)

        agent = _make_agent(with_audit=True)
        agent.load_state = lambda: {"cycle_time": "2026-04-14T10:00:00+00:00"}

        client = _full_mock_client()
        client.search_knowledge = AsyncMock(side_effect=RuntimeError("kg timeout"))

        result = await agent.run_cycle(client)

        # Cycle completes, no Sentinel findings referenced, groundskeeper still ran
        assert result is not None
        assert "Sentinel/" not in result.summary
        client.audit_knowledge.assert_awaited()
