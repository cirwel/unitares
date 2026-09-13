"""Every tool filter must count the tool a call DISPATCHED to.

`audit.tool_usage.tool_name` records the name the caller invoked. A workflow
name reaches a different tool: `sync_state` is a `process_agent_update` call,
`record_result` an `outcome_event` call, `search_shared_memory` /
`store_finding` / `update_finding` are `knowledge` calls. The recorder writes
`payload.canonical_tool` on every aliased row (#1424), so the dispatched tool
is `coalesce(payload->>'canonical_tool', tool_name)`.

WHY THIS FILE EXISTS. Twice a name-keyed filter here undercounted without an
error. First, `cohort_engaged` named `search_knowledge_graph` but not
`search_shared_memory`, which an independent reviewer caught after the #1856
sweep had judged the file clean. Second, the check-in, conversion and
outcome-pipe filters named `process_agent_update` and `outcome_event` only.
Over the 14d before 2026-09-13 they missed 1,696 of 8,050 check-ins, and the
outcome pipe read ZERO rows because all 370 outcome calls arrived as
`record_result`. These tests check what the queries SELECT, not what the module
refrains from saying.
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


DISPATCHED = re.compile(
    r"coalesce\((?:\w+\.)?payload->>'canonical_tool',\s*(?:\w+\.)?tool_name\)"
    r"\s*(?:IN\s*\(([^)]*)\)|=\s*'([^']+)')",
    re.S | re.I,
)
NAME_ONLY = re.compile(
    r"(?<![\w>])(?:\w+\.)?tool_name\s*(?:IN\s*\(([^)]*)\)|=\s*'([^']+)')",
    re.S,
)

# Queries keyed on the invoked name on purpose, and the continuity columns
# that keep a changed figure's old name-only count beside it.
INVOKED_NAME_QUERIES = {"surface_return_rate"}
CONTINUITY_MARKER = "invoked_name"


def _names(match: re.Match) -> set[str]:
    listed, single = match.group(1), match.group(2)
    return set(re.findall(r"'([^']+)'", listed)) if listed else {single}


def _queries() -> dict[str, str]:
    return _load()._snapshot_queries()


def _alias_names() -> set[str]:
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    return set(_TOOL_ALIASES)


def test_dispatched_filters_exist():
    """Guards the guard: a regex that matches nothing would pass vacuously."""
    found = sum(len(DISPATCHED.findall(sql)) for sql in _queries().values())
    assert found >= 5


def test_name_only_filters_are_the_listed_exceptions():
    """Outside the invoked-name query, a bare tool_name filter must be a continuity column."""
    offenders = []
    for key, sql in _queries().items():
        if key in INVOKED_NAME_QUERIES:
            continue
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") or "canonical_tool" in stripped:
                continue
            if NAME_ONLY.search(stripped):
                window = sql[sql.index(line):sql.index(line) + 400]
                if CONTINUITY_MARKER not in window:
                    offenders.append(f"{key}: {stripped}")
    assert not offenders, "\n".join(offenders)


def test_a_dispatched_filter_names_no_alias():
    """An alias never comes out of coalesce(canonical_tool, tool_name), so naming one is dead."""
    aliases = _alias_names()
    dead = []
    for key, sql in _queries().items():
        for match in DISPATCHED.finditer(sql):
            dead += [f"{key}: {name}" for name in sorted(_names(match) & aliases)]
    assert not dead, dead


@pytest.mark.parametrize(
    "query, tool",
    [
        ("checkin_concentration", "process_agent_update"),
        ("onboard_conversion", "process_agent_update"),
        ("onboard_conversion", "knowledge"),
        ("onboard_conversion", "outcome_event"),
        ("outcome_pipe_health", "outcome_event"),
        ("agent_kg_retrieval", "knowledge"),
        ("review_nudge_conversion", "dialectic"),
    ],
)
def test_each_figure_counts_its_dispatched_tool(query, tool):
    sql = _queries()[query]
    assert any(tool in _names(m) for m in DISPATCHED.finditer(sql)), (query, tool)


def test_the_workflow_names_reach_the_tools_these_filters_name():
    """If an alias is retargeted, the dispatched filters above stop covering it."""
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    expected = {
        "sync_state": "process_agent_update",
        "record_result": "outcome_event",
        "search_shared_memory": "knowledge",
        "store_finding": "knowledge",
        "update_finding": "knowledge",
        "request_review": "dialectic",
    }
    assert {name: _TOOL_ALIASES[name].new_name for name in expected} == expected


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
