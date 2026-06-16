"""v1 entrypoint: 2 dedicated harness identities, ~200 stratified episodes.

SAFETY: run this ONLY against an isolated governance instance (a server bound
to governance_test), never the live fleet — the episodes inject synthetic
failures into the GLOBAL tactical calibration pool, which is agent-unscoped on
the read side. Guard below refuses the known prod URL unless --i-know.

    # bring up an isolated server first (see README), then:
    GOVERNANCE_HTTP_URL=http://127.0.0.1:8771 \
    UNITARES_HTTP_API_TOKEN=<test-token> \
    python -m scripts.dev.calibration_harness.run_v1 --episodes 200
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import random
from pathlib import Path

from . import report
from .client import GovernanceClient
from .config import BINS, EPISODE_COUNT, HARNESS_AGENT_NAME, Transport
from .runner import run_slot
from .sampler import plan

SEED = 1729  # fixed -> reproducible
PROD_URL_MARKERS = (":8767", ":8766")  # the live governance ports on this host
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _guard_not_prod(base_url: str, force: bool) -> None:
    """Refuse anything that isn't a loopback address (council C1).

    A port-substring blocklist was theater: a prod server fronted by nginx on
    80/443 or a remote host without the magic ports would sail through. The
    isolated instance is always local, so allowlist loopback instead.
    """
    if force:
        return
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    if host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            f"REFUSING {base_url!r} — not a loopback address. The harness injects "
            "synthetic failures into the GLOBAL tactical pool; run it only against a "
            "local isolated instance (governance_test), or pass --i-know."
        )
    # belt-and-suspenders: never the known live ports, even on loopback
    if any(m in base_url for m in PROD_URL_MARKERS):
        raise SystemExit(
            f"REFUSING {base_url!r} — that is a live governance port. "
            "Point GOVERNANCE_HTTP_URL at the isolated instance (e.g. :8771), or pass --i-know."
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=EPISODE_COUNT)
    ap.add_argument("--gap", type=float, default=0.2,
                    help="injected overconfidence gap; report should recover ~ this ECE")
    ap.add_argument("--out", type=str, default="data/calibration_harness/run_v1.csv")
    ap.add_argument("--i-know", action="store_true", help="bypass the prod-URL guard")
    args = ap.parse_args()

    if args.episodes < len(BINS):
        raise SystemExit(
            f"--episodes must be >= {len(BINS)} (one per confidence bin); got "
            f"{args.episodes}. Fewer collapses the per-bin plan to empty (council H6)."
        )

    transport = Transport()
    _guard_not_prod(transport.base_url, args.i_know)
    client = GovernanceClient(transport)
    rng = random.Random(SEED)

    ident = client.onboard(HARNESS_AGENT_NAME)
    print(f"onboarded harness identity: {ident.agent_uuid}")

    before = report.snapshot_tactical(client)
    slots = plan(args.episodes)
    print(f"planned {len(slots)} episodes against {transport.base_url} (injected gap={args.gap})")

    rows = []
    for k, slot in enumerate(slots):
        rows.append(run_slot(client, ident, slot, rng, args.gap))
        if (k + 1) % 25 == 0:
            print(f"  {k + 1}/{len(slots)} done")

    after = report.snapshot_tactical(client)

    if rows:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[f.name for f in dataclasses.fields(rows[0])])
            w.writeheader()
            for r in rows:
                w.writerow(dataclasses.asdict(r))
        print(f"wrote {len(rows)} rows -> {out}")
    else:
        print("no rows produced; skipping CSV write")

    report.emit(rows, args.gap, before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
