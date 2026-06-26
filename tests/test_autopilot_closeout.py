"""Tests for scripts/dev/autopilot_closeout.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def autopilot_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "dev" / "autopilot_closeout.py"
    spec = importlib.util.spec_from_file_location("autopilot_closeout", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["autopilot_closeout"] = module
    spec.loader.exec_module(module)
    return module


def _git_state(module, *, entries: list[str], delivery: str = "local_changes"):
    return module.closeout_lib.GitState(
        branch="codex/test",
        dirty=bool(entries),
        entries=entries,
        staged=[],
        unstaged=[module.porcelain_path(entry) for entry in entries],
        untracked=[],
        head="abc123",
        upstream="origin/codex/test",
        ahead=0,
        behind=0,
        delivery_status=delivery,
        delivery_detail="test detail",
    )


def _closeout(module, state):
    return module.closeout_lib.CloseoutResult(
        workspace="/repo",
        git=state,
        baseline_path=None,
        baseline_used=False,
        stashed=False,
        stash_message=None,
        repo_processes=[],
        expected_repo_processes=[],
        stopped_processes=[],
        booted_out_labels=[],
        errors=[],
        branch_hygiene=None,
    )


def _probe(module, name: str, status: str = "ok", detail: str = "ok"):
    return module.CommandProbe(
        name=name,
        status=status,
        detail=detail,
        command=[],
    )


def test_auto_test_policy_skips_docs_only_changes(autopilot_module):
    state = _git_state(
        autopilot_module,
        entries=[" M docs/operations/runbook.md", " M commands/closeout.md"],
    )

    command = autopilot_module.test_command_for_policy("auto", state)

    assert command is None


def test_auto_test_policy_runs_for_code_changes(autopilot_module):
    state = _git_state(
        autopilot_module,
        entries=[" M scripts/dev/autopilot_closeout.py"],
    )

    command = autopilot_module.test_command_for_policy("auto", state)

    assert command == ["./scripts/dev/test-cache.sh"]


def test_policy_marks_local_changes_as_human_required(autopilot_module):
    state = _git_state(autopilot_module, entries=[" M src/foo.py"])
    result = autopilot_module.policy_from_result(
        closeout=_closeout(autopilot_module, state),
        watcher=_probe(autopilot_module, "watcher"),
        tests=_probe(autopilot_module, "test-cache", status="skipped"),
        ship_plan=_probe(autopilot_module, "ship-plan", status="skipped"),
        ship=_probe(autopilot_module, "ship", status="skipped"),
        ship_requested=False,
    )

    assert result.policy == "needs_human"
    assert "local changes are not committed" in result.human_required[0]


def test_policy_marks_failed_test_as_blocked(autopilot_module):
    state = _git_state(autopilot_module, entries=[], delivery="pushed_branch")
    result = autopilot_module.policy_from_result(
        closeout=_closeout(autopilot_module, state),
        watcher=_probe(autopilot_module, "watcher"),
        tests=_probe(
            autopilot_module,
            "test-cache",
            status="failed",
            detail="exit 1",
        ),
        ship_plan=_probe(autopilot_module, "ship-plan", status="skipped"),
        ship=_probe(autopilot_module, "ship", status="skipped"),
        ship_requested=False,
    )

    assert result.policy == "blocked"
    assert result.blockers == ["test-cache failed: exit 1"]


def test_ship_preflight_blocks_on_failed_tests(autopilot_module):
    state = _git_state(autopilot_module, entries=[" M src/foo.py"])

    blockers = autopilot_module.ship_preflight_blockers(
        watcher=_probe(autopilot_module, "watcher"),
        tests=_probe(autopilot_module, "test-cache", status="failed", detail="exit 1"),
        closeout=_closeout(autopilot_module, state),
    )

    assert blockers == ["tests failed (exit 1)"]


def test_run_watcher_reports_attention_for_unresolved_lines(
    autopilot_module,
    monkeypatch,
    tmp_path,
):
    watcher = tmp_path / "agents" / "watcher" / "agent.py"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("# placeholder\n")

    def fake_run_cmd(args, cwd, *, timeout):
        return autopilot_module.CommandProbe(
            name="agent.py",
            status="ok",
            detail="exit 0",
            command=args,
            returncode=0,
            stdout="P001 unresolved\n",
        )

    monkeypatch.setattr(autopilot_module, "run_cmd", fake_run_cmd)

    probe = autopilot_module.run_watcher(tmp_path, "print")

    assert probe.status == "attention"
    assert probe.detail == "reported 1 unresolved finding(s)"


def test_run_watcher_uses_total_count_instead_of_header_lines(
    autopilot_module,
    monkeypatch,
    tmp_path,
):
    watcher = tmp_path / "agents" / "watcher" / "agent.py"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("# placeholder\n")

    def fake_run_cmd(args, cwd, *, timeout):
        return autopilot_module.CommandProbe(
            name="agent.py",
            status="ok",
            detail="exit 0",
            command=args,
            returncode=0,
            stdout=(
                "<unitares-watcher-findings>\n"
                "Total unresolved: 0 (showing 0)\n"
                "Plus 1 finding(s) in other worktrees (src=1)\n"
                "</unitares-watcher-findings>\n"
            ),
        )

    monkeypatch.setattr(autopilot_module, "run_cmd", fake_run_cmd)

    probe = autopilot_module.run_watcher(tmp_path, "print")

    assert probe.status == "ok"
    assert (
        probe.detail
        == "no current-worktree unresolved findings; "
        "1 other-worktree finding(s) listed"
    )


def test_render_text_includes_policy_and_delivery(autopilot_module):
    state = _git_state(autopilot_module, entries=[" M src/foo.py"])
    closeout = _closeout(autopilot_module, state)
    policy = autopilot_module.policy_from_result(
        closeout=closeout,
        watcher=_probe(autopilot_module, "watcher"),
        tests=_probe(autopilot_module, "test-cache", status="skipped"),
        ship_plan=_probe(autopilot_module, "ship-plan", status="skipped"),
        ship=_probe(autopilot_module, "ship", status="skipped"),
        ship_requested=False,
    )
    result = autopilot_module.AutopilotResult(
        workspace="/repo",
        watcher=_probe(autopilot_module, "watcher"),
        tests=_probe(autopilot_module, "test-cache", status="skipped"),
        ship_plan=_probe(autopilot_module, "ship-plan", status="skipped"),
        ship=_probe(autopilot_module, "ship", status="skipped"),
        closeout=closeout,
        policy=policy,
    )

    text = autopilot_module.render_text(result)

    assert "policy: needs_human" in text
    assert "delivery: local_changes" in text
