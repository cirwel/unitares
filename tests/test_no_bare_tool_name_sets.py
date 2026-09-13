"""A new tool-name table in src/ is either a CallSet or a listed exception.

Tool-name sets that key behavior drift silently: a set written with the names
of one era stops matching when the call arrives under another name, and no
test notices, because each set was written without a check against the
registries. ``src/tool_call_sets.py`` answers membership for the call a name
dispatches to, and ``tests/test_tool_call_sets.py`` holds its entries to the
registries. This test closes the other side: it scans ``src/`` for literal
tables of tool names that bypass it.

What counts as a table: a set literal, a ``frozenset(...)`` / ``set(...)`` of
a list or tuple literal, a list or tuple literal on the right of ``in``, or the
keys of a module-level dict literal. A table is flagged when it holds at least
two distinct names that are registered tools or aliases. Literals passed to
``call_set(...)`` are declarations, not tables.

Every flagged table must appear in ``EXCEPTIONS`` with the reason it is not a
CallSet, and every entry there must still be flagged, so the list cannot rot.
Entries are keyed by file, enclosing function, and the name the literal is
assigned to; an inline literal, which has no name, is keyed by the tool names
it holds. Line numbers are not part of a key.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Settle handler imports so every tool and router is registered.
import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.support.model_inference  # noqa: F401

from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.tool_stability import _TOOL_ALIASES

REPO_ROOT = Path(__file__).resolve().parents[1]

GUARDED_REGISTRY = "a registry table with its own guard"

EXCEPTIONS: dict[str, str] = {
    # --- the registries themselves ---------------------------------------
    "src/mcp_handlers/tool_stability.py::_TOOL_ALIASES": (
        f"{GUARDED_REGISTRY}: the alias table; tool_edge_index.py --lint checks every target"
    ),
    "src/tool_annotations.py::TOOL_ANNOTATIONS": (
        f"{GUARDED_REGISTRY}: tests/test_tool_annotations.py holds it to the roster both ways"
    ),
    "src/mcp_handlers/introspection/tool_catalog.py::CATEGORY_PRESENTATION": (
        f"{GUARDED_REGISTRY}: keyed by category, whose names coincide with router names; "
        "tests/test_tool_registry_bookkeeping.py"
    ),
    "src/mcp_handlers/introspection/tool_catalog.py::TOOL_DESCRIPTION_OVERRIDES": (
        f"{GUARDED_REGISTRY}: dispatch-only alias names by design; "
        "tests/test_describe_tool_drift.py pins the scope"
    ),
    "src/alias_schema.py::ALIAS_SCHEMA_KEEP": (
        f"{GUARDED_REGISTRY}: keyed by the workflow alias whose schema it narrows; "
        "tests/test_alias_schema_narrowing.py"
    ),
    "src/alias_schema.py::ALIAS_SCHEMA_PROPERTY_OVERRIDES": (
        f"{GUARDED_REGISTRY}: keyed by the advertised alias whose schema it overrides, "
        "so the canonical tool keeps its own; tests/test_mcp_schema_parity.py holds "
        "every overridden value to what that alias's dispatch path accepts"
    ),
    # --- keyed on the invoked name by design ------------------------------
    "src/mcp_handlers/middleware/envelope_step.py::_COMPACT_READ_ALIASES": (
        "the experience envelope is chosen by the friendly name the caller used; "
        "a canonical call gets no envelope on purpose"
    ),
    "src/services/http_tool_service.py::_DIRECT_HTTP_TOOL_HANDLERS": (
        "REST shortcut keyed on the invoked name; an alias deliberately takes the full pipeline"
    ),
    "src/mcp_handlers/middleware/identity_step.py::resolve_identity()[name in identity,onboard]": (
        "PATH 0 agent_uuid passthrough for the invoked identity/onboard; whether their "
        "aliases take it is an open identity-posture decision"
    ),
    # --- compared with a name already resolved ----------------------------
    "src/mcp_handlers/middleware/envelope_step.py::build_experience_envelope()"
    "[canonical_name in get_governance_metrics,process_agent_update]": (
        "compared with canonical_name"
    ),
    "src/mcp_handlers/middleware/envelope_step.py::build_experience_envelope()"
    "[friendly_name in store_finding,update_finding]": (
        "the experience envelope is built only for workflow aliases; this branch picks "
        "the knowledge-write summary by which of the two was called"
    ),
    "src/mcp_handlers/middleware/identity_step.py::_IDENTITY_LIFECYCLE_TOOLS": (
        "compared with canonical_name"
    ),
    "src/tool_schemas.py::_HIDE_IDENTITY_PARAMS_TOOLS": (
        "consulted with the canonical tool whose schema is built; aliases inherit that schema"
    ),
    "src/http_routes/access.py::_HTTP_PREBIND_SKIP_TOOLS": (
        "REST prebind skip; matched on the invoked name and, with the identity-gate "
        "parity change, on its canonical tool"
    ),
    # --- keyed by a handler's own name, not by dispatch -------------------
    "src/mcp_handlers/validators.py::PARAM_ALIASES": (
        "parameter synonyms keyed by the handler that applies them; the knowledge "
        "handlers pass their pre-consolidation names explicitly"
    ),
    "src/mcp_handlers/introspection/tool_catalog.py::LITE_PARAMETER_PRIORITIES": (
        "describe_tool parameter ordering, looked up by requested then canonical name"
    ),
    # --- not a membership decision ----------------------------------------
    "src/mcp_handlers/error_helpers.py::tool_not_found_error()"
    "[t in describe_tool,health_check,list_tools,process_agent_update,search_knowledge_graph]": (
        "suggestion list filtered through the registry at call time"
    ),
    "src/gateway/query_engine.py::INTENTS": "gateway intent labels, not tool names",
    "src/mcp_handlers/validators.py::RESERVED_NAMES": "reserved agent display names, not tool names",
    # --- description tables with alias keys that are never served ---------
    # Known drift (tool-surface sweep 2026-09-13): these readers look up the
    # canonical tool, so alias keys here are dead text. Left for the change
    # that owns tool_descriptions.py / tool_catalog.py, not repaired here.
    "src/tool_descriptions.py::_EISV_CLIENT_TOOLS": (
        "description decoration; six of eleven names are aliases whose text is never served"
    ),
    "src/tool_descriptions.py::_IDENTITY_DESCRIPTION_OVERRIDES": "description text keyed by tool",
    "src/tool_descriptions.py::_INFERENCE_DESCRIPTION_OVERRIDES": (
        "description text keyed by tool; the request_review key is never served"
    ),
    "src/tool_descriptions.py::_DESCRIPTION_APPENDICES": "description text keyed by tool",
    "src/mcp_handlers/introspection/tool_catalog.py::COMMON_PATTERNS": (
        "describe_tool usage examples; its alias keys (list_agents, observe_agent, "
        "store_knowledge_graph) describe calls that fail on /mcp/"
    ),
}


def _tool_names() -> set[str]:
    return set(get_tool_registry()) | set(_TOOL_ALIASES)


def _strings(nodes) -> set[str]:
    return {
        node.value for node in nodes
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _enclosing_scope(node: ast.AST, parents: dict) -> str:
    scopes = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(f"{current.name}()")
        elif isinstance(current, ast.ClassDef):
            scopes.append(current.name)
    return ".".join(reversed(scopes))


def _assigned_target(node: ast.AST, parents: dict) -> str | None:
    """The name a table literal is assigned to, through frozenset()/set()."""
    current = node
    parent = parents.get(current)
    if (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id in {"frozenset", "set"}
    ):
        current, parent = parent, parents.get(parent)
    if isinstance(parent, ast.Assign) and parent.value is current:
        return ast.unparse(parent.targets[0])
    if isinstance(parent, ast.AnnAssign) and parent.value is current:
        return ast.unparse(parent.target)
    return None


def _table_key(relative: str, node: ast.AST, parents: dict, names: set[str]) -> str:
    """Stable per-table key: the assigned name, or the names an inline literal holds.

    An assigned table keeps its key when entries change, so registry tables
    do not churn this file. An inline literal has no name of its own, so the
    expression it tests and its contents are its identity: a second inline
    table in the same function is a different key, not a free ride on an
    excepted one, and the key says which name it is keyed on.
    """
    scope = _enclosing_scope(node, parents)
    target = _assigned_target(node, parents)
    if target is not None:
        label = f"{scope}.{target}" if scope else target
    else:
        parent = parents.get(node)
        tested = (
            f"{ast.unparse(parent.left)} in "
            if isinstance(parent, ast.Compare) and node in parent.comparators
            else ""
        )
        label = f"{scope or '<module>'}[{tested}{','.join(sorted(names))}]"
    return f"{relative}::{label}"


def _inside_call_set(node: ast.AST, parents: dict) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "call_set"
        ):
            return True
    return False


def _table_names(node: ast.AST, parents: dict) -> set[str]:
    parent = parents.get(node)
    if isinstance(node, ast.Set):
        return _strings(node.elts)
    if isinstance(node, (ast.List, ast.Tuple)):
        if (
            isinstance(parent, ast.Compare)
            and node in parent.comparators
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
        ):
            return _strings(node.elts)
        if (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id in {"frozenset", "set"}
        ):
            return _strings(node.elts)
    if (
        isinstance(node, ast.Dict)
        and isinstance(parent, (ast.Assign, ast.AnnAssign))
        and isinstance(parents.get(parent), ast.Module)
    ):
        return _strings(key for key in node.keys if key is not None)
    return set()


def _flagged_tables() -> tuple[dict[str, list[str]], dict[str, set]]:
    names = _tool_names()
    flagged: dict[str, list[str]] = {}
    contents: dict[str, set] = {}
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            found = _table_names(node, parents) & names
            if len(found) < 2 or _inside_call_set(node, parents):
                continue
            key = _table_key(relative, node, parents, found)
            flagged[key] = sorted(found)
            contents.setdefault(key, set()).add(frozenset(found))
    return flagged, contents


FLAGGED, CONTENTS = _flagged_tables()


def test_scan_sees_the_known_tables():
    """Guard against a vacuous scan: the alias table itself must be found."""
    assert "src/mcp_handlers/tool_stability.py::_TOOL_ALIASES" in FLAGGED


def test_every_tool_name_table_is_a_call_set_or_a_listed_exception():
    unexplained = {key: names for key, names in FLAGGED.items() if key not in EXCEPTIONS}
    assert not unexplained, (
        "tool-name tables that bypass src/tool_call_sets.py:\n"
        + "\n".join(f"  {key}: {names[:6]}" for key, names in sorted(unexplained.items()))
        + "\nDeclare a call_set(...) matched on the call, or add an EXCEPTIONS "
        "entry saying which name the table is keyed on and why."
    )


def test_every_key_names_one_table():
    """A reassigned name holding different tables would let one hide behind the other."""
    shared = sorted(key for key, tables in CONTENTS.items() if len(tables) > 1)
    assert not shared, f"keys that name more than one distinct table: {shared}"


def test_every_exception_is_still_a_table():
    stale = sorted(key for key in EXCEPTIONS if key not in FLAGGED)
    assert not stale, f"EXCEPTIONS entries that no longer match a table: {stale}"
