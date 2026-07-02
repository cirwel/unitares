#!/usr/bin/env python3
"""EISV individuality test v2 — pre-registered, fresh-data-only.

Spec: docs/proposals/eisv-individuality-v2-preregistration.md. Read it before
touching thresholds — they are FROZEN as of 2026-07-02; edits after
post-registration data exists invalidate the pre-registration.

Three legs on the raw pre-EMA series (`behavioral_eisv.raw_obs`):

  A — anchoredness: variance-ratio VR(8) vs an increment-permutation null.
      Permutation preserves the marginal increment distribution (stickiness,
      drift) and destroys only serial ordering — which is where reversion
      lives. Survives both v1 artifacts.
  B — individuality of the home: split-half per-agent means, Spearman rho
      across agents vs an agent-label permutation null.
  C — jump-conditional reference quality (estimator leg, NOT part of the
      axiom verdict): at moved observations, does the runtime EMA reference
      beat last-value?

The verdict counts ONLY rows recorded after REGISTRATION_TS. The fetch
excludes earlier rows; there is deliberately no flag to include them.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_individuality_v2.py
    PYTHONPATH=. python3 scripts/analysis/eisv_individuality_v2.py --output data/analysis/eisv_individuality_v2.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# --- Pre-registered constants (FROZEN 2026-07-02 — see the proposal doc) ----

REGISTRATION_TS = dt.datetime(2026, 7, 2, 18, 0, 0, tzinfo=dt.timezone.utc)

RAW_DIMS = ("E", "I", "S")
# Runtime EMA alphas — must mirror state_json.behavioral_eisv.alphas. The
# fetch cross-checks every row's persisted alphas against these and the
# report flags any mismatch (leg C is invalid for a mismatched agent).
EMA_ALPHA = {"E": 0.12, "I": 0.08, "S": 0.15}

# raw_obs components are ~10-event rolling-window features
# (src/behavioral_sensor.py: history[-10:]). The primary VR horizon sits
# PAST that window (h >= 2w) so leg A measures behavior-rate stability, not
# mechanical window overlap — see the spec's leg-A reframe + provenance note.
FEATURE_WINDOW = 10
VR_HORIZON = 24                     # primary horizon for leg A (> 2*window)
VR_HORIZONS_DESCRIPTIVE = (8, 16, 48)
N_PERMUTATIONS = 1000
PERM_SEED = 20260702                # registration date — fixed, not wall-clock
ALPHA_LEVEL = 0.05

MIN_STATES = 100                    # per-agent eligibility floor
MIN_MOVED = 30                      # moved-observation floor (leg C power)
MIN_ELIGIBLE_AGENTS = 4             # verdict floor — see spec (n=4: leg B
                                    # passes only at perfect rho, p=1/24)
MOVED_EPS = 1e-12                   # "moved" = consecutive values differ
LEG_C_BURNIN = 15                   # obs skipped before leg C scores (the
                                    # cold-started reference needs ~2/alpha
                                    # folds to shed its init transient)

DIM_MAJORITY = 2                    # leg passes on >= 2 of 3 dims

# Leg A part (ii) — the drift veto. The VR test (part i) proves reversion
# structure exists but is blind to a slowly drifting level hidden under
# window-filtered measurement noise (the adversarial-review F1 organism:
# a random-walking behavior rate seen through the window still shows
# short-horizon noise reversion). The veto compares the dispersion of
# big-block means against a small-block permutation null: small blocks
# (>= window length) preserve the feature's short-range correlation while
# permutation scrambles long-range level wandering, so observed dispersion
# above the null's 95th percentile = the level drifts = veto.
DRIFT_BLOCK_SMALL = 16              # permutation unit (> feature window 10)
DRIFT_BLOCK_BIG = 32                # dispersion unit (2 small blocks)


# --- data ---------------------------------------------------------------


def _extract_raw(state_json: dict) -> dict | None:
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


async def fetch_post_registration(db_url: str) -> tuple[dict[str, list[dict]],
                                                         dict[str, str],
                                                         set[str]]:
    """Raw trajectories restricted to rows recorded AFTER REGISTRATION_TS.

    The cutoff is applied in SQL — pre-registration rows never enter the
    process. This is the fresh-data-only rule; it is not configurable.
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
            LEFT JOIN core.agents a ON a.id = i.agent_id
            WHERE s.synthetic IS NOT TRUE
              AND s.recorded_at > $1
            ORDER BY i.agent_id, s.recorded_at ASC, s.state_id ASC
            """,
            REGISTRATION_TS,
        )
    finally:
        await conn.close()

    import json

    traj: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    alpha_mismatch: set[str] = set()
    for r in records:
        sj = r["state_json"]
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                continue
        if r["label"]:
            labels[r["agent_id"]] = r["label"]
        raw = _extract_raw(sj)
        if raw is not None:
            traj.setdefault(r["agent_id"], []).append(raw)
            # Leg C validity guard: the persisted per-row alphas must match
            # the hardcoded runtime mirror, else leg C scores the wrong EMA.
            alphas = sj.get("behavioral_eisv", {}).get("alphas")
            if isinstance(alphas, dict) and any(
                abs(float(alphas.get(d, EMA_ALPHA[d])) - EMA_ALPHA[d]) > 1e-9
                for d in RAW_DIMS
            ):
                alpha_mismatch.add(r["agent_id"])
    return traj, labels, alpha_mismatch


# --- shared helpers -------------------------------------------------------


def _moved_count(seq: list[dict]) -> int:
    n = 0
    for a, b in zip(seq, seq[1:]):
        if any(abs(b[d] - a[d]) > MOVED_EPS for d in RAW_DIMS):
            n += 1
    return n


def _variance(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mu = sum(xs) / n
    return sum((x - mu) ** 2 for x in xs) / (n - 1)


def variance_ratio(series: list[float], h: int) -> float:
    """VR(h) with overlapping h-differences. nan when undefined."""
    if len(series) < h + 2:
        return float("nan")
    d1 = [b - a for a, b in zip(series, series[1:])]
    dh = [series[i + h] - series[i] for i in range(len(series) - h)]
    v1 = _variance(d1)
    vh = _variance(dh)
    if not (v1 > 0):
        return float("nan")
    return vh / (h * v1)


# --- Leg A: anchoredness --------------------------------------------------


def _block_mean_dispersion(series: list[float], big: int) -> float:
    """Variance of non-overlapping big-block means. nan below 3 blocks."""
    k = len(series) // big
    if k < 3:
        return float("nan")
    means = [sum(series[i * big:(i + 1) * big]) / big for i in range(k)]
    return _variance(means)


def drift_veto(series: list[float], *,
               small: int = DRIFT_BLOCK_SMALL,
               big: int = DRIFT_BLOCK_BIG,
               n_perm: int = N_PERMUTATIONS,
               seed: int = PERM_SEED) -> dict:
    """Leg A part (ii): is big-block-mean dispersion consistent with a
    stationary level, judged against a small-block permutation null?

    Small blocks preserve within-window correlation; permuting their order
    destroys long-range level wandering. Observed dispersion above the null
    = drift = veto. One-sided; veto fires at p < ALPHA_LEVEL. Weak on short
    series (few big blocks) — a non-veto at the eligibility floor is low
    power, which is why part (i) must ALSO pass affirmatively.
    """
    obs = _block_mean_dispersion(series, big)
    if obs != obs:
        return {"dispersion": obs, "p": None, "veto": False}
    blocks = [series[i:i + small] for i in range(0, len(series), small)]
    rng = random.Random(seed)
    at_or_above = 0
    for _ in range(n_perm):
        order = list(range(len(blocks)))
        rng.shuffle(order)
        x = [v for bi in order for v in blocks[bi]]
        pd = _block_mean_dispersion(x, big)
        if pd == pd and pd >= obs:
            at_or_above += 1
    p = (1 + at_or_above) / (1 + n_perm)
    return {"dispersion": obs, "p": p, "veto": p < ALPHA_LEVEL}


def leg_a_agent(seq_by_dim: dict[str, list[float]], *,
                h: int = VR_HORIZON,
                n_perm: int = N_PERMUTATIONS,
                seed: int = PERM_SEED) -> dict:
    """Leg A per dim = (i) increment-permutation VR test passes AND (ii) the
    drift veto does not fire. Agent passes on dim majority."""
    dims = {}
    for di, d in enumerate(RAW_DIMS):
        series = seq_by_dim[d]
        obs = variance_ratio(series, h)
        if obs != obs:  # nan — constant series or too short
            dims[d] = {"vr": obs, "p": None, "passes": False,
                       "vr_descriptive": {}, "drift": None}
            continue
        increments = [b - a for a, b in zip(series, series[1:])]
        # Per-dim seed offset: a shared seed would apply the IDENTICAL
        # permutation-index sequence to all three dims (shuffle depends only
        # on list length), artificially coupling the 2-of-3 majority vote.
        rng = random.Random(seed * 31 + di)
        at_or_below = 0
        for _ in range(n_perm):
            perm = increments[:]
            rng.shuffle(perm)
            x = [series[0]]
            for inc in perm:
                x.append(x[-1] + inc)
            pvr = variance_ratio(x, h)
            if pvr == pvr and pvr <= obs:
                at_or_below += 1
        p = (1 + at_or_below) / (1 + n_perm)
        drift = drift_veto(series, n_perm=n_perm, seed=seed * 31 + di + 200)
        dims[d] = {"vr": obs, "p": p,
                   "passes": p < ALPHA_LEVEL and not drift["veto"],
                   "vr_descriptive": {hh: variance_ratio(series, hh)
                                      for hh in VR_HORIZONS_DESCRIPTIVE},
                   "drift": drift}
    n_pass = sum(1 for v in dims.values() if v["passes"])
    return {"dims": dims, "n_dims_pass": n_pass,
            "passes": n_pass >= DIM_MAJORITY}


# --- Leg B: individuality of the home --------------------------------------


def _spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def leg_b(traj_by_agent: dict[str, dict[str, list[float]]], *,
          n_perm: int = N_PERMUTATIONS, seed: int = PERM_SEED) -> dict:
    """Split-half home stability across agents, per dim, label-permutation null."""
    import itertools

    agents = sorted(traj_by_agent)
    dims = {}
    for d in RAW_DIMS:
        first, second = [], []
        for a in agents:
            s = traj_by_agent[a][d]
            mid = len(s) // 2
            first.append(sum(s[:mid]) / mid)
            second.append(sum(s[mid:]) / (len(s) - mid))
        obs = _spearman(first, second)
        if obs != obs:
            dims[d] = {"rho": obs, "p": None, "passes": False}
            continue
        n = len(agents)
        rng = random.Random(seed * 31 + RAW_DIMS.index(d) + 100)
        if n <= 7:  # exact permutation (7! = 5040 orderings)
            perms = list(itertools.permutations(range(n)))
            stats = [_spearman(first, [second[i] for i in pm]) for pm in perms]
            # exact null includes identity; standard exact p
            p = sum(1 for s_ in stats if s_ == s_ and s_ >= obs) / len(stats)
        else:
            at_or_above = 0
            idx = list(range(n))
            for _ in range(n_perm):
                rng.shuffle(idx)
                s_ = _spearman(first, [second[i] for i in idx])
                if s_ == s_ and s_ >= obs:
                    at_or_above += 1
            p = (1 + at_or_above) / (1 + n_perm)
        dims[d] = {"rho": obs, "p": p, "passes": p < ALPHA_LEVEL}
    n_pass = sum(1 for v in dims.values() if v["passes"])
    return {"dims": dims, "n_dims_pass": n_pass,
            "passes": n_pass >= DIM_MAJORITY, "n_agents": len(agents)}


# --- Leg C: jump-conditional reference quality ------------------------------


def _binom_p_greater_half(wins: int, n: int) -> float:
    """One-sided binomial P(X >= wins) under p=0.5.

    Exact for n <= 100 (covers the MIN_MOVED floor where precision matters);
    normal approximation with continuity correction above — the exact sum
    overflows float conversion for large n and the approximation error there
    is far below the 0.05 decision threshold.
    """
    if n == 0:
        return 1.0
    if n <= 100:
        total = sum(math.comb(n, k) for k in range(wins, n + 1))
        return total / (2 ** n)
    mu = n / 2
    sd = math.sqrt(n) / 2
    z = (wins - 0.5 - mu) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))


def leg_c_agent(seq_by_dim: dict[str, list[float]]) -> dict:
    """At moved observations, reference-vs-persistence; pooled dims.

    The reference is the live EMA's FORM (runtime alphas, folding every
    observation) cold-started at the first post-registration observation —
    NOT a replay of the true runtime EMA, whose pre-registration history is
    excluded by the fresh-data rule. The first LEG_C_BURNIN observations are
    fold-only (never scored) so the init transient doesn't contaminate the
    comparison. Ties go to the null (persistence). NB the pooled binomial
    treats cross-dim trials at the same timestep as independent — they are
    not; the p-value is anti-conservative and leg C is non-gating partly for
    this reason (see spec).
    """
    wins = 0
    n = 0
    for d in RAW_DIMS:
        series = seq_by_dim[d]
        if not series:
            continue
        ema = series[0]
        for i in range(1, len(series)):
            cur = series[i]
            prev = series[i - 1]
            if i > LEG_C_BURNIN and abs(cur - prev) > MOVED_EPS:
                n += 1
                if abs(ema - cur) < abs(prev - cur):
                    wins += 1
            ema = EMA_ALPHA[d] * cur + (1 - EMA_ALPHA[d]) * ema
    p = _binom_p_greater_half(wins, n)
    win_rate = (wins / n) if n else float("nan")
    return {"wins": wins, "n_moved": n, "win_rate": win_rate, "p": p,
            "passes": (n > 0 and win_rate > 0.5 and p < ALPHA_LEVEL)}


# --- verdict ---------------------------------------------------------------


def evaluate_v2(traj: dict[str, list[dict]], *,
                alpha_mismatch: set[str] | None = None) -> dict:
    """Run all legs on post-registration trajectories; apply the frozen rule."""
    alpha_mismatch = alpha_mismatch or set()
    per_agent = []
    eligible_by_dim: dict[str, dict[str, list[float]]] = {}
    for agent, seq in sorted(traj.items(), key=lambda kv: -len(kv[1])):
        moved = _moved_count(seq)
        eligible = len(seq) >= MIN_STATES and moved >= MIN_MOVED
        seq_by_dim = {d: [m[d] for m in seq] for d in RAW_DIMS}
        row = {"agent": agent, "n_states": len(seq), "n_moved": moved,
               "eligible": eligible, "leg_a": None, "leg_c": None,
               "leg_c_alpha_mismatch": agent in alpha_mismatch}
        if eligible:
            row["leg_a"] = leg_a_agent(seq_by_dim)
            # Leg C scores against the hardcoded runtime alphas; a persisted
            # per-row alpha mismatch invalidates that comparison (leg A/B are
            # alpha-free and unaffected).
            row["leg_c"] = (leg_c_agent(seq_by_dim)
                            if agent not in alpha_mismatch else None)
            eligible_by_dim[agent] = seq_by_dim
        per_agent.append(row)

    n_eligible = len(eligible_by_dim)
    a_winners = sum(1 for r in per_agent
                    if r["eligible"] and r["leg_a"]["passes"])
    leg_a_majority = (a_winners * 2 > n_eligible) if n_eligible else None
    leg_b_res = leg_b(eligible_by_dim) if n_eligible >= 2 else None

    if n_eligible < MIN_ELIGIBLE_AGENTS:
        verdict = "NOT EVALUABLE"
    elif leg_a_majority and leg_b_res and leg_b_res["passes"]:
        verdict = "AXIOM EARNED"
    else:
        verdict = "FAIL"
    return {
        "registration_ts": REGISTRATION_TS.isoformat(),
        "per_agent": per_agent,
        "n_eligible": n_eligible,
        "leg_a_winners": a_winners,
        "leg_a_majority": leg_a_majority,
        "leg_b": leg_b_res,
        "verdict": verdict,
    }


# --- report ----------------------------------------------------------------


def build_report(res: dict, labels: dict[str, str]) -> str:
    a: list[str] = []
    a.append("# EISV individuality v2 — pre-registered read\n")
    a.append(f"Registration: `{res['registration_ts']}` — only rows recorded "
             "after this instant are counted. Spec: "
             "`docs/proposals/eisv-individuality-v2-preregistration.md` "
             "(thresholds frozen; do not reinterpret).\n")
    a.append("## Per-agent")
    a.append(f"| Agent | states | moved | eligible | leg A (VR{VR_HORIZON} "
             "dims E/I/S, p) | A | leg C win-rate (p) | C |")
    a.append("|---|---:|---:|---|---|---|---|---|")
    for r in res["per_agent"]:
        name = labels.get(r["agent"], r["agent"][:8])
        if r["eligible"]:
            la = r["leg_a"]
            dims = " / ".join(
                f"{la['dims'][d]['vr']:.2f}({la['dims'][d]['p']:.3f})"
                if la["dims"][d]["p"] is not None else "—"
                for d in RAW_DIMS)
            a_flag = "**pass**" if la["passes"] else "fail"
            lc = r["leg_c"]
            if lc is None:
                c_txt, c_flag = "ALPHA MISMATCH — invalid", "—"
            elif lc["n_moved"]:
                c_txt = f"{lc['win_rate']:.2f} ({lc['p']:.3f}, n={lc['n_moved']})"
                c_flag = "pass" if lc["passes"] else "fail"
            else:
                c_txt, c_flag = "—", "—"
        else:
            dims, a_flag, c_txt, c_flag = "—", "—", "—", "—"
        a.append(f"| {name} | {r['n_states']} | {r['n_moved']} | "
                 f"{'yes' if r['eligible'] else 'no'} | {dims} | {a_flag} | "
                 f"{c_txt} | {c_flag} |")

    eligible_rows = [r for r in res["per_agent"] if r["eligible"]]
    if eligible_rows:
        a.append(f"\n### Descriptive VR curve (h ∈ {VR_HORIZONS_DESCRIPTIVE}, "
                 "no inference — trend context for the primary h)")
        a.append("| Agent | dim | " + " | ".join(
            f"VR{hh}" for hh in VR_HORIZONS_DESCRIPTIVE) + f" | VR{VR_HORIZON} |")
        a.append("|---|---|" + "---:|" * (len(VR_HORIZONS_DESCRIPTIVE) + 1))
        for r in eligible_rows:
            name = labels.get(r["agent"], r["agent"][:8])
            for d in RAW_DIMS:
                dd = r["leg_a"]["dims"][d]
                cells = " | ".join(
                    (f"{dd['vr_descriptive'].get(hh, float('nan')):.2f}"
                     if dd["vr_descriptive"].get(hh, float("nan"))
                     == dd["vr_descriptive"].get(hh, float("nan")) else "—")
                    for hh in VR_HORIZONS_DESCRIPTIVE)
                vr_p = f"{dd['vr']:.2f}" if dd["vr"] == dd["vr"] else "—"
                a.append(f"| {name} | {d} | {cells} | {vr_p} |")

    a.append(f"\nEligible agents: **{res['n_eligible']}** (verdict floor "
             f"{MIN_ELIGIBLE_AGENTS}); leg A winners: "
             f"**{res['leg_a_winners']} / {res['n_eligible']}**")
    if res["leg_b"]:
        b = res["leg_b"]
        dims = " / ".join(
            f"{d}: rho={b['dims'][d]['rho']:.2f} (p={b['dims'][d]['p']:.3f})"
            if b["dims"][d]["p"] is not None else f"{d}: —"
            for d in RAW_DIMS)
        a.append(f"\nLeg B (split-half home stability, {b['n_agents']} agents): "
                 f"{dims} → **{'pass' if b['passes'] else 'fail'}**")
    a.append(f"\n## Verdict: **{res['verdict']}**")
    a.append(
        "\n- AXIOM EARNED = leg A majority AND leg B pass, at >= "
        f"{MIN_ELIGIBLE_AGENTS} eligible agents. Earns the individuality "
        "axiom's estimator half ONLY — outcome validity remains "
        "label-blocked; no public 'self-model' framing, no Stage B, nothing "
        "new wired to the live verdict path, regardless of outcome."
        "\n- Leg C is an estimator finding (is the runtime EMA reference fit "
        "to size residuals at informative moments) — it neither rescues nor "
        "kills the axiom."
        "\n- FAIL at the final read (2026-07-30) triggers the kill criterion: "
        "the axiom is retired for raw behavioral EISV as currently measured; "
        "no v3 without changing the measurement process."
    )
    return "\n".join(a) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    traj, labels, alpha_mismatch = await fetch_post_registration(args.db_url)
    res = evaluate_v2(traj, alpha_mismatch=alpha_mismatch)
    report = build_report(res, labels)
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
    p.add_argument("--output", help="optional markdown output path")
    return p.parse_args(argv)


def main(argv=None) -> int:
    import asyncio

    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
