"""Every name list_tools emits is a name the MCP wire dispatches.

On /mcp/ FastMCP registers the register=True tools and the eight workflow
aliases and nothing else (src/tool_registration.py). REST and stdio resolve
legacy names through the alias table before dispatch, so `list_agents` works
there and is `ToolError: Unknown tool` on the mount. A schema-driven client
calls only what tools/list returned, so a name list_tools emits that the mount
does not carry is a dead end -- the class #2165 closed for response hints, one
surface over.

Until 2026-09-12 list_tools(lite=false) carried 30 such names (F1 of
docs/operations/tool-surface-audit-2026-09-12.md): a hand-written `categories`
dict that predated the router consolidation, getting_started.next_steps,
tool_catalog.WORKFLOWS, the related_to / depends_on fields declared in
src/tool_meta.py, and the ASCII tool_map. The hint scanner admitted bare names
under related_tools only, so it reported clean throughout.

The mount is built here through the production registrars on a fresh instance,
not read from the server module, so the assertion is against what
auto_register_all_tools and _register_common_aliases actually register. The
plugin-lifting fixture keeps both the mount and the listing to the surface this
repo ships.
"""

import json
import re
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts" / "diagnostics"))

import src.mcp_handlers  # noqa: F401  (populates the decorator registry)

import hint_target_advertisement as scanner
from tests.helpers.wire_names import (
    build_mount,
    dead_ends,
    declared_actions,
    parse_call_shape,
)

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

_BARE_TOKEN = re.compile(r"\b[a-z][a-z0-9_]*\b")
_OR_PREFIX = re.compile(r"\AOR\s+")


@pytest.fixture
def mounted(first_party_tool_surface) -> set[str]:
    return build_mount()


async def _list_tools(**arguments):
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    return json.loads((await handle_list_tools(arguments))[0].text)


def _tool_map_names(text):
    """Call shapes and tool-shaped words in the ASCII map.

    A word is tool-shaped when it contains an underscore or is a name the
    dispatcher knows -- a registered tool or ANY alias -- so `state`, an
    alias of get_governance_metrics, counts, and the uppercase box titles do
    not. Parenthesised text is stripped before the bare pass: it is prose
    ("main check-in") or the argument list of a call shape already collected.
    """
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import list_all_aliases

    known = set(get_tool_registry()) | set(list_all_aliases())
    out = []
    for match in scanner.CALL_PATTERN.finditer(text):
        action = scanner._hinted_action(text, match)
        out.append(("tool_map", f"{match.group(1)}(action={action!r})" if action else f"{match.group(1)}()"))
    stripped = re.sub(r"\([^()]*\)", " ", text)
    for token in _BARE_TOKEN.findall(stripped):
        if "_" in token or token in known:
            out.append(("tool_map", token))
    return out


def _getting_started_names(prefix, path, toolkit):
    out = []
    for i, step in enumerate(path):
        for key in ("tool", "canonical_tool", "implementation_tool"):
            if step.get(key):
                out.append((f"{prefix}.path[{i}].{key}", step[key]))
    out += [(f"{prefix}.essential_toolkit.default_path", n) for n in toolkit["default_path"]]
    out += [
        (f"{prefix}.essential_toolkit.preferred_consolidated_tools", n)
        for n in toolkit["preferred_consolidated_tools"]
    ]
    return out


def _full_view_names(payload):
    """(where, value) for every field of the full view that carries a tool name.

    `not_advertised.tools` is deliberately not walked: that block exists to
    name what is NOT on the wire, and test_lite_wire_surface pins it empty.
    """
    out = []
    for i, tool in enumerate(payload["tools"]):
        out.append((f"tools[{i}].name", tool["name"]))
        if tool.get("superseded_by"):
            out.append((f"tools[{i}].superseded_by", tool["superseded_by"]))
    for tier, names in payload["tiers"].items():
        out += [(f"tiers.{tier}", n) for n in names]
    for category, block in payload["categories"].items():
        out += [(f"categories.{category}.tools", n) for n in block["tools"]]
    started = payload["getting_started"]
    out += _getting_started_names("getting_started", started["path"], started["essential_toolkit"])
    for group in ("for_new_agents", "next_steps"):
        for i, step in enumerate(started[group]):
            out += [(f"getting_started.{group}[{i}].tools", n) for n in step["tools"]]
    for workflow, steps in payload["workflows"].items():
        out += [(f"workflows.{workflow}", n) for n in steps]
    for name, record in payload["relationships"].items():
        out.append(("relationships", name))
        for field in ("depends_on", "related_to", "replaces"):
            out += [(f"relationships.{name}.{field}", n) for n in record.get(field) or []]
        if record.get("superseded_by"):
            out.append((f"relationships.{name}.superseded_by", record["superseded_by"]))
        out += [
            (f"relationships.{name}.recovery_hierarchy.{key}", call)
            for key, call in (record.get("recovery_hierarchy") or {}).items()
        ]
    for section, block in (payload.get("sections") or {}).items():
        out += [(f"sections.{section}.tools", n) for n in block["tools"]]
    out += _tool_map_names(payload["tool_map"])
    return out


def _lite_view_names(payload):
    out = [(f"tools[{i}].name", t["name"]) for i, t in enumerate(payload["tools"])]
    for category, block in payload["categories_summary"].items():
        out += [(f"categories_summary.{category}.tools", n) for n in block["tools"]]
    for workflow, steps in payload["workflows"].items():
        out += [(f"workflows.{workflow}", _OR_PREFIX.sub("", step)) for step in steps]
    out += [("signatures", key) for key in payload["signatures"]]
    out += _getting_started_names("", payload["getting_started_path"], payload["essential_toolkit"])
    return out


def _call_shapes_everywhere(payload, path="$"):
    """Every `name(` token in any string of the payload, keys included.

    Prose fields (descriptions, notes, tips, quick_start) are not name fields,
    but an instruction written as a call inside them is one all the same.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _call_shapes_everywhere(key, f"{path}.{key}")
            yield from _call_shapes_everywhere(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            yield from _call_shapes_everywhere(value, f"{path}[{i}]")
    elif isinstance(payload, str):
        for match in scanner.CALL_PATTERN.finditer(payload):
            action = scanner._hinted_action(payload, match)
            yield path, f"{match.group(1)}(action={action!r})" if action else f"{match.group(1)}()"


def test_the_mount_is_the_roster_and_nothing_else(mounted):
    """What the registrars mount: every registered tool, every workflow alias,
    no legacy alias. The premise every other test here rests on."""
    from src.tool_modes import advertised_tool_names_full

    assert mounted == advertised_tool_names_full()
    assert "list_agents" not in mounted and "agent" in mounted
    assert "store_knowledge_graph" not in mounted and "store_finding" in mounted


@pytest.mark.asyncio
async def test_the_full_view_names_only_what_the_mount_dispatches(mounted):
    payload = await _list_tools(lite=False)
    entries = _full_view_names(payload)
    assert len(entries) > 200, "the walk found too little to be checking anything"
    actions = declared_actions()
    assert dead_ends(entries, mounted, actions) == []
    assert dead_ends(list(_call_shapes_everywhere(payload)), mounted, actions) == []


@pytest.mark.asyncio
async def test_the_compact_view_names_only_what_the_mount_dispatches(mounted):
    payload = await _list_tools(lite=True)
    entries = _lite_view_names(payload)
    assert len(entries) > 100
    actions = declared_actions()
    assert dead_ends(entries, mounted, actions) == []
    assert dead_ends(list(_call_shapes_everywhere(payload)), mounted, actions) == []


@pytest.mark.asyncio
async def test_categories_are_the_listed_tools_partitioned():
    """The derived block covers every listed name exactly once and nothing else.

    The hand-written block it replaced omitted 33 of the 50 advertised names
    -- every router and workflow alias among them -- beside the 30 twins.
    """
    payload = await _list_tools(lite=False)
    listed = [t["name"] for t in payload["tools"]]
    in_categories = [n for block in payload["categories"].values() for n in block["tools"]]
    assert sorted(in_categories) == sorted(listed)
    by_name = {t["name"]: t for t in payload["tools"]}
    for category, block in payload["categories"].items():
        assert {by_name[n]["category"] for n in block["tools"]} == {category}
        assert block["name"].endswith(by_name[block["tools"][0]]["category_name"])
    assert {
        "agent", "admin", "knowledge", "dialectic", "export", "observe", "config",
        "calibration", "start_session", "sync_state", "store_finding", "consult",
        "skills", "bind_session",
    } <= set(in_categories)
    priorities = [block["priority"] for block in payload["categories"].values()]
    assert priorities == sorted(priorities)
    assert set(payload["relationships"]) == set(listed)


@pytest.mark.asyncio
async def test_a_filter_narrows_categories_and_relationships_with_the_listing():
    payload = await _list_tools(lite=False, tier="essential")
    listed = {t["name"] for t in payload["tools"]}
    assert listed == set(payload["tiers"]["essential"])
    assert {n for block in payload["categories"].values() for n in block["tools"]} == listed
    assert set(payload["relationships"]) == listed


def test_the_checker_is_not_vacuous(mounted):
    """Planted dead ends of each kind are reported; the fixed shapes pass."""
    actions = declared_actions()
    planted = [
        ("categories.lifecycle.tools", "list_agents"),  # dispatch-only twin
        ("workflows.monitoring", "observe_agent"),  # dispatch-only twin
        ("relationships.x.related_to", "agent(action='nope')"),  # unrouted action
        ("relationships.x.related_to", "list_agents(action='list')"),  # call against a twin
        ("relationships.x.related_to", "ping_agent"),  # no such tool
        ("tool_map", "state"),  # an alias word, not a wire name
    ]
    problems = dead_ends(planted, mounted, actions)
    assert len(problems) == len(planted), problems
    assert dead_ends(
        [("ok", "agent(action='list')"), ("ok", "store_finding"), ("ok", "consult(brief='...')"),
         ("ok", "self_recovery(action='review', reflection='...')")],
        mounted, actions,
    ) == []


def test_the_tool_map_walk_sees_the_words_that_matter():
    text = "│ (view state) │ list_agents │ agent(action='list') │ OBSERVABILITY │ knowledge │"
    names = [value for _, value in _tool_map_names(text)]
    assert "list_agents" in names
    assert "agent(action='list')" in names
    assert "knowledge" in names
    assert "state" not in names, "parenthesised prose is stripped"
    assert "OBSERVABILITY" not in names
    assert "state" in [value for _, value in _tool_map_names("call state to read the verdict")]


def test_parse_call_shape():
    assert parse_call_shape("agent(action='list')") == ("agent", "list")
    assert parse_call_shape("consult(brief='...')") == ("consult", None)
    assert parse_call_shape("start_session(force_new=true)") == ("start_session", None)
    assert parse_call_shape("list_agents") is None
    assert parse_call_shape("use agent(action='list') first") is None
    assert parse_call_shape("agent (action=get | update)") is None
    assert parse_call_shape('knowledge(query="action=\'store\'", action="search")') == ("knowledge", "search")
    assert parse_call_shape("dialectic(action='get'|'list')") == ("dialectic", None)
