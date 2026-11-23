"""Tests for GitHub Actions workflow pruning in scripts/ops/unitares-automations."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def census_mod():
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations_gha", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules["unitares_automations_gha"] = module
    loader.exec_module(module)
    return module


def _write_workflow(repo: Path, filename: str = "ci.yml", name: str = "CI") -> None:
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    wf = repo / ".github" / "workflows" / filename
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(f"name: {name}\non: [push]\n")


def test_collect_github_actions_prunes_worktrees_security_drafts_and_deps(census_mod, tmp_path):
    projects = tmp_path / "projects"
    _write_workflow(projects / "real-repo", "test.yml", "Real CI")
    _write_workflow(projects / "nested" / "sub-repo", "lint.yaml", "Nested CI")
    # Nested directory inside real-repo must not be walked after .git is found.
    _write_workflow(projects / "real-repo" / "vendor" / "embedded", "hidden.yml", "Embedded CI")
    # Excluded directories must be pruned.
    for excluded in (
        projects / "wt" / "feature-branch",
        projects / "_security" / "audit-clone",
        projects / "_drafts" / "scratch",
        projects / ".ref" / "dep",
        projects / "node_modules" / "pkg",
        projects / ".venv" / "pkg",
        projects / "_archive" / "old-repo",
    ):
        _write_workflow(excluded, "noise.yml", "Noise CI")

    warnings: list[str] = []
    items = census_mod.collect_github_actions(projects, warnings)
    assert warnings == []
    assert sorted(item.name for item in items) == ["Nested CI", "Real CI"]


def test_collect_github_actions_discovers_repos_named_like_build_dirs_and_prunes_non_repos(
    census_mod, tmp_path,
):
    projects = tmp_path / "projects"
    # Real git repositories whose names happen to match common build directory names
    # must be discovered because they contain a `.git` entry.
    for repo_name in ("deps", "build", "dist", "target", "cache", "_build"):
        _write_workflow(projects / repo_name, "ci.yml", f"{repo_name} CI")

    # Non-repo build/dependency containers (no `.git` on the container itself)
    # must still be pruned so nested vendored/cloned repos do not pollute the census.
    for pruned in (
        projects / "workspace" / "build" / "nested-repo",
        projects / "workspace" / "deps" / "pkg",
        projects / "workspace" / "dist" / "artifact-repo",
        projects / "workspace" / "target" / "sub-repo",
        projects / "workspace" / "cache" / "cached-repo",
        projects / "workspace" / "_build" / "elixir-dep",
    ):
        _write_workflow(pruned, "noise.yml", "Pruned CI")

    # Always-excluded containers remain pruned even if they have a `.git` directory.
    _write_workflow(projects / "wt", "noise.yml", "Worktree Root CI")

    warnings: list[str] = []
    items = census_mod.collect_github_actions(projects, warnings)
    assert warnings == []
    assert sorted(item.name for item in items) == sorted(
        f"{name} CI" for name in ("deps", "build", "dist", "target", "cache", "_build")
    )
