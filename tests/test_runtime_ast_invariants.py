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
    # --files mode checks explicit files directly without baseline exemptions
    assert guard.main(["--root", str(tmp_path), "--files", str(f)]) == 1

