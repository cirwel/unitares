"""Static hint inventory: syntax, conservative reachability and action coverage.

A green test suite verifies the instrument, not a clean default tool surface.
The expanded field/name inventory intentionally leaves unreviewed candidates
outside the four-entry ledger inherited from #2119.
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

    @pytest.mark.parametrize("text", [
        'knowledge(query="action=\'store\'", action="search")',
        'knowledge(query="a ) character", action="search")',
        'knowledge(query=make_query("x"), action="search")',
    ])
    def test_action_parsing_respects_strings_and_nested_calls(self, text):
        match = scanner.CALL_PATTERN.search(text)
        assert scanner._hinted_action(text, match) == "search"

    def test_does_not_borrow_an_action_from_the_next_instruction(self):
        text = "knowledge(query='x'); dialectic(action='get')"
        assert scanner._hinted_action(text, scanner.CALL_PATTERN.search(text)) is None


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

    def test_entries_are_tool_site_and_action_triples(self):
        for tool, site, action in scanner.KNOWN_DEAD_ENDS:
            assert ":" in site and site.startswith("src/"), (tool, site)
            assert action is None or isinstance(action, str)

    def test_broader_inventory_exposes_unreviewed_default_candidates(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fail-on-finding"],
            capture_output=True, text=True, cwd=project_root,
        )
        assert result.returncode == scanner.EXIT_DEAD_END_HINT, result.stderr
        assert "UNREVIEWED hint candidate" in result.stderr
        assert "STALE ledger" not in result.stderr

    def test_the_guard_fails_on_an_unlisted_finding(self, monkeypatch):
        # Emptying the ledger must make the known findings unlisted, not silent.
        monkeypatch.setattr(scanner, "KNOWN_DEAD_ENDS", {})
        findings = scanner.find_dead_end_hints("standard")
        seen = scanner.finding_keys(findings)
        assert seen, "expected the known dead ends to still be reported"
        assert seen - set(scanner.KNOWN_DEAD_ENDS)

    def test_the_ledger_matches_what_is_reported(self):
        # A stale entry is a lie about outstanding work; the guard reports it.
        findings = scanner.find_dead_end_hints("standard")
        seen = scanner.finding_keys(findings)
        assert set(scanner.KNOWN_DEAD_ENDS) <= seen, (
            "Stale reviewed candidates: "
            f"{sorted(set(scanner.KNOWN_DEAD_ENDS) - seen)}"
        )
        assert seen - set(scanner.KNOWN_DEAD_ENDS), "new scope must not be silently accepted"


class TestNoRegression:
    def test_dialectic_is_no_longer_a_dead_end_on_standard(self):
        findings = scanner.find_dead_end_hints("standard")
        assert "dialectic" not in {f.tool for f in findings}

    def test_raw_names_in_other_fields_remain_visible(self):
        # #2119 fixed 27 call-shaped hints in selected fields. It did not fix
        # raw names in structured lists, safe_options or message strings.
        findings = {f.tool for f in scanner.find_dead_end_hints("standard")}
        assert {"onboard", "process_agent_update"} <= findings


@pytest.fixture
def handler_tree(tmp_path, monkeypatch):
    root = tmp_path / "src" / "mcp_handlers"
    root.mkdir(parents=True)
    monkeypatch.setattr(scanner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scanner, "HANDLER_ROOT", root)

    def write(name, source):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    return write


def test_candidates_follow_nested_fields_bindings_builders_and_structured_names(handler_tree):
    handler_tree("hints.py", '''
def advice():
    return "dialectic(session_id='x', action='get')"

@mcp_tool("onboard")
def onboard():
    names = ["process_agent_update"]
    next_call: str = advice()
    response = {"next_call": next_call, "safe_options": [{"call": "bind_session()"}]}
    response["related_tools"] = names
    return response
''')
    sites = scanner.collect_hint_sites({"dialectic", "bind_session", "process_agent_update"})
    assert set(sites) == {"dialectic", "bind_session", "process_agent_update"}
    assert {action for _, action, _ in sites["dialectic"]} == {"get"}


@pytest.mark.parametrize("caller", [
    "import helper\n@mcp_tool('onboard')\ndef agent_entry():\n    return helper.shared()\n",
    "from helper import shared as advice\n@mcp_tool('onboard')\ndef agent_entry():\n    return advice()\n",
    "import helper\ndef dispatch_step():\n    return helper.shared()\n",
])
def test_agent_and_middleware_callers_cannot_be_hidden_by_operator_caller(handler_tree, caller):
    handler_tree("helper.py", "def shared():\n    return {'hint': \"bind_session()\"}\n")
    handler_tree("operator.py", "@mcp_tool('operator_resume_agent')\ndef operator_entry():\n    return shared()\n")
    handler_tree("middleware/step.py" if "dispatch_step" in caller else "agent.py", caller)
    assert "bind_session" in {f.tool for f in scanner.find_dead_end_hints("standard")}


def test_english_plural_and_docstrings_are_not_hints(handler_tree):
    handler_tree("english.py", '''
def helper():
    """hint: bind_session()"""
    return {"message": "2 session(s); another agent (writes/mutations)"}
''')
    assert not scanner.collect_hint_sites({"session", "agent", "bind_session"})


def test_different_aliases_cover_different_actions_and_partial_coverage_stays_visible(handler_tree, monkeypatch):
    monkeypatch.setattr("src.tool_modes.get_tools_for_mode", lambda mode: {"search_shared_memory", "store_finding"})
    handler_tree("actions.py", '''
hint = "knowledge(action='search'); knowledge(summary='x', action='store')"
''')
    finding, = scanner.find_dead_end_hints("standard")
    assert finding.kind == "names_unadvertised_twin"
    assert finding.advertised_aliases == ["search_shared_memory", "store_finding"]
    assert {(c["action"], c["advertised_alias"]) for c in finding.calls} == {
        ("search", "search_shared_memory"), ("store", "store_finding"),
    }
    handler_tree("uncovered.py", "hint = \"knowledge(action='cleanup')\"\n")
    finding, = scanner.find_dead_end_hints("standard")
    assert finding.kind == "partially_covered_actions"
    assert any(c["action"] == "cleanup" and c["advertised_alias"] is None for c in finding.calls)


def test_invalid_profile_and_unreadable_tree_cannot_report_clean(handler_tree, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["hint_target_advertisement", "--mode", "standrad"])
    assert scanner.main() == scanner.EXIT_REGISTRY_UNAVAILABLE
    assert "unknown" in capsys.readouterr().err
    handler_tree("broken.py", "def broken(\n")
    monkeypatch.setattr("sys.argv", ["hint_target_advertisement"])
    assert scanner.main() == scanner.EXIT_REGISTRY_UNAVAILABLE
    assert "unknown" in capsys.readouterr().err
