#!/usr/bin/env python3
"""Helper for ship.sh: Phase A advisory-mode lease around the ship operation.

Two subcommands:

    acquire --surface-id <id> --surface-kind <kind> [--intent <s>] [--ttl-s <n>]
        Always exits 0 (Phase A is non-fatal). Stdout is a single JSON line:
            {"outcome": "...", "lease_id": "..." | null}
        Outcomes: acquired_new, acquired_idempotent, held_by_other,
                  service_unavailable, permission_denied, schema_invalid,
                  client_error.

    release --lease-id <uuid>
        Always exits 0. Stdout: {"ok": true|false}.

Per RFC v0.5 §6.1: failed acquire MUST NOT block the ship. ship.sh logs
the outcome and proceeds regardless.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from uuid import UUID, uuid4

# Ensure the project root is importable when this script is invoked directly
# (e.g., from ship.sh as `python3 scripts/dev/_ship_lease_advisory.py ...`).
# Pytest adds the rootdir to sys.path automatically; bash does not.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.lease_plane import LeasePlaneClient, ReleaseRequest  # noqa: E402
from src.lease_plane.advisory import (  # noqa: E402
    AcquireRequest,
    acquire_advisory,
    make_advisory_client,
)


def _cmd_acquire(args: argparse.Namespace) -> int:
    client: LeasePlaneClient = make_advisory_client()

    request = AcquireRequest(
        surface_id=args.surface_id,
        surface_kind=args.surface_kind,
        holder_agent_uuid=uuid4(),
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=args.ttl_s,
        intent=args.intent,
    )

    outcome, lease_id = acquire_advisory(client, request)

    json.dump(
        {"outcome": outcome, "lease_id": str(lease_id) if lease_id else None},
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    if not args.lease_id:
        json.dump({"ok": False, "reason": "empty lease_id"}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    try:
        lease_id = UUID(args.lease_id)
    except ValueError:
        json.dump({"ok": False, "reason": "invalid lease_id"}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    client = make_advisory_client()
    try:
        result = client.release(ReleaseRequest(lease_id=lease_id, release_reason="normal"))
        ok = bool(getattr(result, "ok", False))
    except Exception as exc:  # defensive
        json.dump({"ok": False, "reason": f"release raised: {exc!r}"}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    json.dump({"ok": ok}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    acq = sub.add_parser("acquire")
    acq.add_argument("--surface-id", required=True)
    acq.add_argument("--surface-kind", required=True)
    acq.add_argument("--intent", default=None)
    acq.add_argument("--ttl-s", type=int, default=300)
    acq.set_defaults(func=_cmd_acquire)

    rel = sub.add_parser("release")
    rel.add_argument("--lease-id", required=True)
    rel.set_defaults(func=_cmd_release)

    args = parser.parse_args(argv)

    # The advisory module logs to its own logger; ship.sh consumes JSON on
    # stdout. Send any logger output to stderr so it doesn't pollute the
    # JSON line bash will parse.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
