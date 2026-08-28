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


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "adoption_kpi", REPO / "scripts/dev/adoption_kpi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

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


# --- the scheduled cohort must come from the roster, not a literal ---------

def test_the_resident_half_is_derived_from_the_configured_roster(monkeypatch):
    """A hardcoded roster drifts, and the drift inflated the number.

    The regex named six residents literally — the same six the fleet-identity
    guard's own FLEET_IDENTITIES list exists to keep out of shipped source. It
    never tripped CI because that guard's DEFAULT_PATHS covers src /
    governance_core / config / agents/sdk/src and not scripts/dev.

    The correctness cost: `surface_return_rate` EXCLUDES these labels from its
    denominator, and scheduled callers return by construction. So a resident
    added to UNITARES_RESIDENTS and not to the literal silently entered the
    denominator as an ordinary caller and INFLATED the measured return rate —
    failing in the comfortable direction.
    """
    import src.grounding.class_indicator as ci

    mod = _load()
    monkeypatch.setattr(ci, "load_resident_labels", lambda: frozenset({"Aardvark", "Zephyr"}))

    pattern = mod._scheduled_label_re()
    assert "Aardvark" in pattern and "Zephyr" in pattern
    # ...and a name that is NOT on the roster must not be excluded.
    assert "Vigil" not in pattern


def test_the_residentless_install_excludes_only_jobs(monkeypatch):
    """The default install (UNITARES_RESIDENTS unset) is the case to test."""
    import re as _re

    import src.grounding.class_indicator as ci

    mod = _load()
    monkeypatch.setattr(ci, "load_resident_labels", lambda: frozenset())

    pattern = mod._scheduled_label_re()
    for job in mod._SCHEDULED_JOB_PREFIXES:
        assert _re.match(pattern, job), job
    # No resident name is baked in when the roster is empty.
    assert not _re.match(pattern, "Vigil")
    assert not _re.match(pattern, "Sentinel")


def test_no_resident_name_is_hardcoded_in_the_module():
    """The literal roster is gone from executable source.

    Comments are stripped: the provenance note names what was removed, and the
    house rule says provenance in a comment is deliberately not flagged.
    """
    live = "\n".join(ln for ln in SOURCE.splitlines()
                      if not ln.lstrip().startswith("#"))
    for name in ("Vigil", "Sentinel", "Watcher", "Steward", "Chronicler", "Lumen"):
        assert name not in live, name


def test_the_exclusion_is_disclosed_not_silent():
    """The same regex DISCLOSES composition two metrics up; here it deleted.

    `surface_return_rate` removed agents from its own denominator and printed
    no excluded count, so a reader could not tell whether the rate covered the
    fleet or a filtered slice of it.
    """
    mod = _load()
    query = mod._snapshot_queries()["surface_return_rate"]

    assert "scheduled_excluded" in query
    assert "excludes {ec.get('scheduled_excluded', 0)} scheduled" in SOURCE

    # Eligibility is derived once. Both the denominator and the disclosed
    # exclusion count split that candidate relation, so an ineligible scheduled
    # call cannot be reported as something removed from the denominator.
    assert query.count("FROM audit.tool_usage") == 1
    assert "WITH eligible_calls AS" in query
    assert "a.label ~* %(scheduled_re)s AS scheduled" in query
    assert "FROM eligible_calls\n                WHERE NOT scheduled" in query
    assert "FROM eligible_calls\n                     WHERE scheduled" in query


@pytest.mark.asyncio
async def test_scheduled_exclusion_equals_eligible_rows_removed(live_postgres_backend):
    """Execute the production query over qualifying and non-qualifying calls.

    The regression case is a scheduled agent whose only call is lifecycle
    ceremony. It never qualified for the return-rate denominator, so it must
    not be disclosed as excluded. A second scheduled agent makes an eligible
    ``observe`` call and is the one actual removal.
    """
    mod = _load()
    query = mod._snapshot_queries()["surface_return_rate"]
    query = (
        query.replace("%(days)s", "$1")
        .replace("%(scheduled_re)s", "$2")
        .replace("%(return_gap_s)s", "$3")
    )

    rows = (
        ("ordinary-eligible", "ordinary", "observe"),
        ("scheduled-eligible", "canary_eligible", "observe"),
        ("scheduled-ceremony", "canary_ceremony", "process_agent_update"),
    )
    async with live_postgres_backend.acquire() as conn:
        for agent_id, label, tool_name in rows:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key, label) VALUES ($1, $2, $3)",
                agent_id,
                f"key-{agent_id}",
                label,
            )
            await conn.execute(
                """
                INSERT INTO audit.tool_usage (ts, agent_id, tool_name, payload)
                VALUES (now(), $1, $2, '{}'::jsonb)
                """,
                agent_id,
                tool_name,
            )

        result = await conn.fetchrow(query, 1, mod._scheduled_label_re(), 60)

    assert result["agents_with_calls"] == 1
    assert result["scheduled_excluded"] == 1


def test_did_anything_excludes_forwarded_lease_substrate():
    """A presence-lease heartbeat row must not flip an agent out of did_nothing.

    lease.* rows in audit.tool_usage are lease-plane events projected in by
    the BEAM outbox forwarder — overwhelmingly heartbeats (~93%
    holder_class=process_instance presence from ordinary session
    onboarding). Substrate emission, not agent
    action: an agent whose only rows are heartbeats has done nothing, and
    counting the heartbeat as "did anything" understated true bounce.
    """
    mod = _load()
    query = mod._snapshot_queries()["onboard_conversion"]
    assert "AS did_anything" in query
    did_anything = query.split("AS did_anything")[0].split("AS engaged_value")[1]
    assert "NOT LIKE 'lease.%%'" in did_anything


def test_the_volition_word_is_gone_from_the_filter():
    """"Elected" is the category error the module's own docstring names.

    The metric was renamed off it on 2026-08-18; the presupposition survived
    one layer down, inside the filter, which is where it decided a number.
    """
    live = "\n".join(ln for ln in SOURCE.splitlines()
                      if not ln.lstrip().startswith("#"))
    for word in ("electing it", "rather than\n# elected"):
        assert word not in live
