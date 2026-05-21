#!/usr/bin/env python3
"""Enroll a substrate-anchored resident in core.substrate_claims (S19).

Operator pre-seed enrollment
v2 §Enrollment workflow. Run BEFORE the resident first connects: this closes
the launchctl-bootstrap adversary (proposal Q3 (b)) by removing the
trust-on-first-use race.

Usage:
    scripts/ops/enroll_resident.py \\
        --agent-id <UUID> \\
        --launchd-label com.unitares.sentinel \\
        --executable /opt/homebrew/bin/sentinel \\
        [--notes "deployed 2026-04-25 by kenny"] \\
        [--allow-user-writable]

The CLI inspects the executable path's parent chain and emits a loud warning
to stderr when any ancestor is user-writable (i.e. a same-UID adversary
could substitute the binary; see proposal v2 §Adversary models A2-escalated).
The warning is informational, not blocking, by design — the operator
chooses whether to harden the deployment. Pass --allow-user-writable to
suppress the *exit-non-zero* behavior on user-writable paths in
non-interactive scripts; the warning is always written.

Idempotent: re-running with the same agent-id updates the row in place.
This is the right behavior for re-enrollment after deployment changes
(binary moves, label rename); use --force to also update enrolled_at.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from src.substrate.path_safety import (  # noqa: E402
    first_user_writable_ancestor,
    is_path_user_writable,
)


# Matches launchd label conventions: reverse-DNS, lowercase plus digits/dots/dashes.
# Permissive enough to accept third-party labels but strict enough to catch typos.
_LABEL_RE = re.compile(r"^[a-z][a-z0-9._-]+\.[a-z0-9._-]+$")


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            f"agent-id must be a valid UUID, got {value!r}"
        ) from exc


def _validate_label(value: str) -> str:
    if not _LABEL_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"launchd-label must be a reverse-DNS string (e.g. com.unitares.sentinel), got {value!r}"
        )
    return value


def _validate_executable(value: str) -> str:
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise argparse.ArgumentTypeError(
            f"executable path must be absolute, got {value!r}"
        )
    resolved = p.resolve()
    if not resolved.exists():
        raise argparse.ArgumentTypeError(
            f"executable path does not exist: {resolved}"
        )
    if not os.access(str(resolved), os.X_OK):
        raise argparse.ArgumentTypeError(
            f"executable path is not executable: {resolved}"
        )
    return str(resolved)


def _emit_user_writable_warning(executable: str) -> bool:
    """Inspect ``executable`` and emit a loud warning if user-writable.

    Returns True when the path IS user-writable (caller decides whether
    to exit non-zero based on --allow-user-writable).
    """
    if not is_path_user_writable(executable):
        return False

    weak = first_user_writable_ancestor(executable) or executable
    print(
        "\n" + "=" * 78,
        "DEPLOYMENT-RISK WARNING — same-UID-writable executable path",
        "=" * 78,
        f"  executable: {executable}",
        f"  weak link:  {weak}",
        "",
        "  A same-UID process can replace the binary at this path before",
        "  invoking `launchctl kickstart` to spawn under the registered",
        "  label — defeating M3's launchd-label match (proposal v2",
        "  §Adversary models A2-escalated).",
        "",
        "  Mitigation options:",
        "    1. Move the binary to a non-user-writable location",
        "       (e.g. /opt/homebrew/bin or /usr/local/bin) and re-enroll.",
        "    2. Accept the residual risk: M3 still attests launchd identity",
        "       and process instance, just not binary immutability.",
        "",
        "  Enrollment will proceed; this warning is informational.",
        "=" * 78 + "\n",
        sep="\n",
        file=sys.stderr,
    )
    return True


async def _upsert_substrate_claim(
    agent_id: str,
    label: str,
    executable: str,
    notes: str | None,
    force_timestamp: bool,
) -> str:
    """Insert or update the substrate-claim row.

    Returns "inserted" or "updated" for the caller to log. ``force_timestamp``
    when True also resets ``enrolled_at`` (use when the operator wants to
    record the re-enrollment as a fresh deployment event).
    """
    # Defer DB import until we actually need it so --help and validation
    # paths don't pay the asyncpg/connection cost.
    from src.db import get_db

    db = get_db()
    async with db.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM core.substrate_claims WHERE agent_id = $1",
            agent_id,
        )
        if existing:
            if force_timestamp:
                await conn.execute(
                    """
                    UPDATE core.substrate_claims
                    SET expected_launchd_label = $2,
                        expected_executable_path = $3,
                        notes = $4,
                        enrolled_at = NOW(),
                        enrolled_by_operator = TRUE
                    WHERE agent_id = $1
                    """,
                    agent_id, label, executable, notes,
                )
            else:
                await conn.execute(
                    """
                    UPDATE core.substrate_claims
                    SET expected_launchd_label = $2,
                        expected_executable_path = $3,
                        notes = $4,
                        enrolled_by_operator = TRUE
                    WHERE agent_id = $1
                    """,
                    agent_id, label, executable, notes,
                )
            return "updated"
        else:
            await conn.execute(
                """
                INSERT INTO core.substrate_claims
                    (agent_id, expected_launchd_label, expected_executable_path,
                     enrolled_by_operator, notes)
                VALUES ($1, $2, $3, TRUE, $4)
                """,
                agent_id, label, executable, notes,
            )
            return "inserted"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
 epilog=" for the full design.",
    )
    parser.add_argument(
        "--agent-id", required=True, type=_validate_uuid,
        help="UUID of the resident to enroll (must already exist in core.agents).",
    )
    parser.add_argument(
        "--launchd-label", required=True, type=_validate_label,
        help="Expected launchd label (e.g. com.unitares.sentinel).",
    )
    parser.add_argument(
        "--executable", required=True, type=_validate_executable,
        help="Absolute path to the resident's executable binary.",
    )
    parser.add_argument(
        "--notes", default=None,
        help="Free-form notes to record at enrollment time.",
    )
    parser.add_argument(
        "--allow-user-writable", action="store_true",
        help=(
            "Permit enrollment even if the executable path is same-UID-writable. "
            "Without this flag, a writable path causes the script to exit non-zero "
            "after writing the warning. The DB row is still inserted in either case."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help="On re-enrollment, also reset enrolled_at to now.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate inputs and emit the writability warning, but do not touch the DB.",
    )

    args = parser.parse_args(argv)

    # Path-safety check is the load-bearing user-facing behavior. Always run.
    is_writable = _emit_user_writable_warning(args.executable)

    if args.dry_run:
        print(
            f"[dry-run] would enroll: agent_id={args.agent_id} "
            f"label={args.launchd_label} executable={args.executable}",
            file=sys.stderr,
        )
        return 1 if (is_writable and not args.allow_user_writable) else 0

    try:
        action = asyncio.run(
            _upsert_substrate_claim(
                args.agent_id,
                args.launchd_label,
                args.executable,
                args.notes,
                args.force,
            )
        )
    except Exception as exc:
        print(f"[error] enrollment failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[ok] {action} substrate_claim: agent_id={args.agent_id} "
        f"label={args.launchd_label} executable={args.executable}",
        file=sys.stderr,
    )

    if is_writable and not args.allow_user_writable:
        # Row is in the DB; we still exit non-zero so non-interactive
        # callers notice the deployment-risk warning.
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
