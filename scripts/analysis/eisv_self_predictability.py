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

# Cadence bands for STRATIFYING the (unchanged) gate verdict, by median
# inter-check-in gap in minutes. The 2026-07-01 early read failed 0/2 on
# high-cadence residents whose raw obs barely move between check-ins minutes
# apart — a regime where persistence wins almost by construction. The bands
# separate that regime from agents whose observations have time to move.
# Stratification is descriptive; the pre-registered majority rule is NOT
# re-scored per band.
CADENCE_BANDS = (
    ("fast (<10m)", 0.0, 10.0),
    ("mid (10-60m)", 10.0, 60.0),
    ("slow (>=60m)", 60.0, float("inf")),
)

# Decimation factors for the within-agent cadence DIAGNOSTIC: re-score the
# same agent through the same walk-forward math on every k-th raw observation.
# The runtime folds one EMA step per check-in (not per wall-clock unit), so
# seq[::k] is exactly the series the identical measurement process would
# produce for an agent checking in k-times less often. If the persistence
# advantage decays with k and the per-agent reference overtakes at realistic
# cadences, the native-cadence FAIL is a sampling artifact; if persistence
# wins at every k, the FAIL is real. Diagnostic only — never substitutes for
# the native-cadence gate.
DECIMATION_FACTORS = (1, 2, 4, 8, 16)

# Minimum scored predictions for a decimated series to be reported; below
# this the MAE comparison is noise.
DECIMATION_MIN_SCORED = 20


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
) -> tuple[dict[str, list[dict]], dict[str, list[dict]],
           dict[str, list], dict[str, str]]:
    """Return (smoothed, raw, raw_ts, labels).

    smoothed: {agent_id: [eisv_dict, ...]} time-ordered, non-synthetic,
    behavioral, thresholded at min_states (unchanged from the original eval).
    raw: {agent_id: [raw_dict, ...]} for rows carrying raw_obs, thresholded at
    WARMUP_RAW + 2 (enough for at least one scored prediction).
    raw_ts: {agent_id: [recorded_at, ...]} aligned index-for-index with raw —
    the cadence stratification and decimation diagnostics need real gaps.
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
    raw_ts: dict[str, list] = {}
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
            raw_ts.setdefault(r["agent_id"], []).append(r["recorded_at"])
    kept_raw = {a: seq for a, seq in raw_traj.items()
                if len(seq) >= WARMUP_RAW + 2}
    return (
        {a: seq for a, seq in traj.items() if len(seq) >= min_states},
        kept_raw,
        {a: raw_ts[a] for a in kept_raw},
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


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return float("nan")
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _median_gap_min(ts: list) -> float | None:
    """Median inter-observation gap in minutes, or None without timestamps."""
    if not ts or len(ts) < 2:
        return None
    gaps = []
    for a, b in zip(ts, ts[1:]):
        try:
            gaps.append((b - a).total_seconds() / 60.0)
        except (TypeError, AttributeError):
            return None
    return _median(gaps)


def _cadence_band(gap_min: float | None) -> str | None:
    if gap_min is None:
        return None
    for name, lo, hi in CADENCE_BANDS:
        if lo <= gap_min < hi:
            return name
    return None


def _score_raw_series(seq: list[dict], gmean: dict[str, float]) -> dict | None:
    """Walk-forward scoring of one raw series against all RAW_MODELS.

    Shared by the native-cadence gate and the decimation diagnostic so both
    run through byte-identical math. Returns per-model error sums, scored
    count, and the final fitted (clamped) AR(1) phi per dim — or None when
    the series is too short to score anything.
    """
    T = len(seq)
    run_sum = {d: 0.0 for d in RAW_DIMS}
    ema = {d: seq[0][d] for d in RAW_DIMS}
    # AR(1) online pair sums over (x_{i-1}, x_i), per dim
    ar = {d: {"n": 0, "sx": 0.0, "sy": 0.0, "sxx": 0.0, "sxy": 0.0}
          for d in RAW_DIMS}
    err = {mdl: {d: 0.0 for d in RAW_DIMS} for mdl in RAW_MODELS}
    cnt = 0
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
                    err[mdl][d] += abs(p - cur[d])
            cnt += 1
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

    if cnt == 0:
        return None
    phi = {}
    for d in RAW_DIMS:
        s = ar[d]
        denom = s["n"] * s["sxx"] - s["sx"] * s["sx"]
        if s["n"] < 2 or abs(denom) < 1e-12:
            phi[d] = float("nan")
        else:
            phi[d] = max(-1.0, min(1.0, (s["n"] * s["sxy"] - s["sx"] * s["sy"]) / denom))
    return {"err": err, "cnt": cnt, "phi": phi}


def _fleet_mean(traj: dict[str, list[dict]]) -> dict[str, float]:
    gmean = {d: 0.0 for d in RAW_DIMS}
    n_all = 0
    for seq in traj.values():
        for m in seq:
            for d in RAW_DIMS:
                gmean[d] += m[d]
            n_all += 1
    return {d: (gmean[d] / n_all if n_all else 0.0) for d in RAW_DIMS}


def evaluate_raw(traj: dict[str, list[dict]], *,
                 timestamps: dict[str, list] | None = None,
                 gate_min_states: int = RAW_GATE_MIN_STATES) -> dict:
    """Walk-forward eval on the RAW pre-EMA series — the decontamination null.

    This is the test the smoothed eval declares confounded: on raw_obs,
    persistence is no longer trivially accurate, so per-agent references can be
    honestly compared against per-agent persistence and AR(1) nulls.

    Gate (pre-registered, next-move doc step 4): the runtime-shaped per-agent
    reference (`ema`, mirroring the live baseline) must have lower avg MAE than
    `global_mean` AND `persistence` AND `ar1` for a majority of agents with
    >= gate_min_states raw states. Beating fleet-mean alone is NOT success.

    `timestamps` (optional, aligned with traj) adds per-agent median check-in
    gaps and a cadence-band breakdown of the same verdicts — descriptive
    stratification only; the majority rule above is unchanged.
    """
    gmean = _fleet_mean(traj)
    pooled_err = {mdl: {d: 0.0 for d in RAW_DIMS} for mdl in RAW_MODELS}
    pooled_cnt = 0
    per_agent: list[dict] = []

    for agent, seq in traj.items():
        scored = _score_raw_series(seq, gmean)
        if scored is None:
            continue
        a_err, a_cnt = scored["err"], scored["cnt"]
        a_mae = {mdl: {d: a_err[mdl][d] / a_cnt for d in RAW_DIMS}
                 for mdl in RAW_MODELS}
        avg = {mdl: _mae_avg(a_mae[mdl]) for mdl in RAW_MODELS}
        wins_gate = (avg["ema"] < avg["global_mean"]
                     and avg["ema"] < avg["persistence"]
                     and avg["ema"] < avg["ar1"])
        gap = _median_gap_min(timestamps.get(agent)) if timestamps else None
        phi_vals = [v for v in scored["phi"].values() if v == v]  # drop NaN
        per_agent.append({
            "agent": agent,
            "n_states": len(seq),
            "n_scored": a_cnt,
            "mae_avg": avg,
            "mae": a_mae,
            "gate_eligible": len(seq) >= gate_min_states,
            "wins_gate": wins_gate,
            "median_gap_min": gap,
            "cadence_band": _cadence_band(gap),
            "phi_mean": (sum(phi_vals) / len(phi_vals)) if phi_vals else None,
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
    bands = []
    for name, _lo, _hi in CADENCE_BANDS:
        b_eligible = [a for a in eligible if a["cadence_band"] == name]
        if not b_eligible and not any(a["cadence_band"] == name for a in per_agent):
            continue
        bands.append({
            "band": name,
            "eligible": len(b_eligible),
            "winners": sum(1 for a in b_eligible if a["wins_gate"]),
            "agents_total": sum(1 for a in per_agent if a["cadence_band"] == name),
        })
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
        "cadence_bands": bands,
    }


def evaluate_decimation(traj: dict[str, list[dict]], *,
                        timestamps: dict[str, list] | None = None,
                        gate_min_states: int = RAW_GATE_MIN_STATES,
                        factors: tuple[int, ...] = DECIMATION_FACTORS,
                        min_scored: int = DECIMATION_MIN_SCORED) -> dict:
    """Within-agent cadence diagnostic: re-score gate-eligible agents on
    every k-th observation.

    seq[::k] simulates the same agent at a k-times-coarser check-in cadence
    running through the identical runtime math (one EMA fold per check-in).
    DIAGNOSTIC ONLY: it deliberately makes persistence's job harder, so a win
    here can never substitute for the native-cadence gate — it can only tell
    us whether the native FAIL is explained by sampling cadence.
    """
    gmean = _fleet_mean(traj)  # fixed fleet reference across all k
    agents = []
    for agent, seq in traj.items():
        if len(seq) < gate_min_states:
            continue
        rows = []
        for k in factors:
            dec = seq[::k]
            scored = _score_raw_series(dec, gmean)
            if scored is None or scored["cnt"] < min_scored:
                continue
            a_mae = {mdl: {d: scored["err"][mdl][d] / scored["cnt"]
                           for d in RAW_DIMS} for mdl in RAW_MODELS}
            avg = {mdl: _mae_avg(a_mae[mdl]) for mdl in RAW_MODELS}
            ts = timestamps.get(agent) if timestamps else None
            eff_gap = _median_gap_min(ts[::k]) if ts else None
            phi_vals = [v for v in scored["phi"].values() if v == v]
            rows.append({
                "k": k,
                "n_states": len(dec),
                "n_scored": scored["cnt"],
                "eff_gap_min": eff_gap,
                "mae_avg": avg,
                "phi_mean": (sum(phi_vals) / len(phi_vals)) if phi_vals else None,
                # The individuality-carrying signal: does ANY per-agent
                # stable-normal reference (runtime ema OR the fitted ar1, whose
                # intercept encodes the per-agent mean) beat raw persistence?
                # Persistence is the no-stable-normal null; the ema-vs-ar1
                # comparison is estimator choice, not individuality.
                "normal_beats_pers": (min(avg["ema"], avg["ar1"])
                                      < avg["persistence"]),
                # Mirror of the strict pre-registered gate comparison.
                "ema_beats_nulls": (avg["ema"] < avg["persistence"]
                                    and avg["ema"] < avg["ar1"]),
            })
        if rows:
            agents.append({"agent": agent, "rows": rows})
    return {"agents": agents, "factors": factors, "min_scored": min_scored}


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
    a.append("| Agent | states | gap(min) | phi | persistence | ar1 | exp_mean | ema | global | gate |")
    a.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for ag in res["per_agent"]:
        name = labels.get(ag["agent"], ag["agent"][:8])
        m = ag["mae_avg"]
        flag = ("**PASS**" if ag["wins_gate"] else "fail") if ag["gate_eligible"] \
            else ("(pass)" if ag["wins_gate"] else "(fail)")
        gap = f"{ag['median_gap_min']:.1f}" if ag.get("median_gap_min") is not None else "—"
        phi = f"{ag['phi_mean']:.2f}" if ag.get("phi_mean") is not None else "—"
        a.append(
            f"| {name} | {ag['n_states']} | {gap} | {phi} | "
            f"{m['persistence']:.4f} | "
            f"{m['ar1']:.4f} | {m['expanding_mean']:.4f} | {m['ema']:.4f} | "
            f"{m['global_mean']:.4f} | {flag} |"
        )
    a.append("\n(parenthesised = below the gate's states floor; shown for trend only; "
             "gap = median minutes between check-ins; phi = final fitted clamped "
             "AR(1) slope averaged over E/I/S — near 1.0 means the raw series is "
             "random-walk-like at this cadence)")

    if res.get("cadence_bands"):
        a.append("\n## Cadence stratification (descriptive — the gate rule is unchanged)")
        a.append("| Band | agents | gate-eligible | gate-winners |")
        a.append("|---|---:|---:|---:|")
        for b in res["cadence_bands"]:
            a.append(f"| {b['band']} | {b['agents_total']} | {b['eligible']} | "
                     f"{b['winners']} |")
        a.append(
            "\nThe 2026-07-01 FAIL was read entirely off the fast band. If "
            "eligible slow-band agents also fail, cadence does not explain the "
            "FAIL; if only the fast band fails, the gate population is "
            "unrepresentative and the verdict should wait for slow-band accrual."
        )

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


def build_decimation_report(res: dict, labels: dict[str, str]) -> str:
    a: list[str] = []
    a.append("\n---\n\n# Decimation diagnostic — is the FAIL a cadence artifact?\n")
    a.append(
        "Each gate-eligible agent re-scored on every k-th raw observation "
        "(`seq[::k]`) through the identical walk-forward math — the series the "
        "same measurement process would produce at a k-times-coarser check-in "
        "cadence (the runtime folds one EMA step per check-in, not per "
        "wall-clock unit). **Diagnostic only**: decimation makes persistence's "
        "job harder by construction, so a win at k>1 never substitutes for the "
        "native-cadence gate. What it can settle is *why* the native gate "
        "failed:\n\n"
        "- persistence's edge **decays with k and `ema` overtakes** at gaps "
        "realistic for ordinary fleet agents → the FAIL is a sampling-cadence "
        "artifact; wait for slow-band accrual before treating it as final.\n"
        "- persistence (or the AR(1) null) **wins at every k** even as the "
        "effective gap grows → cadence does not explain the FAIL; the raw "
        "measurement process is autocorrelated at its core and the "
        "individuality claim dies per the next-move doc.\n"
    )
    if not res["agents"]:
        a.append("**No gate-eligible agents to decimate yet.**\n")
        return "\n".join(a)
    for entry in res["agents"]:
        name = labels.get(entry["agent"], entry["agent"][:8])
        a.append(f"\n## {name}")
        a.append("| k | states | scored | eff gap (min) | phi | persistence | ar1 | ema | normal beats pers? | strict gate? |")
        a.append("|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
        for r in entry["rows"]:
            gap = f"{r['eff_gap_min']:.1f}" if r["eff_gap_min"] is not None else "—"
            phi = f"{r['phi_mean']:.2f}" if r["phi_mean"] is not None else "—"
            m = r["mae_avg"]
            normal = "**yes**" if r["normal_beats_pers"] else "no"
            strict = "yes" if r["ema_beats_nulls"] else "no"
            a.append(
                f"| {r['k']} | {r['n_states']} | {r['n_scored']} | {gap} | {phi} | "
                f"{m['persistence']:.4f} | {m['ar1']:.4f} | {m['ema']:.4f} | "
                f"{normal} | {strict} |"
            )
    a.append(
        "\nReading the columns:\n\n"
        "- `normal beats pers?` — the individuality-carrying signal. "
        "Persistence is the no-stable-normal null; the fitted AR(1)'s "
        "intercept encodes a per-agent mean, so EITHER the runtime ema or the "
        "ar1 fit beating persistence is evidence a stable per-agent normal "
        "exists at that cadence. Watch whether this flips to yes as k grows.\n"
        "- `strict gate?` — mirror of the pre-registered comparison (ema beats "
        "persistence AND ar1). NOTE a structural property discovered while "
        "testing this diagnostic: AR(1)-with-intercept NESTS the stationary "
        "per-agent normal (phi→0 reduces it to the per-agent mean), so for a "
        "PERFECTLY stable normal the expanding AR(1) fit converges to the "
        "optimal predictor and the fixed-alpha ema loses this comparison "
        "asymptotically. The strict gate is therefore winnable only where the "
        "normal also DRIFTS slowly (non-stationarity the ema tracks better "
        "than an expanding fit). A strict-gate FAIL with `normal beats pers? "
        "= yes` and low phi means a stable per-agent normal exists but the "
        "runtime's ema is not its best tracker — an estimator-tuning finding, "
        "NOT a death sentence for individuality. A FAIL with persistence "
        "dominant and phi ≈ 1 at every k is the fatal pattern (dressed-up "
        "autocorrelation).\n"
        "- `phi` — fitted clamped AR(1) slope, mean over E/I/S: ≈1 means "
        "random-walk-like (no visible normal), ≈0 means mean-reverting around "
        "a per-agent level."
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
    traj, raw_traj, raw_ts, labels = await fetch_trajectories(
        args.db_url, min_states=args.min_states
    )
    if not traj:
        print("no agents meet the minimum-states threshold", file=sys.stderr)
        return 1
    res = evaluate(traj)
    report = build_report(res, min_states=args.min_states)
    raw_res = evaluate_raw(raw_traj, timestamps=raw_ts,
                           gate_min_states=args.raw_gate_min_states)
    report += build_raw_report(raw_res, labels)
    dec_res = evaluate_decimation(raw_traj, timestamps=raw_ts,
                                  gate_min_states=args.raw_gate_min_states)
    report += build_decimation_report(dec_res, labels)
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
