#!/usr/bin/env python3
"""Wave 3a §6 Q1 — transitive-closure mutation audit for the §1.1 handlers.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §6 Q1:

    "The audit ships as a mechanical script [...] and re-runs as a gate on
    each handler PR (#5, #6, #7, #8). [...] a script that lists every
    reached callable and refuses to clear if any of them touch global
    state on the first-call path."

FIND-R2 is the named example: a ``list_all_aliases()``-style helper that
builds a module-level cache on first call would make the probe-endpoint
Python path see a different snapshot than cold-start Python.

What it does
------------

Starting from each audited tool's registered handler function (resolved
live from ``TOOL_HANDLERS``, unwrapped past decorators), the script walks
the PROJECT-LOCAL static call graph via ``ast`` and, for every reached
function, flags:

* ``global-statement`` — a ``global X`` declaration inside the function
  (the lazy-init signature: ``global _cache; if _cache is None: ...``).
* ``module-state-write`` — assignment/augmented-assignment to an attribute
  or subscript whose base resolves to a module-level binding or an
  imported module (``_CACHE[k] = v``, ``mcp_server.attr = ...``).
* ``module-state-mutating-call`` — a mutating method call
  (``append/update/setdefault/...``) whose receiver resolves to a
  module-level binding.
* ``setattr-on-module-state`` — ``setattr(X, ...)`` where ``X`` resolves
  to a module-level binding or imported module.

Exit codes: 0 = clear; 1 = flags raised; 2 = resolution failure.

Known limits (best-effort static analysis, stated per the RFC's
"mechanical" bar rather than hidden):

* Dynamic dispatch (``getattr(mod, name)()``, callables passed as values)
  is not followed; such calls appear in the "unresolved calls" section of
  the report for human review rather than silently vanishing.
* Methods on class instances are not recursed into (the §1.1 handlers are
  module-function surfaces; instance state is out of scope by the RFC's
  cut: "single-call-per-request, no DB, no Redis, no agent state").
* Module-IMPORT-time population (e.g. ``tool_stability``'s alias dict,
  built at import) is intentionally NOT flagged — the Q1 risk is
  first-CALL divergence between probe-path and cold-start Python, and
  import-time state is identical on both.

Usage::

    python3 scripts/dev/wave3a_transitive_audit.py                  # all four §1.1 tools
    python3 scripts/dev/wave3a_transitive_audit.py health_check get_server_info
    python3 scripts/dev/wave3a_transitive_audit.py --function src/foo.py::bar
    python3 scripts/dev/wave3a_transitive_audit.py --list           # show reached callables
"""

from __future__ import annotations

import argparse
import ast
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Directories whose modules count as project-local (recursed into).
PROJECT_PREFIXES = (
    REPO_ROOT / "src",
    REPO_ROOT / "governance_core",
)

DEFAULT_TOOLS = ["health_check", "get_server_info", "list_tools", "describe_tool"]

MUTATING_METHODS = frozenset(
    {
        "append",
        "add",
        "update",
        "setdefault",
        "pop",
        "popitem",
        "clear",
        "extend",
        "insert",
        "remove",
        "discard",
        "sort",
        "reverse",
    }
)


@dataclass
class Finding:
    kind: str
    function: str  # "module.py::qualname"
    lineno: int
    detail: str


@dataclass
class ModuleInfo:
    path: Path
    tree: ast.Module
    # name -> ("function", FunctionDef) for module-level defs
    functions: Dict[str, ast.AST] = field(default_factory=dict)
    # names bound at module top level (assignments, defs, classes)
    module_level_names: Set[str] = field(default_factory=set)
    # import alias -> dotted module path (``import x.y as z`` → z: x.y)
    imported_modules: Dict[str, str] = field(default_factory=dict)
    # imported name -> (dotted module path, original name)
    imported_names: Dict[str, Tuple[str, str]] = field(default_factory=dict)


class _ImportCollector(ast.NodeVisitor):
    """Collect imports anywhere in the module (incl. function-local lazy
    imports, which the wave3a modules use deliberately)."""

    def __init__(self, info: ModuleInfo) -> None:
        self.info = info

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.info.imported_modules[bound] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0:
            for alias in node.names:
                bound = alias.asname or alias.name
                self.info.imported_names[bound] = (node.module, alias.name)
        self.generic_visit(node)


def load_module_info(path: Path, cache: Dict[Path, ModuleInfo]) -> ModuleInfo:
    if path in cache:
        return cache[path]
    tree = ast.parse(path.read_text(), filename=str(path))
    info = ModuleInfo(path=path, tree=tree)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.functions[node.name] = node
            info.module_level_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            info.module_level_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name_node in ast.walk(target):
                    if isinstance(name_node, ast.Name):
                        info.module_level_names.add(name_node.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            info.module_level_names.add(node.target.id)
    _ImportCollector(info).visit(tree)
    cache[path] = info
    return info


def module_path_for(dotted: str) -> Optional[Path]:
    """Resolve a dotted module path to a project-local file, or None."""
    rel = Path(*dotted.split("."))
    for candidate in (
        REPO_ROOT / rel.with_suffix(".py"),
        REPO_ROOT / rel / "__init__.py",
    ):
        if candidate.exists() and any(
            str(candidate).startswith(str(prefix)) for prefix in PROJECT_PREFIXES
        ):
            return candidate
    return None


def _rel(path: Path) -> str:
    """Repo-relative path for display; absolute when outside the repo
    (synthetic test modules under tmp dirs)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _base_name(node: ast.AST) -> Optional[str]:
    """Innermost Name at the base of an attribute/subscript chain."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


@dataclass
class AuditResult:
    reached: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    unresolved_calls: List[str] = field(default_factory=list)


class _FunctionAuditor(ast.NodeVisitor):
    def __init__(
        self,
        info: ModuleInfo,
        func: ast.AST,
        func_label: str,
        result: AuditResult,
    ) -> None:
        self.info = info
        self.func = func
        self.func_label = func_label
        self.result = result
        # Names assigned locally (params + simple assignments) — writes to
        # these are NOT module state.
        self.local_names: Set[str] = set()
        args = func.args
        for a in (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
            + ([args.vararg] if args.vararg else [])
            + ([args.kwarg] if args.kwarg else [])
        ):
            self.local_names.add(a.arg)
        self.global_declared: Set[str] = set()
        self.calls: List[ast.Call] = []

    # -- local-binding tracking -------------------------------------------
    def _collect_local_targets(self, target: ast.AST) -> None:
        for name_node in ast.walk(target):
            if isinstance(name_node, ast.Name) and isinstance(
                name_node.ctx, ast.Store
            ):
                self.local_names.add(name_node.id)

    def _is_module_state(self, base: Optional[str]) -> bool:
        if base is None or base in self.local_names:
            return False
        if base in self.global_declared:
            return True
        return (
            base in self.info.module_level_names
            or base in self.info.imported_modules
            or base in self.info.imported_names
        )

    # -- flag visitors ------------------------------------------------------
    def visit_Global(self, node: ast.Global) -> None:
        self.global_declared.update(node.names)
        self.result.findings.append(
            Finding(
                kind="global-statement",
                function=self.func_label,
                lineno=node.lineno,
                detail=f"global {', '.join(node.names)}",
            )
        )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                base = _base_name(target)
                if self._is_module_state(base):
                    self.result.findings.append(
                        Finding(
                            kind="module-state-write",
                            function=self.func_label,
                            lineno=node.lineno,
                            detail=ast.unparse(target),
                        )
                    )
            else:
                self._collect_local_targets(target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, (ast.Attribute, ast.Subscript)):
            base = _base_name(node.target)
            if self._is_module_state(base):
                self.result.findings.append(
                    Finding(
                        kind="module-state-write",
                        function=self.func_label,
                        lineno=node.lineno,
                        detail=ast.unparse(node.target),
                    )
                )
        elif isinstance(node.target, ast.Name):
            if node.target.id in self.global_declared:
                self.result.findings.append(
                    Finding(
                        kind="module-state-write",
                        function=self.func_label,
                        lineno=node.lineno,
                        detail=ast.unparse(node.target),
                    )
                )
            else:
                self.local_names.add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        # setattr(X, ...) on module state
        if isinstance(node.func, ast.Name) and node.func.id == "setattr" and node.args:
            base = _base_name(node.args[0])
            if self._is_module_state(base):
                self.result.findings.append(
                    Finding(
                        kind="setattr-on-module-state",
                        function=self.func_label,
                        lineno=node.lineno,
                        detail=ast.unparse(node),
                    )
                )
        # mutating method on module state: X.append(...), _CACHE.update(...)
        if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
            base = _base_name(node.func.value)
            if self._is_module_state(base):
                self.result.findings.append(
                    Finding(
                        kind="module-state-mutating-call",
                        function=self.func_label,
                        lineno=node.lineno,
                        detail=ast.unparse(node.func),
                    )
                )
        self.generic_visit(node)


def audit_function(
    info: ModuleInfo,
    func_name: str,
    result: AuditResult,
    module_cache: Dict[Path, ModuleInfo],
    visited: Set[Tuple[Path, str]],
    depth: int = 0,
) -> None:
    key = (info.path, func_name)
    if key in visited or depth > 25:
        return
    visited.add(key)

    func = info.functions.get(func_name)
    if func is None:
        result.unresolved_calls.append(
            f"{_rel(info.path)}::{func_name} (no module-level def)"
        )
        return

    label = f"{_rel(info.path)}::{func_name}"
    result.reached.append(label)

    auditor = _FunctionAuditor(info, func, label, result)
    auditor.visit(func)

    for call in auditor.calls:
        callee = call.func
        # Bare-name call: same-module def, or `from project.mod import fn`
        if isinstance(callee, ast.Name):
            name = callee.id
            if name in info.functions:
                audit_function(info, name, result, module_cache, visited, depth + 1)
            elif name in info.imported_names:
                dotted, original = info.imported_names[name]
                target_path = module_path_for(dotted)
                if target_path is not None:
                    target_info = load_module_info(target_path, module_cache)
                    audit_function(
                        target_info, original, result, module_cache, visited, depth + 1
                    )
            # builtins / non-project imports: out of scope
        # Attribute call: `mod.fn(...)` where mod is an imported project module
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            mod_alias = callee.value.id
            if mod_alias in info.imported_modules:
                dotted = info.imported_modules[mod_alias]
                target_path = module_path_for(dotted)
                if target_path is not None:
                    target_info = load_module_info(target_path, module_cache)
                    if callee.attr in target_info.functions:
                        audit_function(
                            target_info,
                            callee.attr,
                            result,
                            module_cache,
                            visited,
                            depth + 1,
                        )
                    else:
                        result.unresolved_calls.append(
                            f"{label}:{call.lineno} → {ast.unparse(callee)} "
                            "(attr not a module-level def)"
                        )
            elif mod_alias in info.imported_names:
                # `from x import mod` then `mod.fn()` — resolve one level.
                dotted, original = info.imported_names[mod_alias]
                target_path = module_path_for(f"{dotted}.{original}")
                if target_path is not None:
                    target_info = load_module_info(target_path, module_cache)
                    if callee.attr in target_info.functions:
                        audit_function(
                            target_info,
                            callee.attr,
                            result,
                            module_cache,
                            visited,
                            depth + 1,
                        )


def resolve_handler(tool_name: str) -> Tuple[Path, str]:
    """Resolve a registered tool to (source file, function name), live."""
    from src.mcp_handlers import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise SystemExit(f"error: {tool_name!r} not in TOOL_HANDLERS")
    unwrapped = inspect.unwrap(handler)
    path = Path(inspect.getsourcefile(unwrapped)).resolve()
    return path, unwrapped.__name__


def run_audit(entry_points: List[Tuple[Path, str]], show_reached: bool) -> int:
    module_cache: Dict[Path, ModuleInfo] = {}
    exit_code = 0
    for path, func_name in entry_points:
        result = AuditResult()
        visited: Set[Tuple[Path, str]] = set()
        info = load_module_info(path, module_cache)
        audit_function(info, func_name, result, module_cache, visited)

        entry_label = f"{_rel(path)}::{func_name}"
        print(f"\n=== audit: {entry_label} ===")
        print(f"reached callables: {len(result.reached)}")
        if show_reached:
            for label in result.reached:
                print(f"  · {label}")
        if result.unresolved_calls:
            print(f"unresolved calls (human review): {len(result.unresolved_calls)}")
            for item in result.unresolved_calls:
                print(f"  ? {item}")
        if result.findings:
            exit_code = 1
            print(f"FLAGS: {len(result.findings)} — does NOT clear")
            for f in result.findings:
                print(f"  ✗ [{f.kind}] {f.function}:{f.lineno} — {f.detail}")
        else:
            print("CLEAR: no first-call global-state mutation found")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tools", nargs="*", default=None)
    parser.add_argument(
        "--function",
        action="append",
        default=[],
        metavar="FILE.py::fn",
        help="audit an arbitrary entry point instead of a registered tool",
    )
    parser.add_argument(
        "--list", action="store_true", help="print every reached callable"
    )
    args = parser.parse_args()

    entry_points: List[Tuple[Path, str]] = []
    for spec in args.function:
        file_part, _, fn_part = spec.partition("::")
        if not fn_part:
            parser.error(f"--function expects FILE.py::fn, got {spec!r}")
        entry_points.append(((REPO_ROOT / file_part).resolve(), fn_part))

    if not entry_points or args.tools:
        tools = args.tools or DEFAULT_TOOLS
        try:
            entry_points.extend(resolve_handler(t) for t in tools)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — diagnostic surface
            print(f"error: handler resolution failed: {exc!r}", file=sys.stderr)
            return 2

    return run_audit(entry_points, show_reached=args.list)


if __name__ == "__main__":
    sys.exit(main())
