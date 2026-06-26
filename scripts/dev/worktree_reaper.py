#!/usr/bin/env python3
"""Reap git worktrees whose PR has merged — safely, with a liveness guard.

Keyed on MERGED pull-request status ONLY, never on commit-ancestry. A naive
"0 commits ahead of master" reaper is unsafe on a live system: a freshly-created
worktree sitting at master is indistinguishable from an abandoned one, so such a
rule can delete a worktree another session just opened. (That exact mistake
happened on 2026-06-18 — the work survived only because the other session had
pushed.) The fix is the signal git lacks on its own: liveness.

Safety properties:
  - Removes a worktree only if its branch has a MERGED PR (squash-merge safe —
    the merged change never appears in master's history, so PR state is the only
    correct signal).
  - Skips any worktree touched within --min-idle-hours (the liveness guard:
    last commit time, index mtime, and HEAD mtime).
  - Skips any worktree with uncommitted changes, and NEVER uses --force, so a
    modification is always a hard backstop against data loss.
  - Dry-run by default; --apply to actually remove. State is re-checked at apply
    time, never from a stale snapshot.
  - Refuses to run if PR status cannot be fetched (no merge data => unsafe).

Distinct from the `clean_gone` skill, which keys on `[gone]` remote-tracking
branches: GitHub only auto-deletes *some* head branches on merge, so a
merged-PR-keyed pass is the complete sweep.

Usage:
    python3 scripts/dev/worktree_reaper.py [--apply] [--min-idle-hours 12]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
LEASE_SURFACE_ID = "resident:/worktree_reaper"
LEASE_TTL_S = 900


def _git(*args: str, cwd: Path = _REPO_ROOT) -> Tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def decide(
    pr_state: Optional[str],
    has_uncommitted_changes: bool,
    idle_hours: Optional[float],
    min_idle_hours: float,
) -> Tuple[str, str]:
    """Pure removal decision. Returns ("remove" | "skip", reason).

    Order matters: a dirty or recently-active worktree is preserved even if its
    PR is merged — merged status authorizes removal, liveness vetoes it.
    """
    if has_uncommitted_changes:
        return "skip", "uncommitted changes (never force-removed)"
    if pr_state != "MERGED":
        return "skip", f"PR not merged (state={pr_state or 'none'})"
    if idle_hours is None:
        return "skip", "liveness unknown — cannot confirm idle"
    if idle_hours < min_idle_hours:
        return "skip", f"active {idle_hours:.1f}h ago (< {min_idle_hours:.0f}h idle floor)"
    return "remove", f"PR merged, idle {idle_hours:.1f}h"


def _pr_states() -> Dict[str, str]:
    """branch -> PR state, via gh. Raises on failure (no data => unsafe)."""
    p = subprocess.run(
        ["gh", "pr", "list", "--state", "all", "--limit", "1000",
         "--json", "headRefName,state"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {p.stderr.strip()[:200]}")
    states: Dict[str, str] = {}
    for pr in json.loads(p.stdout):
        # A branch can have multiple PRs; MERGED wins over CLOSED/OPEN for safety
        # of *removal* only when the latest is merged — but to avoid deleting a
        # branch that has a newer OPEN PR, prefer the non-merged state if present.
        ref = pr["headRefName"]
        st = pr["state"]
        if ref not in states or st in ("OPEN", "CLOSED"):
            states[ref] = st
    return states


def _worktrees() -> List[Tuple[str, str]]:
    """List (path, branch) for each branch-checked-out worktree, excluding main.

    The main worktree is the first entry git reports; it is never a reap target
    regardless of which worktree the script is invoked from.
    """
    out = _git("worktree", "list", "--porcelain")[1]
    result: List[Tuple[str, str]] = []
    path = None
    is_first = True
    main_path: Optional[Path] = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if is_first:
                main_path = Path(path).resolve()
                is_first = False
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
            resolved = Path(path).resolve() if path else None
            if resolved is not None and resolved != _REPO_ROOT and resolved != main_path:
                result.append((path, branch))
    return result


def _idle_hours(path: str) -> Optional[float]:
    """Hours since the worktree was last active.

    Uses the most recent of: last commit time, the per-worktree index mtime, and
    HEAD mtime. Writes (edits, adds, commits, checkouts) all bump one of these,
    so a freshly-touched worktree reads as recently active.
    """
    now = time.time()
    stamps: List[float] = []
    rc, ct = _git("-C", path, "log", "-1", "--format=%ct")
    if rc == 0 and ct.isdigit():
        stamps.append(float(ct))
    rc, gitdir = _git("-C", path, "rev-parse", "--absolute-git-dir")
    if rc == 0 and gitdir:
        for fname in ("index", "HEAD", "logs/HEAD"):
            f = Path(gitdir) / fname
            try:
                stamps.append(f.stat().st_mtime)
            except OSError:
                pass
    if not stamps:
        return None
    return max(0.0, (now - max(stamps)) / 3600.0)


def _run_reaper(args: argparse.Namespace) -> int:
    try:
        pr_states = _pr_states()
    except Exception as exc:
        print(f"REFUSING TO RUN: {exc}", file=sys.stderr)
        return 2

    plan = []
    for path, branch in _worktrees():
        dirty = bool(_git("-C", path, "status", "--porcelain")[1])
        idle = _idle_hours(path)
        action, reason = decide(pr_states.get(branch), dirty, idle, args.min_idle_hours)
        plan.append({"branch": branch, "path": path, "action": action, "reason": reason})

    to_remove = [p for p in plan if p["action"] == "remove"]
    removed, failed = [], []
    if args.apply:
        for item in to_remove:
            # Re-check cleanliness at apply time (no stale snapshot).
            if _git("-C", item["path"], "status", "--porcelain")[1]:
                item["action"], item["reason"] = "skip", "became dirty before removal"
                continue
            rc, _ = _git("worktree", "remove", item["path"])  # no --force, ever
            if rc != 0:
                failed.append(item["branch"])
                continue
            _git("branch", "-D", item["branch"])
            removed.append(item["branch"])

    if args.json:
        print(json.dumps({"plan": plan, "removed": removed, "failed": failed,
                          "applied": args.apply, "min_idle_hours": args.min_idle_hours},
                         indent=2))
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Worktree reaper [{mode}] — merged-PR-keyed, >= {args.min_idle_hours:.0f}h idle\n")
    elig = [p for p in plan if p["action"] == "remove"]
    print(f"Eligible for removal: {len(elig)}")
    for p in elig:
        print(f"  - {p['branch']:<52} ({p['reason']})")
    skipped = [p for p in plan if p["action"] == "skip"]
    print(f"\nKept: {len(skipped)}")
    for p in skipped:
        print(f"  - {p['branch']:<52} {p['reason']}")
    if args.apply:
        print(f"\nRemoved: {len(removed)}   Failed: {len(failed)}")
        for b in failed:
            print(f"  ! failed: {b}")
    else:
        print(f"\n(dry-run — re-run with --apply to remove the {len(elig)} eligible)")
    return 0


def _lease_scope(*, apply: bool, min_idle_hours: float):
    from unitares_sdk.lease_plane import advisory as lease_advisory

    mode = "apply" if apply else "dry-run"
    return lease_advisory.lease_advisory_scope(
        surface_id=LEASE_SURFACE_ID,
        holder_agent_uuid=lease_advisory.new_holder_uuid(),
        ttl_s=LEASE_TTL_S,
        intent=f"worktree reaper {mode} min_idle_hours={min_idle_hours:g}",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually remove eligible worktrees (default: dry-run).")
    ap.add_argument("--min-idle-hours", type=float, default=12.0,
                    help="Skip worktrees active more recently than this (default 12).")
    ap.add_argument("--json", action="store_true", help="Emit JSON.")
    args = ap.parse_args(argv)

    with _lease_scope(apply=args.apply, min_idle_hours=args.min_idle_hours):
        return _run_reaper(args)


if __name__ == "__main__":
    raise SystemExit(main())
