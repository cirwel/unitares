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
from .client import GovernanceClient, Identity
from .config import CLASSES, EPISODE_COUNT, Transport
from .runner import run_episode
from .sampler import plan

SEED = 1729  # fixed -> reproducible
PROD_URL_MARKERS = (":8767", ":8766")  # the live governance ports on this host


def _guard_not_prod(base_url: str, force: bool) -> None:
    if not force and any(m in base_url for m in PROD_URL_MARKERS):
        raise SystemExit(
            f"REFUSING to run against {base_url!r} — that looks like the live fleet. "
            "Point GOVERNANCE_HTTP_URL at an isolated test instance, or pass --i-know."
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=EPISODE_COUNT)
    ap.add_argument("--classes", type=int, default=len(CLASSES))
    ap.add_argument("--out", type=str, default="data/calibration_harness/run_v1.csv")
    ap.add_argument("--i-know", action="store_true", help="bypass the prod-URL guard")
    args = ap.parse_args()

    transport = Transport()
    _guard_not_prod(transport.base_url, args.i_know)
    client = GovernanceClient(transport)
    rng = random.Random(SEED)

    n_classes = max(1, min(args.classes, len(CLASSES)))
    idents: list[Identity] = [client.onboard(CLASSES[i].display_name) for i in range(n_classes)]
    print(f"onboarded {n_classes} harness identities: {[i.agent_uuid for i in idents]}")

    before = report.snapshot_tactical(client)
    episodes = plan(args.episodes)
    print(f"planned {len(episodes)} episodes against {transport.base_url}")

    rows = []
    for k, ep in enumerate(episodes):
        ident = idents[k % n_classes]
        row = run_episode(client, ident, ep, rng)
        rows.append(row)
        if (k + 1) % 25 == 0:
            print(f"  {k + 1}/{len(episodes)} done")

    after = report.snapshot_tactical(client)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[f.name for f in dataclasses.fields(rows[0])])
        w.writeheader()
        for r in rows:
            w.writerow(dataclasses.asdict(r))
    print(f"wrote {len(rows)} rows -> {out}")

    report.emit([i.agent_uuid for i in idents], before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
