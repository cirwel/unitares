"""Hot-path safety invariants for the governance check-in (update_dynamics).

Two regressions are locked here, both born from the 2026-06-16 fleet-wide
check-in outage (#800/#803):

1. **Init-prologue invariant (lint).** Every ``self.X`` attribute that
   ``UNITARESMonitor._initialize_fresh_state`` sets must ALSO be set
   unconditionally in ``__init__`` (outside the ``if load_state:`` branch).
   The persisted-state load path does NOT call ``_initialize_fresh_state``, so
   an attribute set only there is absent on every established agent — exactly
   how #803 reached ``update_dynamics`` with a missing ``_sensor_divergence_*``
   attribute and rejected the check-in. This test fails the moment a new
   attribute is added to the fresh-state helper without also being added to the
   prologue.

2. **Fail-open telemetry (behaviour).** ``_record_sensor_divergence`` is
   optional telemetry on the mandatory check-in path; a fault inside it must
   degrade to "no record this cycle", never raise.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

import src.governance_monitor as gm

MONITOR_SRC = Path(gm.__file__)


# ── 1. Init-prologue invariant ───────────────────────────────────────────────

def _self_attrs_assigned(func_node: ast.FunctionDef, *, exclude_subtree=None) -> set[str]:
    """Names X for every `self.X = ...` (Assign/AnnAssign) under func_node.

    ``exclude_subtree`` is an AST node whose descendants are skipped — used to
    drop the ``if load_state:`` block so we only see the unconditional prologue.
    """
    excluded = set()
    if exclude_subtree is not None:
        excluded = {id(n) for n in ast.walk(exclude_subtree)}

    found: set[str] = set()
    for node in ast.walk(func_node):
        if id(node) in excluded:
            continue
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            # self.NAME = ...  (plain attribute on self; not self.x.y = ...)
            if (
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
            ):
                found.add(t.attr)
    return found


def _find_method(cls_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for n in cls_node.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"method {name!r} not found on {cls_node.name}")


@pytest.fixture(scope="module")
def monitor_ast():
    tree = ast.parse(MONITOR_SRC.read_text())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "UNITARESMonitor"
    )
    return cls


def test_fresh_state_attrs_are_all_set_in_init_prologue(monitor_ast):
    init = _find_method(monitor_ast, "__init__")
    fresh = _find_method(monitor_ast, "_initialize_fresh_state")

    # The load branch inside __init__ that the persisted path takes instead of
    # _initialize_fresh_state(). Attributes set only inside it do NOT count as
    # part of the unconditional prologue.
    load_branch = next(
        (n for n in init.body
         if isinstance(n, ast.If)
         and "load_state" in {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}),
        None,
    )
    assert load_branch is not None, "expected an `if load_state:` block in __init__"

    prologue_attrs = _self_attrs_assigned(init, exclude_subtree=load_branch)
    fresh_attrs = _self_attrs_assigned(fresh)

    missing = fresh_attrs - prologue_attrs
    assert not missing, (
        "These self attributes are set in _initialize_fresh_state but NOT in the "
        "unconditional __init__ prologue, so the persisted-state load path "
        f"(which skips _initialize_fresh_state) leaves them unset: {sorted(missing)}. "
        "Set them in __init__ before the `if load_state:` branch (the #803 rule)."
    )


def test_known_hotpath_attrs_are_in_the_prologue(monitor_ast):
    """Belt-and-suspenders: the specific attrs from the #803 incident."""
    init = _find_method(monitor_ast, "__init__")
    load_branch = next(
        n for n in init.body
        if isinstance(n, ast.If)
        and "load_state" in {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
    )
    prologue = _self_attrs_assigned(init, exclude_subtree=load_branch)
    for attr in ("_last_sensor_divergence", "_sensor_divergence_history", "created_at"):
        assert attr in prologue, f"{attr} must be set in the __init__ prologue (#803)"


# ── 2. Fail-open telemetry behaviour ─────────────────────────────────────────

@pytest.fixture()
def monitor():
    # load_state=False: in-process, no DB, fresh state. Cheap.
    return gm.UNITARESMonitor("test-hotpath-agent", load_state=False)


def test_record_sensor_divergence_none_is_noop(monitor):
    before = len(monitor._sensor_divergence_history)
    monitor._record_sensor_divergence(None)
    assert monitor._last_sensor_divergence is None
    assert len(monitor._sensor_divergence_history) == before


def test_record_sensor_divergence_happy_path(monitor):
    before = len(monitor._sensor_divergence_history)
    # Divergence of the current ODE state against itself: a valid, ~zero record.
    monitor._record_sensor_divergence(monitor.state.unitaires_state)
    assert isinstance(monitor._last_sensor_divergence, dict)
    assert "magnitude" in monitor._last_sensor_divergence
    assert len(monitor._sensor_divergence_history) == before + 1


def test_record_sensor_divergence_fails_open(monitor, monkeypatch):
    """A fault in the telemetry must NOT propagate — the check-in must survive."""
    def boom(*_a, **_k):
        raise RuntimeError("simulated eisv_divergence fault")

    monkeypatch.setattr(gm, "eisv_divergence", boom)
    before = len(monitor._sensor_divergence_history)

    # Must not raise.
    monitor._record_sensor_divergence(object())

    assert monitor._last_sensor_divergence is None
    assert len(monitor._sensor_divergence_history) == before


def test_record_sensor_divergence_survives_missing_history_deque(monitor, monkeypatch):
    """Self-heal: a monitor that lost its deque (old pickle/cache) still records."""
    monkeypatch.setattr(
        gm, "eisv_divergence",
        lambda *_a, **_k: {"magnitude": 0.0, "dE": 0.0, "dI": 0.0, "dS": 0.0, "dV": 0.0},
    )
    del monitor._sensor_divergence_history
    monitor._record_sensor_divergence(object())
    assert isinstance(monitor._sensor_divergence_history, deque)
    assert len(monitor._sensor_divergence_history) == 1
    assert monitor._last_sensor_divergence is not None
