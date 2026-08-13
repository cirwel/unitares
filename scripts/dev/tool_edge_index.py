#!/usr/bin/env python3
"""Generate docs/dev/TOOL_EDGE_INDEX.md — the resolved tool dispatch graph.

Why this exists: every hop from an MCP tool name to the code that runs is made
at *import time*, not written down anywhere a reader can follow.

    tool name        -> registered by the ``@mcp_tool`` decorator into
                        ``_TOOL_DEFINITIONS`` (112 decorator sites, no static edge)
    action="..."     -> routed through an ``action_router`` closure that holds
                        the action->delegate map (invisible to grep entirely)
    parameter schema -> loaded by ``importlib.import_module`` off a *string list*
                        in ``src/tool_schemas.py``
    legacy name      -> canonicalized through the ``_TOOL_ALIASES`` table, which
                        may also inject an action

So "where does ``knowledge(action='store')`` actually go?" cannot be answered by
reading the tree. This generator answers it by importing the handler package and
reading the live registries — the same objects dispatch uses.

**Runtime-grounded on purpose.** A grep-based index of this surface produces
false "it's dead" claims: the 13 ``schemas/`` modules have zero static importers
and are fully live. ``docs/operations/dormant-capability-registry.md`` records
5+ such false positives from an earlier static pass. Reading the registry cannot
make that class of error, because it reports what actually registered.

Imports the handler package only — no DB, no Redis, no running server. Needs
``requirements-core.txt`` (mcp, pydantic, numpy, PyYAML), the same floor CI
already installs.

Usage:
    python3 scripts/dev/tool_edge_index.py            # write the index
    python3 scripts/dev/tool_edge_index.py --check    # exit 1 if stale
    python3 scripts/dev/tool_edge_index.py --json     # machine-readable dump

Exit codes:
    0 — index written, or up to date under --check
    1 — index is stale (--check)
    2 — handler package not importable (dependencies absent). Distinct from 1 so
        a caller can tell "cannot look" from "looked and found drift"; the
        doctor SKIPs on 2 rather than reporting a false failure.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "dev" / "TOOL_EDGE_INDEX.md"
UNKNOWN = "?"


@dataclass
class ActionEdge:
    action: str
    target: str
    param_maps: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolEdge:
    name: str
    handler: str
    schema: str | None = None
    identity: str = "required"
    stakes: str = "baseline"
    timeout: float = 30.0
    deprecated: bool = False
    hidden: bool = False
    superseded_by: str | None = None
    default_action: str | None = None
    actions: list[ActionEdge] = field(default_factory=list)


@dataclass
class AliasEdge:
    old_name: str
    new_name: str
    reason: str
    inject_action: str | None = None


def _site(func) -> str:
    """`path/to/file.py:LINE funcname` for a function object, repo-relative."""
    target = inspect.unwrap(func)
    try:
        src = inspect.getsourcefile(target) or ""
        line = target.__code__.co_firstlineno
    except (TypeError, AttributeError):
        return UNKNOWN
    if not src:
        return UNKNOWN
    try:
        rel = Path(src).resolve().relative_to(REPO)
    except ValueError:
        rel = Path(src)
    return f"{rel}:{line} {target.__name__}"


def _class_site(cls) -> str:
    """`path/to/file.py:LINE ClassName` for a class object."""
    try:
        src = inspect.getsourcefile(cls)
        _, line = inspect.getsourcelines(cls)
    except (TypeError, OSError):
        return f"{cls.__name__}"
    if not src:
        return f"{cls.__name__}"
    try:
        rel = Path(src).resolve().relative_to(REPO)
    except ValueError:
        rel = Path(src)
    return f"{rel}:{line} {cls.__name__}"


def _load_registries() -> tuple[dict, dict, dict, list[str]]:
    """Import the handler package and return the live registries.

    Walks every submodule under ``src.mcp_handlers`` so a tool registered by a
    module that ``__init__`` does not re-export is still counted. Import
    failures are collected, never swallowed — a module this generator could not
    import is a hole in the index and is reported as one.
    """
    sys.path.insert(0, str(REPO))
    import src.mcp_handlers as handlers_pkg  # noqa: E402

    failures: list[str] = []
    for info in pkgutil.walk_packages(handlers_pkg.__path__, handlers_pkg.__name__ + "."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 — reported, not suppressed
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")

    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES
    from src.tool_schemas import get_pydantic_schemas

    return _TOOL_DEFINITIONS, get_pydantic_schemas(), _TOOL_ALIASES, failures


def _router_declaration_sites() -> dict[str, str]:
    """tool name -> `file:line` of its ``action_router(...)`` call.

    A router's runtime handler object always reports ``decorators.py`` as its
    source — it is generated there — which tells a reader nothing. The call that
    declares the tool is what they want, and it is an ordinary AST node, so it
    is recovered statically. Purely additive: a router missing here still lists
    its full action table below.
    """
    import ast

    sites: dict[str, str] = {}
    for path in (REPO / "src").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = getattr(func, "id", None) or getattr(func, "attr", None)
            if func_name != "action_router":
                continue
            name_node = next(
                (kw.value for kw in node.keywords if kw.arg == "name"),
                node.args[0] if node.args else None,
            )
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                rel = path.resolve().relative_to(REPO)
                sites[name_node.value] = f"{rel}:{node.lineno} action_router"
    return sites


def _router_actions(handler) -> tuple[dict, dict]:
    """(actions, param_maps) if this handler is an action_router, else ({}, {}).

    ``action_router`` builds its routing table as a closure over ``actions``;
    there is no attribute or module-level name holding it. Reading the closure
    is the only way to recover the edge without re-implementing the router.
    """
    inner = getattr(handler, "__wrapped__", None)
    if inner is None or getattr(inner, "__name__", "") != "router":
        return {}, {}
    try:
        nonlocals = inspect.getclosurevars(inner).nonlocals
    except (TypeError, ValueError):
        return {}, {}
    actions = nonlocals.get("actions")
    if not isinstance(actions, dict):
        return {}, {}
    param_maps = nonlocals.get("_param_maps")
    return actions, param_maps if isinstance(param_maps, dict) else {}


def collect() -> tuple[list[ToolEdge], list[AliasEdge], list[str], int]:
    definitions, schemas, aliases, failures = _load_registries()
    router_sites = _router_declaration_sites()

    tools: list[ToolEdge] = []
    for name in sorted(definitions):
        td = definitions[name]
        schema_model = schemas.get(name)
        edge = ToolEdge(
            name=name,
            handler=router_sites.get(name) or _site(td.handler),
            schema=_class_site(schema_model) if schema_model is not None else None,
            identity=td.requires_identity,
            stakes=td.requires_verdict,
            timeout=td.timeout,
            deprecated=td.deprecated,
            hidden=td.hidden,
            superseded_by=td.superseded_by,
            default_action=td.default_action,
        )
        action_map, param_maps = _router_actions(td.handler)
        for action in sorted(action_map):
            edge.actions.append(
                ActionEdge(
                    action=action,
                    target=_site(action_map[action]),
                    param_maps=dict(param_maps.get(action, {})),
                )
            )
        tools.append(edge)

    alias_edges = [
        AliasEdge(
            old_name=old,
            new_name=alias.new_name,
            reason=alias.reason,
            inject_action=alias.inject_action,
        )
        for old, alias in sorted(aliases.items())
    ]

    registered = {t.name for t in tools}
    unbound_schemas = sum(1 for key in schemas if key not in registered)
    return tools, alias_edges, failures, unbound_schemas


def _flags(tool: ToolEdge) -> str:
    marks = []
    if tool.deprecated:
        marks.append(f"deprecated→`{tool.superseded_by}`" if tool.superseded_by else "deprecated")
    if tool.hidden:
        marks.append("hidden")
    if tool.identity != "required":
        marks.append(f"identity={tool.identity}")
    if tool.stakes != "baseline":
        marks.append(f"stakes={tool.stakes}")
    return ", ".join(marks) or "—"


def render(
    tools: list[ToolEdge],
    aliases: list[AliasEdge],
    failures: list[str],
    unbound_schemas: int,
) -> str:
    routers = [t for t in tools if t.actions]
    action_count = sum(len(t.actions) for t in routers)

    lines = [
        "<!-- GENERATED by scripts/dev/tool_edge_index.py — do not edit by hand. Re-run to refresh. -->",
        "",
        "# Tool Edge Index",
        "",
        "**Every MCP tool resolved to the code that runs.** Generated by importing the",
        "handler package and reading the live registries — `_TOOL_DEFINITIONS`, the",
        "`action_router` closures, the `src/tool_schemas.py` schema loader, and",
        "`_TOOL_ALIASES`.",
        "",
        "This index exists because none of those edges are readable in the source. Tool",
        "registration happens in a decorator, action routing lives in a closure, schema",
        "modules are loaded from a string list in `src/tool_schemas.py`, and legacy names",
        "are rewritten by an alias table. Reading the tree cannot tell you where",
        "`knowledge(action=\"store\")` goes; this file can.",
        "",
        "It is **runtime-grounded, not grep-derived**: it reports what actually",
        "registered. That is deliberate — a static pass over this surface reports live",
        "code as dead (the 13 `schemas/` modules have zero static importers and are fully",
        "wired). See",
        "[`dormant-capability-registry.md`](../operations/dormant-capability-registry.md)",
        "for the false-positive history this avoids.",
        "",
        f"**{len(tools)} registered tools · {len(routers)} consolidated "
        f"({action_count} actions) · {len(aliases)} aliases.**",
        "",
        "## Tools",
        "",
        "`handler` is the function the dispatcher calls. For a consolidated tool that is",
        "the generated router — see [Action routing](#action-routing) for its delegates.",
        "",
        "| Tool | Handler | Params schema | Timeout | Notes |",
        "|---|---|---|---|---|",
    ]
    for tool in tools:
        schema = f"`{tool.schema}`" if tool.schema else "—"
        lines.append(
            f"| `{tool.name}` | `{tool.handler}` | {schema} | {tool.timeout:g}s | {_flags(tool)} |"
        )

    lines += [
        "",
        "## Action routing",
        "",
        "The `action=` value each consolidated tool accepts, and the delegate it reaches.",
        "This map is derived from the router's own closure, so it cannot drift from what",
        "actually routes. `remaps` shows parameter aliasing applied before the delegate",
        "runs (`from→to`, filled only when the destination is absent).",
        "",
    ]
    for tool in routers:
        default = f" · default `{tool.default_action}`" if tool.default_action else ""
        lines += [
            f"### `{tool.name}`{default}",
            "",
            "| Action | Delegate | Remaps |",
            "|---|---|---|",
        ]
        for edge in tool.actions:
            remaps = (
                ", ".join(f"`{k}`→`{v}`" for k, v in sorted(edge.param_maps.items()))
                if edge.param_maps
                else "—"
            )
            lines.append(f"| `{edge.action}` | `{edge.target}` | {remaps} |")
        lines.append("")

    lines += [
        "## Aliases",
        "",
        "Names that resolve to another tool before dispatch. `injects` is an action",
        "supplied by the alias itself, so the call arrives at the canonical tool as if",
        "the caller had passed it.",
        "",
        "| Called as | Resolves to | Injects | Reason |",
        "|---|---|---|---|",
    ]
    for alias in aliases:
        injects = f"`{alias.inject_action}`" if alias.inject_action else "—"
        lines.append(
            f"| `{alias.old_name}` | `{alias.new_name}` | {injects} | {alias.reason} |"
        )

    lines += [
        "",
        "## Coverage",
        "",
        f"- Schema models defined but bound to no registered tool: **{unbound_schemas}**.",
        "  Expected — `_load_pydantic_schemas` collects every `*Params` class in the",
        "  schema modules, including nested and per-action models that never carry a",
        "  tool's own name.",
    ]
    if failures:
        lines += [
            f"- **Handler modules this index could not import: {len(failures)}.** Each is a",
            "  hole in the tables above — a tool registered there is missing, not absent.",
            "",
        ]
        lines += [f"  - `{failure}`" for failure in failures]
    else:
        lines.append(
            "- Every handler submodule imported cleanly, so the tables above are complete."
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the index is stale")
    ap.add_argument("--json", action="store_true", help="dump the graph as JSON to stdout")
    args = ap.parse_args()

    try:
        tools, aliases, failures, unbound = collect()
    except ImportError as exc:
        print(
            f"cannot import the handler package ({exc}) — install "
            "requirements-core.txt to generate or check this index",
            file=sys.stderr,
        )
        return 2

    if args.json:
        json.dump(
            {
                "tools": [
                    {
                        **{k: v for k, v in vars(tool).items() if k != "actions"},
                        "actions": [vars(edge) for edge in tool.actions],
                    }
                    for tool in tools
                ],
                "aliases": [vars(alias) for alias in aliases],
                "import_failures": failures,
                "unbound_schemas": unbound,
            },
            sys.stdout,
            indent=2,
        )
        print()
        return 0

    content = render(tools, aliases, failures, unbound)
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != content:
            print(
                f"{OUT.relative_to(REPO)} is stale — run: "
                "python3 scripts/dev/tool_edge_index.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.relative_to(REPO)} is up to date.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO)} ({content.count(chr(10))} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
