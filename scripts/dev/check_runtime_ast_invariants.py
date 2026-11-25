#!/usr/bin/env python3
"""Deterministic AST guard for server runtime invariants (P001, P003, P004).

Static source-only checks over ``src/`` with zero runtime imports and zero LLM
inference. Enforces three high-signal runtime invariants:

- **P001 (untracked background task)**: ``create_task(...)`` or
  ``ensure_future(...)`` whose returned ``Task`` is discarded at the call
  statement (bare expression statement, ``_ = ...``, or a discarded container /
  comprehension / conditional expression). CPython holds only a weak reference
  to tasks scheduled on an event loop, so an unreferenced task can be
  garbage-collected mid-await and has no cancellation handle.
  Safe forms: ``create_tracked_task(...)``, ``_supervised_create_task(...)``,
  ``tg.create_task(...)`` inside ``async with asyncio.TaskGroup() as tg:``, or
  storing/passing/returning the ``Task`` reference.
  Scope note: checks structural discard at the call statement; does not perform
  whole-function dataflow on named locals after assignment.

- **P003 (uncached monitor instantiation)**: ``UNITARESMonitor(...)`` outside
  ``def get_or_create_monitor`` (and outside the ``if`` body of
  ``if __name__ == "__main__":``) without ``load_state=False``. Bypassing
  ``mcp_server.get_or_create_monitor`` hydrates disk state into a throwaway
  instance that never enters the server cache. Explicit ``load_state=False``
  simulation/comparison instances are exempt.

- **P004 (unguarded raw Redis operation in MCP handlers)**: ``await`` / ``async
  for`` / ``async with`` — or an unawaited command coroutine not passed to a
  bounded runner — on a raw async Redis client (from
  ``src.cache.redis_client.get_redis``) inside ``src/mcp_handlers/`` that is
  neither wrapped in ``asyncio.wait_for(..., timeout=...)`` (with a non-``None``
  timeout, including ``asyncio.gather(...)`` or a single-use coroutine variable
  consumed there) nor confined to a non-entrypoint helper whose statically
  resolved intra-package call/reference sites are all bounded by
  ``asyncio.wait_for`` or backgrounded via ``create_tracked_task`` /
  ``_supervised_create_task``. Synchronous ``r.pipeline()`` command buffering is
  exempt; ``async with r.pipeline()`` and ``await pipe.execute()`` must still be
  bounded. (``asyncpg`` uses ``ExecutorPool`` and ``SessionCache`` bounds its
  own calls; raw Redis clients do not.)

Inline escape hatch (real Python ``#`` comments only; requires a non-empty reason):
    ``# invariant-allow: P001 reason explaining why this site is safe``
    ``# invariant-allow: P001, P004 reason explaining why this site is safe``
Any reasonless ``# invariant-allow: ...`` comment anywhere in a scanned file is
reported as ``ALLOW_NO_REASON``. Strings and docstrings are ignored via
``tokenize``. Unparseable Python files fail with ``PARSE_ERROR`` rather than
being skipped silently.

Ratchet mode (default, matching ``check_component_boundaries.py``):
- Counts violations per ``(rule, file)`` against
  ``scripts/dev/runtime_ast_invariants_baseline.txt``. Note: because the
  baseline stores counts per ``(rule, file)`` rather than line numbers,
  replacing one grandfathered site with another in the same file keeps the count
  unchanged by design; fixing a site requires ``--lower`` in the same change so
  the count can only move downward.
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
import io
import re
import subprocess
import tokenize
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
    r"^#\s*invariant-allow:\s*([A-Za-z0-9_-]+(?:\s*,\s*[A-Za-z0-9_-]+)*)(.*)$"
)

_TRACKED_TASK_HELPERS = frozenset({
    "create_tracked_task",
    "_supervised_create_task",
})
_TASK_SPAWN_NAMES = frozenset({"create_task", "ensure_future"})
_SYNC_REDIS_METHODS = frozenset({
    "pipeline",
    "pubsub",
    "lock",
    "register_script",
    "client",
})
_ASYNC_PIPELINE_METHODS = frozenset({
    "execute",
    "watch",
    "unwatch",
    "aclose",
    "close",
    "reset",
})


@dataclass(frozen=True)
class Violation:
    rule: str
    rel_path: str
    line: int
    message: str


def _parse_allow_comment(comment_text: str) -> tuple[set[str], bool] | None:
    """Parse a real ``tokenize.COMMENT`` string for ``# invariant-allow: ...``.

    Returns ``None`` if absent, else ``(allowed_rules, has_non_empty_reason)``.
    """
    m = _ALLOW_RE.match(comment_text.strip())
    if not m:
        return None
    raw_rules, tail = m.group(1), m.group(2).strip()
    cleaned_reason = re.sub(r"^(?:[-:—]+|\breason\s*=\s*)", "", tail).strip()
    if not cleaned_reason:
        return set(), False
    allowed = {tok.strip().upper() for tok in raw_rules.split(",") if tok.strip()}
    return allowed, True


def _collect_file_pragmas(
    rel_path: str, source: str
) -> tuple[dict[int, set[str]], list[Violation]]:
    """Scan real ``tokenize.COMMENT`` tokens in ``source`` for ``# invariant-allow:``."""
    allowed_by_line: dict[int, set[str]] = {}
    pragma_violations: list[Violation] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            lineno = tok.start[0]
            parsed = _parse_allow_comment(tok.string)
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
                allowed_by_line.setdefault(lineno, set()).update(rules)
    except (tokenize.TokenError, IndentationError):
        # Syntax/token errors are reported separately via ast.parse -> PARSE_ERROR.
        pass
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


def _has_non_none_timeout(call: ast.Call) -> bool:
    """Return True iff ``asyncio.wait_for(aw, timeout)`` supplies a non-``None`` timeout."""
    if not call.args:
        return False
    timeout_expr: ast.AST | None = call.args[1] if len(call.args) >= 2 else None
    has_kw_unpack = False
    for kw in call.keywords:
        if kw.arg == "timeout":
            timeout_expr = kw.value
        elif kw.arg is None:
            has_kw_unpack = True
    if timeout_expr is None:
        return has_kw_unpack
    if isinstance(timeout_expr, ast.Constant) and timeout_expr.value is None:
        return False
    return True


def _is_wait_for_call(node: ast.AST, imports: "_ModuleImports") -> bool:
    if not isinstance(node, ast.Call):
        return False
    recv, name = _call_func_name(node)
    is_asyncio_wait_for = (recv is None and name in imports.wait_for_aliases) or (
        recv in imports.asyncio_modules and name == "wait_for"
    )
    if not is_asyncio_wait_for:
        return False
    return _has_non_none_timeout(node)


def _is_gather_call(node: ast.AST, imports: "_ModuleImports") -> bool:
    if not isinstance(node, ast.Call):
        return False
    recv, name = _call_func_name(node)
    return (recv is None and name in imports.gather_aliases) or (
        recv in imports.asyncio_modules and name == "gather"
    )


def _is_tracked_task_call(node: ast.AST, imports: "_ModuleImports") -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    recv, name = _call_func_name(node)
    if recv is None:
        return name in imports.tracked_task_aliases
    if recv != "<expr>" and recv not in imports.asyncio_modules and name in _TRACKED_TASK_HELPERS:
        return True
    return False


def _is_taskgroup_context(item: ast.withitem, imports: "_ModuleImports") -> str | None:
    """If ``item`` is ``asyncio.TaskGroup() as tg``, return ``'tg'``."""
    if not isinstance(item.optional_vars, ast.Name):
        return None
    ctx = item.context_expr
    if not isinstance(ctx, ast.Call):
        return None
    recv, name = _call_func_name(ctx)
    if (recv is None and name in imports.taskgroup_aliases) or (
        recv in imports.asyncio_modules and name == "TaskGroup"
    ):
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
        (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        )
        or (
            isinstance(right, ast.Name)
            and right.id == "__name__"
            and isinstance(left, ast.Constant)
            and left.value == "__main__"
        )
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
        self.asyncio_modules: set[str] = set()
        self.task_spawn_aliases: set[str] = set()
        self.tracked_task_aliases: set[str] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _TRACKED_TASK_HELPERS
        }
        self.taskgroup_aliases: set[str] = set()
        self.wait_for_aliases: set[str] = set()
        self.gather_aliases: set[str] = set()
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
                is_asyncio = node.level == 0 and (mod == "asyncio" or mod.startswith("asyncio."))
                resolved_mod_rel = (
                    _resolve_import_rel_path(rel_path, node.module, node.level)
                    if node.module
                    else None
                )
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name in _TASK_SPAWN_NAMES:
                        self.task_spawn_aliases.add(local)
                        self.tracked_task_aliases.discard(local)
                    elif alias.name in _TRACKED_TASK_HELPERS and not is_asyncio:
                        self.tracked_task_aliases.add(local)
                        self.task_spawn_aliases.discard(local)
                    else:
                        self.tracked_task_aliases.discard(local)

                    if is_asyncio and alias.name == "TaskGroup":
                        self.taskgroup_aliases.add(local)
                    else:
                        self.taskgroup_aliases.discard(local)

                    if is_asyncio and alias.name == "wait_for":
                        self.wait_for_aliases.add(local)
                    else:
                        self.wait_for_aliases.discard(local)

                    if is_asyncio and alias.name == "gather":
                        self.gather_aliases.add(local)
                    else:
                        self.gather_aliases.discard(local)

                    if alias.name == "UNITARESMonitor":
                        self.monitor_names.add(local)
                    if alias.name == "governance_monitor":
                        self.monitor_modules.add(local)
                    if mod.endswith("redis_client") and alias.name == "get_redis":
                        self.raw_redis_getters.add(local)
                    if alias.name == "redis_client" and (
                        mod in {"src.cache", "cache", ""} or mod.endswith(".cache")
                    ):
                        self.raw_redis_modules.add(local)

                    # Check submodule import (`from . import persistence` or
                    # `from src.mcp_handlers.identity import persistence`)
                    submod_name = f"{node.module}.{alias.name}" if node.module else alias.name
                    sub_rel = _resolve_import_rel_path(rel_path, submod_name, node.level)
                    if sub_rel is not None:
                        self.handler_mod_imports[local] = sub_rel
                    if resolved_mod_rel is not None:
                        self.handler_func_imports[local] = (resolved_mod_rel, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    tail_local = alias.asname or alias.name.split(".")[-1]
                    if alias.name == "asyncio" or alias.name.startswith("asyncio."):
                        self.asyncio_modules.add(local)
                    if alias.name.endswith("governance_monitor"):
                        self.monitor_modules.add(tail_local)
                    if alias.name.endswith("redis_client"):
                        self.raw_redis_modules.add(tail_local)
                    if alias.name.startswith("src.mcp_handlers."):
                        cand = alias.name.replace(".", "/") + ".py"
                        self.handler_mod_imports[tail_local] = cand


def _iter_discarded_calls(expr: ast.expr) -> Iterable[ast.Call]:
    """Yield ``ast.Call`` nodes whose return values are discarded when ``expr`` is discarded."""
    if isinstance(expr, ast.Call):
        yield expr
    elif isinstance(expr, ast.IfExp):
        yield from _iter_discarded_calls(expr.body)
        yield from _iter_discarded_calls(expr.orelse)
    elif isinstance(expr, (ast.ListComp, ast.SetComp)):
        yield from _iter_discarded_calls(expr.elt)
    elif isinstance(expr, ast.DictComp):
        yield from _iter_discarded_calls(expr.key)
        yield from _iter_discarded_calls(expr.value)
    elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        for elt in expr.elts:
            yield from _iter_discarded_calls(elt)
    elif isinstance(expr, ast.BoolOp):
        for val in expr.values:
            yield from _iter_discarded_calls(val)


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
        if _is_tracked_task_call(call, imports):
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
            visit(
                node.test,
                in_monitor_factory=in_monitor_factory,
                in_main_guard=in_main_guard,
                active_taskgroups=active_taskgroups,
            )
            for stmt in node.body:
                visit(
                    stmt,
                    in_monitor_factory=in_monitor_factory,
                    in_main_guard=True,
                    active_taskgroups=active_taskgroups,
                )
            for stmt in node.orelse:
                visit(
                    stmt,
                    in_monitor_factory=in_monitor_factory,
                    in_main_guard=in_main_guard,
                    active_taskgroups=active_taskgroups,
                )
            return
        elif isinstance(node, ast.AsyncWith):
            extra_tgs = set(active_taskgroups)
            for item in node.items:
                tg = _is_taskgroup_context(item, imports)
                if tg:
                    extra_tgs.add(tg)
            active_taskgroups = frozenset(extra_tgs)

        discarded_expr: ast.expr | None = None
        if isinstance(node, ast.Expr):
            discarded_expr = node.value
        elif isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_"
            ):
                discarded_expr = node.value
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "_"
                and node.value is not None
            ):
                discarded_expr = node.value

        if discarded_expr is not None:
            for discarded_call in _iter_discarded_calls(discarded_expr):
                spawn_desc = is_untracked_spawn_call(discarded_call, set(active_taskgroups))
                if spawn_desc is not None:
                    record_if_not_allowed(
                        "P001",
                        discarded_call,
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


def _local_redis_imports(
    fn: ast.AST, base_getters: set[str], base_modules: set[str]
) -> tuple[set[str], set[str]]:
    """Collect module + function-local ``get_redis`` and ``redis_client`` aliases."""
    local_getters = set(base_getters)
    local_modules = set(base_modules)
    for sub in ast.walk(fn):
        if isinstance(sub, ast.ImportFrom):
            mod = sub.module or ""
            for alias in sub.names:
                local = alias.asname or alias.name
                if mod.endswith("redis_client") and alias.name == "get_redis":
                    local_getters.add(local)
                if alias.name == "redis_client" and (
                    mod in {"src.cache", "cache", ""} or mod.endswith(".cache")
                ):
                    local_modules.add(local)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                tail_local = alias.asname or alias.name.split(".")[-1]
                if alias.name.endswith("redis_client"):
                    local_modules.add(tail_local)
    return local_getters, local_modules


def _collect_lexical_scope_defs(
    stmts: list[ast.stmt],
) -> tuple[dict[str, list[ast.AsyncFunctionDef]], dict[str, list[ast.FunctionDef]], list[ast.ClassDef]]:
    """Collect all ``async def``, ``def``, and ``class`` nodes in a lexical block,
    descending into compound statements (``if``/``try``/``with``/``for``/``match``)
    without entering nested function or class bodies.
    """
    async_defs: dict[str, list[ast.AsyncFunctionDef]] = {}
    sync_defs: dict[str, list[ast.FunctionDef]] = {}
    class_defs: list[ast.ClassDef] = []

    def walk_stmt(node: ast.AST) -> None:
        if isinstance(node, ast.AsyncFunctionDef):
            async_defs.setdefault(node.name, []).append(node)
            return
        if isinstance(node, ast.FunctionDef):
            sync_defs.setdefault(node.name, []).append(node)
            return
        if isinstance(node, ast.ClassDef):
            class_defs.append(node)
            return
        if isinstance(node, ast.expr):
            return
        for child in ast.iter_child_nodes(node):
            walk_stmt(child)

    for s in stmts:
        walk_stmt(s)
    return async_defs, sync_defs, class_defs


def _collect_p004_module_facts(
    tree: ast.Module,
    imports: _ModuleImports,
) -> tuple[dict[str, list[ast.AsyncFunctionDef]], set[str]]:
    """Return ``(async_funcs_by_name, raw_getter_names)`` for a handler module."""
    async_funcs, _sync_defs, _class_defs = _collect_lexical_scope_defs(tree.body)

    raw_getters = set(imports.raw_redis_getters)
    changed = True
    while changed:
        changed = False
        for fname, fns in async_funcs.items():
            if fname in raw_getters or len(fns) != 1:
                continue
            fn = fns[0]
            local_getters, local_modules = _local_redis_imports(
                fn, raw_getters, imports.raw_redis_modules
            )
            returns_raw = False
            for sub in ast.walk(fn):
                if (
                    isinstance(sub, ast.Return)
                    and isinstance(sub.value, ast.Await)
                    and isinstance(sub.value.value, ast.Call)
                    and _is_raw_redis_getter_call(sub.value.value, local_getters, local_modules)
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
            target_rel, orig_name = imports.handler_func_imports[name]
            if orig_name in defined_by_module.get(target_rel, ()):
                return {(target_rel, orig_name)}, True
            # Imported from a package `__init__.py` or re-export module: fall
            # back conservatively across matching helper names.
            return set(all_by_bare_name.get(orig_name, ())), False
        return set(), True
    if recv in imports.handler_mod_imports:
        t_rel = imports.handler_mod_imports[recv]
        if t_rel in defined_by_module:
            if name in defined_by_module[t_rel]:
                return {(t_rel, name)}, True
            return set(all_by_bare_name.get(name, ())), False
    return set(all_by_bare_name.get(name, ())), False


def _scan_block_references(
    stmts: list[ast.stmt],
    *,
    rel_path: str,
    imports: _ModuleImports,
    defined_by_module: dict[str, set[str]],
    all_by_bare_name: dict[str, set[HandlerKey]],
    enclosing_bounded: bool = False,
    is_module_scope: bool = False,
) -> tuple[set[HandlerKey], set[HandlerKey], set[HandlerKey], dict[str, bool]]:
    """Single top-down pass over ``stmts`` (never double-visiting nested defs).

    Returns ``(directly_guarded, awaited_calls, escaped_unguarded, nested_async_is_bounded)``:
    - ``directly_guarded``: unambiguous coroutine calls passed directly as ``args[0]``
      of a valid ``asyncio.wait_for(..., timeout=...)`` or ``create_tracked_task(...)``,
      or ``await helper(...)`` inside a scope whose own execution is already bounded.
    - ``awaited_calls``: unambiguous ``await helper(...)`` calls inside an unbounded
      async function (eligible to become bounded only if all callers of the enclosing
      function are themselves bounded).
    - ``escaped_unguarded``: unawaited calls (``return _leaf()``, ``coro = _leaf()``),
      pre-awaited calls inside ``wait_for(await _leaf(), ...)``, callback/attribute
      references, inexact re-export matches, or calls from sync/module scope. Any
      helper in ``escaped_unguarded`` can never inherit caller-boundedness.
    """
    directly_guarded: set[HandlerKey] = set()
    awaited_calls: set[HandlerKey] = set()
    escaped_unguarded: set[HandlerKey] = set()

    if is_module_scope:
        local_nested_async: dict[str, list[ast.AsyncFunctionDef]] = {}
        local_nested_sync: dict[str, list[ast.FunctionDef]] = {}
        local_classes: list[ast.ClassDef] = []
    else:
        local_nested_async, local_nested_sync, local_classes = _collect_lexical_scope_defs(stmts)
    nested_guarded_names: set[str] = set()
    nested_escaped_names: set[str] = set()
    nested_names = set(local_nested_async) | set(local_nested_sync)

    def walk_call_subexprs(call: ast.Call) -> None:
        if isinstance(call.func, ast.Attribute):
            walk_expr(call.func.value)
        elif not isinstance(call.func, ast.Name):
            walk_expr(call.func)
        for arg in call.args:
            walk_expr(arg)
        for kw in call.keywords:
            walk_expr(kw.value)

    def record_call_site(call: ast.Call, *, mode: str) -> None:
        """Record ``call`` with ``mode`` in ``{'guarded', 'awaited', 'escaped'}``."""
        recv, cname = _call_func_name(call)
        if recv is None and cname in nested_names:
            if cname in local_nested_async and (
                mode == "guarded" or (mode == "awaited" and enclosing_bounded)
            ):
                nested_guarded_names.add(cname)
            else:
                nested_escaped_names.add(cname)
        else:
            targets, exact = _resolve_callee_targets(
                rel_path, recv, cname, imports, defined_by_module, all_by_bare_name
            )
            if not exact:
                escaped_unguarded.update(targets)
            elif mode == "guarded" or (mode == "awaited" and enclosing_bounded):
                directly_guarded.update(targets)
            elif mode == "awaited":
                awaited_calls.update(targets)
            else:
                escaped_unguarded.update(targets)
        walk_call_subexprs(call)

    def walk_expr(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return

        if isinstance(node, ast.Await):
            if isinstance(node.value, ast.Call):
                inner_call = node.value
                if _is_wait_for_call(inner_call, imports) or _is_tracked_task_call(
                    inner_call, imports
                ):
                    walk_expr(inner_call)
                else:
                    record_call_site(inner_call, mode="awaited")
                return
            walk_expr(node.value)
            return

        if isinstance(node, ast.Call):
            if _is_wait_for_call(node, imports) or _is_tracked_task_call(node, imports):
                if node.args:
                    first = node.args[0]
                    if _is_gather_call(first, imports):
                        for garg in first.args:
                            if isinstance(garg, ast.Call) and not (
                                _is_wait_for_call(garg, imports)
                                or _is_tracked_task_call(garg, imports)
                            ):
                                record_call_site(garg, mode="guarded")
                            else:
                                walk_expr(garg)
                        for gkw in first.keywords:
                            walk_expr(gkw.value)
                        if isinstance(first.func, ast.Attribute):
                            walk_expr(first.func.value)
                    elif isinstance(first, ast.Call) and not (
                        _is_wait_for_call(first, imports)
                        or _is_tracked_task_call(first, imports)
                    ):
                        record_call_site(first, mode="guarded")
                    else:
                        walk_expr(first)
                for arg in node.args[1:]:
                    walk_expr(arg)
                for kw in node.keywords:
                    walk_expr(kw.value)
                if isinstance(node.func, ast.Attribute):
                    walk_expr(node.func.value)
                return

            record_call_site(node, mode="escaped")
            return

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in nested_names:
                nested_escaped_names.add(node.id)
            else:
                targets, _ = _resolve_callee_targets(
                    rel_path, None, node.id, imports, defined_by_module, all_by_bare_name
                )
                escaped_unguarded.update(targets)
            return

        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            recv = node.value.id if isinstance(node.value, ast.Name) else "<expr>"
            targets, _ = _resolve_callee_targets(
                rel_path, recv, node.attr, imports, defined_by_module, all_by_bare_name
            )
            escaped_unguarded.update(targets)
            walk_expr(node.value)
            return

        for child in ast.iter_child_nodes(node):
            walk_expr(child)

    def walk_scope_stmt(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                walk_expr(dec)
            for dflt in node.args.defaults:
                walk_expr(dflt)
            for kdflt in node.args.kw_defaults:
                if kdflt is not None:
                    walk_expr(kdflt)
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                walk_expr(dec)
            for base in node.bases:
                walk_expr(base)
            for kw in node.keywords:
                walk_expr(kw.value)
            return
        if isinstance(node, ast.expr):
            walk_expr(node)
            return
        for child in ast.iter_child_nodes(node):
            walk_scope_stmt(child)

    for stmt in stmts:
        walk_scope_stmt(stmt)

    nested_bounded_map: dict[str, bool] = {}
    for nname, nfns in local_nested_async.items():
        is_b = nname in nested_guarded_names and nname not in nested_escaped_names
        nested_bounded_map[nname] = is_b
        for nfn in nfns:
            cg, ca, ce, _ = _scan_block_references(
                nfn.body,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                enclosing_bounded=is_b,
            )
            directly_guarded.update(cg)
            awaited_calls.update(ca)
            escaped_unguarded.update(ce)

    for _sname, sfns in local_nested_sync.items():
        for sfn in sfns:
            cg, ca, ce, _ = _scan_block_references(
                sfn.body,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                enclosing_bounded=False,
            )
            directly_guarded.update(cg)
            escaped_unguarded.update(ca)
            escaped_unguarded.update(ce)

    for cls in local_classes:
        cg, ca, ce, _ = _scan_block_references(
            cls.body,
            rel_path=rel_path,
            imports=imports,
            defined_by_module=defined_by_module,
            all_by_bare_name=all_by_bare_name,
            enclosing_bounded=False,
        )
        directly_guarded.update(cg)
        escaped_unguarded.update(ca)
        escaped_unguarded.update(ce)

    return directly_guarded, awaited_calls, escaped_unguarded, nested_bounded_map


def _extract_assign_targets_and_value(stmt: ast.AST) -> tuple[list[str], ast.expr | None]:
    if isinstance(stmt, ast.Assign):
        names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        return names, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return [stmt.target.id], stmt.value
    if isinstance(stmt, ast.NamedExpr) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id], stmt.value
    return [], None


def _compute_func_raw_vars(
    fn: ast.AST,
    seed_client_vars: set[str],
    seed_pipe_vars: set[str],
    local_getters: set[str],
    local_modules: set[str],
) -> tuple[set[str], set[str]]:
    """Return ``(client_vars, pipe_vars)`` across assignments, annotations, rebinds, and pipelines."""
    client_vars = set(seed_client_vars)
    pipe_vars = set(seed_pipe_vars)
    changed = True
    while changed:
        changed = False
        for sub in ast.walk(fn):
            names, val = _extract_assign_targets_and_value(sub)
            if names and val is not None:
                if (
                    isinstance(val, ast.Await)
                    and isinstance(val.value, ast.Call)
                    and _is_raw_redis_getter_call(val.value, local_getters, local_modules)
                ) or (isinstance(val, ast.Name) and val.id in client_vars):
                    for nm in names:
                        if nm not in client_vars:
                            client_vars.add(nm)
                            changed = True
                elif (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Attribute)
                    and isinstance(val.func.value, ast.Name)
                    and val.func.value.id in client_vars
                    and val.func.attr == "pipeline"
                ) or (isinstance(val, ast.Name) and val.id in pipe_vars):
                    for nm in names:
                        if nm not in pipe_vars:
                            pipe_vars.add(nm)
                            changed = True
            elif isinstance(sub, ast.AsyncWith):
                for item in sub.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Attribute)
                        and isinstance(item.context_expr.func.value, ast.Name)
                        and item.context_expr.func.value.id in client_vars
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        target_set = (
                            pipe_vars
                            if item.context_expr.func.attr == "pipeline"
                            else client_vars
                        )
                        if item.optional_vars.id not in target_set:
                            target_set.add(item.optional_vars.id)
                            changed = True
    return client_vars, pipe_vars


def _find_bounded_coro_vars(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, imports: _ModuleImports
) -> set[str]:
    """Return local names assigned once whose every Load is a bounded coroutine slot."""
    assign_counts: Counter[str] = Counter()
    bounded_loads: set[str] = set()
    unbounded_loads: set[str] = set()

    def walk_expr(node: ast.AST, *, bounded_slot: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if bounded_slot:
                bounded_loads.add(node.id)
            else:
                unbounded_loads.add(node.id)
            return
        if isinstance(node, ast.Call):
            if _is_wait_for_call(node, imports) or _is_tracked_task_call(node, imports):
                if node.args:
                    walk_expr(node.args[0], bounded_slot=True)
                for arg in node.args[1:]:
                    walk_expr(arg, bounded_slot=False)
                for kw in node.keywords:
                    walk_expr(kw.value, bounded_slot=False)
                if isinstance(node.func, ast.Attribute):
                    walk_expr(node.func.value, bounded_slot=False)
                return
            if bounded_slot and _is_gather_call(node, imports):
                for garg in node.args:
                    walk_expr(
                        garg,
                        bounded_slot=not isinstance(garg, ast.Starred),
                    )
                for gkw in node.keywords:
                    walk_expr(gkw.value, bounded_slot=False)
                if isinstance(node.func, ast.Attribute):
                    walk_expr(node.func.value, bounded_slot=False)
                return
        for child in ast.iter_child_nodes(node):
            walk_expr(child, bounded_slot=False)

    def walk_stmt(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        names, val = _extract_assign_targets_and_value(node)
        if names and val is not None:
            for nm in names:
                assign_counts[nm] += 1
            walk_expr(val, bounded_slot=False)
            return
        if isinstance(node, ast.expr):
            walk_expr(node, bounded_slot=False)
            return
        for child in ast.iter_child_nodes(node):
            walk_stmt(child)

    for s in fn.body:
        walk_stmt(s)

    return {
        nm
        for nm, cnt in assign_counts.items()
        if cnt == 1 and nm in bounded_loads and nm not in unbounded_loads
    }


def _check_p004_mcp_handlers(
    handler_trees: list[tuple[str, ast.Module, dict[int, set[str]], _ModuleImports]],
) -> list[Violation]:
    """Check P004 across all parsed files under ``src/mcp_handlers/``."""
    defined_by_module: dict[str, set[str]] = {}
    all_by_bare_name: dict[str, set[HandlerKey]] = {}
    async_funcs_by_key: dict[HandlerKey, list[ast.AsyncFunctionDef]] = {}
    ambiguous_keys: set[HandlerKey] = set()
    imports_by_module: dict[str, _ModuleImports] = {}
    raw_getters_by_module: dict[str, set[str]] = {}

    for rel_path, tree, _allowed, imports in handler_trees:
        imports_by_module[rel_path] = imports
        async_funcs, mod_getters = _collect_p004_module_facts(tree, imports)
        raw_getters_by_module[rel_path] = mod_getters
        defined_by_module[rel_path] = set(async_funcs)
        for fname, fns in async_funcs.items():
            key = (rel_path, fname)
            async_funcs_by_key[key] = fns
            if len(fns) > 1:
                ambiguous_keys.add(key)
            all_by_bare_name.setdefault(fname, set()).add(key)

    def is_mcp_entrypoint(fn: ast.AsyncFunctionDef) -> bool:
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call):
                _, dname = _call_func_name(dec)
                if dname in {"mcp_tool", "tool"}:
                    return True
            elif isinstance(dec, ast.Name) and dec.id in {"mcp_tool", "tool"}:
                return True
        return False

    all_directly_guarded: set[HandlerKey] = set()
    all_escaped_unguarded: set[HandlerKey] = set(ambiguous_keys)
    awaited_callers_of: dict[HandlerKey, set[HandlerKey]] = {}

    for rel_path, tree, _allowed, imports in handler_trees:
        mod_async, mod_sync, mod_classes = _collect_lexical_scope_defs(tree.body)
        for fname, fns in mod_async.items():
            caller_key: HandlerKey = (rel_path, fname)
            for fn in fns:
                dg, ac, eu, _ = _scan_block_references(
                    fn.body,
                    rel_path=rel_path,
                    imports=imports,
                    defined_by_module=defined_by_module,
                    all_by_bare_name=all_by_bare_name,
                    enclosing_bounded=False,
                )
                all_directly_guarded.update(dg)
                all_escaped_unguarded.update(eu)
                for target in ac:
                    awaited_callers_of.setdefault(target, set()).add(caller_key)

        for _sname, sfns in mod_sync.items():
            for sfn in sfns:
                dg, ac, eu, _ = _scan_block_references(
                    sfn.body,
                    rel_path=rel_path,
                    imports=imports,
                    defined_by_module=defined_by_module,
                    all_by_bare_name=all_by_bare_name,
                    enclosing_bounded=False,
                )
                all_directly_guarded.update(dg)
                all_escaped_unguarded.update(ac)
                all_escaped_unguarded.update(eu)

        for cls in mod_classes:
            dg, ac, eu, _ = _scan_block_references(
                cls.body,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                enclosing_bounded=False,
            )
            all_directly_guarded.update(dg)
            all_escaped_unguarded.update(ac)
            all_escaped_unguarded.update(eu)

        dg, ac, eu, _ = _scan_block_references(
            tree.body,
            rel_path=rel_path,
            imports=imports,
            defined_by_module=defined_by_module,
            all_by_bare_name=all_by_bare_name,
            enclosing_bounded=False,
            is_module_scope=True,
        )
        all_directly_guarded.update(dg)
        all_escaped_unguarded.update(ac)
        all_escaped_unguarded.update(eu)

    entrypoints: set[HandlerKey] = {
        key
        for key, fns in async_funcs_by_key.items()
        if any(is_mcp_entrypoint(fn) for fn in fns)
    }
    caller_bounded_helpers: set[HandlerKey] = set()
    for key in async_funcs_by_key:
        if (
            key not in entrypoints
            and key in all_directly_guarded
            and key not in all_escaped_unguarded
            and not awaited_callers_of.get(key)
        ):
            caller_bounded_helpers.add(key)

    # Propagate caller-boundedness across multi-hop awaited helper chains
    for _ in range(6):
        added = False
        for key in async_funcs_by_key:
            if key in caller_bounded_helpers or key in entrypoints or key in all_escaped_unguarded:
                continue
            callers = awaited_callers_of.get(key, set())
            if callers and all(c in caller_bounded_helpers for c in callers):
                caller_bounded_helpers.add(key)
                added = True
        if not added:
            break

    # Propagate raw-Redis client and pipeline variables across positional & keyword arguments
    client_seeds: dict[HandlerKey, set[str]] = {k: set() for k in async_funcs_by_key}
    pipe_seeds: dict[HandlerKey, set[str]] = {k: set() for k in async_funcs_by_key}
    raw_vars_by_key: dict[HandlerKey, tuple[set[str], set[str]]] = {}
    for _ in range(4):
        changed_params = False
        for key, fns in async_funcs_by_key.items():
            rel_path, _fname = key
            imports = imports_by_module[rel_path]
            agg_clients: set[str] = set()
            agg_pipes: set[str] = set()
            for fn in fns:
                local_getters, local_modules = _local_redis_imports(
                    fn, raw_getters_by_module[rel_path], imports.raw_redis_modules
                )
                cvars, pvars = _compute_func_raw_vars(
                    fn, client_seeds[key], pipe_seeds[key], local_getters, local_modules
                )
                agg_clients.update(cvars)
                agg_pipes.update(pvars)
                if not cvars and not pvars:
                    continue
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Call):
                        continue
                    recv, cname = _call_func_name(sub)
                    targets, exact = _resolve_callee_targets(
                        rel_path, recv, cname, imports, defined_by_module, all_by_bare_name
                    )
                    if not exact or len(targets) != 1:
                        continue
                    t_key = next(iter(targets))
                    target_fns = async_funcs_by_key.get(t_key)
                    if not target_fns or len(target_fns) != 1:
                        continue
                    target_fn = target_fns[0]
                    pos_params = [a.arg for a in target_fn.args.posonlyargs] + [
                        a.arg for a in target_fn.args.args
                    ]
                    named_params = set(pos_params) | {a.arg for a in target_fn.args.kwonlyargs}
                    for idx, arg in enumerate(sub.args):
                        if isinstance(arg, ast.Name) and idx < len(pos_params):
                            pname = pos_params[idx]
                            if arg.id in cvars and pname not in client_seeds[t_key]:
                                client_seeds[t_key].add(pname)
                                changed_params = True
                            if arg.id in pvars and pname not in pipe_seeds[t_key]:
                                pipe_seeds[t_key].add(pname)
                                changed_params = True
                    for kw in sub.keywords:
                        if kw.arg and kw.arg in named_params and isinstance(kw.value, ast.Name):
                            if kw.value.id in cvars and kw.arg not in client_seeds[t_key]:
                                client_seeds[t_key].add(kw.arg)
                                changed_params = True
                            if kw.value.id in pvars and kw.arg not in pipe_seeds[t_key]:
                                pipe_seeds[t_key].add(kw.arg)
                                changed_params = True
            raw_vars_by_key[key] = (agg_clients, agg_pipes)
        if not changed_params:
            break

    violations: list[Violation] = []

    for rel_path, tree, allowed_by_line, imports in handler_trees:
        module_raw_getters = raw_getters_by_module[rel_path]

        def record_p004(node: ast.AST, msg: str) -> None:
            if _is_rule_allowed(allowed_by_line, node, "P004"):
                return
            violations.append(Violation("P004", rel_path, getattr(node, "lineno", 1), msg))

        def scan_func(
            fn: ast.FunctionDef | ast.AsyncFunctionDef,
            *,
            is_top_level: bool,
            inherited_client_vars: set[str],
            inherited_pipe_vars: set[str],
            enclosing_bounded: bool,
        ) -> None:
            if (
                is_top_level
                and isinstance(fn, ast.AsyncFunctionDef)
                and (rel_path, fn.name) in caller_bounded_helpers
            ):
                enclosing_bounded = True

            local_getters, local_modules = _local_redis_imports(
                fn, module_raw_getters, imports.raw_redis_modules
            )
            if is_top_level:
                c_seed, p_seed = raw_vars_by_key.get((rel_path, fn.name), (set(), set()))
            else:
                c_seed, p_seed = inherited_client_vars, inherited_pipe_vars
            client_vars, pipe_vars = _compute_func_raw_vars(
                fn, set(c_seed), set(p_seed), local_getters, local_modules
            )
            all_rvars = client_vars | pipe_vars
            bounded_coro_vars = _find_bounded_coro_vars(fn, imports)

            _, _, _, nested_bounded_map = _scan_block_references(
                fn.body,
                rel_path=rel_path,
                imports=imports,
                defined_by_module=defined_by_module,
                all_by_bare_name=all_by_bare_name,
                enclosing_bounded=enclosing_bounded,
            )

            def _raw_redis_call_info(call: ast.Call) -> tuple[str, bool] | None:
                """Return ``(desc, is_async_coro_when_unawaited)`` if ``call`` is on raw Redis."""
                if not isinstance(call.func, ast.Attribute):
                    return None
                attr = call.func.attr
                if (
                    isinstance(call.func.value, ast.Await)
                    and isinstance(call.func.value.value, ast.Call)
                    and _is_raw_redis_getter_call(call.func.value.value, local_getters, local_modules)
                ):
                    return f"(await get_redis()).{attr}(...)", attr not in _SYNC_REDIS_METHODS
                if isinstance(call.func.value, ast.Name):
                    vid = call.func.value.id
                    if vid in client_vars:
                        return f"{vid}.{attr}(...)", attr not in _SYNC_REDIS_METHODS
                    if vid in pipe_vars:
                        return f"{vid}.{attr}(...)", attr in _ASYNC_PIPELINE_METHODS
                return None

            def _walk_call_args_and_recv(call: ast.Call) -> None:
                if isinstance(call.func, ast.Attribute):
                    walk_node(call.func.value)
                elif not isinstance(call.func, ast.Name):
                    walk_node(call.func)
                for arg in call.args:
                    walk_node(arg)
                for kw in call.keywords:
                    walk_node(kw.value)

            def walk_node(node: ast.AST, *, direct_bounded_coro: bool = False) -> None:
                if isinstance(node, ast.AsyncFunctionDef):
                    child_bounded = nested_bounded_map.get(node.name, False)
                    scan_func(
                        node,
                        is_top_level=False,
                        inherited_client_vars=client_vars,
                        inherited_pipe_vars=pipe_vars,
                        enclosing_bounded=child_bounded,
                    )
                    return
                if isinstance(node, ast.FunctionDef):
                    scan_func(
                        node,
                        is_top_level=False,
                        inherited_client_vars=client_vars,
                        inherited_pipe_vars=pipe_vars,
                        enclosing_bounded=False,
                    )
                    return
                if isinstance(node, ast.ClassDef):
                    scan_class_body(node.body)
                    return

                names, assigned_val = _extract_assign_targets_and_value(node)
                if (
                    len(names) == 1
                    and names[0] in bounded_coro_vars
                    and isinstance(assigned_val, ast.Call)
                ):
                    walk_node(assigned_val, direct_bounded_coro=True)
                    return

                if isinstance(node, ast.AsyncWith):
                    for item in node.items:
                        if (
                            isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Attribute)
                            and isinstance(item.context_expr.func.value, ast.Name)
                            and item.context_expr.func.value.id in all_rvars
                            and not enclosing_bounded
                        ):
                            record_p004(
                                node,
                                f"unguarded async with {item.context_expr.func.value.id}.{item.context_expr.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                            )
                        walk_node(item.context_expr)
                        if item.optional_vars is not None:
                            walk_node(item.optional_vars)
                    for stmt in node.body:
                        walk_node(stmt)
                    return

                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        call = node.value
                        info = _raw_redis_call_info(call)
                        if info is not None:
                            desc, _ = info
                            if not enclosing_bounded:
                                record_p004(
                                    node,
                                    f"unguarded await {desc} in MCP handler; wrap with asyncio.wait_for",
                                )
                            _walk_call_args_and_recv(call)
                            return
                    walk_node(node.value)
                    return

                if isinstance(node, ast.AsyncFor):
                    it = node.iter
                    if (
                        isinstance(it, ast.Call)
                        and isinstance(it.func, ast.Attribute)
                        and isinstance(it.func.value, ast.Name)
                        and it.func.value.id in all_rvars
                    ):
                        if not enclosing_bounded:
                            record_p004(
                                node,
                                f"unguarded async for over {it.func.value.id}.{it.func.attr}(...) in MCP handler; wrap with asyncio.wait_for",
                            )
                        _walk_call_args_and_recv(it)
                    else:
                        walk_node(it)
                    for stmt in node.body:
                        walk_node(stmt)
                    for stmt in node.orelse:
                        walk_node(stmt)
                    return

                if isinstance(node, ast.Call):
                    if _is_wait_for_call(node, imports) or _is_tracked_task_call(node, imports):
                        if node.args:
                            walk_node(
                                node.args[0],
                                direct_bounded_coro=isinstance(node.args[0], ast.Call),
                            )
                        for arg in node.args[1:]:
                            walk_node(arg)
                        for kw in node.keywords:
                            walk_node(kw.value)
                        if isinstance(node.func, ast.Attribute):
                            walk_node(node.func.value)
                        return

                    if direct_bounded_coro and _is_gather_call(node, imports):
                        for garg in node.args:
                            walk_node(
                                garg,
                                direct_bounded_coro=isinstance(garg, ast.Call),
                            )
                        for gkw in node.keywords:
                            walk_node(gkw.value)
                        if isinstance(node.func, ast.Attribute):
                            walk_node(node.func.value)
                        return

                    info = _raw_redis_call_info(node)
                    if (
                        info is not None
                        and info[1]
                        and not enclosing_bounded
                        and not direct_bounded_coro
                    ):
                        record_p004(
                            node,
                            f"unguarded raw Redis coroutine {info[0]} in MCP handler; wrap with asyncio.wait_for",
                        )
                    _walk_call_args_and_recv(node)
                    return

                for child in ast.iter_child_nodes(node):
                    walk_node(child)

            for stmt in fn.body:
                walk_node(stmt)

        def scan_class_body(stmts: list[ast.stmt]) -> None:
            sub_async, sub_sync, sub_classes = _collect_lexical_scope_defs(stmts)
            for _n, fns in sub_async.items():
                for fn in fns:
                    scan_func(
                        fn,
                        is_top_level=False,
                        inherited_client_vars=set(),
                        inherited_pipe_vars=set(),
                        enclosing_bounded=False,
                    )
            for _s, sfns in sub_sync.items():
                for sfn in sfns:
                    scan_func(
                        sfn,
                        is_top_level=False,
                        inherited_client_vars=set(),
                        inherited_pipe_vars=set(),
                        enclosing_bounded=False,
                    )
            for cls in sub_classes:
                scan_class_body(cls.body)

        mod_async, mod_sync, mod_classes = _collect_lexical_scope_defs(tree.body)
        for _fname, fns in mod_async.items():
            for top_fn in fns:
                scan_func(
                    top_fn,
                    is_top_level=True,
                    inherited_client_vars=set(),
                    inherited_pipe_vars=set(),
                    enclosing_bounded=False,
                )
        for _sname, sfns in mod_sync.items():
            for sfn in sfns:
                scan_func(
                    sfn,
                    is_top_level=False,
                    inherited_client_vars=set(),
                    inherited_pipe_vars=set(),
                    enclosing_bounded=False,
                )
        for cls in mod_classes:
            scan_class_body(cls.body)

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
        allowed_by_line, pragma_violations = _collect_file_pragmas(rel_path, source)
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
