"""Behavioural cover for get_recent_outcomes' provenance filter.

The earlier tests in test_outcome_anchors.py assert on the SQL *constants*.
These assert on the query the method actually issues and the rows it actually
returns, which is what the verdict path consumes -- a predicate that is correct
but unapplied would pass the former and fail these.
"""

import json
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



class _RecordingConn:
    """Captures what the shared write path actually hands the database."""

    DETAIL_ARG_INDEX = 13  # agent..is_bad (5) + eisv_* (8) -> detail is $15

    def __init__(self):
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return "outcome-id"

    def stored_detail(self, call=0):
        return json.loads(self.calls[call][1][self.DETAIL_ARG_INDEX])


class _RecordingAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _WritePathHarness(ToolUsageMixin):
    """The real ToolUsageMixin over a recording connection, so a test can read
    the row the mixin would have written rather than simulating it."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _RecordingAcquire(self.conn)


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


class TestCappingIsTheDefault:
    """The 2026-09-18 dialectic review (a36255d62a1310f3) rejected an OPT-IN
    ceiling: a future public write path that forgot to pass it would inherit
    the full grade range silently. Trust is now opt-in and capping is the
    default, so a path that forgets anything is capped rather than uncapped.

    These are behavioural. The earlier versions asserted on `inspect.getsource`
    substrings and all passed with the cap deleted outright.
    """

    @staticmethod
    def _ceiling_seen(args):
        """Run the real write path far enough to capture the grader call."""
        from unittest.mock import patch
        from src.mcp_handlers.observability import outcome_events as oe

        class _Stop(Exception):
            pass

        seen = {}

        def _spy(detail, *, outcome_type, verification_source, ceiling=None):
            seen["ceiling"] = ceiling
            raise _Stop()

        with patch.object(oe, "enrich_detail_with_corroboration", _spy):
            try:
                _run(oe._record_outcome_event_inline(dict(args)))
            except _Stop:
                pass
        return seen.get("ceiling", "NEVER_CALLED")

    def _base(self, **kw):
        args = {
            "outcome_type": "task_completed",
            "agent_id": "test-agent-ceiling",
            "detail": {"source": "sensor_sync"},
        }
        args.update(kw)
        return args

    def test_unvouched_write_is_capped(self):
        assert self._ceiling_seen(
            self._base(verification_source="agent_reported_tool_result")
        ) == "tool_observed"

    def test_unvouched_write_is_capped_even_claiming_server_provenance(self):
        """The invariant that makes a forgotten path safe: an unvouched caller
        is capped by what it IS, not by what it claims. Deriving the cap from
        verification_source alone would re-expose the grader the moment a new
        handler forgot the source downgrade -- the schema lets a caller ask for
        external_signal."""
        for claimed in ("external_signal", "server_observation"):
            assert self._ceiling_seen(
                self._base(verification_source=claimed)
            ) == "tool_observed", claimed

    def test_write_path_with_no_provenance_at_all_is_capped(self):
        """Safe by omission: the enumeration condition in practice. Rather than
        listing today's public entrypoints, this pins the property EVERY path
        inherits -- pass nothing, get capped."""
        assert self._ceiling_seen(self._base()) == "tool_observed"

    def test_vouched_provenance_keeps_the_range_its_own_label_asserts(self):
        """The inversion must not silently disarm legitimate ingestion: the
        operator-gated routes and in-process emitters still reach substrate and
        external grades.

        The ceiling is what the provenance ITSELF claims, not no ceiling. A
        vouched ``server_observation`` row can still be graded
        substrate_observed on its own evidence; what it can no longer do is let
        caller-authored payload text carry it to externally_verified. External
        review of this PR, 2026-09-19.
        """
        assert self._ceiling_seen(
            self._base(
                outcome_type="trajectory_validated",
                verification_source="server_observation",
                _trusted_ingestion=True,
            )
        ) == "substrate_observed"
        assert self._ceiling_seen(
            self._base(
                verification_source="external_signal",
                _trusted_ingestion=True,
            )
        ) == "externally_verified"

    def test_vouched_caller_does_not_get_a_blanket_pass(self):
        """A vouched site that emits AGENT-attested rows -- the Phase-5 evidence
        loop does exactly this -- keeps those rows capped."""
        assert self._ceiling_seen(
            self._base(
                verification_source="agent_reported_tool_result",
                _trusted_ingestion=True,
            )
        ) == "tool_observed"

    def test_public_handler_strips_a_caller_supplied_trust_key(self):
        """A caller must not be able to vouch for ITSELF.

        This asserted on `inspect.getsource` until an external review pointed
        out what source-text coverage permits: leave the pop in a dead branch,
        or re-add the flag after it, and the assertion stays green while caller
        trust is accepted. It now runs the public tool and reads the ceiling
        the grader was actually handed.
        """
        from unittest.mock import patch
        from src.mcp_handlers.observability import outcome_events as oe

        seen = {}

        async def _spy(arguments):
            seen["args"] = dict(arguments)
            return {"success": True, "outcome_id": "x"}

        forged = {
            "outcome_type": "task_completed",
            "agent_id": "test-agent-selfvouch",
            "verification_source": "server_observation",
            "detail": {"source": "sensor_sync", "verified": True},
            "_trusted_ingestion": True,
        }
        with patch.object(oe, "_record_outcome_event_inline", _spy):
            _run(oe.handle_outcome_event(dict(forged)))

        # The assertion is on what the handler FORWARDS, because that is the
        # only place the pop is observable. Asserting the resulting ceiling
        # cannot work: the handler also forces verification_source down to
        # agent_reported_tool_result, so trusted and untrusted resolve to the
        # same "tool_observed" and the removed pop stays invisible. Two
        # independent controls, and this one pins the control it names.
        assert "_trusted_ingestion" not in seen.get("args", {}), seen
        # The other control, asserted here so removing EITHER is visible.
        assert seen["args"].get("verification_source") == "agent_reported_tool_result"

    def test_trusted_call_sites_are_the_expected_four(self):
        """A new trust grant should be a deliberate, reviewable act -- not
        something that accretes.

        This matched two exact source SPELLINGS until an external review
        pointed out that `args[_TRUSTED_INGESTION_KEY] = internal`, or any
        helper, evades both while granting the same trust. It now finds every
        module that so much as names the key, by AST, in either form: the
        string literal or the constant.
        """
        import ast
        import pathlib as _pl

        root = _pl.Path(__file__).parent.parent
        key = "_trusted_ingestion"
        const = "_TRUSTED_INGESTION_KEY"
        vouched = set()
        for f in sorted(root.glob("src/**/*.py")):
            try:
                tree = ast.parse(f.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == key:
                    vouched.add(str(f.relative_to(root)))
                if isinstance(node, (ast.Name, ast.Attribute)) and (
                    getattr(node, "id", None) or getattr(node, "attr", None)
                ) == const:
                    vouched.add(str(f.relative_to(root)))

        assert vouched == {
            # Where the key is DEFINED and where it is stripped off the public
            # path -- the boundary itself, not a grant.
            "src/mcp_handlers/observability/outcome_events.py",
            # The four grants.
            "src/http_routes/sentinel.py",
            "src/http_routes/substrate.py",
            "src/mcp_handlers/dialectic/resolution.py",
            "src/mcp_handlers/updates/phases.py",
        }, vouched


class TestEveryWritePathIsAccountedFor:
    """The enumeration the dialectic reviewer asked for (a36255d62a1310f3).

    TestCappingIsTheDefault pins the property every path INHERITS. This pins the
    set of paths itself, so adding a write path is a change someone has to look
    at rather than one that lands quietly. The two together are what the
    reviewer's condition asked for: no public entrypoint can exceed
    tool_observed through caller-supplied detail.
    """

    @staticmethod
    def _call_sites():
        """Every module that calls the shared outcome write path, by AST -- not
        by grep, so a call inside a string or comment cannot pad the list."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).parent.parent
        found = set()
        for f in sorted(root.glob("src/**/*.py")):
            try:
                tree = ast.parse(f.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if name == "_record_outcome_event_inline":
                        found.add(str(f.relative_to(root)))
        return found

    @staticmethod
    def _referencing_modules(target):
        """Modules that reference `target` AT ALL -- not just call it by name.

        The call-only version of this was evadable: `from ... import X as _rec`
        then `await _rec(args)` produced zero detections, so the enumeration
        could pass while an unlisted write path existed. Checking ImportFrom
        names (which catches any asname) plus every Name/Attribute reference
        closes both that and the pass-as-value shape.
        """
        import ast
        import pathlib as _pl

        root = _pl.Path(__file__).parent.parent
        found = set()
        for f in sorted(root.glob("src/**/*.py")):
            try:
                tree = ast.parse(f.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    a.name == target for a in node.names
                ):
                    found.add(str(f.relative_to(root)))
                if isinstance(node, (ast.Name, ast.Attribute)) and (
                    getattr(node, "id", None) or getattr(node, "attr", None)
                ) == target:
                    found.add(str(f.relative_to(root)))
        return found

    def test_the_real_write_chokepoint_is_pinned(self):
        """db.record_outcome_event is the shared path every outcome row takes.

        The enumeration below pins _record_outcome_event_inline, which is NOT
        that chokepoint: a 2026-09-19 review found four phases.py callers that
        reach the database directly, bypassing the recorder and its cap, while
        this suite stayed green. Behavioural coverage of the wrong function
        proves nothing, so pin the right one too.
        """
        import ast
        import pathlib as _pl

        # Matching `ast.Call` on `node.func.attr` missed a whole shape:
        # `from ...tool_usage import record_outcome_event as _write` then
        # `_write(...)` is an ast.Name call and was invisible. That is the same
        # evasion already closed for the recorder below, and an external review
        # caught it still open here. _referencing_modules sees any reference.
        callers = TestEveryWritePathIsAccountedFor._referencing_modules(
            "record_outcome_event"
        )

        # The defining module is absent by construction: _referencing_modules
        # matches imports and references, not a `def`, so `tool_usage.py` --
        # which declares the method and never calls it -- does not appear.
        assert callers == {
            "src/mcp_handlers/observability/outcome_events.py",
            # FOUR direct writers here bypass _record_outcome_event_inline
            # entirely. The enumeration below never saw them, which is why the
            # cap had to move into the shared write path itself.
            "src/mcp_handlers/updates/phases.py",
        }, callers

    def test_persistence_does_not_regrade_an_already_graded_detail(self):
        """The cap must not be undone between the recorder and the row.

        Reported defect: the db mixin re-graded the recorder's output with no
        ceiling, so a row capped at tool_observed/0.65 was STORED as
        substrate_observed/0.85 and the tool response disagreed with the
        database -- in the direction that flattered the claim.

        This test was TAUTOLOGICAL until an external review read it: it wrote
        `stored_detail = dict(response_detail)` and asserted they matched,
        never calling `db.record_outcome_event` at all. The headline test for
        the headline fix did not touch the code it named, and reverting the
        mixin left it green -- the same class of defect this PR already
        committed twice. It now drives the real mixin over a recording
        connection and reads the row.
        """
        from src.outcome_corroboration import (
            TOOL_OBSERVED,
            enrich_detail_with_corroboration,
        )

        # Detail whose free text grades externally_verified when ungated, so
        # the two paths give DIFFERENT answers and the assertion can fail.
        detail = {"pr": 123, "verified": True, "source": "github"}
        vs = "external_signal"

        response_detail = enrich_detail_with_corroboration(
            dict(detail), outcome_type="task_completed",
            verification_source=vs, ceiling=TOOL_OBSERVED,
        )
        assert response_detail["corroboration_grade"] == "tool_observed"

        conn = _RecordingConn()
        _run(_WritePathHarness(conn).record_outcome_event(
            agent_id="agent-persist",
            outcome_type="task_completed",
            is_bad=False,
            detail=dict(response_detail),
            verification_source=vs,
            corroboration_applied=True,
        ))
        stored = conn.stored_detail()

        # Drop the `corroboration_applied` skip and the mixin re-grades this
        # under ceiling_for_verification_source("external_signal") ==
        # externally_verified, storing 1.00 against a 0.65 response.
        assert stored["corroboration_grade"] == response_detail["corroboration_grade"]
        assert stored["evidence_weight"] == response_detail["evidence_weight"]
        assert stored["evidence_weight"] == 0.65

    def test_an_ungraded_detail_reaching_the_mixin_is_capped_by_provenance(self):
        """What the four direct phases.py writers now get.

        Honest about its own strength: with the grader itself default-deny,
        deleting the mixin's explicit `ceiling=` argument leaves this green,
        because the omitted argument resolves to the same provenance default.
        It pins the OUTCOME, and the redundancy is deliberate -- the explicit
        ceiling is belt over braces, not the only control.
        """
        conn = _RecordingConn()
        _run(_WritePathHarness(conn).record_outcome_event(
            agent_id="agent-direct",
            outcome_type="task_completed",
            is_bad=False,
            detail={"verified": True, "source": "sensor_sync"},
            verification_source="agent_reported_tool_result",
        ))
        stored = conn.stored_detail()

        assert stored["corroboration_grade"] == "tool_observed"
        assert stored["evidence_weight"] == 0.65

    def test_a_caller_cannot_supply_its_own_grade(self):
        """A forged grade in `detail` is neutralised twice over, and NEITHER
        control reads the payload to decide.

        The docstring here used to say the write path "skips re-grading a
        detail that already carries a grade". That described the FIRST attempt
        at the persistence fix, where presence-checking a caller-shaped field
        was itself the authorization bypass; it became a parameter for exactly
        that reason. An external review of this PR (gpt-5.6-terra, 2026-09-19)
        found the same stale wording still in the source comment, so the
        assertions below now pin the real invariant rather than the old one.
        """
        from src.mcp_handlers.observability.outcome_events import (
            _strip_provenance_claims,
        )
        from src.outcome_corroboration import enrich_detail_with_corroboration

        forged = {
            "corroboration_grade": "externally_verified",
            "evidence_weight": 1.0,
            "claim_risk": "low",
            "verified_fields": ["pr", "commit"],
            "summary": "trust me",
            "nested": {"corroboration_reasons": ["fabricated"], "keep": 1},
        }

        # Control 1 (public path only): the keys never reach the recorder.
        clean = _strip_provenance_claims(forged)
        assert clean == {"summary": "trust me", "nested": {"keep": 1}}

        # Control 2 (every path): re-grading OVERWRITES a forged grade, so the
        # forgery does not survive even when it is not stripped first.
        regraded = enrich_detail_with_corroboration(
            dict(forged),
            outcome_type="task_completed",
            verification_source="agent_reported_tool_result",
        )
        assert regraded["corroboration_grade"] == "claim_only"
        assert regraded["evidence_weight"] == 0.10

    def test_grader_is_default_deny(self):
        """Omission and explicit None must both cap; only the sentinel lifts.

        The earlier design inverted the default in the RECORDER while
        assess_outcome_corroboration(ceiling=None) still meant "no cap", so every
        grader call site that forgot inherited the unsafe behaviour. Three did.
        """
        from src.outcome_corroboration import (
            NO_CEILING,
            assess_outcome_corroboration,
            ceiling_for_verification_source,
        )

        detail = {"verified": True, "source": "sensor_sync"}
        vs = "agent_reported_tool_result"

        assert assess_outcome_corroboration("task_completed", detail, vs).grade == "tool_observed"
        assert assess_outcome_corroboration(
            "task_completed", detail, vs, ceiling=None
        ).grade == "tool_observed"
        assert assess_outcome_corroboration(
            "task_completed", detail, vs, ceiling=NO_CEILING
        ).grade == "substrate_observed"

        # Unknown/NULL provenance is not evidence of verification.
        assert ceiling_for_verification_source(None) == "tool_observed"
        # A vouched provenance caps at its OWN claim, not at nothing.
        assert ceiling_for_verification_source("server_observation") == "substrate_observed"

    def test_the_write_path_has_exactly_these_callers(self):
        assert self._call_sites() == {
            # The public MCP entrypoint. Unvouched, and it pops the trust key
            # so a caller cannot vouch for itself.
            "src/mcp_handlers/observability/outcome_events.py",
            # Vouched in-process emitters and operator-gated routes.
            "src/mcp_handlers/updates/phases.py",
            "src/mcp_handlers/dialectic/resolution.py",
            "src/http_routes/substrate.py",
            "src/http_routes/sentinel.py",
        }, (
            "a new outcome write path appeared. It is capped by default, so this "
            "is not a vulnerability -- but confirm it should not be vouched, and "
            "add it here deliberately."
        )

    def test_no_unvouched_caller_can_reach_the_top_two_grades(self):
        """Ties the enumeration to the invariant: for every call site that does
        NOT vouch itself, the grader is handed a ceiling."""
        from src.outcome_corroboration import GRADE_ORDER, TOOL_OBSERVED

        capped_rank = GRADE_ORDER.index(TOOL_OBSERVED)
        # The two grades that assert a NON-AGENT observer saw this.
        assert [g for g in GRADE_ORDER[capped_rank + 1:]] == [
            "substrate_observed",
            "externally_verified",
        ]
        assert TestCappingIsTheDefault._ceiling_seen(
            {
                "outcome_type": "task_completed",
                "agent_id": "enumeration-check",
                "detail": {"source": "sensor_sync", "evidence_source": "github"},
                "verification_source": "external_signal",
            }
        ) == TOOL_OBSERVED


class TestOperatorDecisionIsPinned:
    """The operator decided (criterion: "best for federation") that capped rows
    REMAIN eligible to train tactical calibration at exactly 0.65.

    That decision rests on a factual claim about the gate's comparison
    direction, which the author originally got backwards in the PR body. Pin the
    fact so the recorded decision cannot be quietly invalidated by moving a
    constant or flipping a `<` to `<=`.
    """

    def test_the_cap_lands_exactly_on_the_calibration_gate(self):
        from src.mcp_handlers.observability.outcome_events import (
            _MIN_TACTICAL_EVIDENCE_WEIGHT,
        )
        from src.outcome_corroboration import GRADE_WEIGHTS, TOOL_OBSERVED

        assert GRADE_WEIGHTS[TOOL_OBSERVED] == _MIN_TACTICAL_EVIDENCE_WEIGHT

    def test_a_capped_row_is_admitted_to_calibration_not_excluded(self):
        """The actuation fact the decision was made against: the gate excludes
        `evidence_weight < MIN`, so equality passes. A capped row still trains
        calibration, and calibration_error reaches the drift/EISV path. If this
        inverts, the operator's decision was made against a premise that no
        longer holds and must be revisited, not silently inherited."""
        from src.mcp_handlers.observability.outcome_events import (
            _MIN_TACTICAL_EVIDENCE_WEIGHT,
        )
        from src.outcome_corroboration import GRADE_WEIGHTS, TOOL_OBSERVED

        capped_weight = GRADE_WEIGHTS[TOOL_OBSERVED]
        assert not (capped_weight < _MIN_TACTICAL_EVIDENCE_WEIGHT), (
            "a capped row is now EXCLUDED from calibration. That is a different "
            "policy than the one recorded; re-open the operator decision."
        )

    def test_grades_below_the_cap_are_still_excluded(self):
        """The gate must still do its original job."""
        from src.mcp_handlers.observability.outcome_events import (
            _MIN_TACTICAL_EVIDENCE_WEIGHT,
        )
        from src.outcome_corroboration import (
            CLAIM_ONLY,
            GRADE_WEIGHTS,
            SELF_REPORT_WITH_REFS,
        )

        for grade in (CLAIM_ONLY, SELF_REPORT_WITH_REFS):
            assert GRADE_WEIGHTS[grade] < _MIN_TACTICAL_EVIDENCE_WEIGHT, grade


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
