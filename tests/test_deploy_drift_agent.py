"""Tests for the governed deploy-drift runner.

Deliberately does NOT onboard. Minting a governance identity is real fleet
state; these tests exercise the cycle-reporting contract with the SDK's
identity layer stubbed out.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = REPO_ROOT / "scripts" / "ops" / "deploy_drift_agent.py"


@pytest.fixture
def agent_mod(monkeypatch):
    """Import the runner with a stub SDK so no server or identity is needed."""
    captured: dict = {}

    class CycleResult:
        def __init__(self, summary, complexity=0.3, confidence=0.7, **kw):
            self.summary = summary
            self.complexity = complexity
            self.confidence = confidence

        @classmethod
        def simple(cls, summary):
            return cls(summary)

    class GovernanceAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.agent_uuid = "stub-uuid"

    stub = types.ModuleType("unitares_sdk.agent")
    stub.CycleResult = CycleResult
    stub.GovernanceAgent = GovernanceAgent
    pkg = types.ModuleType("unitares_sdk")
    pkg.agent = stub
    monkeypatch.setitem(sys.modules, "unitares_sdk", pkg)
    monkeypatch.setitem(sys.modules, "unitares_sdk.agent", stub)

    spec = importlib.util.spec_from_file_location("deploy_drift_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod._captured = captured  # type: ignore[attr-defined]
    return mod


def test_registers_as_persistent_with_resident_tags(agent_mod):
    """persistent=True stamps the full RESIDENT_TAGS set. A resident stamped
    'persistent' but not 'autonomous' is exactly the 2026-04-20 Steward
    regression, where loop-detection silently starved every write."""
    cls = agent_mod._build_agent_cls()
    cls()
    assert agent_mod._captured["persistent"] is True
    assert agent_mod._captured["name"] == "deploy-drift-doctor"


def test_reports_clean_fleet_honestly(agent_mod, monkeypatch):
    cls = agent_mod._build_agent_cls()
    agent = cls()
    monkeypatch.setattr(agent_mod, "diagnose", lambda s, io: [])
    monkeypatch.setattr(agent_mod.Doctor, "run", lambda self: 0)
    monkeypatch.setattr(agent_mod.os.path, "isdir", lambda p: True)
    res = asyncio.run(agent.run_cycle(None))
    assert "running merged code" in res.summary
    assert res.confidence == 0.85


def test_reports_drift_in_checkin(agent_mod, monkeypatch):
    """The check-in must state what was actually found, not a generic pulse."""
    class D:
        surface, condition = "live-thing", "behind_origin"
    cls = agent_mod._build_agent_cls()
    agent = cls()
    monkeypatch.setattr(agent_mod, "diagnose", lambda s, io: [D()])
    monkeypatch.setattr(agent_mod.Doctor, "run", lambda self: 0)
    monkeypatch.setattr(agent_mod.os.path, "isdir", lambda p: True)
    res = asyncio.run(agent.run_cycle(None))
    assert "drift condition" in res.summary
    assert "live-thing:behind_origin" in res.summary


def test_unreadable_fleet_lowers_confidence_not_a_clean_report(agent_mod, monkeypatch):
    """A cycle that could observe nothing must not report a clean fleet —
    that would be claiming a result never measured."""
    cls = agent_mod._build_agent_cls()
    agent = cls()
    monkeypatch.setattr(agent_mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(agent_mod.Doctor, "run", lambda self: 0)
    res = asyncio.run(agent.run_cycle(None))
    assert "no surfaces readable" in res.summary
    assert res.confidence == 0.2


def test_identity_flows_into_doctor_for_outcome_emission(agent_mod, monkeypatch):
    """The whole point: resolutions must carry the agent's UUID so
    outcome_event snapshots its EISV."""
    monkeypatch.delenv("DEPLOY_DRIFT_DOCTOR_UUID", raising=False)
    cls = agent_mod._build_agent_cls()
    agent = cls()
    monkeypatch.setattr(agent_mod, "diagnose", lambda s, io: [])
    monkeypatch.setattr(agent_mod.Doctor, "run", lambda self: 0)
    monkeypatch.setattr(agent_mod.os.path, "isdir", lambda p: True)
    asyncio.run(agent.run_cycle(None))
    assert agent_mod.os.environ["DEPLOY_DRIFT_DOCTOR_UUID"] == "stub-uuid"


def test_does_not_join_the_resident_roster():
    """Baselined identity and resident-roster membership are separate. Adding
    this to KNOWN_RESIDENT_LABELS would silently change 'N of 6 residents
    reporting' on the dashboard.

    Checked via AST, not grep: both names appear in the module docstring
    explaining that they are deliberately NOT used, so a text search reports a
    false positive on its own documentation.
    """
    import ast

    tree = ast.parse(AGENT_PATH.read_text())
    referenced = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    } | {
        c.value for c in ast.walk(tree)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
        and c is not ast.get_docstring(tree, clean=False)
        and c.value in {"UNITARES_RESIDENTS", "KNOWN_RESIDENT_LABELS"}
    }
    assert "KNOWN_RESIDENT_LABELS" not in referenced
    assert "UNITARES_RESIDENTS" not in referenced
