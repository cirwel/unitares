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
  neither wrapped in ``asyncio.wait_for(...)`` nor confined to a helper whose
  call sites are all bounded by ``asyncio.wait_for(...)`` or backgrounded via
  ``create_tracked_task`` / ``_supervised_create_task``. (``asyncpg`` uses
  ``ExecutorPool`` and ``SessionCache`` bounds its own calls; raw Redis clients
  do not.)

Inline escape hatch (requires a non-empty reason):
    ``# invariant-allow: P001 reason explaining why this site is safe``
A reasonless ``# invariant-allow: P001`` is rejected.

Ratchet mode (default, matching ``check_component_boundaries.py``):
- Counts violations per ``(rule, file)`` against
  ``scripts/dev/runtime_ast_invariants_baseline.txt``.
- Any new ``(rule, file)`` pair or count increase fails.
- Any count decrease requires ``--lower`` in the same change so fixed sites
  cannot silently regress.
- ``--base REF`` verifies the baseline file itself did not add or raise entries
  relative to ``REF``.
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

RULES = ("P001", "P003", "P004", "ALLOW_NO_REASON")

_ALLOW_RE = re.compile(r"#\s*invariant-allow:\s*([A-Za-z0-9_,-]+)(.*)$")

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


def _parse_allow_pragmas(source_lines: list[str], start_line: int, end_line: int) -> tuple[set[str], list[int]]:
    """Return ``(allowed_rules, reasonless_lines)`` across ``[start_line, end_line]`` (1-based)."""
    allowed: set[str] = set()
    reasonless: list[int] = []
    lo = max(1, start_line)
    hi = min(len(source_lines), max(start_line, end_line))
    for ln in range(lo, hi + 1):
        m = _ALLOW_RE.search(source_lines[ln - 1])
        if not m:
            continue
        raw_rules, tail = m.group(1), m.group(2).strip()
        # Strip optional leading punctuation like ":" or "—" or "reason="
        cleaned_reason = re.sub(r"^(?:[-:—]+|\breason\s*=\s*)", "", tail).strip()
        if not cleaned_reason:
            reasonless.append(ln)
            continue
        for token in raw_rules.split(","):
            tok = token.strip().upper()
            if tok:
                allowed.add(tok)
    return allowed, reasonless


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


class _ModuleImports:
    """Import alias table for one module."""

    def __init__(self, tree: ast.Module) -> None:
        self.task_spawn_aliases: set[str] = set(_TASK_SPAWN_NAMES)
        self.tracked_task_aliases: set[str] = set(_TRACKED_TASK_HELPERS)
        self.taskgroup_aliases: set[str] = {"TaskGroup"}
        self.wait_for_aliases: set[str] = {"wait_for"}
        self.monitor_names: set[str] = {"UNITARESMonitor"}
        self.monitor_modules: set[str] = set()
        self.raw_redis_getters: set[str] = set()
        self.raw_redis_modules: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
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
                    if mod.endswith("governance_monitor") and alias.name != "UNITARESMonitor":
                        pass
                    if mod.endswith("redis_client") and alias.name == "get_redis":
                        self.raw_redis_getters.add(local)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[-1]
                    if alias.name.endswith("governance_monitor"):
                        self.monitor_modules.add(local)
                    if alias.name.endswith("redis_client"):
                        self.raw_redis_modules.add(local)


def _check_p001_and_p003(
    tree: ast.Module,
    rel_path: str,
    lines: list[str],
    imports: _ModuleImports,
) -> list[Violation]:
    violations: list[Violation] = []
    reasonless_reported: set[int] = set()

    def record_if_not_allowed(rule: str, node: ast.AST, msg: str) -> None:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        allowed, reasonless = _parse_allow_pragmas(lines, start, end)
        for ln in reasonless:
            if ln not in reasonless_reported:
                reasonless_reported.add(ln)
                violations.append(
                    Violation(
                        "ALLOW_NO_REASON",
                        rel_path,
                        ln,
                        "invariant-allow pragma must include a non-empty reason",
                    )
                )
        if rule in allowed:
            return
        violations.append(Violation(rule, rel_path, start, msg))

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

        # P001: discarded task return value at statement boundary
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

        # P003: UNITARESMonitor(...) outside get_or_create_monitor without load_state=False
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
    # Detect thin local wrappers like `async def _get_redis(): return await get_redis()`
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


def _find_guarded_helpers_in_tree(
    tree: ast.Module,
    imports: _ModuleImports,
) -> tuple[set[str], set[str]]:
    """Return ``(always_guarded_callees, unguarded_callees)`` across ``tree``.

    A call ``foo(...)`` is guarded at its call site if it sits inside the first
    argument of ``asyncio.wait_for(...)`` or ``create_tracked_task(...)`` /
    ``_supervised_create_task(...)``, or inside a nested ``async def`` whose
    only invocation in the enclosing function is bounded by ``wait_for``.
    """
    guarded_calls: set[str] = set()
    unguarded_calls: set[str] = set()

    def walk_expr(node: ast.AST, *, bounded: bool) -> None:
        if isinstance(node, ast.Call):
            if _is_wait_for_call(node, imports.wait_for_aliases):
                if node.args:
                    walk_expr(node.args[0], bounded=True)
                for arg in node.args[1:]:
                    walk_expr(arg, bounded=bounded)
                for kw in node.keywords:
                    walk_expr(kw.value, bounded=bounded)
                return
            if _is_tracked_task_call(node, imports.tracked_task_aliases):
                if node.args:
                    walk_expr(node.args[0], bounded=True)
                for arg in node.args[1:]:
                    walk_expr(arg, bounded=bounded)
                for kw in node.keywords:
                    walk_expr(kw.value, bounded=bounded)
                return

            recv, name = _call_func_name(node)
            if recv is None and name:
                if bounded:
                    guarded_calls.add(name)
                else:
                    unguarded_calls.add(name)

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            walk_expr(child, bounded=bounded)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            # Check nested closures bounded via wait_for(_inner(), ...)
            nested_async: dict[str, ast.AsyncFunctionDef] = {}
            for stmt in getattr(node, "body", []):
                if isinstance(stmt, ast.AsyncFunctionDef):
                    nested_async[stmt.name] = stmt
            for stmt in getattr(node, "body", []):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                walk_expr(stmt, bounded=False)
            # Propagate boundedness into nested async closures whose calls are all guarded
            for nname, nfn in nested_async.items():
                is_n_bounded = (nname in guarded_calls) and (nname not in unguarded_calls)
                for stmt in nfn.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    walk_expr(stmt, bounded=is_n_bounded)

    return guarded_calls, unguarded_calls


def _check_p004_mcp_handlers(
    handler_trees: list[tuple[str, ast.Module, list[str], _ModuleImports]],
) -> list[Violation]:
    """Check P004 across all parsed files under ``src/mcp_handlers/``."""
    # Cross-file call boundedness across src/mcp_handlers/ so an imported helper
    # like `lookup_identity_anchor` (bounded internally) vs an `_inner` helper
    # (bounded by caller) is resolved accurately.
    all_guarded: set[str] = set()
    all_unguarded: set[str] = set()
    for _rel, tree, _lines, imports in handler_trees:
        g, u = _find_guarded_helpers_in_tree(tree, imports)
        all_guarded |= g
        all_unguarded |= u

    # A helper is bounded by callers iff it has >=1 guarded call and 0 unguarded calls,
    # and is not decorated with @mcp_tool / @mcp.tool (which MCP invokes directly).
    def is_mcp_entrypoint(fn: ast.AsyncFunctionDef) -> bool:
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call):
                _, dname = _call_func_name(dec)
                if dname in {"mcp_tool", "tool"}:
                    return True
            elif isinstance(dec, ast.Name) and dec.id in {"mcp_tool", "tool"}:
                return True
        return False

    caller_bounded_helpers: set[str] = set()
    # Map each callee name -> set of top-level callers that invoke it outside wait_for/tracked_task
    unguarded_callers_of: dict[str, set[str]] = {}
    for _rel, tree, _lines, imports in handler_trees:
        for top in tree.body:
            if isinstance(top, ast.AsyncFunctionDef):
                if not is_mcp_entrypoint(top) and top.name in all_guarded and top.name not in all_unguarded:
                    caller_bounded_helpers.add(top.name)
                _, top_unguarded = _find_guarded_helpers_in_tree(
                    ast.Module(body=top.body, type_ignores=[]),
                    imports,
                )
                for callee in top_unguarded:
                    unguarded_callers_of.setdefault(callee, set()).add(top.name)
            elif isinstance(top, ast.FunctionDef):
                _, top_unguarded = _find_guarded_helpers_in_tree(
                    ast.Module(body=top.body, type_ignores=[]),
                    imports,
                )
                for callee in top_unguarded:
                    unguarded_callers_of.setdefault(callee, set()).add(f"sync:{top.name}")
            else:
                _, top_unguarded = _find_guarded_helpers_in_tree(
                    ast.Module(body=[top], type_ignores=[]),
                    imports,
                )
                for callee in top_unguarded:
                    unguarded_callers_of.setdefault(callee, set()).add("<module>")

    # Propagate caller-boundedness across multi-hop helper chains in O(E)
    for _ in range(4):
        added = False
        for _rel, tree, _lines, _imports in handler_trees:
            for top in tree.body:
                if not isinstance(top, ast.AsyncFunctionDef) or top.name in caller_bounded_helpers or is_mcp_entrypoint(top):
                    continue
                callers = unguarded_callers_of.get(top.name, set())
                has_any_caller = bool(callers) or (top.name in all_guarded)
                if has_any_caller and callers and callers <= caller_bounded_helpers:
                    caller_bounded_helpers.add(top.name)
                    added = True
        if not added:
            break

    violations: list[Violation] = []

    for rel_path, tree, lines, imports in handler_trees:
        async_funcs, module_raw_getters = _collect_p004_module_facts(tree, imports)
        reasonless_reported: set[int] = set()

        # Track parameters of local helpers that receive a raw redis client from another local function
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

        # Propagate raw-redis arguments to local helper parameters (e.g. `_helper(redis, ...)`)
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
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", start) or start
            allowed, reasonless = _parse_allow_pragmas(lines, start, end)
            for ln in reasonless:
                if ln not in reasonless_reported:
                    reasonless_reported.add(ln)
                    violations.append(
                        Violation(
                            "ALLOW_NO_REASON",
                            rel_path,
                            ln,
                            "invariant-allow pragma must include a non-empty reason",
                        )
                    )
            if "P004" in allowed:
                return
            violations.append(Violation("P004", rel_path, start, msg))

        def scan_func(
            fn: ast.AsyncFunctionDef,
            *,
            inherited_raw_vars: set[str],
            enclosing_bounded: bool,
        ) -> None:
            if fn.name in caller_bounded_helpers:
                enclosing_bounded = True
            # Skip thin getter wrapper itself (`return await get_redis()`)
            if fn.name in module_raw_getters and fn.name != "get_redis":
                return

            local_getters = set(module_raw_getters)
            for sub in fn.body:
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").endswith("redis_client"):
                    for alias in sub.names:
                        if alias.name == "get_redis":
                            local_getters.add(alias.asname or alias.name)

            rvars = set(inherited_raw_vars) | set(raw_vars_by_func.get(fn.name, set()))

            # Detect nested closures passed to wait_for (e.g. host_availability._bounded -> _run)
            nested_guarded, nested_unguarded = _find_guarded_helpers_in_tree(
                ast.Module(body=fn.body, type_ignores=[]),
                imports,
            )

            def walk_node(node: ast.AST, *, in_wait_for: bool) -> None:
                if isinstance(node, ast.AsyncFunctionDef):
                    child_bounded = enclosing_bounded or (
                        node.name in nested_guarded and node.name not in nested_unguarded
                    )
                    # Check if closure parameter is called as `op(redis)` where `op` is a callable parameter
                    scan_func(node, inherited_raw_vars=rvars, enclosing_bounded=child_bounded)
                    return
                if isinstance(node, ast.FunctionDef):
                    return

                # Track nested `async with redis.pipeline() as pipe:` inside statements
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
                    # Direct inline `(await get_redis()).get(...)`
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
                scan_func(top, inherited_raw_vars=set(), enclosing_bounded=False)

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
            # Include peer handler modules so cross-file wait_for/tracked_task
            # caller boundedness matches a full-repository scan.
            peer_handlers = [
                p.resolve()
                for p in (root / "src" / "mcp_handlers").rglob("*.py")
                if "__pycache__" not in p.parts
            ]
            py_files = sorted(set(explicit) | set(peer_handlers))
        else:
            py_files = explicit

    violations: list[Violation] = []
    handler_trees: list[tuple[str, ast.Module, list[str], _ModuleImports]] = []

    for path in py_files:
        try:
            rel_path = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_path = path.as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()
        imports = _ModuleImports(tree)
        if filter_rels is None or rel_path in filter_rels:
            violations.extend(_check_p001_and_p003(tree, rel_path, lines, imports))
        if rel_path.startswith(MCP_HANDLERS_PREFIX):
            handler_trees.append((rel_path, tree, lines, imports))

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
