"""Tests for `scripts/ops/prepare_release.py` and release sync remediation hints."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts.ci import sdk_release_sync, server_release_sync
from scripts.ops import prepare_release


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_resolve_projects_root_handles_main_checkout_and_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("UNITARES_PROJECTS_ROOT", raising=False)
    projects = tmp_path / "projects"
    main_repo = projects / "unitares"
    main_repo.mkdir(parents=True)
    _git(main_repo, "init", "-q", "-b", "master")
    _git(main_repo, "config", "user.email", "test@example.com")
    _git(main_repo, "config", "user.name", "Test")
    (main_repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _git(main_repo, "add", "VERSION")
    _git(main_repo, "commit", "-q", "-m", "initial")

    wt_repo = projects / "wt" / "unitares-release"
    _git(main_repo, "worktree", "add", "-q", "-b", "release-test", str(wt_repo))

    assert prepare_release.resolve_projects_root(main_repo) == projects.resolve()
    assert prepare_release.resolve_projects_root(wt_repo) == projects.resolve()

    override = tmp_path / "custom-projects"
    monkeypatch.setenv("UNITARES_PROJECTS_ROOT", str(override))
    assert prepare_release.resolve_projects_root(wt_repo) == override.resolve()


def test_stamp_changelog_and_update_citation():
    changelog = (
        "# Changelog\n\n---\n\n## [Unreleased]\n\n### Added\n- **x:** new (#10).\n\n"
        "---\n\n## [1.0.0] - 2026-01-01\n\n### Fixed\n- **y:** old (#1).\n"
    )
    stamped = prepare_release.stamp_changelog_release(changelog, "1.0.1", "2026-10-25")
    assert "## [Unreleased]\n\n---\n\n## [1.0.1] - 2026-10-25\n\n### Added\n- **x:** new (#10)." in stamped
    section = prepare_release.extract_changelog_section(stamped, "1.0.1")
    assert "### Added\n- **x:** new (#10)." in section

    citation = 'cff-version: 1.2.0\nversion: "1.0.1"\ndate-released: "2026-01-01"\n'
    updated = prepare_release.update_citation_date(citation, "2026-10-25")
    assert 'date-released: "2026-10-25"' in updated


def test_run_skill_freshness_fails_closed_on_absent_cited_source(tmp_path: Path):
    script = tmp_path / "scripts" / "client" / "check-skill-freshness.sh"
    script.parent.mkdir(parents=True)
    # Exits 0 but prints the absent-source warning that happens when UNITARES_PROJECTS_ROOT is wrong.
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "FRESH governance-lifecycle (1 cited source(s) absent from this checkout, not covered)"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(RuntimeError, match="absent cited sources"):
        prepare_release.run_skill_freshness(tmp_path, tmp_path.parent, prune_keep=None)

    script.unlink()
    with pytest.raises(FileNotFoundError, match="Required skill freshness script is missing"):
        prepare_release.run_skill_freshness(tmp_path, tmp_path.parent, prune_keep=None)


def test_check_ghcr_visibility_distinguishes_public_private_and_missing():
    responses = {
        ("unitares", "v3.5.0"): 200,
        ("unitares-lease-plane", "v3.5.0"): 403,
        ("unitares-postgres", "v3.5.0"): 404,
    }
    ok, results = prepare_release.check_ghcr_visibility(
        "3.5.0",
        status_fetcher=lambda pkg, tag: responses[(pkg, tag)],
    )
    assert not ok
    by_pkg = {r["package"]: r for r in results}
    assert by_pkg["unitares"]["state"] == "public"
    assert by_pkg["unitares-lease-plane"]["state"] == "private"
    assert "packages/container/unitares-lease-plane/settings" in by_pkg["unitares-lease-plane"]["detail"]
    assert by_pkg["unitares-postgres"]["state"] == "missing"

    ok_all, _ = prepare_release.check_ghcr_visibility(
        "v3.5.0",
        status_fetcher=lambda _pkg, _tag: 200,
    )
    assert ok_all


def test_format_after_merge_renders_copy_paste_sequence():
    card = prepare_release.format_after_merge("3.5.1", "deadbeef1234", Path("/tmp/notes.md"))
    assert 'git tag -s v3.5.1 deadbeef1234 -m "UNITARES v3.5.1"' in card
    assert "unitares-github -- git push origin v3.5.1" in card
    assert "unitares-github -- gh release create v3.5.1" in card
    assert "python3 scripts/ops/prepare_release.py --check-ghcr --version 3.5.1" in card
    assert "unitares-github -- gh workflow run promote-release.yml -f tag=v3.5.1" in card
    assert "--head publish/v3.5.1" in card
    assert "UNITARES agent:" in card


def test_prepare_release_dry_run_in_temp_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # Copy required scripts and version_manager target files into temp repo
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "scripts/dev/changelog_assemble.py",
        "scripts/ci/changelog_coverage.py",
        "scripts/ci/release_series_drift.py",
        "scripts/ops/version_manager.py",
        "pyproject.toml",
        "docker-compose.yml",
        "src/version.py",
        "src/interface_contract.py",
        "agents/sdk/pyproject.toml",
        "agents/sdk/README.md",
        "docs/COMPATIBILITY.md",
        "docs/operations/OPERATOR_RUNBOOK.md",
        "docs/public-site/index.md",
    ):
        src = root / rel
        if src.exists():
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    current_ver = (root / "VERSION").read_text(encoding="utf-8").strip()
    freshness_stub = repo / "scripts" / "client" / "check-skill-freshness.sh"
    freshness_stub.parent.mkdir(parents=True, exist_ok=True)
    freshness_stub.write_text("#!/usr/bin/env bash\necho '[FRESH] all skills'\nexit 0\n", encoding="utf-8")
    freshness_stub.chmod(freshness_stub.stat().st_mode | stat.S_IEXEC)
    (repo / "VERSION").write_text(f"{current_ver}\n", encoding="utf-8")
    (repo / "PUBLISHED_VERSION").write_text(f"{current_ver}\n", encoding="utf-8")
    (repo / "CITATION.cff").write_text(
        f'cff-version: 1.2.0\nversion: "{current_ver}"\ndate-released: "2026-01-01"\n',
        encoding="utf-8",
    )
    (repo / "docs" / "CHANGELOG.md").write_text(
        f"# Changelog\n\n---\n\n## [Unreleased]\n\n---\n\n## [{current_ver}] - 2026-01-01\n\n### Added\n- Initial (#1).\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "chore: seed (#1)")
    _git(repo, "tag", f"v{current_ver}")
    sdk_ver = prepare_release.read_component_versions(repo)["sdk"]
    assert sdk_ver != "unknown"
    _git(repo, "tag", f"sdk-v{sdk_ver}")

    # Add an uncited commit and a Dockerfile touch so both uncited list and quickstart gate trigger
    (repo / "Dockerfile").write_text("FROM python:3.14-slim\n", encoding="utf-8")
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-q", "-m", "feat: update container (#10)")

    out_dir = tmp_path / "out"
    summary = prepare_release.prepare_release(
        repo_root=repo,
        part="patch",
        release_date="2026-10-25",
        output_dir=out_dir,
        dry_run=True,
    )
    assert summary["from_version"] == current_ver
    assert summary["quickstart_gate"] == "NEEDS-QUICKSTART"
    assert not summary["coverage_ok"]
    assert any("#10" in line for line in summary["uncited_prs"])
    assert summary["uncited_prs_file"] is not None
    assert Path(summary["uncited_prs_file"]).exists()
    assert Path(summary["release_notes_file"]).exists()
    notes_text = Path(summary["release_notes_file"]).read_text(encoding="utf-8")
    assert f"`unitares-sdk`: `{sdk_ver}`" in notes_text
    assert Path(summary["pr_body_file"]).exists()
    pr_body = Path(summary["pr_body_file"]).read_text(encoding="utf-8")
    assert "NEEDS-QUICKSTART" in pr_body
    assert "1 uncited PR(s)" in pr_body
    # Dry-run restores the working tree
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == current_ver


def test_release_sync_issues_include_next_commands():
    server_report = server_release_sync.assess(
        source="3.5.0",
        published="3.4.0",
        tagged={"3.4.0", "3.5.0"},
        on_head={"3.4.0", "3.5.0"},
        released={"3.4.0", "3.5.0"},
        digests={"latest": "sha256:aaa", "v3.4.0": "sha256:aaa", "v3.5.0": "sha256:bbb"},
    )
    issue = server_report["issues"][0]
    assert issue["code"] == "release_awaiting_publication"
    assert "promote-release.yml -f tag=v3.5.0" in issue["next_command"]
    assert "promote-release.yml -f tag=v3.5.0" in issue["detail"]

    sdk_report = sdk_release_sync.assess(
        declared="0.6.1",
        published="0.6.0",
        tagged={"0.6.0"},
        advertised={"docs/COMPATIBILITY.md": "0.6.0"},
    )
    by_code = {i["code"]: i for i in sdk_report["issues"]}
    assert "sdk-v0.6.1" in by_code["declared_tag_missing"]["next_command"]
    assert "sdk-v0.6.1" in by_code["declared_registry_mismatch"]["detail"]
