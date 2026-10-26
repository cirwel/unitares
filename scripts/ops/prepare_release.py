#!/usr/bin/env python3
"""Automate mechanical server release preparation and post-merge operator/agent guidance.

Modes:
  1. Prepare release branch files (default):
       python3 scripts/ops/prepare_release.py --part patch
     Folds `docs/changelog.d/` fragments, bumps `VERSION`, propagates across all
     version targets, stamps `## [<version>] - <YYYY-MM-DD>` in `docs/CHANGELOG.md`
     and `date-released` in `CITATION.cff`, prunes skill attestation history,
     checks skill freshness with `UNITARES_PROJECTS_ROOT` resolved across
     worktrees (failing closed on `"cited source(s) absent"`), runs release
     seam gates, and writes draft release notes, PR body, and any uncited PR
     candidates outside the tracked tree.

  2. Post-merge command card (`--after-merge`):
       python3 scripts/ops/prepare_release.py --after-merge [--sha <sha>] [--version <X.Y.Z>]
     Prints copy-paste commands for the operator and releasing agent with the
     merged release commit SHA and version filled in.

  3. Anonymous GHCR visibility check (`--check-ghcr`):
       python3 scripts/ops/prepare_release.py --check-ghcr [--version <X.Y.Z>]
     Verifies that `unitares`, `unitares-lease-plane`, and `unitares-postgres`
     at `v<X.Y.Z>` can be pulled anonymously from GHCR (`public` vs `private`
     vs `missing`) before `promote-release.yml` is dispatched.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[2]

GHCR_OWNER = "cirwel"
GHCR_PACKAGES = (
    "unitares",
    "unitares-lease-plane",
    "unitares-postgres",
)
DOCKER_GATE_PATHS = (
    "Dockerfile",
    "requirements*.txt",
    "constraints.txt",
    "docker-compose.yml",
    "db/postgres",
    "elixir/lease_plane",
    "elixir/unitares_sdk",
    "scripts/demo/quick_demo.py",
    "scripts/demo/coordination_demo.py",
    "scripts/ci/check_mcp_tool_surface.py",
    "scripts/ci/plugin_file_lease_proof.py",
    "scripts/ci/kg_graph_proof.py",
    "docker-compose.full.yml",
    ".github/workflows/docker-quickstart.yml",
)
ABSENT_SOURCE_MARKER = "cited source(s) absent"

UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
RELEASE_HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?[ \t]*$", re.MULTILINE)
CITATION_DATE_RE = re.compile(r'^(date-released:\s*)"[^"]*"', re.MULTILINE)
INTERFACE_VERSION_RE = re.compile(r'^INTERFACE_CONTRACT_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def resolve_projects_root(repo_root: Path) -> Path:
    """Resolve `UNITARES_PROJECTS_ROOT` whether running in a main checkout or a worktree.

    In a standard checkout `<projects>/unitares`, `git rev-parse --git-common-dir`
    returns `.git`, which resolves to `<projects>/unitares/.git`.
    In a linked worktree `<projects>/wt/unitares-topic`, it returns
    `<projects>/unitares/.git`.
    In both layouts, `.resolve().parent.parent` is `<projects>`.
    """
    env_override = os.environ.get("UNITARES_PROJECTS_ROOT", "").strip()
    if env_override:
        return Path(env_override).resolve()

    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        common_dir = (repo_root / proc.stdout.strip()).resolve()
        return common_dir.parent.parent
    return repo_root.resolve().parent


def default_output_dir(repo_root: Path) -> Path:
    """Return an untracked scratch directory inside `.git/` so doc sprawl checks stay clean."""
    proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        git_dir = (repo_root / proc.stdout.strip()).resolve()
        return git_dir / "release-prep"
    return repo_root / ".git" / "release-prep"


def read_current_version(repo_root: Path) -> str:
    return (repo_root / "VERSION").read_text(encoding="utf-8").strip()


def bump_semver(version: str, part: str) -> str:
    parts = [int(x) for x in version.strip().split(".")]
    if len(parts) != 3:
        raise ValueError(f"Expected X.Y.Z in VERSION, got {version!r}")
    major, minor, patch = parts
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown bump part: {part!r}")


def previous_release_tag(repo_root: Path) -> str | None:
    """Return the nearest ancestor release tag `vX.Y.Z`, or fallback to newest `vX.Y.Z` tag."""
    proc = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    proc_list = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*", "--sort=-v:refname"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    tags = [line.strip() for line in proc_list.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def stamp_changelog_release(changelog_text: str, version: str, release_date: str) -> str:
    """Move `## [Unreleased]` content under `## [<version>] - <release_date>`."""
    existing = re.search(rf"^## \[{re.escape(version)}\]", changelog_text, re.MULTILINE)
    if existing:
        # Already stamped for this version; update date if needed.
        return re.sub(
            rf"^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?[ \t]*$",
            f"## [{version}] - {release_date}",
            changelog_text,
            count=1,
            flags=re.MULTILINE,
        )

    match = UNRELEASED_HEADING.search(changelog_text)
    if not match:
        raise ValueError("docs/CHANGELOG.md is missing `## [Unreleased]`")

    replacement = f"## [Unreleased]\n\n---\n\n## [{version}] - {release_date}"
    return (
        changelog_text[: match.start()]
        + replacement
        + changelog_text[match.end() :]
    )


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """Extract the body of `## [<version>]` from `docs/CHANGELOG.md`."""
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog_text)
    if not match:
        return ""
    body = match.group(1).strip()
    if body.endswith("---"):
        body = body[:-3].rstrip()
    return body


def update_citation_date(citation_text: str, release_date: str) -> str:
    """Update `date-released` in `CITATION.cff`."""
    if not CITATION_DATE_RE.search(citation_text):
        raise ValueError("CITATION.cff is missing `date-released:`")
    return CITATION_DATE_RE.sub(rf'\1"{release_date}"', citation_text, count=1)


def run_skill_freshness(
    repo_root: Path,
    projects_root: Path,
    *,
    prune_keep: int | None = 3,
) -> str:
    """Run `check-skill-freshness.sh` with `UNITARES_PROJECTS_ROOT` and fail on absent sources."""
    script = repo_root / "scripts" / "client" / "check-skill-freshness.sh"
    if not script.exists():
        raise FileNotFoundError(
            f"Required skill freshness script is missing: {script}"
        )

    env = {**os.environ, "UNITARES_PROJECTS_ROOT": str(projects_root)}
    if prune_keep is not None:
        prune_proc = subprocess.run(
            ["bash", str(script), "--prune", str(prune_keep)],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        prune_out = (prune_proc.stdout or "") + (prune_proc.stderr or "")
        if ABSENT_SOURCE_MARKER in prune_out or prune_proc.returncode != 0:
            raise RuntimeError(
                f"check-skill-freshness.sh --prune failed:\n{prune_out.strip()}"
            )

    check_proc = subprocess.run(
        ["bash", str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    check_out = (check_proc.stdout or "") + (check_proc.stderr or "")
    if ABSENT_SOURCE_MARKER in check_out:
        raise RuntimeError(
            "Skill freshness check reported absent cited sources "
            f"(UNITARES_PROJECTS_ROOT={projects_root}):\n{check_out.strip()}"
        )
    if check_proc.returncode != 0:
        raise RuntimeError(
            f"Skill freshness check failed (exit {check_proc.returncode}):\n{check_out.strip()}"
        )
    return check_out


def check_quickstart_gate(repo_root: Path, prev_tag: str | None) -> tuple[bool, list[str]]:
    """Check whether Docker/Compose/requirements/migrations changed since `prev_tag`."""
    rev_range = f"{prev_tag}..HEAD" if prev_tag else "HEAD"
    proc = subprocess.run(
        ["git", "log", "--oneline", rev_range, "--", *DOCKER_GATE_PATHS],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    commits = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return bool(commits), commits


def detect_migrations_since(repo_root: Path, prev_tag: str | None) -> list[str]:
    """List migration files added or modified since `prev_tag`."""
    if not prev_tag:
        return []
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{prev_tag}..HEAD", "--", "db/postgres/migrations/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def read_component_versions(repo_root: Path) -> dict[str, str]:
    interface_file = repo_root / "src" / "interface_contract.py"
    interface_version = "unknown"
    if interface_file.exists():
        m = INTERFACE_VERSION_RE.search(interface_file.read_text(encoding="utf-8"))
        if m:
            interface_version = m.group(1)

    sdk_file = repo_root / "agents" / "sdk" / "pyproject.toml"
    sdk_version = "unknown"
    if sdk_file.exists():
        m = re.search(r'^version\s*=\s*"([^"]+)"', sdk_file.read_text(encoding="utf-8"), re.M)
        if m:
            sdk_version = m.group(1)

    return {
        "interface_contract": interface_version,
        "sdk": sdk_version,
    }


def build_release_notes(
    version: str,
    prev_tag: str | None,
    changelog_section: str,
    components: dict[str, str],
    migrations: list[str],
) -> str:
    migration_text = (
        "\n".join(f"- `{m}` (applies automatically on startup)" for m in migrations)
        if migrations
        else "None."
    )
    prev_display = prev_tag or "previous release tag"
    return (
        f"# v{version}\n\n"
        f"{changelog_section or '_See docs/CHANGELOG.md._'}\n\n"
        f"## Compatibility\n"
        f"- `INTERFACE_CONTRACT_VERSION`: `{components['interface_contract']}`\n"
        f"- `unitares-sdk`: `{components['sdk']}`\n\n"
        f"## Migrations\n"
        f"{migration_text}\n\n"
        f"## Rollback\n"
        f"- Return to `{prev_display}` (`docker compose` image tags and git checkout at `{prev_display}`).\n"
    )


def build_pr_body(
    version: str,
    prev_tag: str | None,
    needs_quickstart: bool,
    quickstart_commits: list[str],
    uncited_prs: list[str],
    coverage_ok: bool,
    notes_path: Path,
) -> str:
    paths_str = " ".join(DOCKER_GATE_PATHS)
    if needs_quickstart:
        commit_bullets = "\n".join(f"  - `{c}`" for c in quickstart_commits)
        quickstart_line = (
            f"- **Quickstart gate:** `NEEDS-QUICKSTART` — `{prev_tag or 'initial'}..HEAD` touched "
            f"quickstart-monitored paths (`{paths_str}`):\n"
            f"{commit_bullets}\n"
            "  Run a fresh-clone `docker compose up --build -d` check and record the result here before marking ready."
        )
    else:
        quickstart_line = (
            f"- **Quickstart gate:** `NOT-REQUIRED` (`git log --oneline {prev_tag or 'HEAD'}..HEAD -- "
            f"{paths_str}` is empty)."
        )

    if uncited_prs:
        uncited_line = (
            f"- **Changelog coverage:** {len(uncited_prs)} uncited PR(s) require either a user-facing bullet "
            "or a reviewed `<!-- changelog-coverage-exempt: #<num> <reason> -->` line."
        )
    elif not coverage_ok:
        uncited_line = (
            "- **Changelog coverage:** `scripts/ci/changelog_coverage.py` failed "
            "(check unattributed commits, leftover fragments, or exemption syntax)."
        )
    else:
        uncited_line = "- **Changelog coverage:** `scripts/ci/changelog_coverage.py` passed (0 uncited PRs)."

    return (
        f"## Summary\n\n"
        f"Prepare server release `v{version}`.\n\n"
        f"- Folded `docs/changelog.d/` fragments into `docs/CHANGELOG.md` under `## [{version}]`.\n"
        f"- Bumped `VERSION` to `{version}` and synchronized version targets (`scripts/ops/version_manager.py --update`).\n"
        f"- Draft release notes written to `{notes_path}`.\n\n"
        f"## Release Gates\n\n"
        f"{uncited_line}\n"
        f"{quickstart_line}\n"
    )


def _snapshot_tree(repo_root: Path) -> dict[Path, bytes | None]:
    """Capture tracked files and `docs/changelog.d/` so `--dry-run` can restore them."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    paths = {repo_root / rel for rel in tracked.split("\0") if rel}
    frag_dir = repo_root / "docs" / "changelog.d"
    if frag_dir.is_dir():
        paths.update(p for p in frag_dir.rglob("*") if p.is_file() and not p.is_symlink())
    return {p: (p.read_bytes() if p.exists() else None) for p in paths if not p.is_symlink()}


def _restore_tree(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def prepare_release(
    repo_root: Path,
    part: str,
    release_date: str,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    date.fromisoformat(release_date)
    out_dir = (output_dir or default_output_dir(repo_root)).resolve()
    projects_root = resolve_projects_root(repo_root)
    prev_tag = previous_release_tag(repo_root)
    old_version = read_current_version(repo_root)
    new_version = bump_semver(old_version, part)

    snapshot = _snapshot_tree(repo_root)
    try:
        # 1. Fold changelog fragments into ## [Unreleased]
        assemble_proc = subprocess.run(
            [sys.executable, str(repo_root / "scripts/dev/changelog_assemble.py"), "--root", str(repo_root)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if assemble_proc.returncode != 0:
            raise RuntimeError(
                f"changelog_assemble.py failed:\n{(assemble_proc.stdout or '') + (assemble_proc.stderr or '')}"
            )

        # 2. Bump VERSION and propagate across all version targets
        for flag in (["--bump", part], ["--update"]):
            vm_proc = subprocess.run(
                [sys.executable, str(repo_root / "scripts/ops/version_manager.py"), *flag],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if vm_proc.returncode != 0:
                raise RuntimeError(
                    f"version_manager.py {' '.join(flag)} failed:\n"
                    f"{(vm_proc.stdout or '') + (vm_proc.stderr or '')}"
                )

        # 3. Stamp ## [<version>] - <date> in docs/CHANGELOG.md and date-released in CITATION.cff
        changelog_path = repo_root / "docs" / "CHANGELOG.md"
        changelog_text = changelog_path.read_text(encoding="utf-8")
        stamped_changelog = stamp_changelog_release(changelog_text, new_version, release_date)
        changelog_path.write_text(stamped_changelog, encoding="utf-8")

        citation_path = repo_root / "CITATION.cff"
        if citation_path.exists():
            citation_text = citation_path.read_text(encoding="utf-8")
            citation_path.write_text(
                update_citation_date(citation_text, release_date),
                encoding="utf-8",
            )

        # 4. Run skill freshness check (with --prune 3 when not dry-run)
        run_skill_freshness(
            repo_root,
            projects_root,
            prune_keep=None if dry_run else 3,
        )

        # 5. Run release seam checks against the bumped release tree
        cov_proc = subprocess.run(
            [sys.executable, str(repo_root / "scripts/ci/changelog_coverage.py")],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        coverage_ok = cov_proc.returncode == 0
        uncited_prs = [
            line.strip()
            for line in (cov_proc.stdout or "").splitlines()
            if re.match(r"^#\d+\s+", line.strip())
        ]

        drift_proc = subprocess.run(
            [sys.executable, str(repo_root / "scripts/ci/release_series_drift.py")],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        drift_ok = drift_proc.returncode == 0

        vm_check_proc = subprocess.run(
            [sys.executable, str(repo_root / "scripts/ops/version_manager.py"), "--check"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        version_sync_ok = vm_check_proc.returncode == 0
        components = read_component_versions(repo_root)
    except BaseException:
        _restore_tree(snapshot)
        raise
    finally:
        if dry_run:
            _restore_tree(snapshot)

    # 6. Check range-scoped quickstart gate
    needs_quickstart, quickstart_commits = check_quickstart_gate(repo_root, prev_tag)
    migrations = detect_migrations_since(repo_root, prev_tag)
    changelog_section = extract_changelog_section(stamped_changelog, new_version)

    # 7. Write artifacts to output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    notes_path = out_dir / f"release-notes-{new_version}.md"
    pr_body_path = out_dir / f"pr-body-{new_version}.md"
    uncited_path = out_dir / f"uncited-prs-{new_version}.txt"

    notes_path.write_text(
        build_release_notes(new_version, prev_tag, changelog_section, components, migrations),
        encoding="utf-8",
    )
    pr_body_path.write_text(
        build_pr_body(
            new_version,
            prev_tag,
            needs_quickstart,
            quickstart_commits,
            uncited_prs,
            coverage_ok,
            notes_path,
        ),
        encoding="utf-8",
    )
    if uncited_prs:
        uncited_path.write_text("\n".join(uncited_prs) + "\n", encoding="utf-8")
    elif uncited_path.exists():
        uncited_path.unlink()

    return {
        "from_version": old_version,
        "version": new_version,
        "date": release_date,
        "previous_tag": prev_tag,
        "projects_root": str(projects_root),
        "quickstart_gate": "NEEDS-QUICKSTART" if needs_quickstart else "NOT-REQUIRED",
        "quickstart_commits": quickstart_commits,
        "coverage_ok": coverage_ok,
        "coverage_output": ((cov_proc.stdout or "") + (cov_proc.stderr or "")).strip(),
        "uncited_prs": uncited_prs,
        "uncited_prs_file": str(uncited_path) if uncited_prs else None,
        "release_series_drift_ok": drift_ok,
        "release_series_drift_output": ((drift_proc.stdout or "") + (drift_proc.stderr or "")).strip(),
        "version_sync_ok": version_sync_ok,
        "release_notes_file": str(notes_path),
        "pr_body_file": str(pr_body_path),
        "dry_run": dry_run,
    }


def _default_ghcr_status_fetcher(pkg: str, tag: str) -> int:
    """Return the HTTP status code for an anonymous GHCR manifest check."""
    token_url = f"https://ghcr.io/token?scope=repository:{GHCR_OWNER}/{pkg}:pull"
    token = None
    try:
        with urllib.request.urlopen(token_url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            token = payload.get("token")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return exc.code
        return exc.code
    except OSError:
        return 503

    manifest_url = f"https://ghcr.io/v2/{GHCR_OWNER}/{pkg}/manifests/{tag}"
    headers = {
        "Accept": (
            "application/vnd.oci.image.index.v1+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json, "
            "application/vnd.docker.distribution.manifest.v2+json"
        )
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(manifest_url, headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 503


def check_ghcr_visibility(
    version: str,
    status_fetcher: Callable[[str, str], int] = _default_ghcr_status_fetcher,
) -> tuple[bool, list[dict[str, str]]]:
    """Check anonymous GHCR visibility for all three release images at `v<version>`."""
    clean_version = version.lstrip("v")
    tag = f"v{clean_version}"
    results: list[dict[str, str]] = []
    all_public = True

    for pkg in GHCR_PACKAGES:
        code = status_fetcher(pkg, tag)
        settings_url = f"https://github.com/users/{GHCR_OWNER}/packages/container/{pkg}/settings"
        image_ref = f"ghcr.io/{GHCR_OWNER}/{pkg}:{tag}"
        if code == 200:
            state = "public"
            detail = f"{image_ref} is anonymously pullable (HTTP 200)."
        elif code in (401, 403):
            state = "private"
            all_public = False
            detail = (
                f"{image_ref} denied anonymous pull (HTTP {code} — package is private). "
                f"Change visibility to public at: {settings_url}"
            )
        elif code == 404:
            state = "missing"
            all_public = False
            detail = (
                f"{image_ref} was not found (HTTP 404). Wait for `Publish Container` "
                f"to finish building `{tag}`, or check if the package is private at: {settings_url}"
            )
        else:
            state = "error"
            all_public = False
            detail = f"{image_ref} returned unexpected HTTP {code}."

        results.append(
            {
                "package": pkg,
                "image": image_ref,
                "state": state,
                "http_status": str(code),
                "settings_url": settings_url,
                "detail": detail,
            }
        )

    return all_public, results


def format_after_merge(version: str, sha: str, notes_file: Path | None = None) -> str:
    clean_version = version.lstrip("v")
    tag = f"v{clean_version}"
    notes_arg = (
        str(notes_file)
        if notes_file
        else f'"$(git rev-parse --git-dir)/release-prep/release-notes-{clean_version}.md"'
    )
    return (
        f"# Post-Merge Release Sequence for {tag} (merged commit: {sha})\n\n"
        f"## 1. Releasing Agent — Create and push the release tag\n"
        f"```bash\n"
        f"git fetch origin master\n"
        f'git tag -s {tag} {sha} -m "UNITARES {tag}"\n'
        f"unitares-github -- git push origin {tag}\n"
        f"```\n\n"
        f"## 2. Releasing Agent — Publish the GitHub release page (triggers Publish Container)\n"
        f"```bash\n"
        f"unitares-github -- gh release create {tag} \\\n"
        f'  --title "{tag}" \\\n'
        f"  --notes-file {notes_arg}\n"
        f"```\n\n"
        f"## 3. Releasing Agent — Verify anonymous GHCR visibility after Publish Container completes\n"
        f"```bash\n"
        f"python3 scripts/ops/prepare_release.py --check-ghcr --version {clean_version}\n"
        f"```\n\n"
        f"## 4. Releasing Agent — Dispatch Promote Release\n"
        f"```bash\n"
        f"unitares-github -- gh workflow run promote-release.yml -f tag={tag}\n"
        f"```\n\n"
        f"## 5. Operator — Approve the `release-promotion` environment in GitHub UI\n"
        f"https://github.com/cirwel/unitares/actions/workflows/promote-release.yml\n\n"
        f"## 6. Releasing Agent — Open and merge the automated pin PR (`publish/{tag}`)\n"
        f"Run the `gh pr create --base master --head publish/{tag} ...` command printed in the\n"
        f"`Promote Release` workflow run summary (which embeds the verified index digest and\n"
        f"run evidence link), appending the required provenance footer (`Model:` and\n"
        f"`UNITARES agent: <uuid>`) to the PR body.\n"
    )


def resolve_head_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "<merge-commit-sha>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--part",
        choices=("patch", "minor", "major"),
        default="patch",
        help="SemVer component to bump during release preparation (default: patch).",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Release date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for generated release notes, PR body, and uncited PR list.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview release preparation without mutating tracked files.",
    )
    parser.add_argument(
        "--after-merge",
        action="store_true",
        help="Print the post-merge operator and agent command sequence.",
    )
    parser.add_argument(
        "--sha",
        default=None,
        help="Merged release commit SHA for --after-merge (defaults to HEAD).",
    )
    parser.add_argument(
        "--check-ghcr",
        action="store_true",
        help="Verify anonymous GHCR pullability for unitares, lease-plane, and postgres images.",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Explicit X.Y.Z version for --after-merge or --check-ghcr (defaults to VERSION).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (used by tests).",
    )
    args = parser.parse_args(argv)
    repo_root = args.root.resolve()

    if args.check_ghcr:
        version = (args.version or read_current_version(repo_root)).lstrip("v")
        ok, results = check_ghcr_visibility(version)
        for item in results:
            prefix = "OK" if item["state"] == "public" else item["state"].upper()
            print(f"[{prefix}] {item['detail']}")
        return 0 if ok else 1

    if args.after_merge:
        version = (args.version or read_current_version(repo_root)).lstrip("v")
        sha = args.sha or resolve_head_sha(repo_root)
        out_dir = (args.output_dir or default_output_dir(repo_root)).resolve()
        notes_file = out_dir / f"release-notes-{version}.md"
        print(format_after_merge(version, sha, notes_file=notes_file))
        return 0

    try:
        summary = prepare_release(
            repo_root=repo_root,
            part=args.part,
            release_date=args.date,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"prepare_release: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    if summary["uncited_prs"]:
        print(
            f"\nAction required: {len(summary['uncited_prs'])} uncited PR(s) listed in "
            f"{summary['uncited_prs_file']}. Add changelog entries or reviewed exemptions.",
            file=sys.stderr,
        )
        return 1
    if not summary["coverage_ok"]:
        print(
            f"\nAction required: changelog_coverage.py failed:\n"
            f"{summary['coverage_output']}",
            file=sys.stderr,
        )
        return 1
    if not summary["release_series_drift_ok"]:
        print(
            f"\nAction required: release_series_drift.py reported drift:\n"
            f"{summary['release_series_drift_output']}",
            file=sys.stderr,
        )
        return 1
    if not summary["version_sync_ok"]:
        print(
            "\nAction required: version_manager.py --check reported out-of-sync files.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
