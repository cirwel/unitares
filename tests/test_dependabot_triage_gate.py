"""Tests for dependabot_triage's coverage gate (#finder-2026-08-21 c1).

The gate's contract: unreachability is ALWAYS reported, but only an
unexpected unreachable repo fails the run. An unreachable set that is
exactly a subset of the declared allowlist is partial-by-policy (green
with an explicit caveat in the markdown) — anything else exits 1.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "dependabot_triage.py"


@pytest.fixture(scope="module")
def triage():
    spec = importlib.util.spec_from_file_location("dependabot_triage", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dependabot_triage"] = mod
    spec.loader.exec_module(mod)
    return mod


def _u(*repos):
    return [{"repo": r, "error": "not readable: HTTP 404"} for r in repos]


class TestCoverageExitCode:
    def test_full_coverage_is_green(self, triage):
        assert triage.coverage_exit_code([], set()) == 0
        assert triage.coverage_exit_code([], {"CIRWEL/unitares-pi-plugin"}) == 0

    def test_allowlisted_unreachable_is_green(self, triage):
        assert triage.coverage_exit_code(
            _u("CIRWEL/unitares-pi-plugin"), {"CIRWEL/unitares-pi-plugin"}
        ) == 0

    def test_unexpected_unreachable_is_red(self, triage):
        assert triage.coverage_exit_code(_u("cirwel/unitares"), set()) == 1
        assert triage.coverage_exit_code(
            _u("cirwel/unitares"), {"CIRWEL/unitares-pi-plugin"}
        ) == 1

    def test_mixed_set_is_red_even_with_partial_allowlist(self, triage):
        assert triage.coverage_exit_code(
            _u("CIRWEL/unitares-pi-plugin", "cirwel/anima-mcp"),
            {"CIRWEL/unitares-pi-plugin"},
        ) == 1

    def test_empty_allowlist_keeps_the_original_contract(self, triage):
        # With no allowlist the gate is byte-equivalent to the old behavior:
        # any unreachable repo fails.
        assert triage.coverage_exit_code(_u("CIRWEL/unitares-pi-plugin"), set()) == 1

    def test_repo_matching_is_case_insensitive(self, triage):
        # GitHub repo names are case-insensitive; spelling must not decide.
        assert triage.coverage_exit_code(
            _u("cirwel/unitares-pi-plugin"), {"CIRWEL/unitares-pi-plugin"}
        ) == 0
        assert triage.coverage_exit_code(
            _u("CIRWEL/Unitares-PI-Plugin"), {"cirwel/unitares-pi-plugin"}
        ) == 0


class TestMarkdownCaveat:
    def _report(self, triage, unreachable):
        r = triage.Report()
        r.unreachable = unreachable
        return r

    def test_partial_by_policy_banner_present_when_allowed(self, triage):
        md = triage.render_markdown(
            self._report(triage, _u("CIRWEL/unitares-pi-plugin")),
            allowed_partial=True,
        )
        assert "Partial-by-policy" in md
        # The honesty contract's original warning must survive alongside it.
        assert "Do not read this report as fleet-wide" in md
        assert "CIRWEL/unitares-pi-plugin" in md

    def test_no_banner_on_a_real_coverage_failure(self, triage):
        md = triage.render_markdown(
            self._report(triage, _u("cirwel/unitares")), allowed_partial=False,
        )
        assert "Partial-by-policy" not in md
        assert "Do not read this report as fleet-wide" in md

    def test_no_banner_and_no_unreachable_section_on_full_coverage(self, triage):
        md = triage.render_markdown(self._report(triage, []))
        assert "Partial-by-policy" not in md
        assert "All target repos were read successfully" in md
