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
    python3 scripts/dev/tool_edge_index.py --lint     # deterministic findings

Exit codes:
    0 — index written, or up to date under --check
    1 — index is stale (--check)
        or the snapshot contains error-severity findings (--lint)
    2 — handler package not importable (dependencies absent). Distinct from 1 so
        a caller can tell "cannot look" from "looked and found drift"; the
        doctor SKIPs on 2 rather than reporting a false failure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "dev" / "TOOL_EDGE_INDEX.md"
JSON_SCHEMA = REPO / "docs" / "dev" / "tool_surface_audit_v1.schema.json"
UNKNOWN = "?"
AUDIT_SCHEMA = "unitares.tool-surface-audit.v1"
DISPATCH_SCHEMA = "unitares.tool-dispatch-snapshot.v1"
EXPOSURE_SCHEMA = "unitares.tool-exposure-snapshot.v1"
DEPLOYABLE_MODES = (
    "minimal",
    "lite",
    "operator_readonly",
    "operator_recovery",
    "full",
)
SURFACE_SOURCE_FILES = (
    "src/mcp_compat.py",
    "src/mcp_handlers/decorators.py",
    "src/mcp_handlers/introspection/tool_catalog.py",
    "src/mcp_handlers/introspection/tool_introspection.py",
    "src/mcp_handlers/tool_stability.py",
    "src/tool_descriptions.py",
    "src/tool_modes.py",
    "src/tool_registration.py",
    "src/tool_schemas.py",
)


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
    known_actions: list[str] = field(default_factory=list)
    actions: list[ActionEdge] = field(default_factory=list)


@dataclass
class AliasEdge:
    old_name: str
    new_name: str
    reason: str
    inject_action: str | None = None
    inject_defaults: dict[str, Any] = field(default_factory=dict)
    param_normalizer: str | None = None
    experience: bool = False


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    code: str
    subject: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _jsonable(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_jsonable(item) for item in value]
        return sorted(normalized, key=lambda item: _canonical_json(item))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(value: Any) -> str:
    """Content address for a JSON-compatible value."""
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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
    for info in pkgutil.walk_packages(
        handlers_pkg.__path__, handlers_pkg.__name__ + "."
    ):
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
            known_actions=sorted(td.known_actions or ()),
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
            inject_defaults=dict(alias.inject_defaults or {}),
            param_normalizer=(
                _site(alias.param_normalizer) if alias.param_normalizer else None
            ),
            experience=bool(alias.experience),
        )
        for old, alias in sorted(aliases.items())
    ]

    registered = {t.name for t in tools}
    unbound_schemas = sum(1 for key in schemas if key not in registered)
    return tools, alias_edges, failures, unbound_schemas


def _tool_payload(tool: ToolEdge) -> dict[str, Any]:
    return {
        "name": tool.name,
        "handler": tool.handler,
        "schema": tool.schema,
        "identity": tool.identity,
        "stakes": tool.stakes,
        "timeout": tool.timeout,
        "deprecated": tool.deprecated,
        "hidden": tool.hidden,
        "superseded_by": tool.superseded_by,
        "default_action": tool.default_action,
        "known_actions": list(tool.known_actions),
        "actions": [
            {
                "action": edge.action,
                "target": edge.target,
                "param_maps": dict(sorted(edge.param_maps.items())),
            }
            for edge in tool.actions
        ],
    }


def _alias_payload(alias: AliasEdge) -> dict[str, Any]:
    return {
        "old_name": alias.old_name,
        "new_name": alias.new_name,
        "reason": alias.reason,
        "inject_action": alias.inject_action,
        "inject_defaults": dict(sorted(alias.inject_defaults.items())),
        "param_normalizer": alias.param_normalizer,
        "experience": alias.experience,
    }


def _hashed_snapshot(schema: str, payload: dict[str, Any]) -> dict[str, Any]:
    unhashed = {"schema": schema, **payload}
    return {
        "schema": schema,
        "content_hash": content_hash(unhashed),
        **payload,
    }


def verify_content_hash(snapshot: dict[str, Any]) -> bool:
    """Verify a snapshot whose hash covers every field except itself."""
    expected = snapshot.get("content_hash")
    unhashed = {key: value for key, value in snapshot.items() if key != "content_hash"}
    return isinstance(expected, str) and expected == content_hash(unhashed)


def _repo_path_from_site(site: str | None) -> Path | None:
    if not site:
        return None
    location = site.split(" ", 1)[0]
    path_text, separator, line_text = location.rpartition(":")
    if not separator or not line_text.isdigit():
        return None
    candidate = (REPO / path_text).resolve()
    try:
        candidate.relative_to(REPO)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _source_manifest(
    tools: list[ToolEdge], aliases: list[AliasEdge]
) -> list[dict[str, str]]:
    """Content-address every source file that contributes dispatch/exposure truth."""
    paths = {
        (REPO / relative).resolve()
        for relative in SURFACE_SOURCE_FILES
        if (REPO / relative).is_file()
    }
    paths.update((REPO / "src" / "mcp_handlers" / "schemas").glob("*.py"))
    for tool in tools:
        for site in (
            tool.handler,
            tool.schema,
            *(edge.target for edge in tool.actions),
        ):
            path = _repo_path_from_site(site)
            if path is not None:
                paths.add(path)
    for alias in aliases:
        path = _repo_path_from_site(alias.param_normalizer)
        if path is not None:
            paths.add(path)

    manifest = []
    for path in sorted(paths):
        try:
            relative = path.resolve().relative_to(REPO)
        except ValueError:
            continue
        manifest.append(
            {
                "path": relative.as_posix(),
                "content_hash": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
        )
    return manifest


def build_dispatch_snapshot(
    tools: list[ToolEdge],
    aliases: list[AliasEdge],
    failures: list[str],
    unbound_schemas: int,
) -> dict[str, Any]:
    source_files = _source_manifest(tools, aliases)
    return _hashed_snapshot(
        DISPATCH_SCHEMA,
        {
            "tools": [_tool_payload(tool) for tool in tools],
            "aliases": [_alias_payload(alias) for alias in aliases],
            "import_failures": list(failures),
            "unbound_schemas": unbound_schemas,
            "source_revision": content_hash(source_files),
            "source_files": source_files,
        },
    )


def _collect_wire_catalog() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Build the actual FastMCP full-mode catalog through production registrars.

    The dispatch registry says what can run after dispatch. This catalog says
    what the MCP protocol advertises before dispatch, including the narrowed
    workflow-alias schemas. Building it through the same registration functions
    avoids a second implementation of alias-schema policy in this audit.
    """
    failures: list[str] = []
    try:
        from src.mcp_compat import FastMCP
        import src.tool_modes as tool_modes
        from src.tool_registration import (
            _register_common_aliases,
            auto_register_all_tools,
        )

        original_mode = tool_modes.TOOL_MODE
        try:
            tool_modes.TOOL_MODE = "full"
            mcp = FastMCP(name="unitares-tool-surface-audit")
            auto_register_all_tools(mcp)
            _register_common_aliases(mcp)
        finally:
            tool_modes.TOOL_MODE = original_mode

        manager = getattr(mcp, "_tool_manager", None)
        registered = getattr(manager, "_tools", None)
        if not isinstance(registered, dict):
            raise TypeError("FastMCP tool manager did not expose a tool dictionary")

        catalog: dict[str, dict[str, Any]] = {}
        for name, exposed in sorted(registered.items()):
            input_schema = _jsonable(getattr(exposed, "parameters", {}) or {})
            catalog[name] = {
                "name": name,
                "description": str(getattr(exposed, "description", "") or ""),
                "input_schema": input_schema,
                "input_schema_hash": content_hash(input_schema),
            }
        return catalog, failures
    except Exception as exc:  # noqa: BLE001 — evidence, not silent fallback
        failures.append(f"{type(exc).__name__}: {exc}")
        return {}, failures


def _schema_properties(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return sorted(properties) if isinstance(properties, dict) else []


def build_exposure_snapshot(
    tools: list[ToolEdge], aliases: list[AliasEdge]
) -> dict[str, Any]:
    """Capture declared modes, actual wire registration, and introspection views."""
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        list_all_aliases,
    )
    from src.tool_modes import LITE_MODE_TOOLS, get_tools_for_mode
    from src.tool_schemas import get_pydantic_schemas

    wire_catalog, collection_failures = _collect_wire_catalog()
    wire_names = set(wire_catalog)
    workflow_aliases = set(AGENT_WORKFLOW_ALIASES) & wire_names
    canonical_wire_names = wire_names - workflow_aliases

    modes: dict[str, dict[str, Any]] = {}
    for mode in DEPLOYABLE_MODES:
        declared = set(get_tools_for_mode(mode))
        # Mirrors src/mcp_server.py: canonical registrations are mode-filtered,
        # then workflow aliases are registered unconditionally.
        advertised = (canonical_wire_names & declared) | workflow_aliases
        mode_payload = {
            "declared": sorted(declared),
            "advertised": sorted(advertised),
            "declared_only": sorted(declared - advertised),
            "advertised_only": sorted(advertised - declared),
        }
        modes[mode] = {
            **mode_payload,
            "content_hash": content_hash(mode_payload),
        }

    registered_names = {tool.name for tool in tools}
    deprecated_alias_names = set(list_all_aliases()) - set(AGENT_WORKFLOW_ALIASES)
    list_tools_full = (registered_names | workflow_aliases) - deprecated_alias_names
    orientation = {
        "list_tools_full": sorted(list_tools_full),
        "list_tools_lite": sorted(list_tools_full & set(LITE_MODE_TOOLS)),
        "full_wire_only": sorted(wire_names - list_tools_full),
        "full_orientation_only": sorted(list_tools_full - wire_names),
    }

    schemas = get_pydantic_schemas()
    aliases_by_name = {alias.old_name: alias for alias in aliases}
    views: list[dict[str, Any]] = []
    for name, wire in sorted(wire_catalog.items()):
        alias = aliases_by_name.get(name) if name in workflow_aliases else None
        canonical_name = alias.new_name if alias else name
        model = schemas.get(canonical_name)
        describe_schema = (
            _jsonable(model.model_json_schema())
            if model is not None
            else wire["input_schema"]
        )
        wire_properties = _schema_properties(wire["input_schema"])
        describe_properties = _schema_properties(describe_schema)
        views.append(
            {
                **wire,
                "kind": "workflow_alias" if alias else "registered_tool",
                "canonical_name": canonical_name,
                "inject_action": alias.inject_action if alias else None,
                "wire_properties": wire_properties,
                "wire_required": sorted(wire["input_schema"].get("required", [])),
                "describe_input_schema": describe_schema,
                "describe_input_schema_hash": content_hash(describe_schema),
                "describe_properties": describe_properties,
                "describe_only_properties": sorted(
                    set(describe_properties) - set(wire_properties)
                ),
                "wire_only_properties": sorted(
                    set(wire_properties) - set(describe_properties)
                ),
            }
        )

    return _hashed_snapshot(
        EXPOSURE_SCHEMA,
        {
            "modes": modes,
            "orientation": orientation,
            "tools": views,
            "collection_failures": collection_failures,
        },
    )


def _alias_cycles(alias_targets: dict[str, str]) -> list[list[str]]:
    cycles: set[tuple[str, ...]] = set()
    for start in sorted(alias_targets):
        order: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in alias_targets:
            if current in positions:
                cycle = order[positions[current] :]
                rotations = [
                    tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))
                ]
                cycles.add(min(rotations))
                break
            positions[current] = len(order)
            order.append(current)
            current = alias_targets[current]
    return [list(cycle) for cycle in sorted(cycles)]


def lint_snapshots(
    dispatch: dict[str, Any], exposure: dict[str, Any]
) -> list[AuditFinding]:
    """Return structural findings without granting them certification authority."""
    findings: list[AuditFinding] = []

    def add(
        severity: str,
        code: str,
        subject: str,
        message: str,
        **evidence: Any,
    ) -> None:
        findings.append(
            AuditFinding(severity, code, subject, message, _jsonable(evidence))
        )

    tools = {tool["name"]: tool for tool in dispatch["tools"]}
    aliases = {alias["old_name"]: alias for alias in dispatch["aliases"]}

    for failure in dispatch["import_failures"]:
        add(
            "error",
            "DISPATCH_IMPORT_FAILURE",
            "dispatch",
            "A handler module failed to import; the dispatch snapshot is incomplete.",
            failure=failure,
        )
    for failure in exposure["collection_failures"]:
        add(
            "error",
            "EXPOSURE_COLLECTION_FAILURE",
            "exposure",
            "The production registration path could not be snapshotted.",
            failure=failure,
        )

    for cycle in _alias_cycles(
        {name: alias["new_name"] for name, alias in aliases.items()}
    ):
        add(
            "error",
            "ALIAS_CYCLE",
            cycle[0],
            "Alias resolution contains a cycle.",
            cycle=cycle,
        )

    for name, alias in sorted(aliases.items()):
        target = tools.get(alias["new_name"])
        if target is None:
            add(
                "error",
                "ALIAS_TARGET_MISSING",
                name,
                "Alias target is not a registered dispatch tool.",
                target=alias["new_name"],
            )
            continue
        if alias["new_name"] in aliases:
            add(
                "error",
                "ALIAS_TARGET_IS_ALIAS",
                name,
                "One-hop alias resolution points at another alias.",
                target=alias["new_name"],
            )
        inject_action = alias.get("inject_action")
        known_actions = set(target["known_actions"])
        if inject_action and inject_action not in known_actions:
            add(
                "error",
                "ALIAS_ACTION_UNKNOWN",
                name,
                "Alias injects an action the target router does not declare.",
                target=alias["new_name"],
                inject_action=inject_action,
                known_actions=sorted(known_actions),
            )

    for name, tool in sorted(tools.items()):
        default_action = tool.get("default_action")
        known_actions = set(tool["known_actions"])
        if default_action and default_action not in known_actions:
            add(
                "error",
                "DEFAULT_ACTION_UNKNOWN",
                name,
                "Tool default action does not resolve to a declared delegate.",
                default_action=default_action,
                known_actions=sorted(known_actions),
            )

    for mode, mode_view in sorted(exposure["modes"].items()):
        if mode_view["declared_only"]:
            add(
                "error" if mode == "lite" else "warning",
                "MODE_DECLARED_UNADVERTISED",
                mode,
                "The mode declares names the production registrar would not advertise.",
                names=mode_view["declared_only"],
            )
        if mode_view["advertised_only"]:
            add(
                "error" if mode == "lite" else "warning",
                "MODE_UNDECLARED_ADVERTISED",
                mode,
                "The production registrar advertises names absent from the mode declaration.",
                names=mode_view["advertised_only"],
            )

    for view in exposure["tools"]:
        if view["kind"] != "workflow_alias":
            continue
        if view["inject_action"] and "action" in view["wire_properties"]:
            add(
                "error",
                "WIRE_ALIAS_ACTION_EXPOSED",
                view["name"],
                "Action-injecting alias still asks the caller for action.",
                inject_action=view["inject_action"],
            )
        if view["describe_only_properties"]:
            add(
                "warning",
                "DESCRIBE_SCHEMA_WIDER_THAN_WIRE",
                view["name"],
                "describe_tool advertises parameters the alias wire schema rejects.",
                properties=view["describe_only_properties"],
            )

    for name in exposure["orientation"]["full_orientation_only"]:
        add(
            "warning",
            "ORIENTATION_NAME_NOT_ON_WIRE",
            name,
            "list_tools can name a tool absent from the full MCP wire catalog.",
        )
    for name in exposure["orientation"]["full_wire_only"]:
        add(
            "info",
            "WIRE_NAME_NOT_IN_ORIENTATION",
            name,
            "The full MCP wire catalog advertises a name hidden by list_tools.",
        )

    hidden = {name for name, tool in tools.items() if tool["hidden"]}
    for mode, mode_view in sorted(exposure["modes"].items()):
        for name in sorted(hidden & set(mode_view["advertised"])):
            add(
                "error",
                "HIDDEN_TOOL_ADVERTISED",
                name,
                "A hidden dispatch tool is exposed on the MCP wire.",
                mode=mode,
            )

    severity_order = {"error": 0, "warning": 1, "info": 2}
    return sorted(
        findings,
        key=lambda item: (
            severity_order.get(item.severity, 99),
            item.code,
            item.subject,
        ),
    )


def build_audit_snapshot(
    tools: list[ToolEdge],
    aliases: list[AliasEdge],
    failures: list[str],
    unbound_schemas: int,
) -> dict[str, Any]:
    dispatch = build_dispatch_snapshot(tools, aliases, failures, unbound_schemas)
    exposure = build_exposure_snapshot(tools, aliases)
    findings = lint_snapshots(dispatch, exposure)
    counts = {
        severity: sum(item.severity == severity for item in findings)
        for severity in ("error", "warning", "info")
    }
    payload = {
        "dispatch": dispatch,
        "exposure": exposure,
        "summary": {
            "status": "issues_detected" if findings else "clean",
            "finding_counts": counts,
            "self_certifying": False,
            "note": (
                "This artifact is evidence for review, not authorization or "
                "certification of the components that produced it."
            ),
        },
        "findings": [
            {
                "severity": item.severity,
                "code": item.code,
                "subject": item.subject,
                "message": item.message,
                "evidence": item.evidence,
            }
            for item in findings
        ],
    }
    return _hashed_snapshot(AUDIT_SCHEMA, payload)


def _flags(tool: ToolEdge) -> str:
    marks = []
    if tool.deprecated:
        marks.append(
            f"deprecated→`{tool.superseded_by}`" if tool.superseded_by else "deprecated"
        )
    if tool.hidden:
        marks.append("hidden")
    if tool.identity != "required":
        marks.append(f"identity={tool.identity}")
    if tool.stakes != "baseline":
        marks.append(f"stakes={tool.stakes}")
    return ", ".join(marks) or "—"


def _md_code_list(values: list[str], *, limit: int = 8) -> str:
    if not values:
        return "—"
    shown = values[:limit]
    rendered = ", ".join(f"`{value}`" for value in shown)
    if len(values) > limit:
        rendered += f" … +{len(values) - limit}"
    return rendered


def _md_evidence(evidence: dict[str, Any], *, limit: int = 240) -> str:
    if not evidence:
        return "—"
    rendered = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    if len(rendered) > limit:
        rendered = rendered[: limit - 1] + "…"
    return rendered.replace("|", "\\|")


def render(
    tools: list[ToolEdge],
    aliases: list[AliasEdge],
    failures: list[str],
    unbound_schemas: int,
) -> str:
    routers = [t for t in tools if t.actions]
    action_count = sum(len(t.actions) for t in routers)
    audit = build_audit_snapshot(tools, aliases, failures, unbound_schemas)
    dispatch = audit["dispatch"]
    exposure = audit["exposure"]
    finding_counts = audit["summary"]["finding_counts"]

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
        '`knowledge(action="store")` goes; this file can.',
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
        "## Content-addressed snapshots",
        "",
        "The generated JSON is a dual view: **dispatch** records what the live",
        "registries route, while **exposure** records what the production FastMCP",
        "registrars advertise in each deployable mode and what `describe_tool` says",
        "about those names. The snapshots are immutable evidence inputs; they do not",
        "certify the components that produced them.",
        "",
        f"- Audit bundle: `{audit['content_hash']}` (`{audit['schema']}`).",
        f"- Dispatch snapshot: `{dispatch['content_hash']}`.",
        f"- Audited source revision: `{dispatch['source_revision']}` "
        f"({len(dispatch['source_files'])} files).",
        f"- Exposure snapshot: `{exposure['content_hash']}`.",
        "- JSON contract: "
        "[`tool_surface_audit_v1.schema.json`](tool_surface_audit_v1.schema.json).",
        "- Reproduce with `python3 scripts/dev/tool_edge_index.py --json`; run",
        "  `--lint` to return non-zero when error-severity findings exist.",
        "",
        "## Exposure snapshot",
        "",
        "`declared` is the mode policy set. `advertised` is the production registrar",
        "composition: mode-filtered canonical tools plus the workflow aliases that",
        "are registered unconditionally. Differences are evidence, not automatic",
        "removal authority.",
        "",
        "| Mode | Declared | Advertised | Declared only | Advertised only |",
        "|---|---:|---:|---|---|",
    ]
    for mode in DEPLOYABLE_MODES:
        view = exposure["modes"][mode]
        lines.append(
            f"| `{mode}` | {len(view['declared'])} | {len(view['advertised'])} | "
            f"{_md_code_list(view['declared_only'])} | "
            f"{_md_code_list(view['advertised_only'])} |"
        )

    workflow_views = [
        view for view in exposure["tools"] if view["kind"] == "workflow_alias"
    ]
    lines += [
        "",
        "### Workflow alias views",
        "",
        "The wire schema is what FastMCP accepts. `describe-only` lists parameters",
        "that introspection advertises for the canonical implementation even though",
        "the alias wire rejects them.",
        "",
        "| Public name | Canonical | Wire params | Describe-only | Wire schema hash |",
        "|---|---|---:|---|---|",
    ]
    for view in workflow_views:
        lines.append(
            f"| `{view['name']}` | `{view['canonical_name']}` | "
            f"{len(view['wire_properties'])} | "
            f"{_md_code_list(view['describe_only_properties'])} | "
            f"`{view['input_schema_hash']}` |"
        )

    lines += [
        "",
        "## Deterministic findings",
        "",
        f"**{finding_counts['error']} errors · {finding_counts['warning']} warnings · "
        f"{finding_counts['info']} informational.** Findings make drift reviewable;",
        "they are not self-issued approval or remediation instructions.",
        "",
        "| Severity | Code | Subject | Finding | Evidence |",
        "|---|---|---|---|---|",
    ]
    if audit["findings"]:
        for finding in audit["findings"]:
            lines.append(
                f"| {finding['severity']} | `{finding['code']}` | "
                f"`{finding['subject']}` | {finding['message']} | "
                f"{_md_evidence(finding['evidence'])} |"
            )
    else:
        lines.append("| — | — | — | No structural findings. | — |")

    lines += [
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
    ap.add_argument(
        "--json", action="store_true", help="dump the graph as JSON to stdout"
    )
    ap.add_argument(
        "--lint",
        action="store_true",
        help="print deterministic findings and exit 1 when any are errors",
    )
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

    if args.json or args.lint:
        audit = build_audit_snapshot(tools, aliases, failures, unbound)
        if args.json:
            json.dump(audit, sys.stdout, indent=2, ensure_ascii=False)
            print()
        if args.lint:
            counts = audit["summary"]["finding_counts"]
            print(
                f"tool surface audit {audit['content_hash']}: "
                f"{counts['error']} errors, {counts['warning']} warnings, "
                f"{counts['info']} informational",
                file=sys.stderr if args.json else sys.stdout,
            )
            for finding in audit["findings"]:
                print(
                    f"{finding['severity'].upper()} {finding['code']} "
                    f"[{finding['subject']}]: {finding['message']}",
                    file=sys.stderr if args.json else sys.stdout,
                )
            if counts["error"]:
                return 1
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
