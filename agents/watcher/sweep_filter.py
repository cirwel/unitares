"""Keep only sweep candidates that were really edited.

``watcher-sweep.sh`` finds candidates by modification time. A file's mtime
also moves when git writes it, so creating a worktree, switching branches or
pulling makes every file it writes look edited. Measured 2026-11-05: one sweep
saw 1,256 "changed" files, nearly all from fresh worktree checkouts, and
re-flagged existing code once per copy.

A candidate inside a git work tree is kept when git reports it modified
(staged or not) or untracked, or when a commit made since the sweep's
watermark touched it (an edit committed before the sweep ran). A candidate outside any work tree has no
git history to consult and is kept, as before.

Every git read runs with ``GIT_OPTIONAL_LOCKS=0`` so a status-style command
never refreshes and rewrites the index of a work tree another session is using.
Any git failure keeps the candidate: the worst case is the old behavior, never
a silently skipped edit.

Usage: candidate paths on stdin, one per line, resolved against the current
directory; kept paths on stdout, absolute.

    python3 -m agents.watcher.sweep_filter --since-file ~/.unitares/watcher-sweep.state
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

GIT_TIMEOUT_S = 10.0


def _git(args: list[str], cwd: str) -> str | None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _toplevel(directory: str, cache: dict[str, str | None]) -> str | None:
    if directory not in cache:
        out = _git(["rev-parse", "--show-toplevel"], directory)
        cache[directory] = out.strip() if out and out.strip() else None
    return cache[directory]


def _edited_in(toplevel: str, since_iso: str | None) -> set[str] | None:
    """Absolute, resolved paths edited in this work tree, or None on failure."""
    # Unstaged modifications and untracked files.
    dirty = _git(["ls-files", "-z", "-m", "-o", "--exclude-standard"], toplevel)
    # Staged changes: `ls-files -m` omits an edit once it is `git add`-ed, and
    # the watermark then moves past it, so a staged edit would never be seen.
    # Without a revision, `diff --cached` also works on an unborn branch.
    staged = _git(["diff", "--cached", "--name-only", "-z"], toplevel)
    if dirty is None or staged is None:
        return None
    names = {n for n in dirty.split("\0") if n} | {n for n in staged.split("\0") if n}
    # An unborn branch (no commit yet) has no history, and `git log HEAD`
    # fails there; skip it rather than let that failure fail the whole tree open.
    if since_iso is not None and _git(["rev-parse", "--verify", "-q", "HEAD"], toplevel) is not None:
        # --cc: a merge commit otherwise lists no files, so an edit made while
        # resolving a conflict and committed before the sweep would be missed.
        # It lists only files whose merge result differs from every parent,
        # not everything the merge brought in.
        committed = _git(
            ["log", f"--since={since_iso}", "--cc", "--format=", "--name-only", "-z", "HEAD"],
            toplevel,
        )
        if committed is None:
            return None
        names.update(n.strip("\n") for n in committed.split("\0") if n.strip("\n"))
    root = Path(toplevel)
    return {str((root / name).resolve()) for name in names}


def filter_candidates(paths: list[str], since_epoch: float | None = None) -> list[str]:
    since_iso = (
        datetime.fromtimestamp(since_epoch, timezone.utc).isoformat()
        if since_epoch is not None
        else None
    )
    toplevels: dict[str, str | None] = {}
    edited: dict[str, set[str] | None] = {}
    kept: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        # Absolute from here on: a relative scan root is relative to the
        # caller's directory, and later steps may run from elsewhere.
        path = os.path.abspath(path)
        directory = str(Path(path).parent)
        if not os.path.isdir(directory):
            continue
        top = _toplevel(directory, toplevels)
        if top is None:
            kept.append(path)  # not in a work tree: no history to consult
            continue
        if top not in edited:
            edited[top] = _edited_in(top, since_iso)
        changed = edited[top]
        if changed is None or str(Path(path).resolve()) in changed:
            kept.append(path)  # git failed (fail open) or a real edit
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since-epoch",
        type=float,
        default=None,
        help="sweep watermark; commits after it count as edits",
    )
    parser.add_argument(
        "--since-file",
        default=None,
        help="sweep watermark file; its mtime is the watermark (portable, "
        "unlike BSD `stat -f`); ignored when absent",
    )
    args = parser.parse_args(argv)
    since = args.since_epoch
    if since is None and args.since_file:
        try:
            since = os.stat(args.since_file).st_mtime
        except OSError:
            since = None
    for path in filter_candidates(sys.stdin.read().splitlines(), since):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
