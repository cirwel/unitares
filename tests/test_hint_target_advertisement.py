"""The dead-end-hint scanner, and the ledger the CI guard rests on.

A response that says "poll `dialectic(action='get', ...)`" is an instruction,
and a schema-driven MCP client can only call names `tools/list` returned. This
scanner finds hints that name something the profile does not advertise.

Its value depends entirely on being *right about which hints matter*, so these
tests pin the three judgements that took it from 53 reported sites to 4:

  1. A call is `tool(`, adjacent. Allowing whitespace made the English
     parenthetical "another agent (writes/mutations)" parse as a call.
  2. A hint only strands a caller who can receive it, so each site resolves to
     the tool that emits it -- through undecorated helpers, via the call graph.
  3. What remains is a shrink-only ledger, not a mute button: the guard fails
     on anything unlisted AND on a listed entry that no longer matches.
"""

import subprocess
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts" / "diagnostics"))

import src.mcp_handlers  # noqa: F401  (populates the decorator registry)

import hint_target_advertisement as scanner

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

SCRIPT = project_root / "scripts" / "diagnostics" / "hint_target_advertisement.py"


class TestCallPattern:
    def test_an_adjacent_paren_is_a_call(self):
        assert scanner.CALL_PATTERN.search("poll dialectic(action='get')")

    def test_an_english_parenthetical_is_not_a_call(self):
        # The exact prose that produced a false finding for `agent`:
        # src/mcp_handlers/middleware/params_step.py, "ACTING AS another
        # agent (writes/mutations)".
        text = "this guard only blocks ACTING AS another agent (writes/mutations)"
        assert [m.group(1) for m in scanner.CALL_PATTERN.finditer(text)] == []

    def test_a_bare_mention_is_not_a_call(self):
        assert not scanner.CALL_PATTERN.search("read the knowledge graph first")

    def test_the_action_argument_is_captured(self):
        match = scanner.CALL_PATTERN.search("dialectic(action='thesis', session_id='x')")
        assert match.group(1) == "dialectic"
        assert match.group(2) == "thesis"


class TestEmitterResolution:
    def test_middleware_hints_reach_every_profile(self):
        reachable = scanner._reachable_predicate("minimal")
        assert reachable({scanner.MIDDLEWARE_SENTINEL})

    def test_an_undetermined_emitter_is_reported(self):
        # Failing toward reporting is deliberate: over-reporting a live hint is
        # recoverable, missing one is not.
        reachable = scanner._reachable_predicate("standard")
        assert reachable(None)
        assert reachable(frozenset())

    def test_an_alias_makes_its_implementation_reachable(self):
        # `request_review` is advertised on standard and resolves to
        # dialectic(action='request'), so `request_dialectic_review`'s own
        # responses can reach a standard caller even though that name is not
        # advertised.
        reachable = scanner._reachable_predicate("standard")
        assert reachable({"request_dialectic_review"})

    def test_an_unadvertised_operator_tool_is_not_reachable_on_standard(self):
        reachable = scanner._reachable_predicate("standard")
        assert not reachable({"operator_resume_agent"})

    def test_an_advertised_router_reaches_all_of_its_actions(self):
        # standard advertises the `knowledge` router, so every action it routes
        # is reachable, including ones no alias pins.
        reachable = scanner._reachable_predicate("standard")
        assert reachable({"get_discovery_details"})


class TestLedger:
    def test_every_entry_carries_a_reason(self):
        for key, reason in scanner.KNOWN_DEAD_ENDS.items():
            assert isinstance(reason, str) and len(reason) > 40, key

    def test_entries_are_tool_and_site_pairs(self):
        for tool, site in scanner.KNOWN_DEAD_ENDS:
            assert ":" in site and site.startswith("src/"), (tool, site)

    def test_the_default_profile_guard_passes(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fail-on-finding"],
            capture_output=True, text=True, cwd=project_root,
        )
        assert result.returncode == 0, result.stderr

    def test_the_guard_fails_on_an_unlisted_finding(self, monkeypatch):
        # Emptying the ledger must make the known findings unlisted, not silent.
        monkeypatch.setattr(scanner, "KNOWN_DEAD_ENDS", {})
        findings = scanner.find_dead_end_hints("standard")
        seen = {(f.tool, site) for f in findings for site in f.sites}
        assert seen, "expected the known dead ends to still be reported"
        assert seen - set(scanner.KNOWN_DEAD_ENDS)

    def test_the_ledger_matches_what_is_reported(self):
        # A stale entry is a lie about outstanding work; the guard reports it.
        findings = scanner.find_dead_end_hints("standard")
        seen = {(f.tool, site) for f in findings for site in f.sites}
        assert set(scanner.KNOWN_DEAD_ENDS) == seen, (
            "KNOWN_DEAD_ENDS has drifted from the scan. Unlisted: "
            f"{sorted(seen - set(scanner.KNOWN_DEAD_ENDS))}; stale: "
            f"{sorted(set(scanner.KNOWN_DEAD_ENDS) - seen)}"
        )


class TestNoRegression:
    def test_dialectic_is_no_longer_a_dead_end_on_standard(self):
        findings = scanner.find_dead_end_hints("standard")
        assert "dialectic" not in {f.tool for f in findings}

    def test_the_raw_twins_are_no_longer_hinted_on_standard(self):
        # onboard / process_agent_update were named in 27 caller-facing hints
        # while only start_session / sync_state were advertised.
        findings = {f.tool for f in scanner.find_dead_end_hints("standard")}
        assert "onboard" not in findings
        assert "process_agent_update" not in findings
