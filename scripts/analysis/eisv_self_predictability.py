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

# --- Raw-series (AR(1) decontamination null) section -------------------------
# state_json.behavioral_eisv.raw_obs = [E_obs, I_obs, S_obs] (PR #1294,
# forward-only): the clamped pre-EMA input of that check-in. No V — V is a
# derived EMA of the E-I imbalance and has no per-check-in raw input.
RAW_DIMS = ("E", "I", "S")

# The AR(1) null needs enough (x_{t-1}, x_t) pairs for a stable per-agent fit
# before any model is scored on the raw series.
WARMUP_RAW = 8

# Individuality gate (docs/proposals/eisv-grounding-next-move-v0.md, step 4):
# the per-agent reference must beat fleet-mean AND persistence AND AR(1),
# out-of-sample, for a majority of agents with at least this many raw states.
RAW_GATE_MIN_STATES = 50


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


def _extract_raw(state_json: dict) -> dict | None:
    """Pull the raw pre-EMA [E, I, S] observation from a state row.

    Rows persisted before PR #1294 carry no raw_obs and are skipped — the raw
    eval is forward-only by construction.
    """
    if not isinstance(state_json, dict):
        return None
    beh = state_json.get("behavioral_eisv")
    if not isinstance(beh, dict):
        return None
    raw = beh.get("raw_obs")
    if not isinstance(raw, (list, tuple)) or len(raw) != len(RAW_DIMS):
        return None
    out = {}
    for d, v in zip(RAW_DIMS, raw):
        if not isinstance(v, (int, float)):
            return None
        out[d] = float(v)
    return out


async def fetch_trajectories(
    db_url: str, *, min_states: int
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, str]]:
    """Return (smoothed, raw, labels).

    smoothed: {agent_id: [eisv_dict, ...]} time-ordered, non-synthetic,
    behavioral, thresholded at min_states (unchanged from the original eval).
    raw: {agent_id: [raw_dict, ...]} for rows carrying raw_obs, thresholded at
    WARMUP_RAW + 2 (enough for at least one scored prediction).
    labels: {agent_id: human label} where core.agents knows one.
    """
    try:
        import asyncpg
    except ImportError:
        print("error: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(
            """
            SELECT i.agent_id, a.label, s.recorded_at, s.state_json
            FROM core.identities i
            JOIN core.agent_state s ON s.identity_id = i.identity_id
            LEFT JOIN core.agents a ON a.id::text = i.agent_id
            WHERE s.synthetic IS NOT TRUE
              AND s.state_json IS NOT NULL
            ORDER BY i.agent_id, s.recorded_at ASC
            """
        )
    finally:
        await conn.close()

    import json

    traj: dict[str, list[dict]] = {}
    raw_traj: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    for r in records:
        sj = r["state_json"]
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                continue
        if r["label"]:
            labels[r["agent_id"]] = r["label"]
        eisv = _extract_eisv(sj)
        if eisv is not None:
            traj.setdefault(r["agent_id"], []).append(eisv)
        raw = _extract_raw(sj)
        if raw is not None:
            raw_traj.setdefault(r["agent_id"], []).append(raw)
    return (
        {a: seq for a, seq in traj.items() if len(seq) >= min_states},
        {a: seq for a, seq in raw_traj.items() if len(seq) >= WARMUP_RAW + 2},
        labels,
    )


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


def _ar1_predict(n: int, sx: float, sy: float, sxx: float, sxy: float,
                 x_last: float, fallback: float) -> float:
    """One-step AR(1) prediction from online (x_{t-1}, x_t) pair sums.

    Least-squares fit x_t = c + phi * x_{t-1} on the history pairs only (no
    leakage). phi is clamped to [-1, 1] — this is a *null* model and a
    stationarity-violating fit on a short window would let it explode rather
    than describe. Falls back to the history mean when the slope is
    unidentifiable (constant history / too few pairs).
    """
    denom = n * sxx - sx * sx
    if n < 2 or abs(denom) < 1e-12:
        return fallback
    phi = (n * sxy - sx * sy) / denom
    phi = max(-1.0, min(1.0, phi))
    c = (sy - phi * sx) / n
    return c + phi * x_last


RAW_MODELS = ("persistence", "ar1", "expanding_mean", "ema", "global_mean")


def evaluate_raw(traj: dict[str, list[dict]], *,
                 gate_min_states: int = RAW_GATE_MIN_STATES) -> dict:
    """Walk-forward eval on the RAW pre-EMA series — the decontamination null.

    This is the test the smoothed eval declares confounded: on raw_obs,
    persistence is no longer trivially accurate, so per-agent references can be
    honestly compared against per-agent persistence and AR(1) nulls.

    Gate (pre-registered, next-move doc step 4): the runtime-shaped per-agent
    reference (`ema`, mirroring the live baseline) must have lower avg MAE than
    `global_mean` AND `persistence` AND `ar1` for a majority of agents with
    >= gate_min_states raw states. Beating fleet-mean alone is NOT success.
    """
    gmean = {d: 0.0 for d in RAW_DIMS}
    n_all = 0
    for seq in traj.values():
        for m in seq:
            for d in RAW_DIMS:
                gmean[d] += m[d]
            n_all += 1
    gmean = {d: (gmean[d] / n_all if n_all else 0.0) for d in RAW_DIMS}

    pooled_err = {mdl: {d: 0.0 for d in RAW_DIMS} for mdl in RAW_MODELS}
    pooled_cnt = 0
    per_agent: list[dict] = []

    for agent, seq in traj.items():
        T = len(seq)
        run_sum = {d: 0.0 for d in RAW_DIMS}
        ema = {d: seq[0][d] for d in RAW_DIMS}
        # AR(1) online pair sums over (x_{i-1}, x_i), per dim
        ar = {d: {"n": 0, "sx": 0.0, "sy": 0.0, "sxx": 0.0, "sxy": 0.0}
              for d in RAW_DIMS}
        a_err = {mdl: {d: 0.0 for d in RAW_DIMS} for mdl in RAW_MODELS}
        a_cnt = 0
        for i in range(T):
            cur = seq[i]
            if i >= WARMUP_RAW:
                exp_mean = {d: run_sum[d] / i for d in RAW_DIMS}
                for d in RAW_DIMS:
                    s = ar[d]
                    preds = {
                        "persistence": seq[i - 1][d],
                        "ar1": _ar1_predict(s["n"], s["sx"], s["sy"], s["sxx"],
                                            s["sxy"], seq[i - 1][d],
                                            exp_mean[d]),
                        "expanding_mean": exp_mean[d],
                        "ema": ema[d],
                        "global_mean": gmean[d],
                    }
                    for mdl, p in preds.items():
                        a_err[mdl][d] += abs(p - cur[d])
                a_cnt += 1
            # fold cur into running stats AFTER scoring (no leakage)
            for d in RAW_DIMS:
                if i >= 1:
                    s = ar[d]
                    x, y = seq[i - 1][d], cur[d]
                    s["n"] += 1
                    s["sx"] += x
                    s["sy"] += y
                    s["sxx"] += x * x
                    s["sxy"] += x * y
                run_sum[d] += cur[d]
                ema[d] = EMA_ALPHA[d] * cur[d] + (1 - EMA_ALPHA[d]) * ema[d]

        if a_cnt == 0:
            continue
        a_mae = {mdl: {d: a_err[mdl][d] / a_cnt for d in RAW_DIMS}
                 for mdl in RAW_MODELS}
        avg = {mdl: _mae_avg(a_mae[mdl]) for mdl in RAW_MODELS}
        wins_gate = (avg["ema"] < avg["global_mean"]
                     and avg["ema"] < avg["persistence"]
                     and avg["ema"] < avg["ar1"])
        per_agent.append({
            "agent": agent,
            "n_states": T,
            "n_scored": a_cnt,
            "mae_avg": avg,
            "mae": a_mae,
            "gate_eligible": T >= gate_min_states,
            "wins_gate": wins_gate,
        })
        for mdl in RAW_MODELS:
            for d in RAW_DIMS:
                pooled_err[mdl][d] += a_err[mdl][d]
        pooled_cnt += a_cnt

    pooled_mae = {
        mdl: {d: (pooled_err[mdl][d] / pooled_cnt if pooled_cnt else float("nan"))
              for d in RAW_DIMS}
        for mdl in RAW_MODELS
    }
    eligible = [a for a in per_agent if a["gate_eligible"]]
    winners = [a for a in eligible if a["wins_gate"]]
    return {
        "n_agents": len(per_agent),
        "n_predictions": pooled_cnt,
        "pooled_mae": pooled_mae,
        "global_mean": gmean,
        "per_agent": sorted(per_agent, key=lambda a: -a["n_states"]),
        "gate_min_states": gate_min_states,
        "gate_eligible": len(eligible),
        "gate_winners": len(winners),
        "gate_majority": (len(winners) * 2 > len(eligible)) if eligible else None,
    }


def build_raw_report(res: dict, labels: dict[str, str]) -> str:
    a: list[str] = []
    a.append("\n---\n\n# Raw-series AR(1) null — the individuality gate\n")
    a.append(
        "Target = raw pre-EMA observations (`behavioral_eisv.raw_obs`, "
        "PR #1294, forward-only). On this series persistence is NOT trivially "
        "accurate, so the persistence/AR(1) comparison the smoothed eval "
        "declares confounded is honest here. E/I/S only (V has no raw input).\n"
    )
    if res["n_agents"] == 0:
        a.append("**No agents have enough raw_obs rows yet** — the raw eval is "
                 "forward-only; re-run as data accrues.\n")
        return "\n".join(a)

    a.append(
        f"Agents scored: **{res['n_agents']}**  |  scored predictions: "
        f"**{res['n_predictions']}**  |  gate-eligible (>= "
        f"{res['gate_min_states']} states): **{res['gate_eligible']}**\n"
    )
    a.append("## Pooled MAE by model (raw series)")
    a.append("| Model | E | I | S | avg |")
    a.append("|---|---:|---:|---:|---:|")
    for mdl in RAW_MODELS:
        row = res["pooled_mae"][mdl]
        a.append(f"| `{mdl}` | {row['E']:.4f} | {row['I']:.4f} | "
                 f"{row['S']:.4f} | {_mae_avg(row):.4f} |")

    a.append("\n## Per-agent (gate = `ema` beats global_mean AND persistence AND ar1)")
    a.append("| Agent | states | persistence | ar1 | exp_mean | ema | global | gate |")
    a.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for ag in res["per_agent"]:
        name = labels.get(ag["agent"], ag["agent"][:8])
        m = ag["mae_avg"]
        flag = ("**PASS**" if ag["wins_gate"] else "fail") if ag["gate_eligible"] \
            else ("(pass)" if ag["wins_gate"] else "(fail)")
        a.append(
            f"| {name} | {ag['n_states']} | {m['persistence']:.4f} | "
            f"{m['ar1']:.4f} | {m['expanding_mean']:.4f} | {m['ema']:.4f} | "
            f"{m['global_mean']:.4f} | {flag} |"
        )
    a.append("\n(parenthesised = below the gate's states floor; shown for trend only)")

    a.append("\n## Gate verdict")
    if res["gate_eligible"] == 0:
        a.append(f"- **NOT EVALUABLE** — no agent has >= {res['gate_min_states']} "
                 "raw states yet. Forward-only data; re-run later.")
    else:
        verdict = "PASS" if res["gate_majority"] else "FAIL"
        a.append(
            f"- Majority rule over eligible agents: **{res['gate_winners']} / "
            f"{res['gate_eligible']} win → {verdict}** (pre-registered: beating "
            "fleet-mean alone is not success; the per-agent reference must also "
            "beat per-agent persistence AND AR(1), out-of-sample)."
        )
        if res["gate_eligible"] < 5:
            a.append(
                f"- **Scope: early read.** Only {res['gate_eligible']} agent(s) "
                "clear the states floor; a majority over so few is weak evidence "
                "either way. Treat as trend, re-run as raw_obs accrues fleet-wide."
            )
    a.append(
        "- Interpretation guard: a PASS here earns 'a non-trivial per-agent "
        "self-model exists' (estimator individuality) — it says NOTHING about "
        "outcome validity, which remains label-blocked. A FAIL means the "
        "'self-model' is dressed-up autocorrelation and must not be shipped or "
        "publicly framed (next-move doc, step 4)."
    )
    return "\n".join(a) + "\n"


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
        "That is the label-free test that would actually earn the individuality axiom — "
        "run below on rows that carry `raw_obs` (PR #1294, forward-only)."
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
    traj, raw_traj, labels = await fetch_trajectories(
        args.db_url, min_states=args.min_states
    )
    if not traj:
        print("no agents meet the minimum-states threshold", file=sys.stderr)
        return 1
    res = evaluate(traj)
    report = build_report(res, min_states=args.min_states)
    raw_res = evaluate_raw(raw_traj, gate_min_states=args.raw_gate_min_states)
    report += build_raw_report(raw_res, labels)
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
    p.add_argument("--raw-gate-min-states", type=int, default=RAW_GATE_MIN_STATES,
                   help="raw states an agent needs to count toward the "
                        "individuality-gate majority (next-move doc step 4)")
    p.add_argument("--output", help="optional markdown output path")
    return p.parse_args(argv)


def main(argv=None) -> int:
    import asyncio

    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
