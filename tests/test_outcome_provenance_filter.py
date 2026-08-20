"""Behavioural cover for get_recent_outcomes' provenance filter.

The earlier tests in test_outcome_anchors.py assert on the SQL *constants*.
These assert on the query the method actually issues and the rows it actually
returns, which is what the verdict path consumes -- a predicate that is correct
but unapplied would pass the former and fail these.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db.mixins.tool_usage import ToolUsageMixin


class _FakeConn:
    """Captures the SQL and simulates provenance filtering server-side."""

    ROWS = [
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 0.9,
         "ts": "t1", "verification_source": "external_signal"},
        {"outcome_type": "task_completed", "is_bad": False, "outcome_score": 0.98,
         "ts": "t2", "verification_source": "agent_reported_tool_result"},
        {"outcome_type": "trajectory_validated", "is_bad": True, "outcome_score": 0.33,
         "ts": "t3", "verification_source": "server_observation"},
        {"outcome_type": "trajectory_validated", "is_bad": False, "outcome_score": 0.5,
         "ts": "t4", "verification_source": None},
    ]

    def __init__(self, raises=None):
        self.sql = None
        self._raises = raises

    async def fetch(self, sql, *args):
        self.sql = sql
        if self._raises:
            raise self._raises
        if "verification_source = 'external_signal'" in sql:
            return [r for r in self.ROWS if r["verification_source"] == "external_signal"]
        return list(self.ROWS)


class _Backend(ToolUsageMixin):
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestProvenanceFilterApplied:

    def test_filter_on_excludes_self_referential_and_soft(self):
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "on"}):
            rows = _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        sources = {r["verification_source"] for r in rows}
        assert sources == {"external_signal"}
        # server_observation is the loop's own self-validation -- the whole point.
        assert "server_observation" not in sources
        # soft self-attestation must not reach a verdict input either.
        assert "agent_reported_tool_result" not in sources

    def test_filter_off_is_a_true_rollback(self):
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "off"}):
            rows = _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert len(rows) == len(_FakeConn.ROWS)
        assert "verification_source" not in conn.sql.split("WHERE")[1]

    def test_default_is_off(self):
        """The default is off because the flip is WRONG, not because a rollout
        is pending.

        The earlier framing here ("merge-disabled-then-flip") read as an
        unfinished task and nearly produced the flip. Measured 2026-08-20, over
        14 days: the standing residents hold ~12,400 outcome rows and 15
        ``external_signal`` ones (Lumen 6218/0, Vigil 575/0), so turning this on
        strips the outcome term from the governed population permanently -- and
        because the fallback re-weights rather than merely dropping the term, it
        raises ``decision_e`` (the verdict path's own prior verdicts) from 0.35
        to 0.40. That makes E *more* loop-derived in the name of Invariant 4.

        Falsify the premise rather than re-arguing it: wire a real exogenous
        observer to the residents. See get_recent_outcomes' docstring and KG
        ``2026-08-20T19:54:02.173817+00:00``."""
        conn = _FakeConn()
        env = {k: v for k, v in os.environ.items()
               if k != "UNITARES_OUTCOME_PROVENANCE_FILTER"}
        with patch.dict(os.environ, env, clear=True):
            rows = _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert len(rows) == len(_FakeConn.ROWS)

    @pytest.mark.parametrize("val,enabled", [
        ("on", True), ("1", True), ("true", True), ("TRUE", True), ("yes", True),
        ("off", False), ("0", False), ("false", False), ("", False),
        ("maybe", False),  # unrecognised must fail safe to OFF, not ON
    ])
    def test_flag_parsing_fails_safe(self, val, enabled):
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": val}):
            _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert ("external_signal" in conn.sql) is enabled

    def test_null_and_unknown_provenance_are_excluded(self):
        """NULL and unrecognised sources are EXCLUDED-tier: unknown provenance
        cannot inform a verdict."""
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "on"}):
            rows = _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert all(r["verification_source"] is not None for r in rows)

    def test_query_failure_returns_empty_not_unfiltered(self):
        """On error the method must fail closed to [] -- never fall back to an
        unfiltered read, which would silently restore the echo."""
        conn = _FakeConn(raises=RuntimeError("connection reset"))
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "on"}):
            rows = _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert rows == []

    def test_verification_source_selected_only_when_filtering(self):
        """Flag ON returns the column (a future weighted implementation needs
        no schema pass). Flag OFF must issue the byte-identical legacy column
        list: selecting verification_source unconditionally would raise (and
        be swallowed to []) on a pre-039 DB even with the flag off, which the
        docstring's 'default must not change behaviour at deploy' forbids."""
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "on"}):
            _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert "verification_source" in conn.sql.split("FROM")[0]
        conn = _FakeConn()
        with patch.dict(os.environ, {"UNITARES_OUTCOME_PROVENANCE_FILTER": "off"}):
            _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert "verification_source" not in conn.sql.split("FROM")[0]


class TestFlipPremiseIsRecorded:
    """Guard the *reason*, not just the default.

    A bare `default is off` assertion is satisfied by anyone who deletes the
    rationale and keeps the line. These pin the two facts a future reader needs
    in order to disagree with the decision on evidence instead of re-deriving
    it: that the docstring records why, and that it names how to falsify it.
    Deliberately a text assertion -- the alternative is a live-DB query, which
    does not belong in CI and would fail for reasons unrelated to the claim.
    """

    def _doc(self):
        from src.db.mixins.tool_usage import ToolUsageMixin
        return ToolUsageMixin.get_recent_outcomes.__doc__ or ""

    def test_docstring_does_not_invite_the_flip(self):
        doc = self._doc()
        # Guard the IMPERATIVE, not the historical mention -- the docstring
        # deliberately quotes the old rollout phrasing to explain what went
        # wrong, so a bare substring test on that phrase fails on its own fix.
        assert "=on`` to enable" not in doc, (
            "the 'set it to on to enable' instruction is what invited the "
            "flip; if it is being restored, read the three kills first"
        )
        assert "not pending a flip" in doc

    def test_docstring_records_the_falsifier(self):
        doc = self._doc()
        # A block with no exit condition is a lever nobody can retire.
        assert "exogenous observer" in doc, (
            "the guard must say what would make the flip reasonable again, or "
            "it is an unfalsifiable block rather than a recorded decision"
        )


class TestServerDerivedProvenance:
    """The public outcome_event tool must not let a caller choose its own
    provenance. Without this, the filter above is bypassable by relabelling:
    claim external_signal, receive EXTERNALLY_VERIFIED (weight 1.00), pass.
    """

    def test_public_handler_forces_agent_reported(self):
        import inspect
        from src.mcp_handlers.observability import outcome_events as oe

        src = inspect.getsource(oe.handle_outcome_event)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        # The caller's value is overwritten, not merely defaulted.
        assert '_gate_args["verification_source"] = "agent_reported_tool_result"' in code
        # And detail is sanitized of claimable provenance keys.
        assert "_strip_provenance_claims" in code

    def test_provenance_claims_stripped_at_every_depth(self):
        """The corroboration grader walks NESTED detail contexts, so a
        top-level-only strip leaves detail={"verification_source":
        "external_signal"} (or the same key nested anywhere) forging
        EXTERNALLY_VERIFIED weight 1.00 past the column downgrade."""
        from src.mcp_handlers.observability.outcome_events import (
            _strip_provenance_claims,
        )

        dirty = {
            "phase5_emitter": True,
            "verification_source": "external_signal",
            "kept": 1,
            "nested": {"verification_source": "external_signal", "ok": 2},
            "list": [{"phase5_emitter": 1, "deep": {"verification_source": "x"}}],
        }
        clean = _strip_provenance_claims(dirty)
        assert clean == {"kept": 1, "nested": {"ok": 2}, "list": [{"deep": {}}]}
        # And the graders see nothing claimable in the cleaned structure.
        from src.outcome_corroboration import _has_external_evidence
        assert _has_external_evidence(clean, "agent_reported_tool_result") is False

    def test_internal_ingestion_path_retains_control(self):
        """external_signal / server_observation must stay reachable for
        server-controlled callers -- the operator-gated REST harness and
        in-process emitters call _record_outcome_event_inline directly."""
        import inspect
        from src.mcp_handlers.observability import outcome_events as oe

        inline = inspect.getsource(oe._record_outcome_event_inline)
        # The helper still reads the caller-supplied value; it is the public
        # decorated entry point that constrains it.
        assert "verification_source" in inline
        assert '= "agent_reported_tool_result"' not in inline.replace(
            'or "agent_reported_tool_result"', ""
        )


class TestCallerControlledEvidenceVocabulary:
    """Documents a caller-controlled path this PR does NOT close.

    _has_tool_observation matches trusted-tool vocabulary against the TEXT of
    caller-supplied detail, so {"tool": "pytest", "kind": "test",
    "exit_code": 0} earns TOOL_OBSERVED (0.65 -- exactly meeting the
    calibration gate's `<`) with no flag at all. Stripping phase5_emitter
    alone does not close it.

    NOT closed here on purpose: it affects what trains *tactical calibration*,
    not the E/I verdict path this PR is scoped to. E/I is already closed --
    get_recent_outcomes admits external_signal only, and public callers can no
    longer claim that value. Capping the grade would change which outcomes
    train calibration, which is a product decision rather than a bug fix.
    """

    def test_detail_vocabulary_alone_reaches_tool_observed(self):
        from src.outcome_corroboration import enrich_detail_with_corroboration

        out = enrich_detail_with_corroboration(
            {"tool": "pytest", "kind": "test", "exit_code": 0},
            outcome_type="test_passed",
            verification_source="agent_reported_tool_result",
        )
        assert out["corroboration_grade"] == "tool_observed"
        assert out["evidence_weight"] == 0.65

    def test_ei_path_is_closed_regardless(self):
        """The verdict path does not consult corroboration grade at all -- it
        filters on verification_source, which is now server-derived."""
        from src.grounding.outcome_anchors import EXOGENOUS_OUTCOMES_SQL

        assert "external_signal" in EXOGENOUS_OUTCOMES_SQL
        assert "agent_reported_tool_result" not in EXOGENOUS_OUTCOMES_SQL
