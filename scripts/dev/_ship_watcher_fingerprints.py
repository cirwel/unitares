#!/usr/bin/env python3
"""Helper for ship.sh: emit unresolved Watcher fingerprints touching staged files.

Reads absolute paths of staged files from stdin (one per line). Reads
``data/watcher/findings.jsonl`` from the path given as argv[1]. Prints a
comma-separated list of fingerprints whose ``file`` matches a staged path
and whose ``status`` is unresolved (``open`` or ``surfaced``).

Exit code is always 0 — a missing findings file or no matches simply
yields empty stdout. ship.sh treats empty output as "nothing to append".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

UNRESOLVED = {"open", "surfaced"}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: _ship_watcher_fingerprints.py <findings.jsonl>", file=sys.stderr)
        return 2

    findings_path = Path(sys.argv[1])
    if not findings_path.is_file():
        return 0

    staged = {line.strip() for line in sys.stdin if line.strip()}
    if not staged:
        return 0

    fingerprints: list[str] = []
    seen: set[str] = set()
    with findings_path.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("status") not in UNRESOLVED:
                continue
            if rec.get("file") not in staged:
                continue
            fp = rec.get("fingerprint")
            if not fp or fp in seen:
                continue
            seen.add(fp)
            fingerprints.append(fp)

    if fingerprints:
        print(",".join(fingerprints))
    return 0


if __name__ == "__main__":
    sys.exit(main())
