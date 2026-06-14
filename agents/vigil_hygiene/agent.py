#!/usr/bin/env python3
"""vigil-hygiene — weekly branch hygiene sweep.

Runs once per week via launchd. Prunes [gone] local branches only after
checking that their commits are merged or patch-equivalent, removes their
worktrees (when clean), and deletes origin branches whose commits are all
squash-merged. Reports HOLD branches with unique commits for human salvage
review.

Usage:
    python3 agents/vigil_hygiene/agent.py [--repo PATH] [--live]

Defaults to dry-run. --live enables actual deletions.
See docs/operations/branch-hygiene-runbook.md for the salvage contract.

"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure repo root on sys.path when run directly
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from agents.vigil_hygiene.cherry import CherryResult, CherryVerdict, parse_cherry
from agents.vigil_hygiene.clean_check import check_worktree_clean

KEEPALIVE_BRANCH_NAMES = frozenset({"master", "main", "feat/branch-hygiene-automation"})
NEWER_THAN_SECONDS = 24 * 60 * 60
LOG_FILE = Path.home() / "Library" / "Logs" / "unitares-vigil-hygiene.log"


@dataclass
class SweepReport:
    started_at: str
    duration_s: float = 0.0
    dry_run: bool = True
    branches_prunable: int = 0
    branches_pruned: int = 0
    worktrees_removable: int = 0
    worktrees_removed: int = 0
    origin_orphans_deletable: int = 0
    origin_orphans_deleted: int = 0
    holds_count: int = 0
    holds: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_git(*args: str, repo: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check, capture_output=True, text=True,
    )


def list_open_pr_branches(repo: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", "CIRWEL/unitares", "--state", "open",
             "--json", "headRefName", "--jq", ".[].headRefName"],
            capture_output=True, text=True, check=True, cwd=str(repo),
        )
        return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log(f"WARN: gh pr list failed ({e}); defaulting to empty open-PR set (will be over-conservative)")
        return set()


def list_gone_branches(repo: Path) -> list[str]:
    result = run_git("branch", "-vv", repo=repo)
    gone = []
    for line in result.stdout.splitlines():
        m = re.match(r"^[*+ ]\s*(\S+)\s+\S+(?:\s+\([^)]*\))?\s+\[.*: gone\]", line)
        if m:
            gone.append(m.group(1))
    return gone


def list_worktrees(repo: Path) -> list[tuple[Path, Optional[str]]]:
    result = run_git("worktree", "list", "--porcelain", repo=repo)
    out: list[tuple[Path, Optional[str]]] = []
    cur_path: Optional[str] = None
    cur_branch: Optional[str] = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if cur_path is not None:
                out.append((Path(cur_path), cur_branch))
            cur_path = line.split(" ", 1)[1]
            cur_branch = None
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            if ref.startswith("refs/heads/"):
                cur_branch = ref[len("refs/heads/"):]
    if cur_path is not None:
        out.append((Path(cur_path), cur_branch))
    return out


def list_origin_branches(repo: Path) -> list[str]:
    result = run_git("branch", "-r", repo=repo)
    out = []
    for line in result.stdout.splitlines():
        ln = line.strip()
        if not ln or "HEAD" in ln:
            continue
        if ln.startswith("origin/"):
            out.append(ln[len("origin/"):])
    return out


def get_committer_timestamp(repo: Path, ref: str) -> Optional[int]:
    try:
        result = run_git("log", "-1", "--format=%ct", ref, repo=repo)
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def cherry(repo: Path, branch: str, base: str = "master") -> str:
    try:
        result = run_git("cherry", base, f"origin/{branch}", repo=repo)
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def cherry_local(repo: Path, branch: str, base: str = "master") -> Optional[str]:
    try:
        result = run_git("cherry", base, branch, repo=repo)
        return result.stdout
    except subprocess.CalledProcessError as e:
        log(f"SKIP gone-branch '{branch}': git cherry failed ({e.stderr.strip()})")
        return None


def classify_local_gone_cherry(output: str, base: str = "master") -> CherryResult:
    result = parse_cherry(output)
    if (
        result.verdict == CherryVerdict.SKIP
        and result.plus_count == 0
        and result.minus_count == 0
        and result.reason.startswith("empty output")
    ):
        return CherryResult(
            CherryVerdict.DELETE,
            0,
            0,
            f"no commits ahead of {base}",
        )
    return result


def is_keepalive(
    branch: str,
    open_pr_branches: set[str],
    origin_committer_ts: Optional[int],
    now: float,
) -> tuple[bool, str]:
    if branch in KEEPALIVE_BRANCH_NAMES:
        return True, "name in keepalive list"
    if branch in open_pr_branches:
        return True, "has open PR"
    if origin_committer_ts is not None and (now - origin_committer_ts) < NEWER_THAN_SECONDS:
        age_h = (now - origin_committer_ts) / 3600
        return True, f"newer than 24h (age={age_h:.1f}h)"
    return False, ""


def _safe_status(repo_or_wt: Path) -> Optional[str]:
    try:
        return run_git("status", "--porcelain", repo=repo_or_wt).stdout
    except subprocess.CalledProcessError:
        return None


def sweep(repo: Path, dry_run: bool = True) -> SweepReport:
    started = time.time()
    report = SweepReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
    )

    log(f"sweep started (repo={repo}, dry_run={dry_run})")

    # 1. Fetch + prune
    try:
        run_git("fetch", "--prune", "origin", repo=repo)
        log("fetch --prune origin: ok")
    except subprocess.CalledProcessError as e:
        report.errors.append(f"fetch failed: {e.stderr}")
        log(f"ERROR: fetch failed: {e.stderr}")
        report.duration_s = time.time() - started
        return report

    worktrees = list_worktrees(repo)
    branch_to_wt = {b: p for p, b in worktrees if b is not None}

    # 2. Local [gone] cleanup
    for branch in list_gone_branches(repo):
        cherry_out = cherry_local(repo, branch)
        if cherry_out is None:
            continue
        cherry_result = classify_local_gone_cherry(cherry_out)
        if cherry_result.verdict == CherryVerdict.HOLD:
            report.holds.append(branch)
            report.holds_count += 1
            log(
                f"HOLD gone-branch '{branch}': {cherry_result.reason}; "
                "inspect/salvage before deleting"
            )
            continue
        if cherry_result.verdict == CherryVerdict.SKIP:
            log(f"SKIP gone-branch '{branch}': {cherry_result.reason}")
            continue

        wt = branch_to_wt.get(branch)
        if wt is not None:
            if wt.resolve() == repo.resolve():
                report.holds.append(branch)
                report.holds_count += 1
                log(
                    f"HOLD gone-branch '{branch}': checked out in sweep repo {repo}; "
                    "run hygiene from another checkout before removing this worktree"
                )
                continue
            status = _safe_status(wt)
            if status is None:
                log(f"SKIP gone-branch '{branch}': could not check worktree status")
                continue
            check = check_worktree_clean(wt, status)
            if not check.is_clean:
                log(f"SKIP gone-branch '{branch}': worktree {wt} not clean ({check.reason})")
                continue
            report.worktrees_removable += 1
            if dry_run:
                log(f"DRY-RUN would worktree-remove {wt}")
            else:
                try:
                    run_git("worktree", "remove", str(wt), repo=repo)
                    log(f"removed worktree: {wt}")
                    report.worktrees_removed += 1
                except subprocess.CalledProcessError as e:
                    log(f"ERROR worktree remove {wt}: {e.stderr}")
                    report.errors.append(f"worktree remove {wt}: {e.stderr}")
                    continue

        report.branches_prunable += 1
        if dry_run:
            log(f"DRY-RUN would branch -D '{branch}': {cherry_result.reason}")
        else:
            try:
                run_git("branch", "-D", branch, repo=repo)
                log(f"deleted local branch: {branch}: {cherry_result.reason}")
                report.branches_pruned += 1
            except subprocess.CalledProcessError as e:
                log(f"ERROR branch -D {branch}: {e.stderr}")
                report.errors.append(f"branch -D {branch}: {e.stderr}")

    # 3. Origin orphan sweep
    open_prs = list_open_pr_branches(repo)
    now = time.time()
    for branch in list_origin_branches(repo):
        ts = get_committer_timestamp(repo, f"origin/{branch}")
        keep, reason = is_keepalive(branch, open_prs, ts, now)
        if keep:
            continue

        cherry_out = cherry(repo, branch)
        result = parse_cherry(cherry_out)

        if result.verdict == CherryVerdict.HOLD:
            report.holds.append(branch)
            report.holds_count += 1
            log(f"HOLD origin/{branch}: {result.reason}")
        elif result.verdict == CherryVerdict.SKIP:
            log(f"SKIP origin/{branch}: {result.reason}")
        elif result.verdict == CherryVerdict.DELETE:
            report.origin_orphans_deletable += 1
            if dry_run:
                log(f"DRY-RUN would delete origin/{branch}: {result.reason}")
            else:
                try:
                    run_git("push", "origin", "--delete", branch, repo=repo)
                    log(f"deleted origin/{branch}: {result.reason}")
                    report.origin_orphans_deleted += 1
                except subprocess.CalledProcessError as e:
                    log(f"ERROR push --delete {branch}: {e.stderr}")
                    report.errors.append(f"push --delete {branch}: {e.stderr}")

    # 4. Branchless worktree sweep
    refreshed = list_worktrees(repo)
    for path, branch in refreshed:
        if path == repo or branch is not None:
            continue
        status = _safe_status(path)
        if status is None:
            continue
        check = check_worktree_clean(path, status)
        if not check.is_clean:
            continue
        report.worktrees_removable += 1
        if dry_run:
            log(f"DRY-RUN would worktree-remove {path} (no branch)")
        else:
            try:
                run_git("worktree", "remove", str(path), repo=repo)
                log(f"removed branchless worktree: {path}")
                report.worktrees_removed += 1
            except subprocess.CalledProcessError as e:
                log(f"ERROR worktree remove {path}: {e.stderr}")
                report.errors.append(f"worktree remove {path}: {e.stderr}")

    report.duration_s = time.time() - started
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="vigil-hygiene weekly branch sweep")
    parser.add_argument(
        "--repo",
        default=os.environ.get("UNITARES_REPO", str(Path(__file__).resolve().parents[2])),
        help="path to git repo",
    )
    parser.add_argument("--live", action="store_true", help="actually delete (default: dry-run)")
    args = parser.parse_args()

    repo = Path(args.repo)
    if not (repo / ".git").exists():
        log(f"ERROR: {repo} is not a git repo")
        return 2

    report = sweep(repo, dry_run=not args.live)
    log("sweep done:\n" + json.dumps(asdict(report), indent=2))
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
