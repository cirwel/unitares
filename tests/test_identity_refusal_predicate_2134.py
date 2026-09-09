"""#2134: the typed identity refusal predicate is single-sourced.

``strict_identity_refusal_payload`` is deliberately "a structured success-shape,
not an error", so a refusal carries ``success: true`` with no ``error`` key.
Every consumer that branches on success/error misses it. Two now ask the same
question — the usage recorder (so a refusal is not counted as a successful
call) and the experience envelope (so a refusal is not rebuilt into an ordinary
check-in) — so the predicate lives beside the builder it tests for.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.identity_bootstrap import (
    IDENTITY_REFUSAL_MARKER,
    identity_refusal_status,
    strict_identity_refusal_payload,
)


class TestPredicate:
    def test_recognizes_a_real_refusal(self):
        payload = strict_identity_refusal_payload("process_agent_update")
        assert identity_refusal_status(payload) == "identity_required"

    def test_carries_the_emission_points_own_status(self):
        payload = strict_identity_refusal_payload(
            "onboard", status="lineage_declaration_required"
        )
        assert identity_refusal_status(payload) == "lineage_declaration_required"

    @pytest.mark.parametrize("payload", [
        {"success": True, "status": "healthy"},
        {"success": False, "error": "nope"},
        {"status": "identity_required"},          # status without the marker
        {"rollout_flag": "SOMETHING_ELSE"},
        None,
        "not a dict",
        [],
    ])
    def test_rejects_everything_else(self, payload):
        assert identity_refusal_status(payload) is None

    def test_the_marker_has_exactly_one_writer(self):
        """The predicate is only precise while ``rollout_flag`` has one writer.
        If a second place starts writing it, the predicate produces false
        positives and every consumer silently changes behavior.

        Parsed with ``ast`` rather than grepped: a regex over source counts
        comments and docstrings, misses single-quoted keys and subscript
        assignment, and would fail or pass for reasons unrelated to the
        invariant. The path is anchored to this file, not the working
        directory, so the test does not depend on where pytest was invoked.
        """
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        assert src.is_dir(), src

        writers = set()
        for path in src.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # {"rollout_flag": ...} in a dict literal
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and key.value == "rollout_flag":
                            writers.add(f"{path.relative_to(src)}:{node.lineno}")
                # payload["rollout_flag"] = ...
                elif isinstance(node, ast.Subscript):
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and sl.value == "rollout_flag":
                        writers.add(f"{path.relative_to(src)}:{node.lineno}")

        assert len(writers) == 1, f"expected exactly one writer, found {sorted(writers)}"
        assert next(iter(writers)).startswith("mcp_handlers/identity_bootstrap.py")


class TestSingleSource:
    def test_the_usage_recorder_delegates_to_the_same_predicate(self):
        """One definition, two consumers — assert agreement on real payloads
        rather than on object identity, so the recorder stays free to import
        it function-locally (which it must: see its comment on startup cost).
        """
        from src.services.tool_usage_recorder import _identity_refusal_status

        for payload in (
            strict_identity_refusal_payload("process_agent_update"),
            strict_identity_refusal_payload("onboard", status="lineage_declaration_required"),
            {"success": True, "status": "healthy"},
            {"rollout_flag": "SOMETHING_ELSE"},
            None,
        ):
            assert _identity_refusal_status(payload) == identity_refusal_status(payload)

    def test_the_recorder_does_not_import_mcp_handlers_at_module_scope(self):
        """Regression guard. This module is imported at module scope by the
        transport entry points, and importing any ``src.mcp_handlers`` submodule
        executes that package's ``__init__`` — 86 modules and ~0.55s. Keep every
        such import inside a function, as the rest of this file already does.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse((root / "src/services/tool_usage_recorder.py").read_text())
        offenders = [
            node.lineno
            for node in tree.body  # module scope only, not nested in a function
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and "mcp_handlers" in (getattr(node, "module", "") or "")
        ]
        assert not offenders, f"module-scope mcp_handlers import at line(s) {offenders}"

    def test_the_recorder_still_classifies_a_refusal_as_not_a_success(self):
        import json

        from src.mcp_handlers.response_base import success_response
        from src.services.tool_usage_recorder import classify_tool_result

        raw = success_response(strict_identity_refusal_payload("process_agent_update"))
        assert json.loads(raw[0].text)["success"] is True  # premise guard
        success, error_type = classify_tool_result(raw)
        assert success is False
        assert error_type == "identity_required"


class TestPostExecutionChain:
    """#2134 finding 2 (codex): fixing the envelope alone left the very next
    step contradicting the refusal it had just passed through."""

    @pytest.mark.asyncio
    async def test_the_warning_step_does_not_tell_a_refused_caller_it_succeeded(self):
        import json
        from types import SimpleNamespace

        from src.mcp_handlers.context import (
            clear_continuity_token_invalid,
            mark_continuity_token_invalid,
            set_session_resolution_source,
        )
        from src.mcp_handlers.middleware.identity_warning_step import (
            apply_identity_warnings,
        )
        from src.mcp_handlers.response_base import success_response

        try:
            mark_continuity_token_invalid()
            set_session_resolution_source("ip_ua_fingerprint")
            raw = success_response(
                strict_identity_refusal_payload("process_agent_update")
            )
            out = await apply_identity_warnings(
                "process_agent_update", {}, SimpleNamespace(original_name="sync_state"), raw
            )
            assert out is raw
            body = json.dumps(json.loads(out[0].text))
            assert "the call succeeded via" not in body
            assert "identity_warnings" not in json.loads(out[0].text)
        finally:
            clear_continuity_token_invalid()

    @pytest.mark.asyncio
    async def test_the_full_post_execution_chain_leaves_a_refusal_intact(self):
        """Run the real POST_EXECUTION_STEPS in their registered order, rather
        than each step in isolation — the isolated tests are what missed this.
        """
        import json
        from types import SimpleNamespace

        from src.mcp_handlers.context import (
            clear_continuity_token_invalid,
            mark_continuity_token_invalid,
            set_session_resolution_source,
        )
        from src.mcp_handlers.middleware import POST_EXECUTION_STEPS
        from src.mcp_handlers.response_base import success_response

        try:
            mark_continuity_token_invalid()
            set_session_resolution_source("ip_ua_fingerprint")
            result = success_response(
                strict_identity_refusal_payload("process_agent_update")
            )
            ctx = SimpleNamespace(original_name="sync_state", arguments={})
            for step in POST_EXECUTION_STEPS:
                result = await step("process_agent_update", {}, ctx, result)

            payload = json.loads(result[0].text)
            assert payload["status"] == "identity_required"
            for field in ("hint", "next_step", "safe_options", "do_not"):
                assert field in payload, field
            assert "next_action" not in payload
            assert "the call succeeded via" not in json.dumps(payload)
        finally:
            clear_continuity_token_invalid()
