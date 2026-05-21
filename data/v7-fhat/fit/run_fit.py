"""Orchestrator: load corpus → build agent series → stratified split → EM → SC1/SC2 → ship.

Invocation:
    python3 -m data.v7-fhat.fit.run_fit \
        [--max-agents N] [--skip-plots]

Produces:
    data/v7-fhat/params.json
    data/v7-fhat/session1-report.md (scaffolded; final version hand-edited after run)
    data/v7-fhat/figures/em_convergence.{png,pdf}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import subprocess
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from .em import (
    AgentSeries,
    FitState,
    em_loop,
    SIGMA_OBS_MAX,
    SIGMA_OBS_MIN,
    SIGMA_TRANS_MAX,
    SIGMA_TRANS_MIN,
)
from .ode import fx as ode_fx
from .ukf_smoother import run_ukf_smoother

REPO = pathlib.Path(__file__).resolve().parents[3]
DATA_DIR = REPO / "data" / "v7-fhat"
CORPUS_DIR = DATA_DIR / "corpus"
FIG_DIR = DATA_DIR / "figures"
ODE_PARAMS_JSON = DATA_DIR / "ode_params.json"
PARAMS_OUT = DATA_DIR / "params.json"


# ---------- corpus loading ----------

def load_corpus(window_start: str, window_end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    state = pd.read_parquet(CORPUS_DIR / f"state_{window_start}_{window_end}.parquet")
    outcomes = pd.read_parquet(CORPUS_DIR / f"outcomes_{window_start}_{window_end}.parquet")
    return state, outcomes


# ---------- per-agent series ----------

def build_agent_series(state: pd.DataFrame, outcomes: pd.DataFrame) -> list[AgentSeries]:
    """Group state rows by agent_id, join nearest outcome within ±60s, build AgentSeries."""
    series: list[AgentSeries] = []
    outcomes = outcomes.copy()
    outcomes["ts"] = pd.to_datetime(outcomes["ts"], utc=True)
    outcomes["is_bad"] = outcomes["is_bad"].apply(lambda x: 1 if str(x).lower() in ("true", "t", "1") else 0)
    out_by_agent = {
        aid: g.sort_values("ts") for aid, g in outcomes.groupby("agent_id")
    }

    state = state.copy()
    state["recorded_at"] = pd.to_datetime(state["recorded_at"], utc=True)
    state["is_resident_persistent_bool"] = state["is_resident_persistent"].apply(
        lambda x: x is True or str(x).lower() in ("true", "t", "1")
    )

    for agent_id, g in state.groupby("agent_id"):
        g = g.sort_values("recorded_at").reset_index(drop=True)
        T = len(g)
        if T < 3:
            continue
        obs = np.column_stack(
            [
                g["observed_e"].astype(float).values,
                g["observed_i"].astype(float).values,
                g["observed_s"].astype(float).values,
                g["observed_v"].astype(float).values,
            ]
        )
        # dt in hours; dts[0] = 1.0 (prior step), subsequent = diff in hours clamped.
        ts = g["recorded_at"].values.astype("datetime64[ns]")
        dts = np.zeros(T)
        dts[0] = 1.0
        if T > 1:
            deltas = (ts[1:] - ts[:-1]).astype("timedelta64[s]").astype(float) / 3600.0
            deltas = np.clip(deltas, 1.0 / 60.0, 1.0)
            dts[1:] = deltas

        # Join nearest outcome within ±60s per state row
        is_bad = np.full(T, np.nan)
        if agent_id in out_by_agent:
            og = out_by_agent[agent_id]
            og_ts = og["ts"].values.astype("datetime64[ns]")
            og_bad = og["is_bad"].values.astype(int)
            for i in range(T):
                st = ts[i]
                idx = np.searchsorted(og_ts, st)
                # Candidates: idx-1, idx (if in bounds)
                best = None
                best_delta = None
                for cand in (idx - 1, idx):
                    if 0 <= cand < len(og_ts):
                        delta = abs((og_ts[cand] - st).astype("timedelta64[s]").astype(float))
                        if delta <= 60.0 and (best_delta is None or delta < best_delta):
                            best = cand
                            best_delta = delta
                if best is not None:
                    is_bad[i] = og_bad[best]

        cls = (
            "resident_persistent"
            if bool(g["is_resident_persistent_bool"].iloc[0])
            else "session_or_unlabeled"
        )
        series.append(
            AgentSeries(agent_id=str(agent_id), cls=cls, obs=obs, dts=dts, is_bad=is_bad)
        )
    return series


# ---------- stratified 70/15/15 split ----------

def density_bucket(T: int) -> str:
    if T < 50:
        return "low"
    if T < 500:
        return "med"
    return "high"


def _split_agent_by_time(ag: AgentSeries, train_frac: float, val_frac: float) -> tuple[AgentSeries, AgentSeries, AgentSeries]:
    """Split a single agent's time series contiguously 70/15/15 — for degenerate single-agent classes."""
    T = ag.T
    i_train = int(round(train_frac * T))
    i_val = int(round((train_frac + val_frac) * T))
    make = lambda s, e, suf: AgentSeries(
        agent_id=f"{ag.agent_id}#{suf}",
        cls=ag.cls,
        obs=ag.obs[s:e].copy(),
        dts=ag.dts[s:e].copy(),
        is_bad=ag.is_bad[s:e].copy(),
    )
    return (make(0, i_train, "train"), make(i_train, i_val, "val"), make(i_val, T, "eval"))


def stratified_split(
    series: list[AgentSeries], seed: int = 42
) -> tuple[list[AgentSeries], list[AgentSeries], list[AgentSeries]]:
    """70/15/15 split stratified by (class, density bucket).

    Degenerate-case handling: when a class has only 1 agent (as happens for
    resident_persistent in the 2026-02-20..2026-03-20 window — only Lumen is
    tagged `persistent`), that agent's time series is split contiguously
    70/15/15 so every split has representation of the class. This trades
    agent-level independence for class coverage; documented in the
    session1-report methodology section.
    """
    # Per-class handling
    train, val, eval_ = [], [], []
    by_class = defaultdict(list)
    for s in series:
        by_class[s.cls].append(s)

    rng = random.Random(seed)
    for cls, lst in by_class.items():
        if len(lst) == 1:
            # Time-contiguous split
            ag = lst[0]
            if ag.T < 20:
                # Too short to split meaningfully; dump into train
                train.append(ag)
                continue
            t_tr, t_vl, t_ev = _split_agent_by_time(ag, 0.70, 0.15)
            train.append(t_tr)
            val.append(t_vl)
            eval_.append(t_ev)
            continue

        # Agent-level split, stratified by density bucket
        buckets = defaultdict(list)
        for s in lst:
            buckets[density_bucket(s.T)].append(s)
        for bucket_lst in buckets.values():
            lst_sorted = sorted(bucket_lst, key=lambda a: a.agent_id)
            rng.shuffle(lst_sorted)
            n = len(lst_sorted)
            n_train = max(1, int(round(0.70 * n)))
            n_val = max(0, int(round(0.15 * n)))
            train.extend(lst_sorted[:n_train])
            val.extend(lst_sorted[n_train : n_train + n_val])
            eval_.extend(lst_sorted[n_train + n_val :])
    return train, val, eval_


# ---------- SC gates ----------

def sc1_check(fit: FitState) -> dict:
    """All emission variances and transition noise must lie in pre-registered bounds."""
    issues = []
    for cls, sigma in fit.sigma_obs.items():
        for j, ch in enumerate(("E", "I", "S", "V")):
            if sigma[j] < SIGMA_OBS_MIN or sigma[j] > SIGMA_OBS_MAX:
                issues.append(f"sigma_obs[{cls},{ch}] = {sigma[j]:.4f} outside [{SIGMA_OBS_MIN}, {SIGMA_OBS_MAX}]")
    for j, ch in enumerate(("E", "I", "S", "V")):
        if fit.sigma_trans[j] < SIGMA_TRANS_MIN or fit.sigma_trans[j] > SIGMA_TRANS_MAX:
            issues.append(
                f"sigma_trans[{ch}] = {fit.sigma_trans[j]:.4f} outside [{SIGMA_TRANS_MIN}, {SIGMA_TRANS_MAX}]"
            )

    # C5 coefficient sign pattern check (spec §2.4):
    # sigma(beta0 - beta_E*E - beta_I*I + beta_S*S + beta_V*|V|)
    # With our storage [b0, b_E, b_I, b_S, b_V_abs] meaning the spec-signed coefs,
    # the expected signs at a "bad outcomes are less likely with high E,I and more
    # with high S, |V|" reading are:
    #   b_E >= 0 (entered with minus in logit: high E → lower odds-bad)
    #   b_I >= 0 (same)
    #   b_S >= 0 (high S → higher odds-bad)
    #   b_V_abs >= 0 (high |V| → higher odds-bad)
    #   b0 free
    sign_violations = []
    for cls, coef in fit.c5_coef.items():
        if np.allclose(coef, 0):
            continue  # skipped class (no outcomes)
        if coef[1] < -0.05:
            sign_violations.append(f"{cls}.beta_E = {coef[1]:.3f} < 0 (expected >= 0)")
        if coef[2] < -0.05:
            sign_violations.append(f"{cls}.beta_I = {coef[2]:.3f} < 0 (expected >= 0)")
        if coef[3] < -0.05:
            sign_violations.append(f"{cls}.beta_S = {coef[3]:.3f} < 0 (expected >= 0)")
        if coef[4] < -0.05:
            sign_violations.append(f"{cls}.beta_V_abs = {coef[4]:.3f} < 0 (expected >= 0)")

    return {
        "pass": len(issues) == 0 and len(sign_violations) == 0,
        "bounds_violations": issues,
        "sign_violations": sign_violations,
    }


def _c5_neg_loglik(mu: np.ndarray, coef: np.ndarray, is_bad_t: float) -> float:
    """-log p(is_bad | mu_post, c) under the spec-signed logistic."""
    E, I, S, V = mu
    logit = coef[0] - coef[1] * E - coef[2] * I + coef[3] * S + coef[4] * abs(V)
    # log p(y|x) = y*log(p) + (1-y)*log(1-p) = y*logit - log(1+exp(logit))
    # -log p = log(1+exp(logit)) - y*logit = softplus(logit) - y*logit
    # Numerically stable softplus:
    if logit > 0:
        sp = logit + np.log1p(np.exp(-logit))
    else:
        sp = np.log1p(np.exp(logit))
    return float(sp - is_bad_t * logit)


def sc2_check(
    val_agents: list[AgentSeries], fit: FitState, ode_params: dict, mu0, sigma0_diag
) -> dict:
    """Pearson r(F_hat_t, ||o_chk_t - mu_{t|t-1}||_2) on validation split.

    F_hat_t is the per-turn variational free energy under the full (C1-C4 + C5
    when is_bad is observed) generative model:
      F_hat_t = complexity + accuracy_neg
      complexity    = 0.5 * ||mu_post - mu_pred||^2 / sigma_trans^2   (proxy for D_KL[q||p_prior])
      accuracy_neg  = 0.5 * sum((o - mu_post)^2 / sigma_obs^2) + sum(log sigma_obs)
                    + [ -log p(is_bad | mu_post, c) if is_bad observed ]
    Spec §2.6: halt on r > 0.9 — model is just denoising the observed EISV.
    """
    Fhats = []
    residuals = []
    c5_turn_count = 0
    for ag in val_agents:
        r = run_ukf_smoother(
            obs=ag.obs,
            dts=ag.dts,
            ode_params=ode_params,
            sigma_obs=fit.sigma_obs[ag.cls],
            sigma_trans=fit.sigma_trans,
            mu0=mu0,
            sigma0_diag=sigma0_diag,
        )
        sigma_obs_cls = fit.sigma_obs[ag.cls]
        sigma_t = fit.sigma_trans
        coef = fit.c5_coef.get(ag.cls, np.zeros(5))
        for t in range(ag.T):
            resid_vec = ag.obs[t] - r.mu_pred[t]
            if np.any(np.isnan(resid_vec)):
                continue
            resid_norm = float(np.linalg.norm(resid_vec))

            accuracy_neg = 0.5 * np.sum((ag.obs[t] - r.mu[t]) ** 2 / sigma_obs_cls**2) + float(
                np.sum(np.log(sigma_obs_cls))
            )
            # C5 component if is_bad observed
            if not np.isnan(ag.is_bad[t]) and np.any(coef != 0):
                accuracy_neg += _c5_neg_loglik(r.mu[t], coef, float(ag.is_bad[t]))
                c5_turn_count += 1
            step_move = r.mu[t] - r.mu_pred[t]
            complexity = 0.5 * np.sum((step_move / sigma_t) ** 2)
            fhat = float(complexity + accuracy_neg)
            Fhats.append(fhat)
            residuals.append(resid_norm)

    Fhats = np.asarray(Fhats)
    residuals = np.asarray(residuals)
    if len(Fhats) < 10:
        return {"pass": False, "r": None, "n": len(Fhats), "reason": "insufficient validation samples"}
    r = float(np.corrcoef(Fhats, residuals)[0, 1]) if np.std(Fhats) > 0 and np.std(residuals) > 0 else float("nan")
    halted = bool(np.isfinite(r) and r > 0.9)
    return {
        "pass": (not halted),
        "r": r,
        "n": int(len(Fhats)),
        "n_validation_agents": len(val_agents),
        "c5_turn_count": c5_turn_count,
    }


# ---------- params.json writer ----------

def write_params_json(
    fit: FitState,
    ode_params: dict,
    sc1: dict,
    sc2: dict,
    meta: dict,
    out_path: pathlib.Path,
) -> None:
    # Re-hash ode_params.json
    ode_json = ODE_PARAMS_JSON.read_text()
    ode_sha = hashlib.sha256(ode_json.encode("utf-8")).hexdigest()

    def arr(x):
        return [float(v) for v in np.asarray(x).ravel()]

    payload = {
        "schema_version": 1,
        "ode_params_source": str(ODE_PARAMS_JSON.relative_to(REPO)),
        "ode_params_file_sha256": ode_sha,
        "ode_params_sha256": json.loads(ode_json)["sha256"],
        "fit_metadata": meta,
        "sigma_obs": {cls: arr(s) for cls, s in fit.sigma_obs.items()},
        "sigma_trans_fleet": arr(fit.sigma_trans),
        "c5_coef": {cls: arr(c) for cls, c in fit.c5_coef.items()},
        "c5_coef_ordering": ["beta0", "beta_E", "beta_I", "beta_S", "beta_V_abs"],
        "c5_sign_convention": "sigma(beta0 - beta_E*E - beta_I*I + beta_S*S + beta_V_abs*|V|)",
        "loglik_history": fit.loglik_history,
        "iter_count": fit.iter_count,
        "converged": bool(fit.converged),
        "sc1": sc1,
        "sc2": sc2,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    print(f"[run] wrote {out_path}")


# ---------- plots ----------

def plot_em_convergence(fit: FitState, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[plot] matplotlib unavailable: {e}", flush=True)
        return

    fig, ax = plt.subplots(1, 1, figsize=(8, 4))
    ax.plot(fit.loglik_history, marker="o", linewidth=1.5)
    ax.set_xlabel("EM iteration")
    ax.set_ylabel("Sum filter log-likelihood")
    ax.set_title("v7 F-hat Session 1b — EM convergence")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    for ext in ("png", "pdf"):
        p = out_dir / f"em_convergence.{ext}"
        fig.savefig(p, dpi=150)
        print(f"[plot] wrote {p}")
    plt.close(fig)


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2026-02-20")
    ap.add_argument("--window-end", default="2026-03-20")
    ap.add_argument("--max-agents-per-class", type=int, default=None,
                    help="Cap per-class agent count for faster smoke runs.")
    ap.add_argument("--max-iters", type=int, default=50)
    ap.add_argument("--max-T-per-agent", type=int, default=None,
                    help="Cap T per agent to reduce per-iteration cost (esp. Lumen).")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[run] loading corpus...", flush=True)
    state, outcomes = load_corpus(args.window_start, args.window_end)
    print(f"[run]   state={len(state):,} outcomes={len(outcomes):,}", flush=True)

    print(f"[run] building agent series...", flush=True)
    series = build_agent_series(state, outcomes)
    print(f"[run]   {len(series)} agents", flush=True)
    if args.max_T_per_agent is not None:
        for ag in series:
            if ag.T > args.max_T_per_agent:
                # Subsample uniformly
                idx = np.linspace(0, ag.T - 1, args.max_T_per_agent).astype(int)
                ag.obs = ag.obs[idx]
                # dts: reconstruct as sum of underlying dts
                # Simpler: reset each to average dt; or use index-based dt=1h default
                # We'll use median-preserving subsample via cumulative approach:
                cum = np.cumsum(ag.dts)
                new_dts = np.zeros(len(idx))
                new_dts[0] = cum[idx[0]]
                for k in range(1, len(idx)):
                    new_dts[k] = cum[idx[k]] - cum[idx[k - 1]]
                ag.dts = np.clip(new_dts, 1.0 / 60.0, 1.0)
                ag.is_bad = ag.is_bad[idx]
                ag.T = args.max_T_per_agent

    print(f"[run] stratified split...", flush=True)
    train, val, eval_ = stratified_split(series, seed=42)
    if args.max_agents_per_class is not None:
        # Cap per-class train set size
        caps = defaultdict(list)
        for ag in train:
            caps[ag.cls].append(ag)
        train = []
        for cls, lst in caps.items():
            train.extend(lst[: args.max_agents_per_class])
    print(f"[run]   train={len(train)} val={len(val)} eval={len(eval_)}", flush=True)

    # Summary per class
    for label, lst in (("train", train), ("val", val), ("eval", eval_)):
        by_cls = defaultdict(int)
        rows_by_cls = defaultdict(int)
        for a in lst:
            by_cls[a.cls] += 1
            rows_by_cls[a.cls] += a.T
        print(f"[run]   {label}: " + ", ".join(
            f"{c}={by_cls[c]} agents/{rows_by_cls[c]} rows" for c in sorted(by_cls)
        ))

    ode_cfg = json.loads(ODE_PARAMS_JSON.read_text())
    ode_params = ode_cfg["parameters"]
    mu0 = np.asarray(ode_cfg["prior_at_t0"]["mu_0"])
    sigma0_diag = np.asarray(ode_cfg["prior_at_t0"]["sigma_0_diag"])

    print(f"[run] running EM ({args.max_iters} iters max)...", flush=True)
    fit = em_loop(
        agents=train,
        ode_params=ode_params,
        mu0=mu0,
        sigma0_diag=sigma0_diag,
        max_iters=args.max_iters,
        verbose=True,
    )

    print(f"[run] SC1 check...", flush=True)
    sc1 = sc1_check(fit)
    print(f"[run]   SC1 pass={sc1['pass']}", flush=True)
    if not sc1["pass"]:
        print(f"[run]   bounds: {sc1['bounds_violations']}")
        print(f"[run]   signs:  {sc1['sign_violations']}")

    print(f"[run] SC2 check...", flush=True)
    sc2 = sc2_check(val, fit, ode_params, mu0, sigma0_diag)
    print(f"[run]   SC2 r={sc2['r']:.4f}  n={sc2['n']}  pass={sc2['pass']}", flush=True)

    plot_em_convergence(fit, FIG_DIR)

    # Code commit SHA
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()
    except Exception:
        sha = "unknown"

    meta = {
        "window_start": args.window_start,
        "window_end": args.window_end,
        "train_agents": len(train),
        "val_agents": len(val),
        "eval_agents": len(eval_),
        "corpus_rows_train": sum(a.T for a in train),
        "corpus_rows_val": sum(a.T for a in val),
        "corpus_rows_eval": sum(a.T for a in eval_),
        "train_rows_by_class": {
            c: sum(a.T for a in train if a.cls == c)
            for c in ("resident_persistent", "session_or_unlabeled")
        },
        "wall_time_seconds": round(time.time() - t0, 2),
        "code_commit_sha": sha,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "max_iters": args.max_iters,
        "em_iters_run": fit.iter_count,
        "em_converged": fit.converged,
        "l2_lambda": 0.01,
        "seed": 42,
    }

    write_params_json(fit, ode_params, sc1, sc2, meta, PARAMS_OUT)
    print(f"[run] DONE in {meta['wall_time_seconds']}s")


if __name__ == "__main__":
    main()
