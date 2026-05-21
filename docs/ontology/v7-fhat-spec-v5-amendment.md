# v7 F-hat Spike — v5 Amendment (Session 1a-Authorized Deltas to v4)

**Status:** Binding amendment. Session 1b executes against v4 **with** these deltas applied.
**Companion to:** `v7-fhat-spec.md` v4 (unchanged on disk; this amendment supersedes specific sections), `data/v7-fhat/session1a-findings.md` (the precondition audit that motivated these deltas).
**Operator authorization:** 2026-04-23, delegated decision ("you decide" applied to the four redirect options in session1a-findings.md §4); chose Option X.

---

## What this amendment does

v4 was written before the production DB was audited against its own claims. Session 1a surfaced that three C6 event channels (`circuit_breaker_trip`, `stuck_detected`, `anomaly_detected`) did not exist in `audit.events` until April 2026, well after the reference-window close (2026-03-20); the "epoch-2" corpus filter in §2.5 evaluates to zero rows; BED `|Δη|` is not vector-reconstructable from the audit log; and the Session 2 target `stuck_detected` is produced by a single agent in the eval window. v5 folds the operator-authorized resolutions of those findings into the fit plan without reopening the core spec.

The generative-model structure (v4 §2.1–2.3 state-space, v4 §2.5 estimator, v4 §2.6 SC1/SC2 gates, v4 §6.3 horse-race shape) is unchanged. v5 narrows the emission model, corrects the epoch filter, resolves the B1/B2 choice, and further narrows the target set.

---

## Delta 1 — drop C6 emission channel (structural)

**v4 §2.2 table rows for C6** — strike:

> | $o^{\text{cbk}}_t$ | `audit.events WHERE event_type = 'circuit_breaker_trip'` | Binary (in window) | 71 |
> | $o^{\text{stk}}_t$ | `audit.events WHERE event_type = 'stuck_detected'` | Binary (in window) | 2,729 |
> | $o^{\text{anm}}_t$ | `audit.events WHERE event_type = 'anomaly_detected'` | Binary (in window) | 252 |

**v5 §2.2 observation channels:**

| Channel | Source | Shape | Reference-window cardinality (actual) |
|---|---|---|---|
| $o^{\text{chk}}_t$ | `core.agent_state` (state_json + columns) | $\R^4$: (observed_E, observed_I, observed_S, observed_V) | 114,883 |
| $o^{\text{out}}_t$ | `audit.outcome_events.is_bad` (join nearest agent_state) | Binary | (pull-time) |

**v4 §2.4 C6 emissions** — strike. No logistic-emission coefficients on event indicators.

**v4 §2.4 per-class parameter count** — rewrite:

| Group | v4 count | v5 count |
|---|---|---|
| C1–C4 Gaussian variances | 4 | 4 |
| C5 logistic coefficients | 5 | 5 |
| C6 logistic coefficients (3 event types × 3) | 9 | **0** (dropped) |
| **Per-class emission total** | **18** | **9** |
| Fleet-wide transition noise | 4 | 4 |
| **Total fit params (2 classes)** | **40** | **22** |

**Rationale.** The three C6 event types did not exist in `audit.events` during the reference window (first appearances: `stuck_detected` 2026-04-11, `anomaly_detected` 2026-04-12, `circuit_breaker_trip` 2026-04-16). Per-class C6 coefficients are structurally unidentifiable from zero positive examples. v5 drops the channel entirely rather than fitting unfittable coefficients. This follows the exact pattern v4 §7.1 change log established for `primitive_feedback` / `watcher_finding` / per-agent calibration in the v3→v4 transition: "dropped channels noted as v7.1 / v8 instrumentation work." C6 becomes v7.1 instrumentation.

**Not a reduction in spike validity.** The spike's question — does $\hat{F}$ beat a raw-EISV baseline at predicting bad outcomes? — is answered by the fit on C1–C5 alone. C6 was supplementary diagnostic signal, not load-bearing.

---

## Delta 2 — correct reference-corpus epoch filter

**v4 §2.5** (verbatim, with strike + insert):

> Reference corpus: epoch-2 epoch-1 ~~, non-archived,~~ non-archived, tag-populated agent-turns from `core.agent_state` joined against `core.agents.tags` via `core.identities`. Time window: **2026-02-20 through 2026-03-20** (28 days, comfortably pre-dating the evaluation slice by a week).

**Rationale.** Epoch 2 started in production on 2026-04-01 (Phase-1+2 grounding merge, per `project_eisv-grounding-phase-1.md`). The v4 spec's "epoch-2" phrase was forward-looking (assumed a Phase-3 swap that has not landed). The reference window is entirely within epoch 1 by the window dates themselves.

**Canonical SQL** that encodes this delta is frozen at `data/v7-fhat/sql/reference_state.sql`.

---

## Delta 3 — B1 → B2 comparator resolution (pre-resolved by v4 §7.1 default)

v4 §6.2 and §7.1 already contained the default rule: "fall back to B2 unless all 4 BED components are recoverable." Session 1a verified:

- `core.agent_state.state_json` keys: `{E, phi, verdict, risk_score, health_status}` — **no η vector components**.
- `audit.outcome_events.detail` keys: `{source, prev_norm, current_norm, norm_delta, prev_verdict}` — only scalar norms.
- Zero of four BED vector components (E_err, I_err, S_err, V_err) reconstructable per-agent-turn.

**v5 §6.2 binding:** **B2 (raw-EISV logistic) is the sole comparator for Session 2.** B1 row struck from §6.2. This is a confirmation of v4's own default, not a new override.

---

## Delta 4 — narrow Session 2 targets to one (data-driven)

**v4 §6.1** listed two targets. Eval-window (2026-03-21 → 2026-04-20) coverage audit at Session 1a:

| Target | Eval rows | Unique agents | Session 2 viability |
|---|---|---|---|
| `outcome_is_bad` | 196 | **50** | Grouped-by-agent CV viable. Keep. |
| `stuck_detected` | 2,458 | **1** | Grouped-by-agent CV degenerate (same failure mode v4 §6.1 fixed for v3); temporal-CV fallback possible but CV fold structure weak on single-agent data. |

**v5 §6.1 binding:** **single primary target — `outcome_is_bad`.** `stuck_detected` is demoted to "diagnostic reporting only; does not enter the decision rule."

**Cascade into §6.4 decision rule.** With one cell, §6.4 v4's "both targets meet" and "exactly one target" branches collapse:

- **Path (d) — full-earn** — `outcome_is_bad`: $\Delta\text{AUC} \geq 0.03$ AND 95% CI lower bound strictly > 0 on grouped-by-agent CV.
- **Path (b)** — `outcome_is_bad` fails the win condition, OR SC2 denoising-collapse tripped.
- **Scope-limited (d)** removed — doesn't apply with one cell. Coherent-subset pre-registration (v4 §6.5) struck.
- Non-regression guardrail (v4 §6.4) removed — only applies with multi-cell arbitration.

**v5 §6.6 prior redistribution:**
- $P(\hat{F} \text{ earns on outcome\_is\_bad}) \approx 0.45$ (was 0.25 full-earn in v4)
- $P(\text{null / path (b)}) \approx 0.55$ (was 0.40)
- Scope-limited fraction (0.35 in v4) **mostly folds into (b)**, not into earn: under the tighter single-target bar, the single-target win already *is* the scope-limited outcome under v4's framing. The coherent-subset mapping in v4 §6.5 remains the paper-facing frame — a win here maps to "variational grounding captures outcome-quality surprise; $V$-debt accumulator is grounded for task-success prediction."

---

## Delta 5 — paper v6 App A as binding ODE-parameter source (confirms intent)

v4 §2.3 specified: "ODE parameters $(\alpha, \beta_E, k, \beta_I, \gamma_I, \mu, \lambda_2, \kappa, \delta)$ are taken **fleet-wide** with v6 production values (Appendix A of `unitares-v6.tex`)."

Session 1a surfaced that `config/governance_config.py` (lines 635–651) holds values that differ from paper Appendix A (α=0.5 vs 0.42; β_I=0.05 vs 0.30; γ_I=0.3 vs 0.169 linear). Per memory `feedback_eisv-bounds-drift.md` ("papers are source of truth"), paper App A is authoritative. v5 confirms this with a frozen artifact.

**v5 binding:** `data/v7-fhat/ode_params.json` is the single source of truth. SHA-256 `dee1182cd109c4a3d2999f21168a3093b9be8660765cd0d34f8c4337fce9751e`. Session 2's pre-dispatch hash check reads this file; mismatch against the live file at dispatch time aborts the horse race (spec §2.5 "stale-prior silent error" protection — unchanged).

**γ_I choice.** Paper App A lists two values: `γ_I = 0.25` (logistic damping) and `γ_I^{lin} = 0.169` (linear damping). v4 §2.3 discretization uses the linear form `-γ_I I_{t-1}`, so the binding value is `γ_I = 0.169`.

**Separate concern, not part of v5.** Runtime-vs-paper ODE divergence in `config/governance_config.py` is a standalone cleanup item for a different work thread. v5 does not touch `config/governance_config.py`.

---

## What v5 does NOT change

- **§2.1 — latent state dimensionality.** 4D, aligned to EISV. Unchanged.
- **§2.3 — transition dynamics.** v6 ODE discretized, 9 parameters, prior $\mu_0, \Sigma_0$ as specified. Unchanged.
- **§2.4 C1–C4 Gaussian emissions + C5 logistic `is_bad` emission.** Unchanged except C6 is dropped.
- **§2.4 class partition.** `resident_persistent` / `session_or_unlabeled` — two classes. Unchanged.
- **§2.5 estimator.** Iterated EKF or UKF with posterior-mean linearization, moment-matching reflection at $V \in [-1, 1]$, EM with seed=42, L2 $\lambda = 0.01$, 50 iters / $|\Delta \log L| < 10^{-4}$. Unchanged.
- **§2.6 SC1 + SC2 gates.** Unchanged. SC2's $r > 0.9$ trip still halts before eval slice.
- **§5 / §5.1 paper §3 rewrite requirements.** Unchanged.
- **§6.3 horse-race shape.** Fit logistic on $(\hat{F}_t, \text{B2}_t)$, AUC-ROC, grouped-by-agent CV with temporal fallback, agent-level bootstrap 1000 resamples. Unchanged.

---

## Concrete Session 1b scope under v5

1. Pull reference corpus via `data/v7-fhat/sql/*.sql` (window parameterized; epoch-1 by construction).
2. Stratify agents by class (`resident_persistent` / `session_or_unlabeled`) × check-in density; apply spec's 70/15/15 split with seed=42.
3. Implement iterated-EKF smoother (or UKF; spec §2.5 accepts either) over the 4D v6 ODE with moment-matching reflection at $V$ boundaries. Synthetic-data validation before real-corpus fit (TDD).
4. EM over **22 parameters** — 9 per class × 2 classes + 4 fleet-wide transition noise. L2 regularization, seed 42, convergence gate unchanged.
5. SC1 — all fitted emission variances / coefficients within pre-registered bounds (`data/v7-fhat/ode_params.json` emission / transition noise bounds).
6. SC2 — Pearson $r(\hat{F}_t, \|o^{\text{chk}}_t - \mu_{t|t-1}\|_2)$ on validation split; halt if $r > 0.9$.
7. Convergence diagnostics figure (per-class param trajectory across EM iterations).
8. Ship `data/v7-fhat/params.json` (fitted values + hash of `ode_params.json` + fit-time metadata) and `data/v7-fhat/session1-report.md`.

**Estimated cost:** ~1 focused session. Preconditions frozen in Session 1a. 22-param fit on ~115k rows (Lumen-dominated) converges in minutes if the EKF is right; the cost is the EKF implementation + synthetic-data validation, not the fit run.

---

## Change log (v4 → v5 deltas, condensed)

- C6 emission channel dropped. Per-class params 18 → 9. Total 40 → 22.
- Reference corpus epoch filter: "epoch-2" → "epoch-1".
- B1 comparator struck; B2 is sole comparator for Session 2.
- Session 2 targets 2 → 1 (`outcome_is_bad` only; `stuck_detected` demoted to diagnostic).
- §6.4 scope-limited branch struck; §6.5 coherent-subset pre-registration struck; non-regression guardrail (§6.4 v4) struck.
- §6.6 priors redistributed: 0.40 full-earn / 0.60 (b).
- ODE parameters bound to `data/v7-fhat/ode_params.json` (SHA-256 frozen).
