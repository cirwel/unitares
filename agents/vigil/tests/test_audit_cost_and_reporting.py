"""Vigil's auto-archive logged `audit timed out after 15s; skipping cycle` on
every run, and its summary line read as a backlog nothing was draining.

The SDK hardcoded `use_model: "true"` on audit_knowledge, routing the audit
through a local LLM. Measured 2026-09-08 against the live server, scope=open
over 477 entries: 0.1s without it; with it, four consecutive runs took 11.3s,
12.1s, 15.8s and 14.9s on an otherwise idle model slot, straddling the 15s
ceiling two of Vigil's three call sites impose. The field it bought,
`model_assessment`, is written by knowledge_graph_lifecycle.py and read by no
caller in this tree.

Separately the same audit reported `{stale: 90, candidate_for_archive: 2}`,
and the summary paired their sum with the archived count. "92 stale, 0
archived" reads as 92 entries queued and none draining. Auto-archive acts only
on `candidate_for_archive`, and only past the age threshold with zero
activity — live, 0 of the 2 qualified, so doing nothing was correct.

These tests assert on behaviour. An earlier version of this file grepped
module source with inspect.getsource(); review showed those checks passed
against the unfixed code and matched the comments the same commit added, and
that invoking the real _run_groundskeeper without redirecting the log wrote
fabricated cycles into the operator's production Vigil log and spawned the
real Watcher. Both are fixed here — note the module-scope isolation fixture.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

# Import the SAME module objects the agent imports. Review caught an earlier
# version importing `agents.sdk.src.unitares_sdk...`, which pyproject's dual
# path entries resolve to a SECOND module object — the test asserted on a copy
# that Vigil never loads.
from unitares_sdk.client import GovernanceClient
from unitares_sdk.sync_client import SyncGovernanceClient

from agents.vigil import agent as vigil


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep this file's tests out of the operator's real log and off the real
    Watcher. agents/vigil/tests/ does not inherit tests/conftest.py, so the
    redirect that exists there does not apply here."""
    monkeypatch.setattr(vigil, "LOG_FILE", tmp_path / "vigil-test.log", raising=False)
    monkeypatch.setenv("VIGIL_LOG_FILE", str(tmp_path / "vigil-test.log"))


class _Recorder:
    def __init__(self):
        self.args: dict | None = None

    async def acall(self, name, args):
        self.args = args
        return {"success": True, "audit": {"buckets": {}, "top_stale": []}}

    def scall(self, name, args):
        self.args = args
        return {"success": True, "audit": {"buckets": {}, "top_stale": []}}


# --- The cost: the model path must be opt-in -------------------------------


@pytest.mark.asyncio
async def test_audit_does_not_request_the_model_by_default():
    rec = _Recorder()
    client = GovernanceClient.__new__(GovernanceClient)
    client.call_tool = rec.acall

    await client.audit_knowledge(scope="open", top_n=60)

    assert rec.args.get("use_model") in (None, "false"), (
        "audit_knowledge hardcoded use_model=true, so every caller paid an "
        "11-16s model round-trip for a field nothing reads"
    )


@pytest.mark.asyncio
async def test_audit_can_opt_in_to_the_model():
    rec = _Recorder()
    client = GovernanceClient.__new__(GovernanceClient)
    client.call_tool = rec.acall

    await client.audit_knowledge(scope="open", use_model=True)

    assert rec.args["use_model"] == "true"


def test_sync_client_ships_the_same_default():
    """Both clients, or one silently keeps the slow path alive for whichever
    agent happens to use it. Asserted on each class directly — an earlier
    version branched on hasattr() and would have checked the async client
    twice if an import moved."""
    rec = _Recorder()
    client = SyncGovernanceClient.__new__(SyncGovernanceClient)
    client.call_tool = rec.scall

    client.audit_knowledge(scope="open")

    assert rec.args.get("use_model") in (None, "false")
    for cls in (GovernanceClient, SyncGovernanceClient):
        assert inspect.signature(cls.audit_knowledge).parameters[
            "use_model"
        ].default is False


@pytest.mark.asyncio
async def test_no_vigil_call_site_opts_into_the_model():
    """Every audit Vigil makes must take the fast path. Asserted by driving
    the real call sites against a recording client, not by grepping source."""
    seen: list[dict] = []

    async def _audit(**kwargs):
        seen.append(kwargs)

        class _R:
            success = True
            audit = {"buckets": {}, "top_stale": []}

        return _R()

    client = AsyncMock()
    client.audit_knowledge = _audit
    client.cleanup_knowledge = AsyncMock(
        return_value=type("C", (), {"success": True, "cleaned_total": 0})()
    )

    agent = vigil.VigilAgent.__new__(vigil.VigilAgent)
    agent.with_hygiene = True
    with patch.object(vigil.subprocess, "run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        await vigil.VigilAgent._run_groundskeeper(agent, client, None)

    assert seen, "groundskeeper did not audit at all"
    assert all(k.get("use_model") in (None, False) for k in seen), (
        f"a Vigil audit call site opted into the model path: {seen}"
    )


# --- The reporting: name the number the archiver acts on -------------------


def test_eligible_predicate_excludes_candidates_under_the_threshold():
    """The live case. Two entries sat in candidate_for_archive at 44 and 39
    days against a 90-day threshold, so the actionable queue was empty."""
    top_stale = [
        {"bucket": "candidate_for_archive", "last_activity_days": 44, "activity_score": 0},
        {"bucket": "candidate_for_archive", "last_activity_days": 39, "activity_score": 0},
    ]
    assert vigil.eligible_for_archive(top_stale, 90) == []
    assert len(vigil.eligible_for_archive(top_stale, 30)) == 2


def test_eligible_predicate_excludes_active_and_wrong_bucket():
    top_stale = [
        {"bucket": "stale", "last_activity_days": 200, "activity_score": 0},
        {"bucket": "candidate_for_archive", "last_activity_days": 200, "activity_score": 3},
        {"bucket": "candidate_for_archive", "last_activity_days": 200, "activity_score": 0},
    ]
    assert len(vigil.eligible_for_archive(top_stale, 90)) == 1


@pytest.mark.asyncio
async def test_summary_separates_the_three_populations():
    """The live numbers behind the misleading line: 90 stale, 2 candidates,
    0 actually archivable."""
    class _Audit:
        success = True
        audit = {
            "buckets": {"healthy": 352, "aging": 33, "stale": 90,
                        "candidate_for_archive": 2},
            "top_stale": [
                {"bucket": "candidate_for_archive", "last_activity_days": 44,
                 "activity_score": 0},
                {"bucket": "candidate_for_archive", "last_activity_days": 39,
                 "activity_score": 0},
            ],
        }

    client = AsyncMock()
    client.audit_knowledge = AsyncMock(return_value=_Audit())
    client.cleanup_knowledge = AsyncMock(
        return_value=type("C", (), {"success": True, "cleaned_total": 0})()
    )

    agent = vigil.VigilAgent.__new__(vigil.VigilAgent)
    agent.with_hygiene = True
    with patch.object(vigil.subprocess, "run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        summary = await vigil.VigilAgent._run_groundskeeper(agent, client, None)

    assert summary["stale_found"] == 92
    assert summary["archive_candidates"] == 2
    assert summary["archive_eligible"] == 0, (
        "reporting the bucket size still overstates the queue — both entries "
        "are younger than the threshold, so nothing was actionable and "
        "'0 archived' was correct"
    )


@pytest.mark.asyncio
async def test_note_is_not_suppressed_when_only_the_eligible_count_moves():
    """Dedup keyed on stale_found alone: 90+2 to 89+3 keeps the sum at 92 and
    silently suppressed the note, hiding the movement the line exists to show."""
    class _Audit:
        success = True
        audit = {
            "buckets": {"stale": 89, "candidate_for_archive": 3},
            "top_stale": [
                {"bucket": "candidate_for_archive", "last_activity_days": 200,
                 "activity_score": 0},
            ],
        }

    client = AsyncMock()
    client.audit_knowledge = AsyncMock(return_value=_Audit())
    client.cleanup_knowledge = AsyncMock(
        return_value=type("C", (), {"success": True, "cleaned_total": 0})()
    )
    client.leave_note = AsyncMock()

    agent = vigil.VigilAgent.__new__(vigil.VigilAgent)
    agent.with_hygiene = True
    prev = {"groundskeeper_stale": 92, "groundskeeper_eligible": 0}
    with patch.object(vigil.subprocess, "run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        summary = await vigil.VigilAgent._run_groundskeeper(agent, client, prev)

    assert summary["stale_found"] == 92, "sum unchanged — the suppression case"
    assert summary["archive_eligible"] == 1, "but the actionable count moved"
    client.leave_note.assert_awaited()


@pytest.mark.asyncio
async def test_absent_previous_eligible_does_not_force_a_note():
    """First cycle after deploy: prev_state predates the field. Absent must
    read as 'no evidence of change', not as a change."""
    class _Audit:
        success = True
        audit = {"buckets": {"stale": 92, "candidate_for_archive": 0},
                 "top_stale": []}

    client = AsyncMock()
    client.audit_knowledge = AsyncMock(return_value=_Audit())
    client.cleanup_knowledge = AsyncMock(
        return_value=type("C", (), {"success": True, "cleaned_total": 0})()
    )
    client.leave_note = AsyncMock()

    agent = vigil.VigilAgent.__new__(vigil.VigilAgent)
    agent.with_hygiene = True
    with patch.object(vigil.subprocess, "run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        await vigil.VigilAgent._run_groundskeeper(
            agent, client, {"groundskeeper_stale": 92}
        )

    client.leave_note.assert_not_awaited()
