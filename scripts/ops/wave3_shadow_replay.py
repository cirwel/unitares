#!/usr/bin/env python3
"""Wave 3 §8.3 shadow-replay harness. See wave3-shadow-replay.sh for context.

Replays a JSONL capture of production requests against --target, compressing
inter-arrival gaps by --rate (2.0 = twice the original rate). Stdlib-only so
it runs anywhere the repo checks out.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

LIVE_MCP_MARKERS = (":8767",)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True, type=Path, help="JSONL capture file")
    p.add_argument(
        "--target",
        required=True,
        help="Base URL of the shadow path (no default, deliberately)",
    )
    p.add_argument("--rate", type=float, default=2.0, help="Rate multiplier (default 2.0)")
    p.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout seconds")
    p.add_argument("--dry-run", action="store_true", help="Parse + schedule, send nothing")
    p.add_argument(
        "--allow-live-target",
        action="store_true",
        help="Required to point at a target that looks like the live MCP (:8767)",
    )
    return p.parse_args()


def load_capture(path: Path) -> list[dict]:
    entries = []
    with path.open() as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.exit(f"capture line {lineno}: invalid JSON ({exc})")
            for key in ("ts", "method", "path"):
                if key not in obj:
                    sys.exit(f"capture line {lineno}: missing required key {key!r}")
            entries.append(obj)
    if not entries:
        sys.exit("capture is empty — nothing to replay")
    return entries


def main() -> int:
    args = parse_args()
    if args.rate <= 0:
        sys.exit("--rate must be > 0")
    if any(m in args.target for m in LIVE_MCP_MARKERS) and not args.allow_live_target:
        sys.exit(
            f"target {args.target!r} looks like the live governance MCP; "
            "refusing without --allow-live-target (§8.3 replay is for the "
            "shadow path, which ships with the Wave 3 implementation)"
        )

    entries = load_capture(args.capture)
    t0 = datetime.fromisoformat(entries[0]["ts"])
    sent = errors = 0
    start = time.monotonic()

    for entry in entries:
        offset_s = (datetime.fromisoformat(entry["ts"]) - t0).total_seconds() / args.rate
        sleep_for = start + offset_s - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        if args.dry_run:
            sent += 1
            continue
        url = args.target.rstrip("/") + entry["path"]
        body = json.dumps(entry.get("body", {})).encode()
        req = urllib.request.Request(
            url,
            data=body if entry["method"] not in ("GET", "HEAD") else None,
            method=entry["method"],
            headers={"Content-Type": "application/json", "X-Wave3-Replay": "1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                resp.read()
            sent += 1
        except (urllib.error.URLError, OSError) as exc:
            errors += 1
            print(f"[replay] {entry['method']} {entry['path']} failed: {exc}", file=sys.stderr)

    elapsed = time.monotonic() - start
    mode = "DRY-RUN " if args.dry_run else ""
    print(
        f"[replay] {mode}done: {sent} sent, {errors} errors, "
        f"{len(entries)} captured, {elapsed:.1f}s at {args.rate}x"
    )
    # Non-zero on any error so the §8.3 "replay completes with zero events"
    # precondition can be machine-checked from exit status.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
