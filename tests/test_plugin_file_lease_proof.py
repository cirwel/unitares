"""Contract tests for the plugin file-lease CI proof.

``scripts/ci/plugin_file_lease_proof.py`` runs only in the Docker Quickstart
job, against a live Compose stack. These tests pin the parts that keep that
proof honest without a stack: the plugin is pinned to one commit, the job
re-runs when the script changes, and the hooks run fail-closed so an
unreachable lease plane cannot pass as an acquire.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/ci/plugin_file_lease_proof.py"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "docker-quickstart.yml"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location(
        "plugin_file_lease_proof", PROJECT_ROOT / SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow):
    return workflow["jobs"]["quickstart"]["steps"]


def test_plugin_checkout_is_pinned_to_a_commit(workflow):
    checkouts = [
        step for step in _steps(workflow)
        if (step.get("with") or {}).get("repository") == "cirwel/unitares-governance-plugin"
    ]
    assert len(checkouts) == 1
    ref = str(checkouts[0]["with"]["ref"])
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"plugin ref is not a commit SHA: {ref!r}"
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_proof_runs_after_the_coordination_demo(workflow):
    runs = [str(step.get("run", "")) for step in _steps(workflow)]
    demo = runs.index("make coordination-demo")
    proof = next(i for i, run in enumerate(runs) if SCRIPT in run)
    assert proof > demo
    env = _steps(workflow)[proof]["env"]
    assert env["LEASE_PLANE_BASE_URL"] == "http://127.0.0.1:8788"
    assert env["UNITARES_SECRETS_ENV"] == "/dev/null"


def test_script_change_triggers_the_job(workflow):
    # PyYAML reads the bare `on:` key as boolean True.
    triggers = workflow.get("on") or workflow[True]
    for event in ("push", "pull_request"):
        assert SCRIPT in triggers[event]["paths"], event


def test_hooks_run_fail_closed_even_with_inherited_opt_out(mod):
    env = mod.hook_env(
        {
            "UNITARES_FILE_LEASES_REQUIRED": "0",
            "UNITARES_FILE_LEASES_ENABLED": "0",
            "EVAL_UNITARES_OFFLINE": "1",
        }
    )
    assert env["UNITARES_FILE_LEASES_REQUIRED"] == "1"
    assert env["UNITARES_FILE_LEASES_ENABLED"] == "1"
    assert "EVAL_UNITARES_OFFLINE" not in env


def test_edit_payload_is_a_claude_edit_event(mod, tmp_path):
    payload = json.loads(
        mod.edit_payload("s1", "toolu_1", tmp_path / "f.txt", "PreToolUse")
    )
    assert payload["tool_name"] == "Edit"
    assert payload["session_id"] == "s1"
    assert payload["tool_use_id"] == "toolu_1"
    assert payload["tool_input"]["file_path"] == str(tmp_path / "f.txt")


def test_workspace_has_two_worktrees_of_one_repository(mod, tmp_path):
    main, linked = mod.build_workspace(tmp_path / "w")
    assert (main / mod.FILE_NAME).is_file()
    assert (linked / mod.FILE_NAME).is_file()
    assert (main / ".git").is_dir()
    assert (linked / ".git").is_file()  # a linked worktree's .git is a pointer
    with pytest.raises(mod.ProofError):
        mod.build_workspace(tmp_path / "w")
