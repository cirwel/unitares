"""An action-injecting alias must not advertise other actions' parameters.

`search_shared_memory` injects action="search" but inherited the whole
`knowledge` router schema — 47 parameters, `content` / `summary` /
`discovery_id` / `superseded_by` among them. The name says search; the schema
offered to store and update. Callers read the schema, because that is where the
affordances are.

Measured 2026-08-11 against the live tool list: a local gemma4 asked to update a
discovery picked `search_shared_memory` in 3 of 3 trials. Production agrees —
`knowledge` action=update/store is the largest validation-error bucket on the
server (73 rows since 2026-08-01).

Negative result, recorded so nobody re-runs it expecting otherwise: narrowing
the schema did NOT change that pick (still 3/3), nor did naming `UPDATE` in the
router's first description line, nor renaming the alias to
`search_only_shared_memory`. Removing the workflow aliases altogether did —
0/3 to 3/3. The lure is the alias NAME occupying the router's semantic space,
not the parameter list. This module guards the honesty of the schema; it does
not claim to fix tool selection.

Two failure directions to guard, and they pull against each other:
  - advertising a write parameter on a read alias lures callers (the bug)
  - dropping a parameter the action DOES read breaks callers (the overcorrection)

The second is the dangerous one. FastMCP validates alias arguments before
dispatch and these aliases carry no extra-argument passthrough, so a wrongly
dropped name is REJECTED, not ignored.
"""

from __future__ import annotations

import inspect
import re

import pytest

import src.mcp_handlers  # noqa: F401  (populates the tool registry)
from src.tool_registration import (
    ALIAS_SCHEMA_DROP,
    ALIAS_SCHEMA_KEEP,
    _ALIAS_ALWAYS_KEEP,
)
from src.mcp_handlers.tool_stability import _TOOL_ALIASES
from src.tool_schemas import get_tool_definitions


def _schema(tool_name: str) -> dict:
    # Via mcp_compat: the SDK exposes `.inputSchema` on 1.x and `.input_schema`
    # on 2.x, and the two versions do differ between dev and CI here.
    from src.mcp_compat import get_tool_input_schema

    for t in get_tool_definitions():
        if t.name == tool_name:
            return get_tool_input_schema(t, {}) or {}
    raise AssertionError(f"{tool_name} has no tool definition")


@pytest.mark.parametrize("table", [ALIAS_SCHEMA_DROP, ALIAS_SCHEMA_KEEP])
def test_every_narrowed_alias_injects_an_action(table):
    """Narrowing only makes sense when the alias pins the action itself."""
    for alias in table:
        info = _TOOL_ALIASES.get(alias)
        assert info is not None, f"{alias} is not a registered alias"
        assert info.inject_action, (
            f"{alias} does not inject an action, so it cannot know which "
            "parameters are out of scope — narrowing it would be a guess"
        )


def test_keep_lists_name_only_real_router_params():
    """A keep entry the router does not have is silent dead weight.

    Keep-lists are allowed only for names with no existing callers, so an
    unrecognised entry cannot break anyone — it just quietly does nothing while
    reading like intent. `related_to` and `discoveries` were in the first draft
    of store_finding's list and neither exists on `knowledge`.
    """
    for alias, keep in ALIAS_SCHEMA_KEEP.items():
        router = _TOOL_ALIASES[alias].new_name
        props = set((_schema(router).get("properties") or {}))
        unknown = sorted(keep - props - _ALIAS_ALWAYS_KEEP)
        assert not unknown, (
            f"{alias} keeps {unknown}, which {router} does not advertise"
        )


def test_write_aliases_do_not_advertise_read_filters():
    """The new write names must not re-create the crowding they exist to fix.

    `store_finding` / `update_finding` were added because a model matching on
    the domain noun never reached `knowledge` — `search_shared_memory` absorbed
    the intent first. A write alias that also advertised `query`, `limit` and
    `search_mode` would compete for read intents the same way, in reverse.
    """
    for alias in ("store_finding", "update_finding"):
        keep = ALIAS_SCHEMA_KEEP[alias]
        for read_only in ("query", "limit", "search_mode", "offset",
                          "include_archived", "semantic", "min_similarity"):
            assert read_only not in keep, (
                f"{alias} advertises the read filter {read_only!r}"
            )


def test_write_aliases_carry_what_their_action_needs():
    """Guard the overcorrection: a keep-list too tight makes the alias useless."""
    assert "discovery_id" in ALIAS_SCHEMA_KEEP["update_finding"], (
        "update_finding cannot identify what to update"
    )
    assert "status" in ALIAS_SCHEMA_KEEP["update_finding"], (
        "update_finding cannot set the field it exists to set — note the router "
        "reads `status`, not the narrow tool's legacy `new_status`"
    )
    assert {"summary", "details"} <= ALIAS_SCHEMA_KEEP["store_finding"], (
        "store_finding cannot carry the finding itself"
    )


def test_dropped_params_exist_on_the_router():
    """A stale name in the drop list is dead weight that reads as protection."""
    for alias, dropped in ALIAS_SCHEMA_DROP.items():
        router = _TOOL_ALIASES[alias].new_name
        props = set((_schema(router).get("properties") or {}))
        missing = sorted(dropped - props)
        assert not missing, (
            f"{alias} drops {missing}, which {router} no longer advertises. "
            "Remove them from ALIAS_SCHEMA_DROP rather than leaving a list that "
            "looks like it is guarding something."
        )


def test_dropped_params_are_never_read_by_the_injected_action():
    """The membership rule, enforced against handler source.

    This is the overcorrection guard: if someone adds a parameter here that the
    action's code path actually reads, callers passing it start getting rejected.
    """
    from src.mcp_handlers.knowledge import handlers as knowledge_handlers

    # Only the knowledge/search path is covered today; extend alongside the map.
    assert set(ALIAS_SCHEMA_DROP) == {"search_shared_memory"}, (
        "ALIAS_SCHEMA_DROP grew — extend this test with the new action's "
        "handler source before trusting the new entry"
    )

    region = inspect.getsource(knowledge_handlers.handle_search_knowledge_graph)
    for helper in ("_KnowledgeSearchState", "_validate_search_backend"):
        obj = getattr(knowledge_handlers, helper, None)
        if obj is not None:
            region += inspect.getsource(obj)

    read_anyway = [
        p for p in sorted(ALIAS_SCHEMA_DROP["search_shared_memory"])
        if re.search(rf"""["']{re.escape(p)}["']""", region)
    ]
    assert not read_anyway, (
        f"the search path reads {read_anyway}, so dropping them from "
        "search_shared_memory's schema would make valid calls fail validation"
    )


def test_search_alias_keeps_every_filter_the_action_uses():
    """Subtracting must not cost the caller a real search capability.

    `include_archived` / `include_cold` are the specific trap: the search path
    reads them, but `search_knowledge_graph`'s own schema never declares them —
    so deriving the alias schema from that narrow tool (the obvious approach)
    would have silently removed working parameters. Subtracting keeps them.
    """
    kept = set((_schema("knowledge").get("properties") or {})) - ALIAS_SCHEMA_DROP[
        "search_shared_memory"
    ]
    for filter_param in (
        "query", "tags", "status", "discovery_type", "severity",
        "limit", "search_mode", "include_archived", "include_cold",
    ):
        assert filter_param in kept, (
            f"{filter_param} is a live search filter but would no longer be "
            "advertised on search_shared_memory"
        )


@pytest.mark.parametrize(
    "lure", ["content", "summary", "discovery_id", "superseded_by"]
)
def test_write_lures_are_gone(lure):
    """The parameters that actually misled a model, named individually."""
    assert lure in ALIAS_SCHEMA_DROP["search_shared_memory"], (
        f"{lure} is back on the read alias; this is the exact affordance that "
        "made a local model choose search_shared_memory for an update task"
    )
