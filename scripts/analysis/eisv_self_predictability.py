#!/usr/bin/env python3
"""Label-free EISV self-predictability eval — does the estimator earn its keep?

The skeptic report (``eisv_skeptic_report.py``) asks whether EISV predicts *bad
outcomes* — a question that needs exogenous labels, and one EISV is underpowered
on (and, per its own interpretation rule, doesn't claim: EISV is proprioception,
not an outcome oracle). This script asks the question the math CAN answer without
any labels: **does an agent's own state-model predict its own next state better
than trivial baselines?**

That is the estimator's falsifiable claim, and it maps to the design axioms:

  1. Mean-reversion (axiom: a stable "normal" exists) — does the agent's running
     mean beat naive persistence at predicting the next measurement? If
     persistence wins, the state is a random walk and the baseline is fitting
     noise.
  2. Distinguishability (a NECESSARY condition for axiom 1, not the whole of it)
     — does the agent's *own* mean beat the *fleet* mean at predicting that
     agent's next state? If the global mean is as good, agents are not even
     distinguishable. NB: beating the fleet mean is NOT sufficient for the
     individuality axiom's "stable per-agent normal" — that needs beating a
     per-agent persistence/AR(1) null, which is confounded on the pre-smoothed
     stored signal (see the "scope limit" section of the report).
  3. Non-stationarity / growth (axiom 2) — does a recency-weighted EMA beat the
     expanding mean? If so, the "normal" genuinely moves and a moving reference
     is warranted; if not, a static baseline suffices.

No outcome labels are read. The only inputs are EISV trajectories
(``core.agent_state.state_json.behavioral_eisv``), so this is falsifiable from
inside the math — the honest counterpart to the outcome eval.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_self_predictability.py
    PYTHONPATH=. python3 scripts/analysis/eisv_self_predictability.py --min-states 20
    PYTHONPATH=. python3 scripts/analysis/eisv_self_predictability.py --output data/analysis/eisv_self_predictability.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

DIMS = ("E", "I", "S", "V")
# Per-dim EMA smoothing constants, matching the live behavioral_eisv alphas
# (state_json.behavioral_eisv.alphas). Kept here so the EMA model mirrors what
# the runtime actually does rather than an invented constant.
EMA_ALPHA = {"E": 0.12, "I": 0.08, "S": 0.15, "V": 0.10}

# Warmup: number of leading states per agent used only to seed the predictors,
# never scored. A prediction needs at least one prior point.
WARMUP = 3


def _extract_eisv(state_json: dict) -> dict | None:
    """Pull the smoothed behavioral E/I/S/V measurement from a state row.

    Returns None for rows without a behavioral_eisv block (controller-era rows) —
    this eval scores the live estimator, so those are skipped rather than coerced
    from the legacy top-level fields.
    """
    if not isinstance(state_json, dict):
        return None
    beh = state_json.get("behavioral_eisv")
    if not isinstance(beh, dict):
        return None
    out = {}
    for d in DIMS:
        v = beh.get(d)
        if not isinstance(v, (int, float)):
            return None
        out[d] = float(v)
    return out


async def fetch_trajectories(db_url: str, *, min_states: int) -> dict[str, list[dict]]:
    """Return {agent_id: [eisv_dict, ...]} time-ordered, non-synthetic, behavioral."""
    try:
        import asyncpg
    except ImportError:
        print("error: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(
            """
            SELECT i.agent_id, s.recorded_at, s.state_json
            FROM core.identities i
            JOIN core.agent_state s ON s.identity_id = i.identity_id
            WHERE s.synthetic IS NOT TRUE
              AND s.state_json IS NOT NULL
            ORDER BY i.agent_id, s.recorded_at ASC
            """
        )
    finally:
        await conn.close()

    import json

    traj: dict[str, list[dict]] = {}
    for r in records:
        sj = r["state_json"]
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                continue
        eisv = _extract_eisv(sj)
        if eisv is None:
            continue
        traj.setdefault(r["agent_id"], []).append(eisv)
    return {a: seq for a, seq in traj.items() if len(seq) >= min_states}


def _global_means(traj: dict[str, list[dict]]) -> dict[str, float]:
    sums = {d: 0.0 for d in DIMS}
    n = 0
    for seq in traj.values():
        for m in seq:
            for d in DIMS:
                sums[d] += m[d]
            n += 1
    return {d: (sums[d] / n if n else 0.0) for d in DIMS}


def evaluate(traj: dict[str, list[dict]]) -> dict:
    """Walk-forward absolute error per model per dim, pooled over all (agent, t)."""
    gmean = _global_means(traj)
    models = ("persistence", "expanding_mean", "ema", "global_mean")
    # pooled absolute error sums and counts, per model per dim
    err = {mdl: {d: 0.0 for d in DIMS} for mdl in models}
    cnt = 0
    # per-agent: count of dims where per-agent expanding_mean beats global_mean
    indiv_wins = 0
    indiv_total = 0

    for seq in traj.values():
        T = len(seq)
        # running state for expanding mean and EMA per dim
        run_sum = {d: 0.0 for d in DIMS}
        ema = {d: seq[0][d] for d in DIMS}
        for i in range(T):
            cur = seq[i]
            if i >= WARMUP:
                # predict cur from history[:i]
                exp_mean = {d: run_sum[d] / i for d in DIMS}
                preds = {
                    "persistence": seq[i - 1],
                    "expanding_mean": exp_mean,
                    "ema": dict(ema),
                    "global_mean": gmean,
                }
                for mdl, p in preds.items():
                    for d in DIMS:
                        err[mdl][d] += abs(p[d] - cur[d])
                cnt += 1
            # fold cur into running stats AFTER scoring (no leakage)
            for d in DIMS:
                run_sum[d] += cur[d]
                ema[d] = EMA_ALPHA[d] * cur[d] + (1 - EMA_ALPHA[d]) * ema[d]

        # per-agent individuality: agent's final expanding mean vs global mean,
        # scored on the agent's own scored points
        if T > WARMUP:
            for d in DIMS:
                ag_err = 0.0
                gl_err = 0.0
                rs = 0.0
                for i in range(T):
                    if i >= WARMUP:
                        am = rs / i
                        ag_err += abs(am - seq[i][d])
                        gl_err += abs(gmean[d] - seq[i][d])
                    rs += seq[i][d]
                indiv_total += 1
                if ag_err < gl_err:
                    indiv_wins += 1

    mae = {
        mdl: {d: (err[mdl][d] / cnt if cnt else float("nan")) for d in DIMS}
        for mdl in models
    }
    return {
        "n_predictions": cnt,
        "n_agents": len(traj),
        "mae": mae,
        "global_mean": gmean,
        "individuality_win_rate": (indiv_wins / indiv_total if indiv_total else float("nan")),
        "individuality_total": indiv_total,
    }


def _mae_avg(mae_row: dict[str, float]) -> float:
    return sum(mae_row.values()) / len(mae_row)


def build_report(res: dict, *, min_states: int) -> str:
    a: list[str] = []
    a.append("# EISV Self-Predictability (label-free estimator eval)\n")
    a.append(
        f"Agents with >= {min_states} behavioral states: **{res['n_agents']}**  |  "
        f"scored predictions: **{res['n_predictions']}**\n"
    )
    a.append(
        "Lower MAE = better one-step prediction of the agent's own next "
        "behavioral EISV. No outcome labels used.\n"
    )
    mae = res["mae"]
    a.append("## Mean Absolute Error by model and dimension")
    a.append("| Model | E | I | S | V | avg |")
    a.append("|---|---:|---:|---:|---:|---:|")
    for mdl in ("persistence", "expanding_mean", "ema", "global_mean"):
        row = mae[mdl]
        a.append(
            f"| `{mdl}` | {row['E']:.4f} | {row['I']:.4f} | {row['S']:.4f} | "
            f"{row['V']:.4f} | {_mae_avg(row):.4f} |"
        )

    pers = _mae_avg(mae["persistence"])
    exp = _mae_avg(mae["expanding_mean"])
    ema = _mae_avg(mae["ema"])
    glob = _mae_avg(mae["global_mean"])

    def skill(x: float, ref: float) -> float:
        return (1 - x / ref) * 100 if ref else float("nan")

    a.append("\n## Skill scores (positive = better than the reference)")
    a.append(f"- expanding_mean vs global_mean: **{skill(exp, glob):+.1f}%** "
             "(per-agent reference beats fleet → individuality is real?) — *robust*")
    a.append(f"- ema vs expanding_mean: **{skill(ema, exp):+.1f}%** "
             "(recency helps → the normal moves?) — *robust*")
    a.append(
        f"- per-agent individuality win rate (agent-mean beats global-mean, "
        f"by agent×dim): **{100*res['individuality_win_rate']:.0f}%** "
        f"of {res['individuality_total']}"
    )
    a.append(f"- expanding_mean vs persistence: {skill(exp, pers):+.1f}% — "
             "**CONFOUNDED, do not interpret** (see caveat)")

    a.append("\n## Caveat — the persistence comparison is confounded")
    a.append(
        f"`persistence` MAE ({pers:.4f}) is far below every reference model. That is "
        "**not** evidence of a random walk: the scored series is `behavioral_eisv`, "
        "which is *already* an EMA-smoothed estimate (live alphas E0.12/I0.08/S0.15/"
        "V0.10), so consecutive values barely move and predicting `next = current` is "
        "trivially accurate on any pre-smoothed signal. This eval therefore CANNOT "
        "cleanly adjudicate mean-reversion vs random walk from the stored signal. The "
        "informative comparisons are the two that share the same target and differ "
        "only in the *reference* — individuality (per-agent vs fleet) and "
        "non-stationarity (ema vs static mean) — where the smoothing affects both "
        "sides equally and cancels."
    )

    a.append("\n## Reading (robust comparisons only)")
    verdicts = []
    if exp < glob:
        verdicts.append("Per-agent reference beats the fleet reference — agents are "
                        "**distinguishable**: they occupy distinct regions of EISV space, "
                        "so per-agent modeling is not redundant with a fleet centroid.")
    else:
        verdicts.append("The fleet reference is as good as the per-agent one — agents "
                        "are NOT distinguishable in this data; per-agent baselines may be "
                        "overfitting a common centroid.")
    if ema < exp:
        verdicts.append("Recency helps — the normal is **non-stationary**; a moving "
                        "reference (EMA) is warranted over a static mean (axiom 2: the "
                        "reference is allowed to move).")
    else:
        verdicts.append("Recency does not help — a static expanding mean suffices; "
                        "the normal is approximately stationary.")
    for v in verdicts:
        a.append(f"- {v}")
    # which dims drive distinguishability (largest global-vs-agent gap)
    gaps = sorted(DIMS, key=lambda d: (mae["global_mean"][d] - mae["expanding_mean"][d]), reverse=True)
    a.append(f"- Distinguishability is concentrated in **{gaps[0]} and {gaps[1]}** "
             f"(largest per-agent advantage over the fleet mean); weakest in {gaps[-1]}.")

    a.append("\n## What this does NOT establish (scope limit)")
    a.append(
        "**Distinguishable + non-stationary is weaker than the individuality axiom's "
        "'stable per-agent normal'.** The axiom wants a reference a residual can be "
        "measured *against*; that requires the per-agent reference to beat a per-agent "
        "**persistence / AR(1)** null (predict next = this agent's last value), not just "
        "the fleet mean. Here persistence dominates every reference — but ONLY because "
        f"the stored signal is pre-EMA-smoothed (persistence MAE {pers:.4f}); the "
        "persistence-vs-reference test is confounded and CANNOT be run cleanly on this "
        "table. So 'agents have stable normals worth measuring deviation from' is "
        "**untested**, not confirmed. The same per-agent autocorrelation is what the "
        "outcome eval's 'previous-outcome' baseline exploits — do not count it as "
        "evidence both for individuality here and against EISV there."
    )
    a.append(
        "\nTo settle it, log the RAW (un-smoothed) per-check-in EISV and re-run "
        "expanding_mean / ema vs a per-agent persistence+AR(1) null on the raw series. "
        "That is the label-free test that would actually earn the individuality axiom."
    )
    a.append(
        "\nInterpretation rule: this measures the *estimator* (does the self-model "
        "predict the self), not the *policy* (does deviation warrant intervention). "
        "Note the policy is NOT only validatable via exogenous outcomes — label-free "
        "stress-tests (invariance under agent-relabel / time-shuffle; response to "
        "synthetic injected regime-shifts) probe it without tripping the "
        "self-referential-anchor circularity. A good estimator is necessary, not "
        "sufficient, for a good governor."
    )
    return "\n".join(a) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    traj = await fetch_trajectories(args.db_url, min_states=args.min_states)
    if not traj:
        print("no agents meet the minimum-states threshold", file=sys.stderr)
        return 1
    res = evaluate(traj)
    report = build_report(res, min_states=args.min_states)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
        print(f"wrote {path}")
    else:
        print(report)
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-url", default=DEFAULT_DB_URL)
    p.add_argument("--min-states", type=int, default=10,
                   help="minimum behavioral states per agent to include")
    p.add_argument("--output", help="optional markdown output path")
    return p.parse_args(argv)


def main(argv=None) -> int:
    import asyncio

    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
