"""Tests for ``scripts/dev/check_runtime_ast_invariants.py``.

Covers:
1. Live-repo conformance: ``src/`` passes against the committed ratchet baseline.
2. Positive detections seeded from real historical bugs (P001 discarded
   create_task/ensure_future, P003 uncached UNITARESMonitor, P004 unguarded raw
   Redis await in ``src/mcp_handlers/``).
3. Safe-pattern exemptions (``create_tracked_task``, ``_supervised_create_task``,
   ``TaskGroup.create_task``, stored Task references, ``get_or_create_monitor``,
   ``load_state=False`` throwaway monitors, ``asyncio.wait_for`` guards,
   multi-hop bounded helpers, ``SessionCache`` non-raw Redis wrappers).
4. Escape-hatch discipline (``# invariant-allow: RULE reason`` requires a
   non-empty reason) and baseline ratchet semantics (``--lower``, ``--base``).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "check_runtime_ast_invariants.py"

_SPEC = importlib.util.spec_from_file_location("check_runtime_ast_invariants", SCRIPT)
assert _SPEC and _SPEC.loader
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["check_runtime_ast_invariants"] = guard
_SPEC.loader.exec_module(guard)


def _write(root: Path, rel: str, code: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return path


def test_live_repository_passes_ratchet() -> None:
    assert guard.main(["--root", str(REPO_ROOT)]) == 0


# ---------------------------------------------------------------------------
# P001 — untracked background task
# ---------------------------------------------------------------------------


def test_p001_flags_discarded_create_task_and_ensure_future(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/sample.py",
        """\
import asyncio
from asyncio import create_task as spawn

async def bad(loop, coro_fn):
    asyncio.create_task(coro_fn())
    loop.create_task(coro_fn())
    asyncio.ensure_future(coro_fn())
    _ = spawn(coro_fn())
""",
    )
    violations = guard.scan_paths(tmp_path)
    lines = [v.line for v in violations if v.rule == "P001"]
    assert lines == [5, 6, 7, 8]


def test_p001_allows_tracked_helpers_stored_refs_and_taskgroup(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/safe_tasks.py",
        """\
import asyncio
from src.background_tasks import create_tracked_task, _supervised_create_task

_TASKS = set()

async def good(coro_fn):
    create_tracked_task(coro_fn(), name="a")
    _supervised_create_task(coro_fn(), name="b")
    t = asyncio.create_task(coro_fn())
    _TASKS.add(t)
    _TASKS.add(asyncio.create_task(coro_fn()))
    async with asyncio.TaskGroup() as tg:
        tg.create_task(coro_fn())
    return asyncio.create_task(coro_fn())
""",
    )
    assert guard.scan_paths(tmp_path) == []


# ---------------------------------------------------------------------------
# P003 — uncached UNITARESMonitor instantiation
# ---------------------------------------------------------------------------


def test_p003_flags_direct_monitor_outside_factory(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/bad_monitor.py",
        """\
from src.governance_monitor import UNITARESMonitor as Mon
import src.governance_monitor as gm

def leak(agent_id: str):
    m1 = Mon(agent_id)
    m2 = gm.UNITARESMonitor(agent_id)
    return m1, m2
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rule, v.line) for v in violations] == [("P003", 5), ("P003", 6)]


def test_p003_allows_factory_main_guard_and_load_state_false(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/safe_monitor.py",
        """\
from src.governance_monitor import UNITARESMonitor

def get_or_create_monitor(agent_id: str):
    return UNITARESMonitor(agent_id)

def simulate():
    return UNITARESMonitor("_simulation_temp_", load_state=False)

if __name__ == "__main__":
    demo = UNITARESMonitor("demo")
""",
    )
    assert guard.scan_paths(tmp_path) == []


# ---------------------------------------------------------------------------
# P004 — unguarded raw Redis await in src/mcp_handlers/
# ---------------------------------------------------------------------------


def test_p004_flags_unguarded_raw_redis_in_mcp_handler(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mcp_handlers/bad_redis.py",
        """\
from src.cache.redis_client import get_redis

async def handle_something(key: str):
    redis = await get_redis()
    if redis:
        val = await redis.get(key)
        return val
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert len(violations) == 1
    assert violations[0].rule == "P004"
    assert violations[0].line == 6


def test_p004_allows_wait_for_tracked_task_and_multihop_bounded_helpers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mcp_handlers/safe_redis.py",
        """\
import asyncio
from src.background_tasks import create_tracked_task
from src.cache.redis_client import get_redis

async def _leaf_op(key: str):
    raw = await get_redis()
    if raw:
        return await raw.get(key)
    return None

async def _mid_op(key: str):
    return await _leaf_op(key)

async def public_lookup(key: str):
    return await asyncio.wait_for(_mid_op(key), timeout=0.5)

async def _bg_write(key: str, val: str):
    raw = await get_redis()
    if raw:
        await raw.set(key, val)

def schedule_bg(key: str, val: str):
    create_tracked_task(_bg_write(key, val), name="bg_redis")

async def direct_bounded(key: str):
    raw = await get_redis()
    if raw:
        return await asyncio.wait_for(raw.get(key), timeout=1.0)
    return None
""",
    )
    # Outside src/mcp_handlers/ raw Redis is allowed (REST / background tasks)
    _write(
        tmp_path,
        "src/http_routes/rest_ok.py",
        """\
from src.cache.redis_client import get_redis

async def http_endpoint():
    r = await get_redis()
    return await r.ping() if r else False
""",
    )
    assert guard.scan_paths(tmp_path) == []


def test_p004_flags_mcp_tool_entrypoint_even_if_called_elsewhere_with_wait_for(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mcp_handlers/tool_entry.py",
        """\
import asyncio
from src.cache.redis_client import get_redis

def mcp_tool(name):
    def _wrap(fn):
        return fn
    return _wrap

@mcp_tool("my_tool")
async def handle_my_tool(arguments):
    r = await get_redis()
    return await r.get("k")

async def other_caller():
    return await asyncio.wait_for(handle_my_tool({}), timeout=1.0)
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rule, v.line) for v in violations] == [("P004", 12)]


# ---------------------------------------------------------------------------
# Escape hatch & ratchet behavior
# ---------------------------------------------------------------------------


def test_invariant_allow_requires_non_empty_reason(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pragmas.py",
        """\
import asyncio

async def f(coro):
    asyncio.create_task(coro())  # invariant-allow: P001
    asyncio.create_task(coro())  # invariant-allow: P001 intentional bootstrap fallback
""",
    )
    violations = guard.scan_paths(tmp_path)
    # Line 4 has no reason -> both ALLOW_NO_REASON and un-exempted P001 fire.
    # Line 5 has a valid reason -> exempted cleanly.
    assert [(v.rule, v.line) for v in violations] == [
        ("ALLOW_NO_REASON", 4),
        ("P001", 4),
    ]


def test_ratchet_rejects_new_or_unlowered_and_forbids_raising_against_base(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    f = _write(
        tmp_path,
        "src/mod.py",
        "import asyncio\nasync def a(c):\n    asyncio.create_task(c())\n",
    )
    baseline = _write(
        tmp_path,
        "scripts/dev/runtime_ast_invariants_baseline.txt",
        "P001\tsrc/mod.py\t1\n",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    # Matches baseline -> passes
    assert guard.main(["--root", str(tmp_path), "--base", "HEAD"]) == 0

    # Attempt to raise baseline relative to HEAD -> fails --base check
    baseline.write_text("P001\tsrc/mod.py\t2\n", encoding="utf-8")
    f.write_text(
        "import asyncio\nasync def a(c):\n    asyncio.create_task(c())\n    asyncio.create_task(c())\n",
        encoding="utf-8",
    )
    assert guard.main(["--root", str(tmp_path), "--base", "HEAD"]) == 1

    # Fix the violation without running --lower -> fails until lowered
    baseline.write_text("P001\tsrc/mod.py\t1\n", encoding="utf-8")
    f.write_text("async def a(c):\n    return await c()\n", encoding="utf-8")
    assert guard.main(["--root", str(tmp_path)]) == 1
    assert guard.main(["--root", str(tmp_path), "--lower"]) == 0
    assert guard.main(["--root", str(tmp_path), "--base", "HEAD"]) == 0


def test_initial_baseline_passes_base_when_absent_at_ref_and_files_mode_ignores_baseline(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    f = _write(
        tmp_path,
        "src/mod.py",
        "import asyncio\nasync def a(c):\n    asyncio.create_task(c())\n",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "before-guard"], cwd=tmp_path, check=True)

    # Introducing the baseline file for the first time relative to HEAD passes --base
    _write(
        tmp_path,
        "scripts/dev/runtime_ast_invariants_baseline.txt",
        "P001\tsrc/mod.py\t1\n",
    )
    assert guard.main(["--root", str(tmp_path), "--base", "HEAD"]) == 0
    # --base with an unresolvable git ref must fail rather than skip the ratchet
    assert guard.main(["--root", str(tmp_path), "--base", "nonexistent-ref-xyz"]) == 1
    # --files mode checks explicit files directly without baseline exemptions
    assert guard.main(["--root", str(tmp_path), "--files", str(f)]) == 1


def test_p004_bounded_nested_closure_and_module_scoped_call_resolution(tmp_path: Path) -> None:
    # Nested closure passed only to asyncio.wait_for bounds its leaf helper
    _write(
        tmp_path,
        "src/mcp_handlers/nested_ok.py",
        """\
import asyncio
from src.cache.redis_client import get_redis

async def _leaf(k: str):
    r = await get_redis()
    return await r.get(k) if r else None

async def handler(k: str):
    async def _run():
        return await _leaf(k)
    return await asyncio.wait_for(_run(), timeout=0.5)
""",
    )
    assert guard.scan_paths(tmp_path) == []

    # Same-named helper in a second module with no callers does NOT inherit
    # boundedness, and passing a helper as an unguarded callback or attribute
    # call revokes caller-boundedness.
    _write(
        tmp_path,
        "src/mcp_handlers/other_mod.py",
        """\
import asyncio
from src.cache.redis_client import get_redis

async def _leaf(k: str):
    r = await get_redis()
    return await r.get(k) if r else None

async def _cb_helper(k: str):
    r = await get_redis()
    return await r.get(k) if r else None

def register(fn):
    return fn

async def caller(k: str):
    register(_cb_helper)
    return await asyncio.wait_for(_cb_helper(k), timeout=0.5)
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/mcp_handlers/other_mod.py", "P004", 6),
        ("src/mcp_handlers/other_mod.py", "P004", 10),
    ]


def test_pragmas_with_comma_spaces_reasonless_anywhere_and_syntax_errors(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pragma_edge.py",
        """\
import asyncio

# invariant-allow: P001
async def ok(coro):
    asyncio.create_task(coro())  # invariant-allow: P001, P004 intentional bootstrap fallback
""",
    )
    _write(tmp_path, "src/broken.py", "def broken(:\n    pass\n")

    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/pragma_edge.py", "ALLOW_NO_REASON", 3),
        ("src/broken.py", "PARSE_ERROR", 1),
    ]


def test_p004_reexport_unguarded_call_caught_and_subpackage_module_call_bounded(
    tmp_path: Path,
) -> None:
    # 1. Subpackage `from . import persistence` + `wait_for(persistence._bounded_op())`
    #    resolves cleanly as guarded without spurious unguarded-attribute hits.
    _write(
        tmp_path,
        "src/mcp_handlers/sub/persistence.py",
        """\
import asyncio
from src.cache.redis_client import get_redis

async def _bounded_op(k: str):
    r = await get_redis()
    return await r.get(k) if r else None

async def _reexported_leak(k: str):
    r = await get_redis()
    return await r.get(k) if r else None

async def local_guarded_caller(k: str):
    return await asyncio.wait_for(_reexported_leak(k), timeout=0.5)
""",
    )
    _write(
        tmp_path,
        "src/mcp_handlers/sub/__init__.py",
        "from .persistence import _reexported_leak\n",
    )
    _write(
        tmp_path,
        "src/mcp_handlers/sub/handlers.py",
        """\
import asyncio
from . import persistence
from src.mcp_handlers.sub import _reexported_leak

async def good_caller(k: str):
    return await asyncio.wait_for(persistence._bounded_op(k), timeout=0.5)

async def bad_reexport_caller(k: str):
    return await _reexported_leak(k)
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/mcp_handlers/sub/persistence.py", "P004", 10),
    ]


_H_REL = "src/mcp_handlers/h.py"
_GR_HDR = (
    "from src.cache.redis_client import get_redis\n"
    "import asyncio\n"
    "def mcp_tool(*a, **k):\n"
    "    return lambda f: f\n"
)


@pytest.mark.parametrize(
    ("case_id", "files", "expected"),
    [
        (
            "pragma-in-string",
            {
                "src/a.py": 'import asyncio\nasync def c(): pass\nasyncio.create_task(c()); s = "# invariant-allow: P001 not a comment"\n'
            },
            {("P001", "src/a.py", 3)},
        ),
        (
            "pragma-in-string-arg",
            {
                "src/a.py": 'import asyncio\nasync def c(x): pass\nasyncio.create_task(c(\n    "# invariant-allow: P001 smuggled"))\n'
            },
            {("P001", "src/a.py", 3)},
        ),
        (
            "docstring-example",
            {
                "src/a.py": 'def f():\n    """Usage:\n\n    # invariant-allow: P001\n    """\n'
            },
            set(),
        ),
        (
            "main-else",
            {
                "src/a.py": 'from m import UNITARESMonitor\nif __name__ == "__main__":\n    pass\nelse:\n    UNITARESMonitor()\n'
            },
            {("P003", "src/a.py", 5)},
        ),
        (
            "spoof-tracked-name",
            {
                "src/a.py": "from asyncio import create_task as create_tracked_task\nasync def c(): pass\ncreate_tracked_task(c())\n"
            },
            {("P001", "src/a.py", 3)},
        ),
        (
            "ifexp-discard",
            {
                "src/a.py": "import asyncio\nasync def c(): pass\nflag = True\nasyncio.create_task(c()) if flag else None\n"
            },
            {("P001", "src/a.py", 4)},
        ),
        (
            "listcomp-discard",
            {
                "src/a.py": "import asyncio\nasync def c(): pass\n[asyncio.create_task(c()) for _ in range(3)]\n"
            },
            {("P001", "src/a.py", 3)},
        ),
        (
            "p004-baseline",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    await r.get("k")\n'
            },
            {("P004", _H_REL, 8)},
        ),
        (
            "p004-guarded-control",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    await asyncio.wait_for(r.get("k"), timeout=1)\n'
            },
            set(),
        ),
        (
            "unrelated-wait_for",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h(cond):\n    r = await get_redis()\n    await cond.wait_for(r.get("k"))\n'
            },
            {("P004", _H_REL, 8)},
        ),
        (
            "timeout-none",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    await asyncio.wait_for(r.get("k"), timeout=None)\n'
            },
            {("P004", _H_REL, 8)},
        ),
        (
            "pre-awaited",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    await asyncio.wait_for(await r.get("k"), timeout=1)\n'
            },
            {("P004", _H_REL, 8)},
        ),
        (
            "annassign",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r: object = await get_redis()\n    await r.get("k")\n'
            },
            {("P004", _H_REL, 8)},
        ),
        (
            "from-pkg-import-module",
            {
                _H_REL: 'from src.cache import redis_client\ndef mcp_tool(*a, **k):\n    return lambda f: f\n@mcp_tool("t")\nasync def h():\n    r = await redis_client.get_redis()\n    await r.get("k")\n'
            },
            {("P004", _H_REL, 7)},
        ),
        (
            "rebind",
            {
                _H_REL: _GR_HDR
                + '@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    r2 = r\n    await r2.get("k")\n'
            },
            {("P004", _H_REL, 9)},
        ),
        (
            "kwarg-param",
            {
                _H_REL: _GR_HDR
                + 'async def _use(client):\n    await client.get("k")\n@mcp_tool("t")\nasync def h():\n    r = await get_redis()\n    await _use(client=r)\n'
            },
            {("P004", _H_REL, 6)},
        ),
        (
            "returns-client-exempts",
            {
                _H_REL: _GR_HDR
                + 'async def _r():\n    c = await get_redis()\n    await c.set("x", 1)\n    return await get_redis()\n@mcp_tool("t")\nasync def h():\n    await _r()\n'
            },
            {("P004", _H_REL, 7)},
        ),
        (
            "returned-coroutine",
            {
                _H_REL: _GR_HDR
                + 'async def _leaf():\n    r = await get_redis()\n    await r.get("k")\nasync def _outer():\n    return _leaf()\n@mcp_tool("t")\nasync def h():\n    coro = await asyncio.wait_for(_outer(), timeout=1)\n    await coro\n'
            },
            {("P004", _H_REL, 7)},
        ),
    ],
)
def test_adversarial_ast_edge_cases(
    tmp_path: Path,
    case_id: str,
    files: dict[str, str],
    expected: set[tuple[str, str, int]],
) -> None:
    for rel, src in files.items():
        _write(tmp_path, rel, src)
    got = {(v.rule, v.rel_path, v.line) for v in guard.scan_paths(tmp_path)}
    assert got == expected, f"case {case_id} mismatch: got={got} expected={expected}"


def test_p004_nested_closure_in_try_class_methods_pipeline_buffering_and_gather(
    tmp_path: Path,
) -> None:
    # 1. Closure inside `try:` calling `_leaf` unbounded revokes `_leaf`'s caller-boundedness
    _write(
        tmp_path,
        "src/mcp_handlers/try_closure.py",
        _GR_HDR
        + """\
async def _leaf(k):
    r = await get_redis()
    return await r.get(k)

async def warm(k):
    return await asyncio.wait_for(_leaf(k), timeout=1)

@mcp_tool("t")
async def h(k):
    try:
        async def _run():
            return await _leaf(k)
        return await _run()
    except Exception:
        return None
""",
    )
    # 2. Class method in src/mcp_handlers/ with unguarded raw Redis await is caught
    _write(
        tmp_path,
        "src/mcp_handlers/cls_handler.py",
        """\
from src.cache.redis_client import get_redis

class Store:
    async def bad_method(self, k: str):
        r = await get_redis()
        return await r.get(k)
""",
    )
    # 3. Synchronous pipeline buffering, single-use coro var, and asyncio.gather inside wait_for are allowed
    _write(
        tmp_path,
        "src/mcp_handlers/safe_patterns.py",
        _GR_HDR
        + """\
@mcp_tool("ok")
async def ok_handler(k: str):
    r = await get_redis()
    pipe = r.pipeline()
    pipe.multi()
    pipe.set(k, "1")
    await asyncio.wait_for(pipe.execute(), timeout=1)
    coro = r.get(k)
    await asyncio.wait_for(coro, timeout=1)
    await asyncio.wait_for(asyncio.gather(r.get("a"), r.get("b")), timeout=1)
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/mcp_handlers/cls_handler.py", "P004", 6),
        ("src/mcp_handlers/try_closure.py", "P004", 7),
    ]


def test_p004_module_scope_dispatch_and_async_nested_in_sync_factory(
    tmp_path: Path,
) -> None:
    # 1. Module-scope DISPATCH table or decorator argument referencing `_leaf` revokes caller-boundedness
    _write(
        tmp_path,
        "src/mcp_handlers/mod_escape.py",
        _GR_HDR
        + """\
async def _leaf(k):
    r = await get_redis()
    return await r.get(k)

async def warm(k):
    return await asyncio.wait_for(_leaf(k), timeout=1)

DISPATCH = {"leaf": _leaf}
""",
    )
    # 2. Async function nested inside a module-level sync factory is checked for P004
    _write(
        tmp_path,
        "src/mcp_handlers/sync_factory.py",
        """\
from src.cache.redis_client import get_redis

def make_handler():
    async def _inner(k: str):
        r = await get_redis()
        return await r.get(k)
    return _inner
""",
    )
    # 3. Sync helper nested inside an async handler returning raw `r.get(k)` coroutine is checked
    _write(
        tmp_path,
        "src/mcp_handlers/nested_sync_coro.py",
        _GR_HDR
        + """\
@mcp_tool("t")
async def h(k: str):
    r = await get_redis()
    def _mk():
        return r.get(k)
    return await _mk()
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/mcp_handlers/mod_escape.py", "P004", 7),
        ("src/mcp_handlers/nested_sync_coro.py", "P004", 9),
        ("src/mcp_handlers/sync_factory.py", "P004", 6),
    ]


def test_codex_review_regressions_escaped_closure_nested_params_and_dict_literal(tmp_path: Path) -> None:
    # 1. Escaped async closure returned from a wait_for-bounded factory does not make leaf bounded
    _write(
        tmp_path,
        "src/mcp_handlers/escaped_closure.py",
        """\
import asyncio
from src.cache.redis_client import get_redis

async def leaf():
    r = await get_redis()
    return await r.get("k")

async def factory():
    async def _closure():
        return await leaf()
    return _closure

async def caller():
    cb = await asyncio.wait_for(factory(), timeout=1.0)
    return await cb()
""",
    )
    # 2. Nested helper receiving raw Redis via renamed parameter is flagged when unguarded
    _write(
        tmp_path,
        "src/mcp_handlers/nested_param.py",
        """\
from src.cache.redis_client import get_redis

async def handler():
    r = await get_redis()
    async def use(client):
        return await client.get("k")
    return await use(r)
""",
    )
    # 3. Discarded dictionary literal containing create_task is flagged for P001
    _write(
        tmp_path,
        "src/discarded_dict.py",
        """\
import asyncio

async def spawn_lost(coro):
    {"task": asyncio.create_task(coro())}
""",
    )
    violations = guard.scan_paths(tmp_path)
    assert [(v.rel_path, v.rule, v.line) for v in violations] == [
        ("src/discarded_dict.py", "P001", 4),
        ("src/mcp_handlers/escaped_closure.py", "P004", 6),
        ("src/mcp_handlers/nested_param.py", "P004", 6),
    ]






