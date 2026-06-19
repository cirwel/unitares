#!/usr/bin/env python3
"""octopus_rollup.py — count logical workers (octopi), not process-instances (tentacles).

The governance identity model mints one identity per process-instance x onboard
event (the `force_new` posture, for WRITE-accountability — each writer is a
distinct, attributable identity). That is the right *write* unit but the wrong
*count* unit: a persistent worker (the BEAM Sentinel, the Discord Hermes harness,
a Claude session-chain across /clears) sheds a fresh identity per episode, so
"how many agents" reports tentacles, not octopi.

This is a READ-ONLY report. It rolls identities up into PRINCIPALS (octopi) using
only signals the agent itself declared — never spoofable heuristics:

  * declared lineage edges (parent_agent_id -> agent_id), and
  * shared thread_id (same conversation / logical worker).

A principal is a connected component over the union of those edges. Deliberately
EXCLUDED as grouping keys:

  * IP/UA fingerprint — spoofable and legitimately shared (e.g. a bridge and a
    gateway both on localhost httpx hash to one fingerprint); rejected as a
    credential in the prefix-bind hijack work. An octopus built on it would
    mis-merge unrelated workers.
  * the public `<harness>_<date>` label (e.g. `mcp_20260414`) — a daily cohort,
    not an agent: hundreds of unrelated process-instances from one day share it.

True singletons (a one-shot session with no lineage and no thread reuse) stay
singular — that is honest. The compression lands where it should: on the
persistent / recurring workers, which is exactly where the model already holds
the signal (thread_id, parent_agent_id) but does not count by it.

Usage:
    scripts/dev/octopus_rollup.py                # active identities (default)
    scripts/dev/octopus_rollup.py --all          # include archived/deleted
    scripts/dev/octopus_rollup.py --db governance --top 10
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict


def fetch_rows(db: str, include_all: bool) -> list[tuple[str, str, str]]:
    """Return (agent_id, thread_id, parent_agent_id) for the requested identities.

    Fields are uuids or empty — no embedded tabs — so a tab-separated read is
    unambiguous.
    """
    where = "" if include_all else "where status = 'active'"
    sql = (
        "select agent_id, coalesce(metadata->>'thread_id',''), "
        "coalesce(parent_agent_id::text,'') "
        f"from core.identities {where}"
    )
    proc = subprocess.run(
        ["psql", "-d", db, "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    rows: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def rollup(rows: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Connected components over {shared thread} U {declared lineage}."""
    uf = UnionFind()
    ids = {aid for aid, _, _ in rows}
    for aid, _, _ in rows:
        uf.find(aid)  # ensure every identity is its own component to start

    by_thread: dict[str, list[str]] = defaultdict(list)
    for aid, thread, _ in rows:
        if thread:
            by_thread[thread].append(aid)
    for members in by_thread.values():
        for m in members[1:]:
            uf.union(members[0], m)

    for aid, _, parent in rows:
        if parent and parent in ids:  # only chain to a parent we actually hold
            uf.union(aid, parent)

    comps: dict[str, list[str]] = defaultdict(list)
    for aid, _, _ in rows:
        comps[uf.find(aid)].append(aid)
    return comps


def _bucket(size: int) -> str:
    if size == 1:
        return "1 (singleton)"
    if size <= 3:
        return "2-3"
    if size <= 10:
        return "4-10"
    return "11+"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="governance")
    ap.add_argument("--all", action="store_true",
                    help="include archived/deleted identities (default: active only)")
    ap.add_argument("--top", type=int, default=8,
                    help="show the N largest octopi")
    args = ap.parse_args(argv)

    try:
        rows = fetch_rows(args.db, args.all)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"error: could not query db {args.db!r}: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("no identities matched.")
        return 0

    comps = rollup(rows)
    n_tent, n_octo = len(rows), len(comps)

    order = ["1 (singleton)", "2-3", "4-10", "11+"]
    octopi_by, tent_by = defaultdict(int), defaultdict(int)
    for members in comps.values():
        b = _bucket(len(members))
        octopi_by[b] += 1
        tent_by[b] += len(members)

    scope = "all" if args.all else "active"
    print(f"scope: {scope} identities")
    print(f"tentacles (identities): {n_tent}")
    print(f"octopi (principals):    {n_octo}")
    print(f"compression:            {n_tent / n_octo:.2f} tentacles/octopus\n")
    print(f"  {'octopus size':16} {'octopi':>7} {'tentacles':>10}")
    for b in order:
        print(f"  {b:16} {octopi_by[b]:7} {tent_by[b]:10}")

    multi_o = sum(v for k, v in octopi_by.items() if k != "1 (singleton)")
    multi_t = sum(v for k, v in tent_by.items() if k != "1 (singleton)")
    print(f"\n  {multi_o} multi-instance octopi hold {multi_t} tentacles "
          f"(counted today as {multi_t} separate agents).")

    largest = sorted(comps.values(), key=len, reverse=True)[:args.top]
    if largest and len(largest[0]) > 1:
        print(f"\n  largest octopi (instances rolled into one logical worker):")
        for members in largest:
            if len(members) == 1:
                break
            print(f"    {len(members):3} instances  (e.g. {members[0][:8]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
