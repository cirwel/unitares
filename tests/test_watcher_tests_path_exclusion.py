"""Tests for runtime-hygiene pattern suppression on test files.

P001 (fire-and-forget task), P003 (transient monitor), and P011
(mutate-then-persist) encode invariants about LIVE runtime behavior. Test
code legitimately violates them — tests spawn unreferenced tasks, build
isolated transient monitors (`UNITARESMonitor(agent_id, load_state=False)`),
and poke in-memory state without persisting. Findings under `/tests/` for
these patterns are false positives and must drop.

False-positive class confirmed 2026-06-27: 156 `UNITARESMonitor(` call sites
across 32 test files would trip P003 (e.g. test_hck_rho_coupling.py:20/26/33/49).

Security patterns (P008 shell-injection, P012 unsafe-deserialization) are NOT
excluded — they must still fire on tests.
"""

from __future__ import annotations

import pytest

from agents.watcher.agent import _verify_finding_against_source
from agents.watcher.findings import Finding


def _make(pattern: str, file: str, line: int = 1) -> Finding:
    return Finding(
        pattern=pattern,
        file=file,
        line=line,
        hint="x",
        severity="high",
        detected_at="2026-06-27T00:00:00Z",
        model_used="test",
    )


class TestRuntimeHygieneSuppressedInTests:
    """P001/P003/P011 must drop when the file lives under /tests/."""

    def test_p003_transient_monitor_in_tests_dropped(self):
        snippet = {20: '        mon = UNITARESMonitor("test-f4-gains-low", load_state=False)'}
        finding = _make("P003", "/Users/x/projects/unitares/tests/test_hck_rho_coupling.py", 20)
        assert _verify_finding_against_source(finding, "", snippet) is False

    def test_p001_create_task_in_tests_dropped(self):
        snippet = {5: "        asyncio.create_task(do_thing())"}
        finding = _make("P001", "tests/test_something.py", 5)
        assert _verify_finding_against_source(finding, "", snippet) is False

    def test_p011_mutate_then_persist_in_tests_dropped(self):
        snippet = {7: "    mon.state.update_count = 3"}
        finding = _make("P011", "tests/test_state.py", 7)
        assert _verify_finding_against_source(finding, "", snippet) is False


class TestRuntimeHygieneStillFiresInRuntime:
    """The same patterns must survive in non-test (runtime) code."""

    def test_p003_transient_monitor_in_runtime_survives(self):
        snippet = {175: "    monitor = UNITARESMonitor(agent_id)"}
        finding = _make("P003", "src/lifecycle/stuck.py", 175)
        assert _verify_finding_against_source(finding, "", snippet) is True


class TestSecurityPatternsNotExcludedInTests:
    """Security patterns must keep firing even under /tests/."""

    def test_p008_shell_injection_in_tests_survives(self):
        snippet = {3: '    subprocess.run(cmd, shell=True)'}
        finding = _make("P008", "tests/test_helper.py", 3)
        assert _verify_finding_against_source(finding, "", snippet) is True
