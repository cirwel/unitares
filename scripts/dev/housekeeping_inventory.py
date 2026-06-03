#!/usr/bin/env python3
"""Read-only UNITARES worktree housekeeping inventory.

Reports the local residue that tends to make agent sessions drift:

- dirty or detached worktrees
- local branches whose upstream was pruned
- fully merged local branch deletion candidates
- old stashes
- open GitHub PRs, when ``gh`` is available
- unresolved watcher output, when the watcher script is available

The command never mutates git state. It is intentionally safe to run from
hooks, shell aliases, and a half-configured fresh checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SEC = 10
DEFAULT_STASH_DAYS = 14


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass
class WorktreeInfo:
    path: str
    head: str = ""
    branch: str = ""
    detached: bool = False
    dirty_paths: list[str] = field(default_factory=list)
    status_error: str = ""


@dataclass
class BranchInfo:
    name: str
    upstream: str
    track: str
    head: str
    date: str
    subject: str


@dataclass
class StashInfo:
    ref: str
    hash: str
    date: str
    subject: str
    age_days: int | None = None


@dataclass
class ProbeResult:
    status: str
    message: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass
class Inventory:
    repo_root: str
    generated_at: str
    worktrees: list[WorktreeInfo]
    gone_upstream_branches: list[BranchInfo]
    merged_branch_candidates: list[BranchInfo]
    unmerged_branches: list[BranchInfo]
    stashes: list[StashInfo]
    old_stashes: list[StashInfo]
    github_prs: ProbeResult
    watcher: ProbeResult


@dataclass
class AttentionSummary:
    dirty_worktrees: int = 0
    detached_worktrees: int = 0
    worktree_status_errors: int = 0
    gone_upstream_branches: int = 0
    merged_branch_candidates: int = 0
    old_stashes: int = 0
    open_github_prs: int = 0
    watcher_unresolved_lines: int = 0
    probe_errors: int = 0

    @property
    def total(self) -> int:
        return (
            self.dirty_worktrees
            + self.detached_worktrees
            + self.worktree_status_errors
            + self.gone_upstream_branches
            + self.merged_branch_candidates
            + self.old_stashes
            + self.open_github_prs
            + self.watcher_unresolved_lines
            + self.probe_errors
        )


ATTENTION_KEYS = (
    "dirty_worktrees",
    "detached_worktrees",
    "worktree_status_errors",
    "gone_upstream_branches",
    "merged_branch_candidates",
    "old_stashes",
    "open_github_prs",
    "watcher_unresolved_lines",
    "probe_errors",
)

ATTENTION_GROUPS = {
    "all": ATTENTION_KEYS,
    "worktrees": (
        "dirty_worktrees",
        "detached_worktrees",
        "worktree_status_errors",
    ),
    "branches": ("gone_upstream_branches", "merged_branch_candidates"),
    "stashes": ("old_stashes",),
    "github": ("open_github_prs",),
    "watcher": ("watcher_unresolved_lines",),
    "probes": ("probe_errors",),
}


def run_cmd(
    args: list[str],
    cwd: Path | str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> CommandResult:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            args=args,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(args=args, returncode=127, stderr=str(exc))


def git_root(cwd: Path) -> Path:
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        raise SystemExit("not inside a git repository")
    return Path(result.stdout.strip()).resolve()


def parse_worktree_porcelain(stdout: str) -> list[WorktreeInfo]:
    worktrees: list[WorktreeInfo] = []
    current: dict[str, str | bool] = {}

    def flush() -> None:
        if not current:
            return
        branch = str(current.get("branch", ""))
        detached = bool(current.get("detached", False)) or not branch
        worktrees.append(
            WorktreeInfo(
                path=str(current.get("worktree", "")),
                head=str(current.get("head", "")),
                branch=branch,
                detached=detached,
            )
        )
        current.clear()

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            current["worktree"] = value
        elif key == "HEAD":
            current["head"] = value[:12]
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
    flush()
    return worktrees


def parse_status_short(stdout: str) -> list[str]:
    paths: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1 has a two-column status plus a space before the path.
        paths.append(line[3:] if len(line) > 3 else line.strip())
    return paths


def collect_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    result = run_cmd(["git", "worktree", "list", "--porcelain"], repo_root)
    if result.returncode != 0:
        return [
            WorktreeInfo(
                path=str(repo_root),
                status_error=result.stderr.strip() or "git worktree list failed",
            )
        ]

    worktrees = parse_worktree_porcelain(result.stdout)
    for wt in worktrees:
        status = run_cmd(
            ["git", "status", "--short", "--untracked-files=all"],
            wt.path,
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        if status.returncode == 0:
            wt.dirty_paths = parse_status_short(status.stdout)
        else:
            wt.status_error = status.stderr.strip() or "git status failed"

        if not wt.branch:
            branch = run_cmd(["git", "branch", "--show-current"], wt.path)
            wt.branch = branch.stdout.strip() if branch.returncode == 0 else ""
            wt.detached = not wt.branch
    return worktrees


def parse_branch_rows(stdout: str) -> list[BranchInfo]:
    rows: list[BranchInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 5)
        while len(parts) < 6:
            parts.append("")
        name, upstream, track, head, date, subject = parts
        rows.append(
            BranchInfo(
                name=name,
                upstream=upstream,
                track=track,
                head=head,
                date=date,
                subject=subject,
            )
        )
    return rows


def branch_rows(repo_root: Path, *extra_args: str) -> list[BranchInfo]:
    fmt = (
        "%(refname:short)%09%(upstream:short)%09%(upstream:track)%09"
        "%(objectname:short)%09%(committerdate:iso8601)%09%(subject)"
    )
    result = run_cmd(
        ["git", "for-each-ref", *extra_args, f"--format={fmt}", "refs/heads"],
        repo_root,
    )
    if result.returncode != 0:
        return []
    return parse_branch_rows(result.stdout)


def protected_branch(name: str) -> bool:
    return name in {"main", "master"} or name.startswith(("backup/", "archive/"))


def collect_branches(
    repo_root: Path,
    checked_out_branches: set[str],
    base: str,
) -> tuple[list[BranchInfo], list[BranchInfo], list[BranchInfo]]:
    all_branches = branch_rows(repo_root)
    gone = [row for row in all_branches if "[gone]" in row.track]

    merged = branch_rows(repo_root, "--merged", base)
    merged_candidates = [
        row
        for row in merged
        if not protected_branch(row.name) and row.name not in checked_out_branches
    ]

    unmerged = branch_rows(repo_root, "--no-merged", base)
    unmerged = [row for row in unmerged if not protected_branch(row.name)]
    return gone, merged_candidates, unmerged


def parse_stash_rows(stdout: str, now: datetime) -> list[StashInfo]:
    stashes: list[StashInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        while len(parts) < 4:
            parts.append("")
        ref, stash_hash, date_raw, subject = parts
        age_days: int | None = None
        try:
            parsed = datetime.fromisoformat(date_raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - parsed.astimezone(timezone.utc)).days)
        except ValueError:
            age_days = None
        stashes.append(
            StashInfo(
                ref=ref,
                hash=stash_hash,
                date=date_raw,
                subject=subject,
                age_days=age_days,
            )
        )
    return stashes


def collect_stashes(repo_root: Path, now: datetime) -> list[StashInfo]:
    result = run_cmd(
        [
            "git",
            "stash",
            "list",
            "--date=iso-strict",
            "--format=%gd%x09%H%x09%cd%x09%gs",
        ],
        repo_root,
    )
    if result.returncode != 0:
        return []
    return parse_stash_rows(result.stdout, now)


def github_repo_from_remote(remote: str) -> str:
    remote = remote.strip()
    if not remote:
        return ""
    patterns = (
        r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?$",
        r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?(?:/)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return match.group(1)
    return ""


def collect_github_prs(
    repo_root: Path,
    *,
    repo: str = "",
    skip: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> ProbeResult:
    if skip:
        return ProbeResult(status="skipped", message="disabled by flag")
    if shutil.which("gh") is None:
        return ProbeResult(status="skipped", message="gh not found on PATH")
    if not repo:
        remote = run_cmd(["git", "remote", "get-url", "origin"], repo_root)
        if remote.returncode == 0:
            repo = github_repo_from_remote(remote.stdout)
    if not repo:
        return ProbeResult(status="skipped", message="could not infer GitHub repo")

    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,isDraft,headRefName,updatedAt,url",
        ],
        repo_root,
        timeout=timeout,
    )
    if result.returncode != 0:
        status = "timeout" if result.timed_out else "error"
        return ProbeResult(
            status=status,
            message=result.stderr.strip() or f"gh exited {result.returncode}",
            stderr=result.stderr,
        )
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return ProbeResult(status="error", message=f"could not parse gh JSON: {exc}")
    return ProbeResult(status="ok", items=items)


def collect_watcher(
    repo_root: Path,
    *,
    skip: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> ProbeResult:
    if skip:
        return ProbeResult(status="skipped", message="disabled by flag")
    watcher = repo_root / "agents" / "watcher" / "agent.py"
    if not watcher.exists():
        return ProbeResult(status="skipped", message="agents/watcher/agent.py not found")

    result = run_cmd(
        [sys.executable, str(watcher), "--print-unresolved"],
        repo_root,
        timeout=timeout,
    )
    if result.returncode != 0:
        status = "timeout" if result.timed_out else "error"
        return ProbeResult(
            status=status,
            message=result.stderr.strip() or f"watcher exited {result.returncode}",
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return ProbeResult(status="ok", stdout=result.stdout)


def build_inventory(args: argparse.Namespace) -> Inventory:
    repo_root = git_root(Path(args.cwd).resolve())
    now = datetime.now(timezone.utc)
    worktrees = collect_worktrees(repo_root)
    checked_out = {wt.branch for wt in worktrees if wt.branch}
    gone, merged_candidates, unmerged = collect_branches(
        repo_root,
        checked_out,
        args.base,
    )
    stashes = collect_stashes(repo_root, now)
    old_stashes = [
        stash
        for stash in stashes
        if stash.age_days is not None and stash.age_days >= args.stash_days
    ]

    github_prs = collect_github_prs(
        repo_root,
        repo=args.github_repo,
        skip=args.no_github,
        timeout=args.github_timeout,
    )
    watcher = collect_watcher(
        repo_root,
        skip=args.no_watcher,
        timeout=args.watcher_timeout,
    )

    return Inventory(
        repo_root=str(repo_root),
        generated_at=now.isoformat(),
        worktrees=worktrees,
        gone_upstream_branches=gone,
        merged_branch_candidates=merged_candidates,
        unmerged_branches=unmerged,
        stashes=stashes,
        old_stashes=old_stashes,
        github_prs=github_prs,
        watcher=watcher,
    )


def _short_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _print_branch_list(title: str, branches: list[BranchInfo], limit: int) -> None:
    print(f"{title}: {len(branches)}")
    for row in branches[:limit]:
        upstream = f" upstream={row.upstream}" if row.upstream else ""
        track = f" {row.track}" if row.track else ""
        print(f"  - {row.name} {row.head}{upstream}{track} :: {row.subject}")
    if len(branches) > limit:
        print(f"  ... {len(branches) - limit} more")


def print_text_report(inventory: Inventory, *, limit: int) -> None:
    root = inventory.repo_root
    dirty = [wt for wt in inventory.worktrees if wt.dirty_paths]
    detached = [wt for wt in inventory.worktrees if wt.detached]
    status_errors = [wt for wt in inventory.worktrees if wt.status_error]

    print("UNITARES housekeeping inventory")
    print(f"repo: {root}")
    print(f"generated_at: {inventory.generated_at}")
    print(f"attention_total: {attention_summary(inventory).total}")
    print()

    print(
        "worktrees: "
        f"{len(inventory.worktrees)} total, {len(dirty)} dirty, "
        f"{len(detached)} detached, {len(status_errors)} status errors"
    )
    for wt in dirty[:limit]:
        label = wt.branch or "(detached)"
        rel = _short_path(wt.path, root)
        print(f"  - {rel} [{label} {wt.head[:8]}] {len(wt.dirty_paths)} changed")
        for path in wt.dirty_paths[:8]:
            print(f"      {path}")
        if len(wt.dirty_paths) > 8:
            print(f"      ... {len(wt.dirty_paths) - 8} more")
    if len(dirty) > limit:
        print(f"  ... {len(dirty) - limit} more dirty worktrees")
    for wt in detached[:limit]:
        if wt in dirty:
            continue
        rel = _short_path(wt.path, root)
        print(f"  - detached clean: {rel} [{wt.head[:8]}]")
    print()

    _print_branch_list("gone-upstream branches", inventory.gone_upstream_branches, limit)
    _print_branch_list(
        "merged local delete candidates",
        inventory.merged_branch_candidates,
        limit,
    )
    _print_branch_list("unmerged local branches", inventory.unmerged_branches, limit)
    print()

    print(f"stashes: {len(inventory.stashes)} total, {len(inventory.old_stashes)} old")
    for stash in inventory.old_stashes[:limit]:
        age = "unknown age" if stash.age_days is None else f"{stash.age_days}d"
        print(f"  - {stash.ref} {stash.hash[:8]} {age} :: {stash.subject}")
    if len(inventory.old_stashes) > limit:
        print(f"  ... {len(inventory.old_stashes) - limit} more old stashes")
    print()

    prs = inventory.github_prs
    if prs.status == "ok":
        print(f"github open PRs: {len(prs.items)}")
        for item in prs.items[:limit]:
            draft = "draft " if item.get("isDraft") else ""
            print(
                f"  - #{item.get('number')} {draft}{item.get('headRefName')}: "
                f"{item.get('title')} ({item.get('url')})"
            )
        if len(prs.items) > limit:
            print(f"  ... {len(prs.items) - limit} more")
    else:
        print(f"github open PRs: {prs.status} - {prs.message}")
    print()

    watcher = inventory.watcher
    if watcher.status == "ok":
        lines = [line for line in watcher.stdout.splitlines() if line.strip()]
        print(f"watcher unresolved output: {len(lines)} non-empty line(s)")
        for line in lines[:limit]:
            print(f"  {line}")
        if len(lines) > limit:
            print(f"  ... {len(lines) - limit} more")
    else:
        print(f"watcher unresolved output: {watcher.status} - {watcher.message}")


def to_jsonable(inventory: Inventory) -> dict[str, Any]:
    payload = asdict(inventory)
    payload["attention"] = asdict(attention_summary(inventory))
    payload["attention"]["total"] = attention_summary(inventory).total
    return payload


def attention_summary(inventory: Inventory) -> AttentionSummary:
    dirty = [wt for wt in inventory.worktrees if wt.dirty_paths]
    detached = [wt for wt in inventory.worktrees if wt.detached]
    status_errors = [wt for wt in inventory.worktrees if wt.status_error]
    watcher_lines = [
        line for line in inventory.watcher.stdout.splitlines() if line.strip()
    ] if inventory.watcher.status == "ok" else []
    probe_errors = sum(
        1
        for probe in (inventory.github_prs, inventory.watcher)
        if probe.status in {"error", "timeout"}
    )
    open_prs = len(inventory.github_prs.items) if inventory.github_prs.status == "ok" else 0
    return AttentionSummary(
        dirty_worktrees=len(dirty),
        detached_worktrees=len(detached),
        worktree_status_errors=len(status_errors),
        gone_upstream_branches=len(inventory.gone_upstream_branches),
        merged_branch_candidates=len(inventory.merged_branch_candidates),
        old_stashes=len(inventory.old_stashes),
        open_github_prs=open_prs,
        watcher_unresolved_lines=len(watcher_lines),
        probe_errors=probe_errors,
    )


def parse_attention_keys(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()

    selected: list[str] = []
    unknown: list[str] = []
    for part in raw.split(","):
        key = part.strip().replace("-", "_")
        if not key:
            continue
        if key in ATTENTION_GROUPS:
            selected.extend(ATTENTION_GROUPS[key])
        elif key in ATTENTION_KEYS:
            selected.append(key)
        else:
            unknown.append(part.strip())

    if unknown:
        choices = ", ".join(sorted((*ATTENTION_GROUPS.keys(), *ATTENTION_KEYS)))
        raise ValueError(
            f"unknown attention key(s): {', '.join(unknown)}; choices: {choices}"
        )

    return tuple(dict.fromkeys(selected))


def selected_attention_total(summary: AttentionSummary, keys: tuple[str, ...]) -> int:
    return sum(getattr(summary, key) for key in keys)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="repo directory to inspect")
    parser.add_argument(
        "--base",
        default="master",
        help="base branch for merged/unmerged branch classification",
    )
    parser.add_argument(
        "--stash-days",
        type=int,
        default=DEFAULT_STASH_DAYS,
        help=f"age threshold for old stashes (default: {DEFAULT_STASH_DAYS})",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--fail-on-attention",
        nargs="?",
        const="all",
        default="",
        metavar="KEYS",
        help=(
            "exit 1 when selected attention items are present; optional KEYS is "
            "comma-separated and may include all, worktrees, branches, stashes, "
            "github, watcher, probes, or exact attention keys"
        ),
    )
    parser.add_argument("--limit", type=int, default=20, help="max rows per text section")
    parser.add_argument("--no-github", action="store_true", help="skip gh PR probe")
    parser.add_argument("--github-repo", default="", help="GitHub owner/repo override")
    parser.add_argument(
        "--github-timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="seconds before gh PR probe times out",
    )
    parser.add_argument("--no-watcher", action="store_true", help="skip watcher probe")
    parser.add_argument(
        "--watcher-timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="seconds before watcher probe times out",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        fail_keys = parse_attention_keys(args.fail_on_attention)
    except ValueError as exc:
        parser.error(str(exc))

    inventory = build_inventory(args)
    if args.json:
        print(json.dumps(to_jsonable(inventory), indent=2, sort_keys=True))
    else:
        print_text_report(inventory, limit=args.limit)
    if fail_keys and selected_attention_total(attention_summary(inventory), fail_keys):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
