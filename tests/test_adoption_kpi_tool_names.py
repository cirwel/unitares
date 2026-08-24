"""Every tool-name filter must cover the LIVE tool name.

`search_knowledge_graph` is a dead alias — `adoption_kpi.py`'s own note records
0 rows in 30d and names `search_shared_memory` as the live one. That correction
was applied to the KG-retrieval and return-rate queries and MISSED in the
`cohort_engaged` predicate, which went on counting only the dead name. An agent
whose only value action was a shared-memory search read as not engaged, so the
metric silently UNDERSTATED engagement.

WHY THIS FILE EXISTS. The #1856 sweep judged `adoption_kpi.py` clean and shipped
a test that asserted only "no verdict token appears in the body". That test
cannot see a wrong tool name, so it passed over this defect while reading as
coverage — the same shape as the instruments the sweep was repairing. An
independent reviewer found it. These tests check what the queries SELECT, not
what the module refrains from saying.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "scripts/dev/adoption_kpi.py").read_text()

LIVE_SEARCH_TOOL = "search_shared_memory"
DEAD_SEARCH_TOOL = "search_knowledge_graph"

# `tool_name IN (...)` / `tool_name = '...'` filters, comments stripped first.
_NO_COMMENTS = "\n".join(
    ln for ln in SOURCE.splitlines() if not ln.lstrip().startswith(("--", "#"))
)
TOOL_FILTERS = re.findall(r"tool_name\s+IN\s*\(([^)]*)\)", _NO_COMMENTS, re.S)


def _names(clause: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", clause))


def test_there_are_tool_name_filters_to_check():
    """Guards the guard: a regex that matches nothing would pass vacuously."""
    assert len(TOOL_FILTERS) >= 3


@pytest.mark.parametrize("clause", TOOL_FILTERS)
def test_no_filter_names_the_dead_alias_without_the_live_one(clause):
    """The defect, stated as the rule that would have caught it.

    Naming the dead alias is fine — it keeps the metric comparable across the
    rename. Naming it WITHOUT the live one is the undercount.
    """
    names = _names(clause)
    if DEAD_SEARCH_TOOL in names:
        assert LIVE_SEARCH_TOOL in names, (
            f"filter names the dead alias but not the live tool: {sorted(names)}")


def test_the_engagement_predicate_counts_the_live_search_tool():
    """The specific query that was missed."""
    engaged = [c for c in TOOL_FILTERS if "process_agent_update" in c and "outcome_event" in c]
    assert engaged, "cohort_engaged predicate not found"
    for clause in engaged:
        assert LIVE_SEARCH_TOOL in _names(clause)


def test_a_search_capable_filter_is_not_left_search_blind():
    """Any filter that mentions searching at all must include the live name."""
    for clause in TOOL_FILTERS:
        names = _names(clause)
        if any("search" in n or n == "knowledge" for n in names):
            assert LIVE_SEARCH_TOOL in names, sorted(names)
