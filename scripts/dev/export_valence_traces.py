#!/usr/bin/env python3
"""Export real per-agent behavioral observation traces for the Valence gate.

Companion to ``validate_valence_formula.py``. The raw pre-EMA behavioral
observations the gate replays (``[E_obs, I_obs, S_obs]``) are persisted ONLY in
the per-agent JSON snapshots' ``behavioral_eisv.obs_history`` (the trimmed DB
row drops history). This dumps them to the JSONL trace format the gate reads.

Honest labeling (the gate gates the false-pause check on ``healthy``/``sentinel``):
  * ``sentinel`` — known long-running autonomous residents (by DB label).
  * ``healthy``  — >=30 obs AND the agent's REAL recorded ``verdict_history``
                   is >=95% "safe" with zero "high-risk". This is production's
                   own ground-truth that the trajectory was healthy, so a
                   candidate-induced flip toward risk on it is the regression
                   we care about — not a label we synthesised.
  * ``unknown``  — everything else (flips still counted/reported, not gated).

USAGE
    python3 scripts/dev/export_valence_traces.py \
        --data-dir ~/projects/unitares-deploy/data/agents \
        --data-dir ~/projects/unitares/data/agents \
        --labels /tmp/agent_labels.tsv \
        --min-obs 10 --out /tmp/real_valence_traces.jsonl
Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

# Known autonomous residents — the tight-baseline shapes the council flagged.
_RESIDENT_LABELS = {"sentinel", "vigil", "watcher", "steward", "lumen"}


def _load_label_map(path: str | None) -> dict:
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    for line in Path(path).read_text().splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        aid = parts[0]
        out[aid] = {
            "label": parts[1] if len(parts) > 1 else "",
            "status": parts[2] if len(parts) > 2 else "",
            "tags": parts[3] if len(parts) > 3 else "",
        }
    return out


def _health_label(obs_len: int, verdict_history: list, db_label: str) -> str:
    name = (db_label or "").strip().lower()
    if any(r in name for r in _RESIDENT_LABELS):
        return "sentinel"
    if obs_len >= 30 and verdict_history:
        vh = [str(v).lower() for v in verdict_history]
        safe = sum(1 for v in vh if v == "safe")
        high = sum(1 for v in vh if v in ("high-risk", "high_risk"))
        if high == 0 and safe / len(vh) >= 0.95:
            return "healthy"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", action="append", required=True,
                    help="data/agents dir (repeatable; earlier = higher priority on tie)")
    ap.add_argument("--labels", help="TSV: agent_id<TAB>label<TAB>status<TAB>tags")
    ap.add_argument("--min-obs", type=int, default=10)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    label_map = _load_label_map(args.labels)

    # Dedup by agent_id, preferring the snapshot with the longest obs_history.
    best: dict = {}
    for d in args.data_dir:
        d = os.path.expanduser(d)
        for f in glob.glob(os.path.join(d, "*_state.json")):
            try:
                data = json.load(open(f))
            except Exception:
                continue
            beh = data.get("behavioral_eisv") or {}
            oh = beh.get("obs_history") or []
            if not oh:
                continue
            aid = os.path.basename(f).replace("_state.json", "")
            prev = best.get(aid)
            if prev is None or len(oh) > len(prev["oh"]):
                best[aid] = {"oh": oh, "vh": data.get("verdict_history") or []}

    rows = []
    for aid, blob in best.items():
        oh = blob["oh"]
        if len(oh) < args.min_obs:
            continue
        db_label = (label_map.get(aid) or {}).get("label", "")
        label = _health_label(len(oh), blob["vh"], db_label)
        rows.append({
            "agent_id": aid,
            "label": label,
            "db_label": db_label,
            "observations": [[float(x) for x in row[:3]] for row in oh],
        })

    rows.sort(key=lambda r: len(r["observations"]), reverse=True)
    with open(os.path.expanduser(args.out), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    from collections import Counter
    by_label = Counter(r["label"] for r in rows)
    print(f"wrote {len(rows)} traces -> {args.out}", file=sys.stderr)
    print(f"  by label: {dict(by_label)}", file=sys.stderr)
    print(f"  obs lengths: {[len(r['observations']) for r in rows]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
