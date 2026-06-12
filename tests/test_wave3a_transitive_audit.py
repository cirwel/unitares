"""Wave 3a §6 Q1 transitive-closure audit — gate tests.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §6 Q1.
The audit script (``scripts/dev/wave3a_transitive_audit.py``) was named as
a PR #1 deliverable and re-runs as a gate on each handler PR; these tests
make the gate CI-enforced:

* The SHIPPED handlers (``health_check`` PR #5, ``get_server_info`` PR #6)
  must stay CLEAR — a regression that introduces first-call global-state
  mutation into their transitive closure fails CI, not a code review.
* The auditor itself must actually catch the FIND-R2 lazy-init pattern
  (synthetic-module test) — guards against the audit rotting into a
  always-green stamp.
* ``list_tools`` / ``describe_tool`` currently DO NOT clear (known flags
  pinned below). PR #7 is gated on these being dispositioned; if a change
  legitimately clears them, update this pin consciously — that IS the
  gate opening.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "dev" / "wave3a_transitive_audit.py"


def _run_audit(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_shipped_handlers_clear():
    """PR #5 + PR #6 handlers must stay mutation-free in their closure."""
    proc = _run_audit("health_check", "get_server_info")
    assert proc.returncode == 0, (
        "Q1 audit no longer clears the SHIPPED Wave 3a handlers — a "
        "first-call global-state mutation entered their transitive "
        f"closure.\n{proc.stdout}\n{proc.stderr}"
    )
    assert proc.stdout.count("CLEAR") == 2


def test_synthetic_lazy_init_is_flagged(tmp_path):
    """The auditor catches the FIND-R2 lazy-init signature."""
    mod = tmp_path / "lazy_mod.py"
    mod.write_text(
        textwrap.dedent(
            """
            _CACHE = None

            def get_cached():
                global _CACHE
                if _CACHE is None:
                    _CACHE = {"built": "on first call"}
                return _CACHE
            """
        )
    )
    proc = _run_audit("--function", f"{mod}::get_cached")
    assert proc.returncode == 1, (
        f"auditor failed to flag a textbook lazy-init:\n{proc.stdout}"
    )
    assert "global-statement" in proc.stdout


def test_synthetic_module_state_write_is_flagged(tmp_path):
    """Subscript writes to module-level dicts are flagged without `global`."""
    mod = tmp_path / "mutating_mod.py"
    mod.write_text(
        textwrap.dedent(
            """
            _REGISTRY = {}

            def remember(key, value):
                _REGISTRY[key] = value
                return _REGISTRY
            """
        )
    )
    proc = _run_audit("--function", f"{mod}::remember")
    assert proc.returncode == 1
    assert "module-state-write" in proc.stdout


def test_synthetic_pure_reader_clears(tmp_path):
    """Read-only access to module state must NOT be flagged."""
    mod = tmp_path / "reader_mod.py"
    mod.write_text(
        textwrap.dedent(
            """
            _TABLE = {"a": 1}

            def lookup(key):
                local = dict(_TABLE)
                local["b"] = 2  # local mutation is fine
                return _TABLE.get(key), local
            """
        )
    )
    proc = _run_audit("--function", f"{mod}::lookup")
    assert proc.returncode == 0, proc.stdout
    assert "CLEAR" in proc.stdout


def test_list_tools_known_flags_pinned():
    """PR #7 gate: list_tools does NOT currently clear — two known sites.

    ``list_all_aliases`` (the FIND-R2 named suspect) is NOT among them —
    it populates at import time, which the audit correctly does not flag.
    The actual blockers found 2026-06-11:

    * ``src/tool_schemas.py::get_pydantic_schemas`` — lazy schema cache.
    * ``src/tool_usage_tracker.py::get_tool_usage_tracker`` — lazy
      singleton; list_tools also ORDERS its response by mutable usage
      stats, so this one is a real cold-vs-warm divergence, not just a
      deterministic cache.

    If this test starts failing because the audit CLEARS, that is the PR
    #7 gate opening — update the pin deliberately and record the
    disposition in the Wave 3a RFC.
    """
    proc = _run_audit("list_tools")
    assert proc.returncode == 1, (
        "list_tools audit cleared — the PR #7 gate condition changed; "
        f"disposition this consciously.\n{proc.stdout}"
    )
    assert "tool_schemas.py::get_pydantic_schemas" in proc.stdout
    assert "tool_usage_tracker.py::get_tool_usage_tracker" in proc.stdout
