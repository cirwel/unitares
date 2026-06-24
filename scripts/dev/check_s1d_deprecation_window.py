#!/usr/bin/env python3
"""S1-d deprecation-window checker — lets an agent (resident / cron / doctor)
decide whether the dead cross-process ``continuity_token`` resume residue is
safe to retire, instead of a human eyeballing logs.

Background (docs/ontology/plan.md S20; PRs #1042 telemetry):
  - The retired cross-process resume path emits a
    ``continuity_token_deprecated_accept`` audit event whenever it is actually
    taken (``used_token_for_resume=True``). It is believed never taken (the S1-c
    reject gate precedes the emit), but that can't be proven from the repo alone.
  - The False path additionally logs a once-per-process ``[S1-d]`` line so that
    "surface reached, observed False" is positively confirmable, not merely
    inferred from the absence of accept events.

A window is CLEAN ⇒ safe to delete the residue when, over the window:
  (1) ZERO ``continuity_token_deprecated_accept`` events, AND
  (2) the ``[S1-d]`` reached-marker is present (the surface IS exercised, so the
      zero-accepts is "gate holds", not "code path never runs").

This script reads the audit JSONL (and, optionally, a server log for the
marker), and exits 0 (clean / safe) or 1 (dirty / not-yet). It is meant to be
run ON the deployment where ``data/audit_log.jsonl`` lives — a stateless API
client cannot see that file. Pure stdlib; importable core for testing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ACCEPT_EVENT = "continuity_token_deprecated_accept"
S1D_MARKER = "[S1-d]"
DEFAULT_WINDOW_DAYS = 14


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp; return None on anything unparseable."""
    if not isinstance(value, str):
        return None
    try:
        # Audit entries use datetime.now().isoformat() (naive local). Tolerate a
        # trailing 'Z' just in case a future writer emits UTC.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def scan_audit_window(
    audit_lines: Iterable[str],
    *,
    window_days: int,
    now: datetime,
    marker_lines: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Pure core: scan audit JSONL lines for in-window deprecated_accept events.

    ``marker_lines`` (optional) is any iterable of log lines to scan for the
    ``[S1-d]`` reached-marker. When omitted, ``surface_reached`` is None
    (unknown) and the verdict is downgraded from CLEAN to UNCONFIRMED.
    """
    cutoff = now - timedelta(days=window_days)
    scanned = 0
    accepts_in_window: List[Dict[str, Any]] = []
    for line in audit_lines:
        line = line.strip()
        if not line:
            continue
        scanned += 1
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("event_type") != ACCEPT_EVENT:
            continue
        ts = _parse_ts(entry.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        accepts_in_window.append(entry)

    surface_reached: Optional[bool] = None
    if marker_lines is not None:
        surface_reached = any(S1D_MARKER in str(l) for l in marker_lines)

    n_accepts = len(accepts_in_window)
    if n_accepts > 0:
        verdict = "DIRTY"
    elif surface_reached is True:
        verdict = "CLEAN"
    else:
        # Zero accepts but we cannot confirm the surface was exercised.
        verdict = "UNCONFIRMED"

    return {
        "verdict": verdict,
        "safe_to_retire": verdict == "CLEAN",
        "window_days": window_days,
        "cutoff": cutoff.isoformat(),
        "events_scanned": scanned,
        "deprecated_accept_in_window": n_accepts,
        "samples": accepts_in_window[:5],
        "surface_reached": surface_reached,
    }


def _read_lines(path: Optional[Path]) -> Optional[List[str]]:
    if path is None:
        return None
    if not path.exists():
        return None
    return path.read_text(errors="replace").splitlines()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    repo_root = Path(__file__).resolve().parents[2]
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=repo_root / "data" / "audit_log.jsonl",
        help="Path to the audit JSONL (default: <repo>/data/audit_log.jsonl).",
    )
    parser.add_argument(
        "--server-log",
        type=Path,
        default=None,
        help="Optional server log file to scan for the [S1-d] reached-marker. "
        "Required to reach a CLEAN (vs UNCONFIRMED) verdict.",
    )
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = parser.parse_args(argv)

    audit_lines = _read_lines(args.audit_log)
    if audit_lines is None:
        msg = (
            f"audit log not found at {args.audit_log} — run this ON the deployment "
            "where data/audit_log.jsonl lives, or pass --audit-log."
        )
        if args.json:
            print(json.dumps({"verdict": "NO_AUDIT_LOG", "safe_to_retire": False, "error": msg}))
        else:
            print(f"⚠️  {msg}", file=sys.stderr)
        return 2

    result = scan_audit_window(
        audit_lines,
        window_days=args.window_days,
        now=datetime.now(),
        marker_lines=_read_lines(args.server_log),
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        v = result["verdict"]
        icon = {"CLEAN": "✅", "UNCONFIRMED": "🟡", "DIRTY": "❌"}.get(v, "❓")
        print(f"{icon}  S1-d deprecation window: {v}")
        print(f"    window: last {result['window_days']}d (since {result['cutoff']})")
        print(f"    audit events scanned: {result['events_scanned']}")
        print(f"    continuity_token_deprecated_accept in window: {result['deprecated_accept_in_window']}")
        print(f"    [S1-d] surface-reached marker: {result['surface_reached']}")
        if v == "CLEAN":
            print("    → SAFE TO RETIRE: 0 deprecated accepts AND the surface is confirmed reached.")
        elif v == "UNCONFIRMED":
            print("    → 0 deprecated accepts, but pass --server-log with the [S1-d] marker "
                  "(or wait for #1042 to deploy + accrue traffic) to confirm the path runs.")
        else:
            print("    → NOT SAFE: the deprecated cross-process resume path is still in use.")
    # exit 0 only on a positive CLEAN verdict; 1 otherwise (dirty/unconfirmed).
    return 0 if result["safe_to_retire"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
