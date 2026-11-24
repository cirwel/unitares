#!/usr/bin/env python3
"""Deterministic AST guard for server runtime invariants (P001, P003, P004).

Static source-only checks over ``src/`` with zero runtime imports and zero LLM
inference. Enforces three high-signal runtime invariants:

- **P001 (untracked background task)**: ``create_task(...)`` or
  ``ensure_future(...)`` whose returned ``Task`` is discarded at the call
  statement (bare expression statement or ``_ = ...``). CPython holds only a
  weak reference to tasks scheduled on an event loop, so an unreferenced task
  can be garbage-collected mid-await and has no cancellation handle.
  Safe forms: ``create_tracked_task(...)``, ``_supervised_create_task(...)``,
  ``tg.create_task(...)`` inside ``async with asyncio.TaskGroup() as tg:``, or
  storing/passing/returning the ``Task`` reference.
  Scope note: checks structural discard at the call statement; does not perform
  whole-function dataflow on named locals after assignment.

- **P003 (uncached monitor instantiation)**: ``UNITARESMonitor(...)`` outside
  ``def get_or_create_monitor`` (and outside ``if __name__ == "__main__":``)
  without ``load_state=False``. Bypassing ``mcp_server.get_or_create_monitor``
  hydrates disk state into a throwaway instance that never enters the server
  cache. Explicit ``load_state=False`` simulation/comparison instances are
  exempt.

- **P004 (unguarded raw Redis await in MCP handlers)**: ``await`` / ``async
  for`` / ``async with`` on a raw async Redis client (from
  ``src.cache.redis_client.get_redis``) inside ``src/mcp_handlers/`` that is
  neither wrapped in ``asyncio.wait_for(...)`` nor confined to a non-entrypoint
  helper whose statically resolved intra-package call/reference sites are all
  bounded by ``asyncio.wait_for(...)`` or backgrounded via
  ``create_tracked_task`` / ``_supervised_create_task``. (``asyncpg`` uses
  ``ExecutorPool`` and ``SessionCache`` bounds its own calls; raw Redis clients
  do not.)

Inline escape hatch (requires a non-empty reason):
    ``# invariant-allow: P001 reason explaining why this site is safe``
    ``# invariant-allow: P001, P004 reason explaining why this site is safe``
Any reasonless ``# invariant-allow: ...`` comment anywhere in a scanned file is
reported as ``ALLOW_NO_REASON``. Unparseable Python files fail with
``PARSE_ERROR`` rather than being skipped silently.

Ratchet mode (default, matching ``check_component_boundaries.py``):
- Counts violations per ``(rule, file)`` against
  ``scripts/dev/runtime_ast_invariants_baseline.txt``.
- Any new ``(rule, file)`` pair or count increase fails.
- Any count decrease requires ``--lower`` in the same change so fixed sites
  cannot silently regress.
- ``--base REF`` verifies ``REF`` resolves to a commit and checks that the
  baseline file did not add or raise entries relative to ``REF``.
- ``--files PATH ...`` checks specific files directly with zero baseline
  allowance (for targeted/edit-time checks).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REL = Path("scripts") / "dev" / "runtime_ast_invariants_baseline.txt"
SRC_REL = Path("src")
MCP_HANDLERS_PREFIX = "src/mcp_handlers/"

RULES = ("P001", "P003", "P004", "ALLOW_NO_REASON", "PARSE_ERROR")

_ALLOW_RE = re.compile(
    r"#\s*invariant-allow:\s*([A-Za-z0-9_-]+(?:\s*,\s*[A-Za-z0-9_-]+)*)(.*)$"
)

_TRACKED_TASK_HELPERS = frozenset({
    "create_tracked_task",
    "_supervised_create_task",
})
_TASK_SPAWN_NAMES = frozenset({"create_task", "ensure_future"})


@dataclass(frozen=True)
class Violation:
    rule: str
    rel_path: str
    line: int
    message: str


def _parse_allow_line(line_text: str) -> tuple[set[str], bool] | None:
    """Parse ``# invariant-allow: ...`` on a single line.

    Returns ``None`` if absent, else ``(allowed_rules, has_non_empty_reason)``.
    """
    m = _ALLOW_RE.search(line_text)
    if not m:
        return None
    raw_rules, tail = m.group(1), m.group(2).strip()
    cleaned_reason = re.sub(r"^(?:[-:—]+|\breason\s*=\s*)", "", tail).strip()
    if not cleaned_reason:
        return set(), False
    allowed = {tok.strip().upper() for tok in raw_rules.split(",") if tok.strip()}
    return allowed, True


def _collect_file_pragmas(
    rel_path: str, source_lines: list[str]
) -> tuple[dict[int, set[str]], list[Violation]]:
    """Scan every line in ``source_lines`` once for ``# invariant-allow:``."""
    allowed_by_line: dict[int, set[str]] = {}
    pragma_violations: list[Violation] = []
    for lineno, line_text in enumerate(source_lines, start=1):
        parsed = _parse_allow_line(line_text)
        if parsed is None:
            continue
        rules, has_reason = parsed
        if not has_reason:
            pragma_violations.append(
                Violation(
                    "ALLOW_NO_REASON",
                    rel_path,
                    lineno,
                    "invariant-allow pragma must include a non-empty reason",
                )
            )
        else:
            allowed_by_line[lineno] = rules
    return allowed_by_line, pragma_violations


def _is_rule_allowed(allowed_by_line: dict[int, set[str]], node: ast.AST, rule: str) -> bool:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    for ln in range(start, max(start, end) + 1):
        if rule in allowed_by_line.get(ln, ()):
            return True
    return False


def _call_func_name(call: ast.Call) -> tuple[str | None, str | None]:
    """Return ``(receiver_name_or_none, func_name)`` for simple/attribute calls."""
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        recv = func.value.id if isinstance(func.value, ast.Name) else "<expr>"
        return recv, func.attr
    return None, None


def _is_wait_for_call(node: ast.AST, wait_for_aliases: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    recv, name = _call_func_name(node)
    if recv is None and name in wait_for_aliases:
        return True
    if name == "wait_for":
        return True
    return False


def _is_tracked_task_call(node: ast.AST, tracked_aliases: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    recv, name = _call_func_name(node)
    if recv is None and name in tracked_aliases:
        return True
    if name in _TRACKED_TASK_HELPERS:
        return True
    return False


def _is_taskgroup_context(item: ast.withitem, taskgroup_aliases: set[str]) -> str | None:
    """If ``item`` is ``asyncio.TaskGroup() as tg``, return ``'tg'``."""
    if not isinstance(item.optional_vars, ast.Name):
        return None
    ctx = item.context_expr
    if not isinstance(ctx, ast.Call):
        return None
    recv, name = _call_func_name(ctx)
    if (recv is None and name in taskgroup_aliases) or name == "TaskGroup":
        return item.optional_vars.id
    return None


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    )


def _has_load_state_false(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "load_state" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _resolve_import_rel_path(from_rel_path: str, module: str | None, level: int) -> str | None:
    """Resolve an ``ImportFrom`` target within ``src/mcp_handlers/`` to ``rel_path``."""
    if level > 0:
        parts = from_rel_path.split("/")[:-1]
        up = level - 1
        if up > len(parts):
            return None
        base_parts = parts[: len(parts) - up]
        if module:
            base_parts.extend(module.split("."))
    else:
        if not module:
            return None
        base_parts = module.split(".")
    candidate = "/".join(base_parts) + ".py"
    return candidate if candidate.startswith(MCP_HANDLERS_PREFIX) else None


class _ModuleImports:
    """Import alias table for one module."""

    def __init__(self, tree: ast.Module, rel_path: str) -> None:
        self.task_spawn_aliases: set[str] = set(_TASK_SPAWN_NAMES)
        self.tracked_task_aliases: set[str] = set(_TRACKED_TASK_HELPERS)
        self.taskgroup_aliases: set[str] = {"TaskGroup"}
        self.wait_for_aliases: set[str] = {"wait_for"}
        self.monitor_names: set[str] = {"UNITARESMonitor"}
        self.monitor_modules: set[str] = set()
        self.raw_redis_getters: set[str] = set()
        self.raw_redis_modules: set[str] = set()
        # local_name -> (target_rel_path, exported_func_name) for handler imports
        self.handler_func_imports: dict[str, tuple[str, str]] = {}
        # local_alias -> target_rel_path for handler module imports (`from . import persistence`)
        self.handler_mod_imports: dict[str, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                resolved_mod_rel = _resolve_import_rel_path(rel_path, node.module, node.level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name in _TASK_SPAWN_NAMES:
                        self.task_spawn_aliases.add(local)
                    if alias.name in _TRACKED_TASK_HELPERS:
                        self.tracked_task_aliases.add(local)
                    if alias.name == "TaskGroup":
                        self.taskgroup_aliases.add(local)
                    if alias.name == "wait_for":
                        self.wait_for_aliases.add(local)
                    if alias.name == "UNITARESMonitor":
                        self.monitor_names.add(local)
                    if mod.endswith("redis_client") and alias.name == "get_redis":
                        self.raw_redis_getters.add(local)
                    if resolved_mod_rel is not None:
                        self.handler_func_imports[local] = (resolved_mod_rel, alias.name)
                    elif node.level > 0 and not node.module:
                        sub_rel = _resolve_import_rel_path(rel_path, alias.name, node.level)
                        if sub_rel is not None:
                            self.handler_mod_imports[local] = sub_rel
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[-1]
                    if alias.name.endswith("governance_monitor"):
                        self.monitor_modules.add(local)
                    if alias.name.endswith("redis_client"):
                        self.raw_redis_modules.add(local)
                    if alias.name.startswith("src.mcp_handlers."):
                        cand = alias.name.replace(".", "/") + ".py"
                        self.handler_mod_imports[local] = cand


def _check_p001_and_p003(
    tree: ast.Module,
    rel_path: str,
    allowed_by_line: dict[int, set[str]],
    imports: _ModuleImports,
) -> list[Violation]:
    violations: list[Violation] = []

    def record_if_not_allowed(rule: str, node: ast.AST, msg: str) -> None:
        if _is_rule_allowed(allowed_by_line, node, rule):
            return
        violations.append(Violation(rule, rel_path, getattr(node, "lineno", 1), msg))

    def is_untracked_spawn_call(call: ast.Call, active_taskgroups: set[str]) -> str | None:
        if _is_tracked_task_call(call, imports.tracked_task_aliases):
            return None
        recv, name = _call_func_name(call)
        if recv is None:
            if name in imports.task_spawn_aliases:
                return name
            return None
        if name in _TASK_SPAWN_NAMES:
            if recv in active_taskgroups and name == "create_task":
                return None
            return f"{recv}.{name}" if recv != "<expr>" else name
        return None

    def is_monitor_ctor(call: ast.Call) -> bool:
        recv, name = _call_func_name(call)
        if recv is None and name in imports.monitor_names:
            return True
        if name == "UNITARESMonitor":
            return True
        return False

    def visit(
        node: ast.AST,
        *,
        in_monitor_factory: bool = False,
        in_main_guard: bool = False,
        active_taskgroups: frozenset[str] = frozenset(),
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "get_or_create_monitor":
                in_monitor_factory = True
        elif isinstance(node, ast.If) and _is_main_guard(node):
            in_main_guard = True
        elif isinstance(node, ast.AsyncWith):
            extra_tgs = set(active_taskgroups)
            for item in node.items:
                tg = _is_taskgroup_context(item, imports.taskgroup_aliases)
                if tg:
                    extra_tgs.add(tg)
            active_taskgroups = frozenset(extra_tgs)

        discarded_call: ast.Call | None = None
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            discarded_call = node.value
        elif isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_"
                and isinstance(node.value, ast.Call)
            ):
                discarded_call = node.value
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "_"
                and isinstance(node.value, ast.Call)
            ):
                discarded_call = node.value

        if discarded_call is not None:
            spawn_desc = is_untracked_spawn_call(discarded_call, set(active_taskgroups))
            if spawn_desc is not None:
                record_if_not_allowed(
                    "P001",
                    node,
                    f"discarded {spawn_desc}(...) return value; use create_tracked_task or store a strong Task reference",
                )

        if isinstance(node, ast.Call) and is_monitor_ctor(node):
            if not in_monitor_factory and not in_main_guard and not _has_load_state_false(node):
                record_if_not_allowed(
                    "P003",
                    node,
                    "direct UNITARESMonitor(...) outside get_or_create_monitor without load_state=False",
                )

        for child in ast.iter_child_nodes(node):
            visit(
                child,
                in_monitor_factory=in_monitor_factory,
                in_main_guard=in_main_guard,
                active_taskgroups=active_taskgroups,
            )

    visit(tree)
    return violations


def _is_raw_redis_getter_call(call: ast.Call, raw_getters: set[str], raw_modules: set[str]) -> bool:
    recv, name = _call_func_name(call)
    if recv is None and name in raw_getters:
        return True
    if recv in raw_modules and name == "get_redis":
        return True
    return False


def _collect_p004_module_facts(
    tree: ast.Module,
    imports: _ModuleImports,
) -> tuple[dict[str, ast.AsyncFunctionDef], set[str]]:
    """Return ``(async_funcs_by_name, raw_getter_names)`` for a handler module."""
    async_funcs: dict[str, ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            async_funcs[node.name] = node

    raw_getters = set(imports.raw_redis_getters)
    changed = True
    while changed:
        changed = False
        for fname, fn in async_funcs.items():
            if fname in raw_getters:
                continue
            local_getters = set(raw_getters)
            for sub in ast.walk(fn):
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").endswith("redis_client"):
                    for alias in sub.names:
                        if alias.name == "get_redis":
                            local_getters.add(alias.asname or alias.name)
            returns_raw = False
            for sub in ast.walk(fn):
                if (
                    isinstance(sub, ast.Return)
                    and isinstance(sub.value, ast.Await)
                    and isinstance(sub.value.value, ast.Call)
                    and _is_raw_redis_getter_call(sub.value.value, local_getters, imports.raw_redis_modules)
                ):
                    returns_raw = True
            if returns_raw:
                raw_getters.add(fname)
                changed = True

    return async_funcs, raw_getters


HandlerKey = tuple[str, str]  # (rel_path, func_name)


def _resolve_callee_targets(
    rel_path: str,
    recv: str | None,
    name: str | None,
    imports: _ModuleImports,
    defined_by_module: dict[str, set[str]],
    all_by_bare_name: dict[str, set[HandlerKey]],
) -> tuple[set[HandlerKey], bool]:
    """Resolve a call/reference to ``(target_keys, exact_resolution)``."""
    if not name:
        return set(), True
    if recv is None:
        if name in defined_by_module.get(rel_path, ()):
            return {(rel_path, name)}, True
        if name in imports.handler_func_imports:
            target = imports.handler_func_imports[name]
            if target[1] in defined_by_module.get(target[0], ()):
                return {target}, True
        return set(), True
    if recv in imports.handler_mod_imports:
        t_rel = imports.handler_mod_imports[recv]
        if name in defined_by_module.get(t_rel, ()):
            return {(t_rel, name)}, True
        return set(), True
    # Dynamic/unknown attribute receiver (e.g. `self._h()` or `obj._h()`):
    # conservatively mark every matching helper name across handler modules as
    # potentially called unguarded so an unguarded attribute call cannot slip past.
    return set(all_by_bare_name.get(name, ())), False


def _scan_block_references(
    stmts: list[ast.stmt],
    *,
    rel_path: str,
    imports: _ModuleImports,
    defined_by_module: dict[str, set[str]],
    all_by_bare_name: dict[str, set[HandlerKey]],
    initial_bounded: bool = False,
) -> tuple[set[HandlerKey], set[HandlerKey], dict[str, bool]]:
    """Single top-down pass over ``stmts`` (never double-visiting nested defs).

    Returns ``(guarded_targets, unguarded_targets, nested_async_is_bounded)``:
    - Calls inside the first arg of ``asyncio.wait_for(...)`` or
      ``create_tracked_task(...)`` count as guarded (when resolved unambiguously).
    - Attribute calls on unknown receivers and bare callback references
      (e.g. ``schedule(_helper)``) count as unguarded references so a helper
      passed around as a value or invoked via ``obj._helper()`` cannot inherit
      a false caller-bounded exemption.
    - Nested ``async def`` closures (e.g. ``async def _run(): return await _leaf()``
      passed to ``await asyncio.wait_for(_run(), timeout=...)``) inherit
      boundedness when every call/reference to the closure in its parent scope
      is bounded.
    """
    guarded: set[HandlerKey] = set()
    unguarded: set[HandlerKey] = set()
    local_nested_async: dict[str, ast.AsyncFunctionDef] = {}
    local_nested_sync: dict[str, ast.FunctionDef] = {}
    nested_guarded_names: set[str] = set()
    nested_unguarded_names: set[str] = set()

    for stmt in stmts:
        if isinstance(stmt, ast.AsyncFunctionDef):
            local_nested_async[stmt.name] = stmt
        elif isinstance(stmt, ast.FunctionDef):
            local_nested_sync[stmt.name] = stmt

    nested_names = set(local_nested_async) | set(local_nested_sync)

    def walk_expr(node: ast.AST, *, bounded: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return

        if isinstance(node, ast.Call):
            if _is_wait_for_call(node, imports.wait_for_aliases) or _is_tracked_task_call(
                node, imports.tracked_task_aliases
            ):
                if node.args:
                    walk_expr(node.args[0], bounded=True)
                for arg in node.args[1:]:
                    walk_expr(arg, bounded=bounded)
                for kw in node.keywords:
                    walk_expr(kw.value, bounded=bounded)
                return

            recv, cname = _call_func_name(node)
            if recv is None and cname in nested_names:
                if bounded:
                    nested_guarded_names.add(cname)
                else:
                    nested_unguarded_names.add(cname)
            else:
                targets, exact = _resolve_callee_targets(
                    rel_path, recv, cname, imports, defined_by_module, all_by_bare_name
                )
                if bounded and exact:
                    guarded.update(targets)
                else:
                    unguarded.update(targets)

            # Walk arguments (skipping node.func when it is a bare Name so the
            # callee identifier itself isn't misread as a callback reference).
            if not isinstance(node.func, ast.Name):
                walk_expr(node.func, bounded=bounded)
            for arg in node.args:
                walk_expr(arg, bounded=bounded)
            for kw in node.keywords:
                walk_expr(kw.value, bounded=bounded)
            return

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in nested_names:
                nested_unguarded_names.add(node.id)
            else:
                targets, _ = _resolve_callee_targets(
                    rel_path, None, node.id, imports, defined_by_module, all_by_bare_name
                )
                unguarded.update(targets)
            return

        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            recv = node.value.id if isinstance(node.value, ast.Name) else "<expr>"
            targets, _ = _resolve_callee_targets(
                rel_path, recv, node.attr, imports, defined_by_module, all_by_bare_name
            )
            unguarded.update(targets)
            walk_expr(node.value, bounded=bounded)
            return

        for child in ast.iter_child_nodes(node):
            walk_expr(child, bounded=bounded)

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        walk_expr(stmt, bounded=initial_bounded)

    nested_bounded_map: dict[str, bool] = {}
    for nname, nfn in local_nested_async.items():
        is_b = initial_bounded or (
            nname in nested_guarded_names and nname not in nested_unguarded_names
        )
        nested_bounded_map[nname] = is_b
        cg, cu, _ = _scan_block_references(
            nfn.body,
            rel_path=rel_path,
            imports=imports,
            defined_by_module=defined_by_module,
            all_by_bare_name=all_by_bare_name,
            initial_bounded=is_b,
        )
        guarded.update(cg)
        unguarded.update(cu)

    for _sname, sfn in local_nested_sync.items():
        cg, cu, _ = _scan_block_references(
            sfn.body,
            rel_path=rel_path,
            imports=imports,
            defined_by_module=defined_by_module,
            all_by_bare_name=all_by_bare_name,
            initial_bounded=False,
        )
        guarded.update(cg)
        unguarded.update(cu)

    return guarded, unguarded, nested_bounded_map


def _check_p004_mcp_handlers(
    handler_trees: list[tuple[str, ast.Module, dict[int, set[str]], _ModuleImports]],
) -> list[Violation]:
    """Check P004 across all parsed files under ``src/mcp_handlers/``."""
    defined_by_module: dict[str, set[str]] = {}
    all_by_bare_name: dict[str, set[HandlerKey]] = {}
    for rel_path, tree, _allowed, _imports in handler_trees:
        funcs = {
            node.name
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        defined_by_module[rel_path] = funcs
        for fname in funcs:
            all_by_bare_name.setdefault(fname, set()).add((rel_path, fname))

    def is_mcp_entrypoint(fn: ast.AsyncFunctionDef) -> bool:
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call):
                _, dname = _call_func_name(dec)
                if dname in {"mcp_tool", "tool"}:
                    return True
            elif isinstance(dec, ast.Name) and dec.id in {"mcp_tool", "tool"}:
                return True
        return False

    all_guarded: set[HandlerKey] = set()
    all_unguarded: set[HandlerKey] = set()
    unguarded_callers_of: dict[HandlerKey, set[HandlerKey | str]] = {}

    for rel_path, tree, _allowed, imports in handler_trees:
        mod_stmts: list[ast.stmt] = []
        for top in tree.body:
            if isinstance(top, ast.AsyncFunctionDef):
                g, u, _ = _scan_block_references(
                    top.body,
                    rel_path=rel_path,
                    imports=imports,
                    defined_by_module=defined_by_module,
                    all_by_bare_name=all_by_bare_name,
                    initial_bounded=False,
                )
                all_guarded.update(g)
                all_unguarded.update(u)
                caller_key: HandlerKey = (rel_path, top.name)
                for target in u:
                    unguarded_callers_of.setdefault(target, set()).add(caller_key)
            elif isinstance(top, ast.FunctionDef):
                g, u, _ = _scan_block_references(
                    top.body,
                    rel_path=rel_path,
                    imports=imports,
                    defined_by_module=defined_by_module,
                    all_by_bare_name=all_by_bare_name,
                    initial_bounded=False,
                )
                all_guarded.update(g)
                all_unguarded.update(u)
                for target in u:
                    unguarded_callers_of.setdefault(target, set()).add(f"sync:{rel_path}:{top.name}")
            else:
                mod_stmts.append(top)

        if mod_stmts:
            g, u, _ = _scan_block_references(
                mod_stmts,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                initial_bounded=False,
            )
            all_guarded.update(g)
            all_unguarded.update(u)
            for target in u:
                unguarded_callers_of.setdefault(target, set()).add(f"module:{rel_path}")

    caller_bounded_helpers: set[HandlerKey] = set()
    entrypoints: set[HandlerKey] = set()
    for rel_path, tree, _allowed, _imports in handler_trees:
        for top in tree.body:
            if isinstance(top, ast.AsyncFunctionDef):
                key = (rel_path, top.name)
                if is_mcp_entrypoint(top):
                    entrypoints.add(key)
                elif key in all_guarded and key not in all_unguarded:
                    caller_bounded_helpers.add(key)

    # Propagate caller-boundedness across multi-hop helper chains in O(E)
    for _ in range(4):
        added = False
        for rel_path, funcs in defined_by_module.items():
            for fname in funcs:
                key = (rel_path, fname)
                if key in caller_bounded_helpers or key in entrypoints:
                    continue
                callers = unguarded_callers_of.get(key, set())
                if callers and all(
                    isinstance(c, tuple) and c in caller_bounded_helpers for c in callers
                ):
                    caller_bounded_helpers.add(key)
                    added = True
        if not added:
            break

    violations: list[Violation] = []

    for rel_path, tree, allowed_by_line, imports in handler_trees:
        async_funcs, module_raw_getters = _collect_p004_module_facts(tree, imports)

        raw_param_map: dict[str, set[int]] = {}
        raw_vars_by_func: dict[str, set[str]] = {}

        for fname, fn in async_funcs.items():
            local_getters = set(module_raw_getters)
            for sub in ast.walk(fn):
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").endswith("redis_client"):
                    for alias in sub.names:
                        if alias.name == "get_redis":
                            local_getters.add(alias.asname or alias.name)
            rvars: set[str] = set()
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    val = sub.value
                    if (
                        isinstance(val, ast.Await)
                        and isinstance(val.value, ast.Call)
                        and _is_raw_redis_getter_call(val.value, local_getters, imports.raw_redis_modules)
                    ):
                        rvars.add(sub.targets[0].id)
                elif isinstance(sub, ast.AsyncWith):
                    for item in sub.items:
                        if (
                            isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Attribute)
                            and isinstance(item.context_expr.func.value, ast.Name)
                            and item.context_expr.func.value.id in rvars
                            and isinstance(item.optional_vars, ast.Name)
                        ):
                            rvars.add(item.optional_vars.id)
            raw_vars_by_func[fname] = rvars

        for fname, fn in async_funcs.items():
            rvars = raw_vars_by_func.get(fname, set())
            if not rvars:
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Call):
                    recv, cname = _call_func_name(sub)
                    if recv is None and cname in async_funcs:
                        for idx, arg in enumerate(sub.args):
                            if isinstance(arg, ast.Name) and arg.id in rvars:
                                raw_param_map.setdefault(cname, set()).add(idx)

        for cname, idxs in raw_param_map.items():
            target_fn = async_funcs[cname]
            for idx in idxs:
                if idx < len(target_fn.args.args):
                    raw_vars_by_func.setdefault(cname, set()).add(target_fn.args.args[idx].arg)

        def record_p004(node: ast.AST, msg: str) -> None:
            if _is_rule_allowed(allowed_by_line, node, "P004"):
                return
            violations.append(Violation("P004", rel_path, getattr(node, "lineno", 1), msg))

        def scan_func(
            fn: ast.AsyncFunctionDef,
            *,
            is_top_level: bool,
            inherited_raw_vars: set[str],
            enclosing_bounded: bool,
        ) -> None:
            if is_top_level and (rel_path, fn.name) in caller_bounded_helpers:
                enclosing_bounded = True
            if is_top_level and fn.name in module_raw_getters and fn.name != "get_redis":
                return

            local_getters = set(module_raw_getters)
            for sub in fn.body:
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").endswith("redis_client"):
                    for alias in sub.names:
                        if alias.name == "get_redis":
                            local_getters.add(alias.asname or alias.name)

            rvars = set(inherited_raw_vars) | (
                set(raw_vars_by_func.get(fn.name, set())) if is_top_level else set()
            )
            if not is_top_level:
                for sub in fn.body:
                    if isinstance(sub, ast.ImportFrom) and (sub.module or "").endswith("redis_client"):
                        for alias in sub.names:
                            if alias.name == "get_redis":
                                local_getters.add(alias.asname or alias.name)
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                        val = sub.value
                        if (
                            isinstance(val, ast.Await)
                            and isinstance(val.value, ast.Call)
                            and _is_raw_redis_getter_call(val.value, local_getters, imports.raw_redis_modules)
                        ):
                            rvars.add(sub.targets[0].id)

            _, _, nested_bounded_map = _scan_block_references(
                fn.body,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                initial_bounded=enclosing_bounded,
            )

            def walk_node(node: ast.AST, *, in_wait_for: bool) -> None:
                if isinstance(node, ast.AsyncFunctionDef):
                    child_bounded = enclosing_bounded or nested_bounded_map.get(node.name, False)
                    scan_func(
                        node,
                        is_top_level=False,
                        inherited_raw_vars=rvars,
                        enclosing_bounded=child_bounded,
                    )
                    return
                if isinstance(node, ast.FunctionDef):
                    return

                if isinstance(node, ast.AsyncWith):
                    for item in node.items:
                        if (
                            isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Attribute)
                            and isinstance(item.context_expr.func.value, ast.Name)
                            and item.context_expr.func.value.id in rvars
                        ):
                            if isinstance(item.optional_vars, ast.Name):
                                rvars.add(item.optional_vars.id)
                            if not enclosing_bounded and not in_wait_for:
                                record_p004(
                                    node,
                                    f"unguarded async with {item.context_expr.func.value.id}.{item.context_expr.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                                )

                if isinstance(node, ast.Call) and (
                    _is_wait_for_call(node, imports.wait_for_aliases)
                    or _is_tracked_task_call(node, imports.tracked_task_aliases)
                ):
                    if node.args:
                        walk_node(node.args[0], in_wait_for=True)
                    for arg in node.args[1:]:
                        walk_node(arg, in_wait_for=in_wait_for)
                    for kw in node.keywords:
                        walk_node(kw.value, in_wait_for=in_wait_for)
                    return

                if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                    call = node.value
                    if (
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Await)
                        and isinstance(call.func.value.value, ast.Call)
                        and _is_raw_redis_getter_call(call.func.value.value, local_getters, imports.raw_redis_modules)
                    ):
                        if not enclosing_bounded and not in_wait_for:
                            record_p004(
                                node,
                                f"unguarded await (await get_redis()).{call.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                            )
                    elif (
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id in rvars
                    ):
                        if not enclosing_bounded and not in_wait_for:
                            record_p004(
                                node,
                                f"unguarded await {call.func.value.id}.{call.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                            )

                if isinstance(node, ast.AsyncFor):
                    it = node.iter
                    if (
                        isinstance(it, ast.Call)
                        and isinstance(it.func, ast.Attribute)
                        and isinstance(it.func.value, ast.Name)
                        and it.func.value.id in rvars
                        and not enclosing_bounded
                        and not in_wait_for
                    ):
                        record_p004(
                            node,
                            f"unguarded async for over {it.func.value.id}.{it.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                        )

                for child in ast.iter_child_nodes(node):
                    walk_node(child, in_wait_for=in_wait_for)

            for stmt in fn.body:
                walk_node(stmt, in_wait_for=False)

        for top in tree.body:
            if isinstance(top, ast.AsyncFunctionDef):
                scan_func(top, is_top_level=True, inherited_raw_vars=set(), enclosing_bounded=False)

    return violations


def scan_paths(root: Path, paths: Iterable[Path] | None = None) -> list[Violation]:
    """Scan ``paths`` (or all ``src/**/*.py`` under ``root``) and return sorted violations."""
    filter_rels: set[str] | None = None
    if paths is None:
        src_root = root / SRC_REL
        py_files = sorted(p for p in src_root.rglob("*.py") if "__pycache__" not in p.parts)
    else:
        explicit = sorted(p.resolve() for p in paths if p.suffix == ".py" and p.is_file())
        filter_rels = set()
        needs_handler_context = False
        for p in explicit:
            try:
                rel = p.relative_to(root.resolve()).as_posix()
            except ValueError:
                rel = p.as_posix()
            filter_rels.add(rel)
            if rel.startswith(MCP_HANDLERS_PREFIX):
                needs_handler_context = True
        if needs_handler_context and (root / "src" / "mcp_handlers").is_dir():
            peer_handlers = [
                p.resolve()
                for p in (root / "src" / "mcp_handlers").rglob("*.py")
                if "__pycache__" not in p.parts
            ]
            py_files = sorted(set(explicit) | set(peer_handlers))
        else:
            py_files = explicit

    violations: list[Violation] = []
    handler_trees: list[tuple[str, ast.Module, dict[int, set[str]], _ModuleImports]] = []

    for path in py_files:
        try:
            rel_path = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_path = path.as_posix()
        in_scope = filter_rels is None or rel_path in filter_rels
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            if in_scope:
                violations.append(Violation("PARSE_ERROR", rel_path, 1, f"cannot read file: {exc}"))
            continue
        lines = source.splitlines()
        allowed_by_line, pragma_violations = _collect_file_pragmas(rel_path, lines)
        if in_scope:
            violations.extend(pragma_violations)
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            if in_scope:
                violations.append(
                    Violation(
                        "PARSE_ERROR",
                        rel_path,
                        exc.lineno or 1,
                        f"cannot parse Python file: {exc.msg}",
                    )
                )
            continue
        imports = _ModuleImports(tree, rel_path)
        if in_scope:
            violations.extend(_check_p001_and_p003(tree, rel_path, allowed_by_line, imports))
        if rel_path.startswith(MCP_HANDLERS_PREFIX):
            handler_trees.append((rel_path, tree, allowed_by_line, imports))

    if handler_trees:
        p004 = _check_p004_mcp_handlers(handler_trees)
        if filter_rels is not None:
            p004 = [v for v in p004 if v.rel_path in filter_rels]
        violations.extend(p004)

    return sorted(violations, key=lambda v: (v.rule, v.rel_path, v.line, v.message))


# ---------------------------------------------------------------------------
# Baseline ratchet (mirrors check_component_boundaries.py)
# ---------------------------------------------------------------------------


def parse_baseline(text: str) -> dict[tuple[str, str], int]:
    entries: dict[tuple[str, str], int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] not in RULES:
            raise ValueError(f"malformed baseline line {lineno}: {raw!r}")
        key = (parts[0], parts[1])
        if key in entries:
            raise ValueError(f"duplicate baseline key on line {lineno}: {key}")
        entries[key] = int(parts[2])
    return entries


def format_baseline(counts: dict[tuple[str, str], int]) -> str:
    header = (
        "# Runtime AST invariant baseline (scripts/dev/check_runtime_ast_invariants.py).\n"
        "# Format: RULE<TAB>PATH<TAB>COUNT\n"
        "# Ratchet: entries may only shrink or be removed (--lower); CI --base forbids\n"
        "# adding or raising any entry.\n"
    )
    body = "".join(
        f"{rule}\t{path}\t{count}\n"
        for (rule, path), count in sorted(counts.items())
        if count > 0
    )
    return header + body


def _ref_exists(root: Path, ref: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def _git_show(root: Path, ref: str, rel_path: Path) -> str | None:
    res = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{rel_path.as_posix()}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout if res.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--baseline", type=Path, default=None, help="override baseline path")
    parser.add_argument("--base", default=None, help="git ref to ratchet baseline against")
    parser.add_argument("--lower", action="store_true", help="lower baseline entries to match improvements")
    parser.add_argument("--write-initial-baseline", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--files", nargs="*", type=Path, default=None, help="scan explicit files directly (no baseline)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if args.files is not None:
        violations = scan_paths(root, args.files)
        for v in violations:
            print(f"{v.rel_path}:{v.line}: [{v.rule}] {v.message}")
        return 1 if violations else 0

    baseline_path = args.baseline or (root / BASELINE_REL)
    violations = scan_paths(root)
    counts: Counter[tuple[str, str]] = Counter((v.rule, v.rel_path) for v in violations)

    if args.write_initial_baseline:
        baseline_path.write_text(format_baseline(counts), encoding="utf-8")
        print(f"wrote {len(counts)} baseline entries ({len(violations)} total findings) to {baseline_path}")
        for v in violations:
            print(f"  {v.rel_path}:{v.line}: [{v.rule}] {v.message}")
        return 0

    baseline = parse_baseline(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}

    if args.lower:
        regressed = {
            k: (baseline.get(k, 0), c)
            for k, c in counts.items()
            if c > baseline.get(k, 0)
        }
        if regressed:
            for (rule, rel), (old, new) in sorted(regressed.items()):
                print(f"FAIL: {rule} {rel}: count rose {old} -> {new} (cannot --lower over a regression)")
            return 1
        lowered = {k: min(v, counts.get(k, 0)) for k, v in baseline.items() if counts.get(k, 0) > 0}
        baseline_path.write_text(format_baseline(lowered), encoding="utf-8")
        print(f"updated baseline ({len(lowered)} entries)")
        return 0

    failures: list[str] = []

    if args.base:
        if not _ref_exists(root, args.base):
            print(f"FAIL: --base ref {args.base!r} does not resolve to a commit")
            return 1
        base_text = _git_show(root, args.base, baseline_path.relative_to(root))
        if base_text is not None:
            base_entries = parse_baseline(base_text)
            for key, cur_val in sorted(baseline.items()):
                old_val = base_entries.get(key)
                if old_val is None:
                    failures.append(
                        f"baseline added {key[0]} {key[1]}={cur_val} relative to {args.base} "
                        "(fix the code or use a reasoned `# invariant-allow:` comment instead)"
                    )
                elif cur_val > old_val:
                    failures.append(
                        f"baseline raised {key[0]} {key[1]} from {old_val} to {cur_val} relative to {args.base}"
                    )

    all_keys = sorted(set(baseline) | set(counts))
    by_key: dict[tuple[str, str], list[Violation]] = {}
    for v in violations:
        by_key.setdefault((v.rule, v.rel_path), []).append(v)

    for key in all_keys:
        rule, rel = key
        cur = counts.get(key, 0)
        allowed = baseline.get(key, 0)
        if cur > allowed:
            failures.append(f"{rule} {rel}: {cur} violation(s) (baseline {allowed})")
            for v in by_key.get(key, []):
                failures.append(f"  {v.rel_path}:{v.line}: [{v.rule}] {v.message}")
        elif cur < allowed:
            failures.append(
                f"{rule} {rel}: improved {allowed} -> {cur}; run "
                "`python3 scripts/dev/check_runtime_ast_invariants.py --lower` to lock in the reduction"
            )

    if failures:
        for line in failures:
            print(line)
        return 1

    print(
        f"runtime AST invariants: clean ({sum(counts.values())} baselined across {len(counts)} file/rule pair(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
