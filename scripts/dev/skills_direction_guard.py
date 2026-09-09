#!/usr/bin/env python3
"""Refuse a skills sync that would revert newer mirror work.

``sync-plugin-skills.sh`` clobbers the plugin mirror with canonical content.
Its uncommitted-changes check only catches a DIRTY mirror; a mirror that was
edited and COMMITTED is indistinguishable from a stale one, so rsync overwrites
it, the script prints "done", and a later run reports "in sync — nothing to do".
The revert leaves no signal anywhere.

``last_verified`` is the right signal because it is a DECLARED verification
date, not a filesystem timestamp — it survives checkout, rsync and worktree
creation, all of which destroy mtime.

THE RULE: when content differs, the sync proceeds only if canonical can be
POSITIVELY shown to be newer. Two things can show that, and nothing else does:
the mirror's declared date is strictly older, or -- when the dates are equal --
the mirror's exact bytes appear as a past version of that file in canonical's
git history, which means canonical has already moved past it. Every other
combination refuses.

The equal-date appeal to history exists because a mirror produced by an earlier
sync inherits canonical's date verbatim. A canonical edit later the same day
then leaves both sides reading one date with different content, which by date
alone is indistinguishable from the #112 case below. It is not indistinguishable
by history: a stale snapshot is a state canonical held, and mirror-side work
canonical never had is not.

That direction is deliberate, and it is the correction to two earlier bugs:

  * ``>`` instead of ``>=`` (fixed 2026-08-09): an EQUAL date with DIFFERENT
    content is the more common hazard. Plugin #112 merged the mirror's content
    ahead of canonical while both sides still read 2026-07-28, because the
    content PR and the freshness PR were split. A ``>`` test sees equal dates
    and waves it past — straight into the revert it exists to prevent.

  * failing OPEN on a missing date (fixed 2026-08-09): the guard required BOTH
    dates before it would refuse, so a mirror carrying newer work but no
    ``last_verified`` line was silently reverted — the exact silent-data-loss
    class this guard exists to prevent, and the opposite of the cost asymmetry
    stated below.

The asymmetry drives all of it: the cost of a false refusal is one forward-port
command, the cost of a false pass is silently deleting merged work.

Usage:
    python3 scripts/dev/skills_direction_guard.py <canonical_dir> <mirror_dir>

Exits 0 when the sync may proceed, 4 when it must not. Blocking reasons go to
stdout, one per line, so the caller can print them.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

_LAST_VERIFIED = re.compile(r'^last_verified:\s*"?([\d-]+)"?', re.M)

EXIT_OK = 0
EXIT_BLOCKED = 4

# A git call that hangs would hang the sync. These are local object reads.
_GIT_TIMEOUT_SECONDS = 30


def _git(repo: pathlib.Path, *args: str) -> str | None:
    """Run git in ``repo``, or return None if it cannot be run at all.

    Every failure path returns None so the caller blocks. A guard whose
    tie-break errors must refuse, not wave the sync past.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_past_canonical_state(canon: pathlib.Path, mirror: pathlib.Path) -> bool:
    """True when the mirror's bytes are a state canonical has already moved past.

    Breaks the equal-date tie that ``last_verified`` alone cannot. A mirror
    produced by an earlier sync inherits canonical's date verbatim, so a
    same-day canonical edit leaves both sides reading the same date with
    different content -- indistinguishable, by date, from the plugin #112 case
    where the mirror carried content canonical never had.

    Git history separates them. If the mirror's exact bytes appear as a past
    version of this file in canonical's history, the mirror is a stale snapshot
    and syncing forward reverts nothing. If they do not, the mirror holds
    content canonical never had, which is #112, and the sync must still refuse.

    Blob ids are compared rather than file contents: git already content-hashes
    every version, so one ``log --raw`` names every past state in a single call.
    """
    repo_root = _git(canon.parent, "rev-parse", "--show-toplevel")
    if repo_root is None:
        return False  # not a git checkout — no history to appeal to
    repo = pathlib.Path(repo_root.strip())

    try:
        rel = canon.resolve().relative_to(repo.resolve())
    except (OSError, ValueError):
        return False

    blob = _git(repo, "hash-object", "--", str(mirror))
    if blob is None:
        return False
    mirror_blob = blob.strip()
    if not mirror_blob:
        return False

    # --raw names the post-image blob of every commit that touched the path,
    # walking from HEAD, so every id it yields is a state an ancestor held.
    history = _git(repo, "log", "--raw", "--no-abbrev", "--format=", "--", str(rel))
    if history is None:
        return False

    for line in history.splitlines():
        if not line.startswith(":"):
            continue
        fields = line.split()
        # :<oldmode> <newmode> <oldblob> <newblob> <status>\t<path>
        if len(fields) >= 4 and fields[3] == mirror_blob:
            return True
    return False


def last_verified(path: pathlib.Path) -> str | None:
    """Return the declared ``last_verified`` date, or None if absent/unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _LAST_VERIFIED.search(text)
    return match.group(1) if match else None


def regressions(src: pathlib.Path, dst: pathlib.Path) -> list[str]:
    """Reasons the mirror must not be overwritten, one per drifted skill."""
    blocked: list[str] = []
    for mirror in sorted(dst.glob("*/SKILL.md")):
        canon = src / mirror.parent.name / "SKILL.md"
        if not canon.exists():
            # Mirror-only skill. rsync --delete drops it by design (canonical is
            # authoritative for WHICH skills exist); not this guard's call.
            continue
        try:
            if canon.read_bytes() == mirror.read_bytes():
                continue
        except OSError:
            # Unreadable on either side — refuse rather than guess.
            blocked.append(f"{mirror.parent.name}: unreadable, cannot compare")
            continue

        name = mirror.parent.name
        cv, mv = last_verified(canon), last_verified(mirror)

        if cv and mv:
            if mv > cv:
                blocked.append(f"{name}: mirror ({mv}) is newer than canonical ({cv})")
            elif mv == cv:
                # A same-date difference is ambiguous by date alone, so appeal
                # to canonical's history before refusing. Passing here still
                # requires canonical to be POSITIVELY newer; git just supplies
                # the proof the dates cannot.
                if not is_past_canonical_state(canon, mirror):
                    blocked.append(
                        f"{name}: mirror ({mv}) is same date as, but differs from, "
                        f"canonical ({cv}), and its content is not a past state of "
                        f"canonical"
                    )
            # mv < cv: canonical is positively newer — the one passing case.
        elif mv and not cv:
            blocked.append(
                f"{name}: mirror declares last_verified ({mv}), canonical declares none"
            )
        elif cv and not mv:
            blocked.append(
                f"{name}: mirror declares no last_verified, canonical says {cv} — "
                "cannot show canonical is newer"
            )
        else:
            blocked.append(
                f"{name}: neither side declares last_verified and the content differs"
            )
    return blocked


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <canonical_dir> <mirror_dir>", file=sys.stderr)
        return 2
    src, dst = (pathlib.Path(p) for p in argv[1:3])
    blocked = regressions(src, dst)
    if blocked:
        print("\n".join(blocked))
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
