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
        """Rollout plan is merge-disabled-then-flip; the default must not
        change behaviour at deploy."""
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

    def test_verification_source_is_selected(self):
        """Returned so a future weighted implementation needs no schema pass."""
        conn = _FakeConn()
        _run(_Backend(conn).get_recent_outcomes(agent_id="a"))
        assert "verification_source" in conn.sql.split("FROM")[0]


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
        # phase5_emitter is the second caller-controlled trust anchor.
        assert "phase5_emitter" in code

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
