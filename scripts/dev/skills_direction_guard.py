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
POSITIVELY shown to be newer — both sides carry a date and the mirror's is
strictly older. Every other combination refuses.

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
import sys

_LAST_VERIFIED = re.compile(r'^last_verified:\s*"?([\d-]+)"?', re.M)

EXIT_OK = 0
EXIT_BLOCKED = 4


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
                blocked.append(
                    f"{name}: mirror ({mv}) is same date as, but differs from, "
                    f"canonical ({cv})"
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
