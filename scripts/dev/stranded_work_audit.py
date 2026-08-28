#!/usr/bin/env python3
"""Stranded-work audit — find branch work that silently never reached master.

The failure mode this catches (bit the repo through 2026-07-01, when a sweep
found 10 branches carrying it): a PR merges, then follow-up commits are pushed
to the same branch. GitHub does not reopen the PR, auto-delete-on-merge does
not fire (the re-push recreates the branch), and no surface ever reports the
gap — so real fixes strand on merged-PR branches indefinitely. A common driver
is a push rejected by a guard (e.g. the repo scope guard) late in a session:
the commit stays on the branch, the session ends, nobody re-lands it.

Classification per remote branch (skips master/main, archive/*, backup/*):

  STRANDED  newest PR for the branch is MERGED, the branch HEAD advanced past
            the merged head, and the post-merge file state has never existed
            in master. Real work is marooned -> re-land or explicitly discard.
            THE ALARM CLASS.
  PRUNABLE  branch content is fully contained in master (merged ghost) -> safe
            to delete, pure hygiene.
  DANGLING-REVIEW
            unique commits but no PR route (no PR ever, or newest PR CLOSED
            unmerged), with a recent tip or a deploy-sensitive path. Requires
            an explicit re-land/discard/park decision. THE SECOND ALARM CLASS.
  INDETERMINATE
            newest PR is MERGED and the branch advanced past it, but the merged
            head could not be fetched, so landed work and stranded work cannot
            be told apart. NOT an instruction to re-land — see
            `resolve_merged_head` for why this is its own class rather than a
            fallback to STRANDED.
  DANGLING-STALE
            older unique commits with no PR route and no deploy-sensitive path.
            Preserved as parked/abandoned work; informational only.
  ACTIVE    newest PR is OPEN, or the branch tip is younger than --active-days.
            Not reported.

Every reported finding also carries a content-presence reading: what share of
its commits' added lines already sit in master today. That is a TRIAGE HINT and
never a classification input -- see `content_presence`. It exists because the
classes above are correct but slow to act on at volume: a 2026-08-28 sweep found
75 branches carrying content not patch-equivalent to master, of which exactly one
had genuinely never landed. The reading is what separates those cases without
reading all 190 commits by hand.

Uses only git + the `gh` CLI (GITHUB_TOKEN scope; no metered APIs).

Usage:
  python3 scripts/dev/stranded_work_audit.py [--repo owner/name] [--json]
                                             [--check] [--active-days N]
                                             [--review-days N]

  --check exits 1 when STRANDED or DANGLING-REVIEW work is found.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

SKIP_EXACT = {
    "master",
    "main",
    "HEAD",
    "origin",
    "gh-pages",
}  # "origin" = the origin/HEAD symref
SKIP_PREFIXES = ("archive/", "backup/")
SENSITIVE_PATH_PREFIXES = (
    "db/postgres/migrations/",
    "scripts/ops/",
)
SENSITIVE_PATH_SUFFIXES = (".plist", ".plist.template")

# Content-presence sampling (triage hint only — see `content_presence`).
_TRIVIAL_LINE = re.compile(r"^[\s\W]*$")
_MIN_SAMPLED_LINE = 12   # shorter lines collide across unrelated files
_MAX_SAMPLED_COMMITS = 20


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def succeeds(*cmd: str) -> bool:
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def remote_branches() -> list[str]:
    out = run("git", "for-each-ref", "refs/remotes/origin", "--format=%(refname:short)")
    branches = []
    for ref in out.splitlines():
        name = ref.removeprefix("origin/")
        if name in SKIP_EXACT or name.startswith(SKIP_PREFIXES):
            continue
        branches.append(name)
    return branches


def newest_pr(repo: str, branch: str) -> dict | None:
    out = run(
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "number,state,headRefOid",
    )
    prs = json.loads(out)
    return prs[0] if prs else None


def unmerged_patch_commits(branch: str, since: str | None = None) -> list[str]:
    """Commits on the branch whose patch-id is absent from origin/master.

    `since` restricts to commits after a known-merged head (the post-merge
    advance); without it the whole branch is compared.
    """
    args = ["git", "cherry", "origin/master", f"origin/{branch}"]
    if since:
        args.append(since)
    out = run(*args)
    return [line.split()[1] for line in out.splitlines() if line.startswith("+ ")]


def unmerged_patch_count(branch: str, since: str | None = None) -> int:
    return len(unmerged_patch_commits(branch, since))


def tip_age_days(branch: str) -> int:
    ts = int(run("git", "log", "-1", "--format=%ct", f"origin/{branch}").strip())
    return int((time.time() - ts) // 86400)


def _annotate(finding: dict, commits: list[str]) -> dict:
    """Attach the content-presence hint to a finding, leaving its class alone.

    Deliberately applied AFTER the class is decided and never read back: the
    measurement informs the reader, it does not vote.
    """
    presence = content_presence(commits)
    if presence:
        finding["content_presence"] = presence
        finding["detail"] += (
            f"; {presence['pct']}% of {presence['total']} added line(s)"
            " already in master"
        )
        if presence["commits_sampled"] < presence["commits_total"]:
            finding["detail"] += (
                f" (first {presence['commits_sampled']} of"
                f" {presence['commits_total']} commits sampled)"
            )
    return finding


def audit(repo: str, active_days: int, review_days: int = 14) -> list[dict]:
    findings = []
    for branch in remote_branches():
        sha = run("git", "rev-parse", f"origin/{branch}").strip()
        pr = newest_pr(repo, branch)
        age = tip_age_days(branch)

        if pr and pr["state"] == "OPEN":
            continue  # ACTIVE: the PR is the tracking surface

        if pr and pr["state"] == "MERGED":
            merged_head = pr["headRefOid"]
            if merged_head == sha:
                findings.append(
                    {
                        "branch": branch,
                        "class": "PRUNABLE",
                        "detail": f"PR #{pr['number']} merged at this exact head; "
                        "auto-delete missed it (likely re-push residue)",
                    }
                )
                continue
            # Branch advanced past the merged head — is the advance in master?
            since = resolve_merged_head(merged_head, pr["number"])
            if since is None:
                # Comparing the whole branch here is what manufactured the
                # false alarms: without the limit `git cherry` reports every
                # commit the PR squashed. Say "cannot tell" instead of "real
                # work is marooned"; the two call for opposite actions.
                findings.append(
                    {
                        "branch": branch,
                        "class": "INDETERMINATE",
                        "detail": f"PR #{pr['number']} merged, branch advanced "
                        f"past head {merged_head[:8]}, but that head could not "
                        "be fetched — cannot distinguish landed from stranded",
                    }
                )
                continue
            unmerged = unmerged_patch_commits(branch, since)
            if unmerged and _end_state_differs(branch, unmerged):
                findings.append(
                    _annotate(
                        {
                            "branch": branch,
                            "class": "STRANDED",
                            "detail": f"{len(unmerged)} commit(s) pushed after PR "
                            f"#{pr['number']} merged; patches NOT in master",
                        },
                        unmerged,
                    )
                )
            else:
                findings.append(
                    {
                        "branch": branch,
                        "class": "PRUNABLE",
                        "detail": f"advanced past merged PR #{pr['number']} but all "
                        "patches landed in master",
                    }
                )
            continue

        # No PR, or newest PR closed-unmerged.
        if age < active_days:
            continue  # ACTIVE: recent tip, assume a session is still on it
        unique = unmerged_patch_count(branch)
        if unique == 0:
            findings.append(
                {
                    "branch": branch,
                    "class": "PRUNABLE",
                    "detail": "no open route needed — content is in master",
                }
            )
        else:
            route = f"PR #{pr['number']} CLOSED unmerged" if pr else "no PR ever opened"
            sensitive = sensitive_paths(branch)
            reasons = []
            if age < review_days:
                reasons.append(f"recent tip ({age}d < {review_days}d)")
            if sensitive:
                shown = ", ".join(sensitive[:3])
                if len(sensitive) > 3:
                    shown += f" (+{len(sensitive) - 3} more)"
                reasons.append(f"sensitive path: {shown}")
            classification = "DANGLING-REVIEW" if reasons else "DANGLING-STALE"
            detail = f"{unique} unique commit(s), {route}, tip {age}d old"
            if reasons:
                detail += "; " + "; ".join(reasons)
            findings.append(
                _annotate(
                    {"branch": branch, "class": classification, "detail": detail},
                    unmerged_patch_commits(branch),
                )
            )
    return findings


def _paths_touched_by(commits: list[str]) -> list[str]:
    paths = set()
    for commit in commits:
        paths.update(
            run(
                "git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit
            ).splitlines()
        )
    return sorted(path for path in paths if path)


def _end_state_differs(branch: str, commits: list[str]) -> bool:
    """Return whether the post-merge work has no demonstrated home on master.

    ``git cherry`` misses squash/reworked landings. First compare the current
    end state, then inspect master's history for a commit where every file
    touched by the allegedly stranded commits exactly matched the branch. If
    that state once existed, the work landed and later evolution must not turn
    the old branch back into an alarm.
    """
    try:
        touched = _paths_touched_by(commits)
        if not touched:
            return False
        if succeeds(
            "git",
            "diff",
            "--quiet",
            "origin/master",
            f"origin/{branch}",
            "--",
            *touched,
        ):
            return False
        candidates = run(
            "git", "log", "--format=%H", "origin/master", "--", *touched
        ).splitlines()
        return not any(
            succeeds(
                "git", "diff", "--quiet", candidate, f"origin/{branch}", "--", *touched
            )
            for candidate in candidates
        )
    except subprocess.CalledProcessError:
        return True  # can't prove it landed -> keep the alarm


def _master_file(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"origin/master:{path}"], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def _added_lines(commit: str) -> dict[str, list[str]]:
    """Substantive added lines per file for one commit, keyed by post-image path."""
    added: dict[str, list[str]] = {}
    path = None
    for line in run(
        "git", "show", "--format=", "--unified=0", commit
    ).splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            added.setdefault(path, [])
        elif path and line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if len(text) >= _MIN_SAMPLED_LINE and not _TRIVIAL_LINE.match(text):
                added[path].append(text)
    return added


def content_presence(commits: list[str]) -> dict | None:
    """How much of these commits' added content sits in master TODAY.

    A TRIAGE HINT, never a classification input. Nothing in `audit` reads the
    return value to decide a class, and it must stay that way: a fuzzy measure
    that can retire an alarm is instrumentation failing toward "healthy", which
    is this repo's named recurring failure mode (see the module docstring and
    `scripts/dev/stranded_work_audit.py`'s own INDETERMINATE class, which exists
    precisely so a degraded run cannot report clean).

    What it adds is triage speed. `_end_state_differs` proves a landing by exact
    file match against some point in master's history; when a fix landed and the
    file kept evolving afterwards, no such point exists and the branch stays an
    alarm with no way to tell "superseded" from "lost" short of reading it. This
    reports the weaker fact that is still decisive in practice -- 94% of the added
    lines are already in master, so look here last; 0% are, so look here first.

    Measured on a manual sweep of 190 such commits (2026-08-28): the reading
    separated 111 landed-elsewhere commits from the one that had genuinely never
    reached master. It is a percentage, not a verdict; a high number on a
    security fix still needs eyes.

    Returns None when nothing substantive was added (a pure deletion or rename),
    which is not the same as 0% and must not be rendered as one.
    """
    sampled = commits[:_MAX_SAMPLED_COMMITS]
    present = total = 0
    try:
        for commit in sampled:
            for path, lines in _added_lines(commit).items():
                if not lines:
                    continue
                body = _master_file(path) or ""
                present += sum(1 for line in lines if line in body)
                total += len(lines)
    except subprocess.CalledProcessError:
        return None
    if not total:
        return None
    return {
        "present": present,
        "total": total,
        "pct": round(100.0 * present / total, 1),
        "commits_sampled": len(sampled),
        "commits_total": len(commits),
    }


def _is_sensitive_path(path: str) -> bool:
    return path.startswith(SENSITIVE_PATH_PREFIXES) or path.endswith(
        SENSITIVE_PATH_SUFFIXES
    )


def sensitive_paths(branch: str) -> list[str]:
    """Deploy-affecting paths changed by a dangling branch.

    Triple-dot limits the scan to the branch side of its merge base. Failure is
    escalated rather than silently classifying the branch as stale.
    """
    try:
        paths = run(
            "git", "diff", "--name-only", f"origin/master...origin/{branch}"
        ).splitlines()
    except subprocess.CalledProcessError:
        return ["(path scan failed)"]
    return sorted(path for path in paths if _is_sensitive_path(path))


def _known_object(sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
        ).returncode
        == 0
    )


def resolve_merged_head(sha: str, pr_number: int) -> str | None:
    """Make the merged head available locally, fetching it if necessary.

    GitHub deletes the head branch on merge, so its commit object survives in a
    local clone only if that clone happened to fetch it before the delete. This
    script fetches nothing, so on any machine that did not, `sha` is simply
    absent -- and the caller then compared the WHOLE branch instead of the
    post-merge advance. Every commit the PR squashed reads as unlanded and the
    branch is reported STRANDED.

    That is not a cosmetic defect. A false STRANDED entry instructs the reader
    to re-land work that is already in master, which means branch surgery on a
    merged-PR branch -- the operation that lost two pushed commits on
    2026-08-19. The alarm class has to be right in the direction that provokes
    action.

    `refs/pull/N/head` is the durable route: GitHub keeps it after the branch
    is deleted, and it does not depend on the server allowing bare-SHA fetches.
    Returns the sha when it is (now) present locally, else None.
    """
    if _known_object(sha):
        return sha
    for ref in (f"refs/pull/{pr_number}/head", sha):
        if succeeds("git", "fetch", "--quiet", "--no-tags", "origin", ref):
            if _known_object(sha):
                return sha
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="cirwel/unitares")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if STRANDED or DANGLING-REVIEW work is found",
    )
    ap.add_argument(
        "--active-days",
        type=int,
        default=7,
        help="tips younger than this with no merged PR are ACTIVE (default 7)",
    )
    ap.add_argument(
        "--review-days",
        type=int,
        default=14,
        help="dangling tips younger than this require review (default 14)",
    )
    args = ap.parse_args()
    if args.review_days < args.active_days:
        ap.error("--review-days must be greater than or equal to --active-days")

    findings = audit(args.repo, args.active_days, args.review_days)
    order = {
        "STRANDED": 0,
        "INDETERMINATE": 1,
        "DANGLING-REVIEW": 2,
        "DANGLING-STALE": 3,
        "PRUNABLE": 4,
    }
    findings.sort(key=lambda f: (order[f["class"]], f["branch"]))

    if args.as_json:
        print(json.dumps(findings, indent=2))
    elif not findings:
        print("stranded-work audit: clean — no stranded/dangling/prunable branches.")
    else:
        width = max(len(f["branch"]) for f in findings)
        class_width = max(len(f["class"]) for f in findings)
        for f in findings:
            print(f"{f['class']:<{class_width}} {f['branch']:<{width}}  {f['detail']}")

    stranded = sum(1 for f in findings if f["class"] == "STRANDED")
    indeterminate = sum(1 for f in findings if f["class"] == "INDETERMINATE")
    dangling_review = sum(1 for f in findings if f["class"] == "DANGLING-REVIEW")
    if indeterminate:
        print(
            f"\n{indeterminate} INDETERMINATE branch(es) — the merged head could "
            "not be fetched, so landed and stranded are indistinguishable. Fetch "
            "the PR head and re-run before acting; do NOT re-land on this alone.",
            file=sys.stderr,
        )
    if stranded:
        print(
            f"\n{stranded} STRANDED branch(es) — real work marooned off master; "
            "re-land (fresh branch off master, cherry-pick, PR) or explicitly discard.",
            file=sys.stderr,
        )
    if dangling_review:
        print(
            f"\n{dangling_review} DANGLING-REVIEW branch(es) — choose re-land, "
            "discard, or explicitly park.",
            file=sys.stderr,
        )
    actionable = stranded + dangling_review
    return 1 if (args.check and actionable) else 0


if __name__ == "__main__":
    sys.exit(main())
