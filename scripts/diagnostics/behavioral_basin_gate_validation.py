#!/usr/bin/env python3
"""Validate issue #689 behavioral basin gating against live persisted state.

The DB stores behavioral EISV snapshots under
core.agent_state.state_json->'behavioral_eisv'. This script reconstructs those
snapshots with BehavioralEISV.from_dict(), compares the previous flat-floor
self-relative scorer to the current worktree scorer, and summarizes the
tight-baseline blast radius.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.behavioral_assessment import (  # noqa: E402
    ABSOLUTE_E_FLOOR,
    ABSOLUTE_I_FLOOR,
    ABSOLUTE_S_CEILING,
    ABSOLUTE_V_CEILING,
    RISK_CAUTION_THRESHOLD,
    RISK_SAFE_THRESHOLD,
    SIGMA_MILD,
    SIGMA_MODERATE,
    SIGMA_SEVERE,
    assess_behavioral_state,
)
from src.behavioral_state import BehavioralEISV  # noqa: E402

LEGACY_MIN_STD = 0.05


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _component_verdict(risk: float) -> str:
    if risk < RISK_SAFE_THRESHOLD:
        return "safe"
    if risk < RISK_CAUTION_THRESHOLD:
        return "caution"
    return "high-risk"


def _legacy_z(state: BehavioralEISV, dim: str) -> float:
    stats = getattr(state, f"_baseline_{dim}")
    current = getattr(state, dim)
    return stats.z_score(current, min_std=LEGACY_MIN_STD)


def _legacy_absolute_floors(state: BehavioralEISV) -> Dict[str, float]:
    components: Dict[str, float] = {}
    if state.E < ABSOLUTE_E_FLOOR:
        components["low_E"] = 0.30 * (ABSOLUTE_E_FLOOR - state.E) / ABSOLUTE_E_FLOOR
    if state.I < ABSOLUTE_I_FLOOR:
        components["low_I"] = 0.30 * (ABSOLUTE_I_FLOOR - state.I) / ABSOLUTE_I_FLOOR
    if state.S > ABSOLUTE_S_CEILING:
        components["high_S"] = 0.20 * min(1.0, (state.S - ABSOLUTE_S_CEILING) / (1.0 - ABSOLUTE_S_CEILING))
    if abs(state.V) > ABSOLUTE_V_CEILING:
        components["high_V"] = 0.20 * min(1.0, (abs(state.V) - ABSOLUTE_V_CEILING) / (1.0 - ABSOLUTE_V_CEILING))
    return components


def legacy_assess(state: BehavioralEISV, rho: float = 0.0, continuity_energy: float = 0.0, task_type: str = "mixed") -> Dict[str, Any]:
    components: Dict[str, float] = {}

    if state.is_baselined:
        z_e = _legacy_z(state, "E")
        components["low_E"] = (
            0.30 * min(1.0, (-z_e - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
            if z_e < -SIGMA_MILD
            else 0.0
        )

        z_i = _legacy_z(state, "I")
        components["low_I"] = (
            0.30 * min(1.0, (-z_i - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
            if z_i < -SIGMA_MILD
            else 0.0
        )

        z_s = _legacy_z(state, "S")
        sigma_threshold = SIGMA_MODERATE if task_type == "convergent" else SIGMA_MILD
        components["high_S"] = (
            0.20 * min(1.0, (z_s - sigma_threshold) / (SIGMA_SEVERE - sigma_threshold))
            if z_s > sigma_threshold
            else 0.0
        )

        z_v = _legacy_z(state, "V")
        components["high_V"] = (
            0.20 * min(1.0, (abs(z_v) - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
            if abs(z_v) > SIGMA_MILD
            else 0.0
        )
    else:
        components["low_E"] = 0.30 * (0.4 - state.E) / 0.4 if state.E < 0.4 else 0.0
        components["low_I"] = 0.30 * (0.4 - state.I) / 0.4 if state.I < 0.4 else 0.0
        s_threshold = 0.6 if task_type == "convergent" else 0.5
        components["high_S"] = (
            0.20 * min(1.0, (state.S - s_threshold) / (1.0 - s_threshold))
            if state.S > s_threshold
            else 0.0
        )
        components["high_V"] = (
            0.20 * min(1.0, (abs(state.V) - 0.15) / 0.85)
            if abs(state.V) > 0.15
            else 0.0
        )

    components["adversarial_rho"] = 0.15 * min(1.0, (-0.2 - rho) / 0.8) if rho < -0.2 else 0.0
    components["high_CE"] = 0.10 * min(1.0, (continuity_energy - 0.5) / 1.5) if continuity_energy > 0.5 else 0.0

    for key, value in _legacy_absolute_floors(state).items():
        components[key] = max(components.get(key, 0.0), value)

    risk = max(0.0, min(1.0, sum(components.values())))
    return {
        "risk": round(risk, 4),
        "verdict": _component_verdict(risk),
        "components": {k: round(v, 4) for k, v in components.items()},
    }


def fetch_latest(conn, limit: int | None = None) -> list[dict]:
    limit_sql = "LIMIT %s" if limit else ""
    sql = f"""
        WITH latest AS (
          SELECT DISTINCT ON (s.identity_id)
                 s.state_id,
                 s.identity_id,
                 s.recorded_at,
                 s.state_json,
                 i.agent_id,
                 i.metadata->>'label' AS label
          FROM core.agent_state s
          JOIN core.identities i USING(identity_id)
          WHERE s.synthetic = false
            AND s.state_json ? 'behavioral_eisv'
          ORDER BY s.identity_id, s.recorded_at DESC
        )
        SELECT *
        FROM latest
        ORDER BY recorded_at DESC
        {limit_sql}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if limit:
            cur.execute(sql, (limit,))
        else:
            cur.execute(sql)
        return [dict(row) for row in cur]


def has_tight_std(state: BehavioralEISV) -> bool:
    return any(
        getattr(state, f"_baseline_{dim}").std < LEGACY_MIN_STD
        for dim in ("E", "I", "S", "V")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--show", type=int, default=12, help="Rows to show from changed decisions")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db_url)
    rows = fetch_latest(conn, args.limit)

    total = 0
    baselined = 0
    tight = 0
    verdict_flips: Counter[str] = Counter()
    risk_deltas = []
    changed = []

    for row in rows:
        state_json = _json_obj(row["state_json"])
        blob = _json_obj(state_json.get("behavioral_eisv"))
        if not blob:
            continue
        state = BehavioralEISV.from_dict(blob)
        total += 1
        if state.is_baselined:
            baselined += 1
        if state.is_baselined and has_tight_std(state):
            tight += 1

        old = legacy_assess(state)
        new = assess_behavioral_state(state)
        risk_deltas.append(new.risk - old["risk"])
        if old["verdict"] != new.verdict:
            verdict_flips[f"{old['verdict']}->{new.verdict}"] += 1
        if old["risk"] != new.risk or old["verdict"] != new.verdict:
            changed.append((row, state, old, new))

    print("BEHAVIORAL BASIN-GATE VALIDATION")
    print(f"latest behavioral rows: {total}")
    print(f"baselined by code:      {baselined}")
    print(f"tight std baselined:    {tight}")
    print(f"changed risk rows:      {len(changed)}")
    print(f"verdict flips:          {dict(verdict_flips)}")
    if risk_deltas:
        print(f"risk delta min/max:     {min(risk_deltas):+.4f} / {max(risk_deltas):+.4f}")

    print()
    print("Changed examples:")
    for row, state, old, new in changed[: args.show]:
        label = row.get("label") or row.get("agent_id")
        print(
            f"- {label} {row['identity_id']} E={state.E:.4f} I={state.I:.4f} "
            f"S={state.S:.4f} V={state.V:.4f} old={old['risk']:.4f}/{old['verdict']} "
            f"new={new.risk:.4f}/{new.verdict}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
