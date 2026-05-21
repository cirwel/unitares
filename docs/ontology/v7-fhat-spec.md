# v7 $\hat{F}$ Spike — Minimal Generative Model for Governance Latents

**Purpose:** Specify a minimum-viable generative model $p(o, s)$ that would make path (d) from the 2026-04-23 FEP-departure decision honest — variational free energy $\hat{F}$ derivable from UNITARES's existing observables, identified with the $V$ accumulator, class-conditional by construction.

**Decides:** Whether path (d) is feasible at v7-scope and what it would look like concretely.

**Does not decide:** Whether to commit v7 to (d) vs (b) demotion. That decision follows the validation spike described in §6 below.

**Companion to:** `paper-positioning.md` (v7 animating thesis), `plan.md` row R1 (behavioral-continuity verification unblocks variational identity work). Supersedes the (a) path framing in `paper-positioning.md` §"Where v6 could resist the ontology."

---

## 0. Framing (v4): UNITARES as the external Markov blanket

A Parr/Pezzulo-literate reviewer will ask: "whose variational free energy is $\hat{F}$?" The honest answer, which the spec now commits to from §0 forward, is that **$\hat{F}$ is the governance observer's surprise over the agent, not the agent's own variational free energy.** UNITARES sits outside the agent as an external Markov blanket; the generative model $p(o, s)$ described below lives in the observer, not in the agent. This reframing is load-bearing for v7:

- It is the honest read of what UNITARES has always computed: the governance server maintains posterior beliefs about agent-state (EISV) based on check-ins and audit events.
- It sidesteps the v6 claim that $E$ equals the agent's own $-F$, which would require access to the agent's internal generative model — something UNITARES never has for opaque LLM-backed agents.
- It gives v7 a clean paper story: "UNITARES is a hierarchical Bayesian observer over a fleet of agents; the v6 EISV dynamics are the transition prior of this observer; $\hat{F}$ is the observer's predictive surprise, and class-conditional calibration emerges as the observer's class-conditional emission structure."

The v7 §3 rewrite leads with this framing regardless of whether the spike lands full path (d), scope-limited (d), or falls back to (b). The observer-vs-agent distinction is honest either way; only the quantitative $\hat{F}$ vs phenomenological $V$ question depends on the spike outcome.

---

## 1. Why the v6 claim is honest-but-empty

v6 §3.2 defines $E = \sigma(-F / E_{\text{scale}})$ with $-F$ the variational free energy of the agent's internal generative model. The paragraph admits the computation is deferred — production uses a resource-rate heuristic $E_{\text{resource}} = (\text{tokens}/s) / (\text{tokens}/s)_{\max}$ tagged `e_source = "resource"`, "not equivalent to $-F$ and does not approximate it under stationarity in any formal sense."

Two gaps:

1. **Token logprobs are not $F$.** Exposing per-token logprobs gives $-\log p(y_t \mid y_{<t}, x)$ under the language model's own token distribution. That is prediction error over *token sequences*, not variational free energy over *governance latents*. Shipping logprobs improves $S$ (response-distribution entropy) and possibly $I$ (paired-logprob MI). It does not buy $-F$.

2. **$F$ requires a generative model of governance-relevant latents.** $F = E_q[\log q(s) - \log p(o, s)]$ needs a declared $p(o, s)$ over latents $s$ and observations $o$. UNITARES does not ship one. Without it, no computation of $F$ exists; "tier-1 estimator" is undefined.

The good news: the *observations* are already there. What is missing is the model.

## 2. Minimal generative model

### 2.1 Latents $s_t = (E_t, I_t, S_t, V_t) \in \R^4$

**Key design choice (v3):** The latents are the four EISV coordinates themselves — not a separate parallel 4-dim decomposition. The generative model treats true $E, I, S, V$ as unobservable; the check-in values stored in `core.agent_state` and `audit.outcome_events.eisv_*` are noisy emissions of those latents, not the latents themselves. This aligns with GPT's 2026-04-23 call: "start with 4D, aligned to E/I/S/V, not 6D+. The point is to test whether an explicit generative layer improves prediction, not to win with an overfit latent soup."

| Symbol | Support | Meaning under v3 |
|---|---|---|
| $E_t$ | $[0,1]$ | True negative-free-energy / productive capacity — inferred, not measured |
| $I_t$ | $[0,1]$ | True information integrity — inferred from consequences |
| $S_t$ | $[0,1]$ | True response-distribution entropy — inferred |
| $V_t$ | $[-1,1]$ | True accumulated free-energy residual — inferred |

Earlier drafts (v1, v2) used a parallel 4-dim decomposition (competence/load/risk/integrity). That was rejected 2026-04-23 on two grounds: (a) it introduced a second coordinate system with no paper-grounded semantics; (b) the v6 EISV dynamics themselves form a natural transition prior, which the parallel decomposition would ignore.

**Observable proxies for the latents** (these are features of observations, not observations themselves):
- `agent_state.state_json->>'E'` — heuristic $E$ from the live check-in
- `agent_state.integrity` — heuristic $I$
- `agent_state.entropy` — heuristic $S$
- `agent_state.volatility` — heuristic $V$
- `outcome_events.eisv_*` — EISV snapshot at outcome time

These are treated as noisy measurements (§2.4 C1–C4).

### 2.2 Observations $o_t$

Five observation channels, all pullable from the governance DB as verified by the 2026-04-23 schema audit. Channels that were in v2 but are not historically recoverable (`primitive_feedback` user corrections, Watcher findings, per-agent calibration state) are dropped; v3 uses only what the DB actually provides.

| Channel | Source | Shape | 30d cardinality |
|---|---|---|---|
| $o^{\text{chk}}_t$ | `core.agent_state` (state_json + columns) | $\R^4$: (observed_E, observed_I, observed_S, observed_V) | 17,654 rows |
| $o^{\text{out}}_t$ | `audit.outcome_events.is_bad` (join nearest agent_state) | Binary | 18,448 rows |
| $o^{\text{cbk}}_t$ | `audit.events` WHERE event_type = 'circuit_breaker_trip' | Binary (in window) | 71 |
| $o^{\text{stk}}_t$ | `audit.events` WHERE event_type = 'stuck_detected' | Binary (in window) | 2,729 |
| $o^{\text{anm}}_t$ | `audit.events` WHERE event_type = 'anomaly_detected' | Binary (in window) | 252 |

Schema notes:
- `core.agent_state` columns: `entropy` (=$S$), `integrity` (=$I$), `volatility` (=$V$), `coherence`, `regime`; `state_json->>'E'` carries $E$; `state_json->>'phi'`, `->>'verdict'`, `->>'risk_score'` carry derived scalars available as additional features if needed.
- `audit.outcome_events`: `is_bad` boolean + `outcome_score` real + `eisv_e/i/s/v/phi/verdict/coherence/regime` columns. Per-outcome EISV snapshot is directly available.
- `audit.events` partitioned by month; timestamp column is `ts`, not `event_time`.

**Time discretization:** Per agent, align events to `core.agent_state.recorded_at` timestamps. For each state row at time $t$, emit one $o_t$ tuple by joining:
- $o^{\text{chk}}$: direct columns from the row.
- $o^{\text{out}}$: the outcome_event nearest to $t$ within ±60s on the same agent, if any; else NULL (missing-observation handling in §2.4).
- $o^{\text{cbk, stk, anm}}$: indicator of whether the event type fired on that agent within the forward window $(t, t + \Delta w]$ with $\Delta w = 60$s.

### 2.3 Transition $p(s_t \mid s_{t-1})$ — the v6 ODE as prior

**Key design choice (v3):** The transition prior is the v6 governing SDE (v6 §2.2), discretized. This is what makes v3 a non-trivial generative model: the v6 dynamics themselves become load-bearing as the prior on latent EISV. $\hat{F}$ ends up measuring how surprising the observations are given the ODE's prediction of where the latents should be.

Discretize v6 equations 2.5–2.8 with step $\Delta t$:

$$E_t = E_{t-1} + \left[\alpha(I_{t-1} - E_{t-1}) - \beta_E E_{t-1} S_{t-1}\right] \Delta t + \eta^E_t$$

$$I_t = I_{t-1} + \left[-k S_{t-1} + \beta_I C(V_{t-1}) - \gamma_I I_{t-1}\right] \Delta t + \eta^I_t$$

$$S_t = S_{t-1} + \left[-\mu S_{t-1} - \lambda_2 C(V_{t-1})\right] \Delta t + \eta^S_t$$

$$V_t = V_{t-1} + \left[\kappa(E_{t-1} - I_{t-1}) - \delta V_{t-1}\right] \Delta t + \eta^V_t$$

where $\eta^j_t \sim \mathcal{N}(0, (\sigma^j_{\text{trans}})^2 \Delta t)$ is per-latent transition noise. The drift-coupling terms ($\gamma_E \|\Delta\eta\|^2$ in $\dot{E}$, $\lambda_1 \|\Delta\eta\|^2$ in $\dot{S}$) are **omitted from the v3 prior** — the BED vector $\|\Delta\eta\|$ is what $\hat{F}$ is being compared against (§6 horse race); including it in the prior would circular-reason.

**ODE parameters:** $(\alpha, \beta_E, k, \beta_I, \gamma_I, \mu, \lambda_2, \kappa, \delta)$ are taken **fleet-wide** with v6 production values (Appendix A of `unitares-v6.tex`). Per GPT's 2026-04-23 call, class-conditioning enters via emissions only in v1 — the latent dynamics stay shared. This also avoids re-calibrating ODE parameters per class, which is not well-justified from the available data.

**Transition noise:** $\sigma^j_{\text{trans}} \in [0.005, 0.05]$, fit by maximum likelihood on the reference corpus per §2.5. Bounds pre-registered.

**Prior at $t=0$:** $s_0 \sim \mathcal{N}(\mu_0, \Sigma_0)$ with $\mu_0 = (0.7, 0.8, 0.2, 0.0)$ (a nominal healthy state, fleet-wide) and $\Sigma_0 = \operatorname{diag}(0.1^2, 0.1^2, 0.1^2, 0.2^2)$ pre-registered.

**Box constraints:** latent $E, I, S \in [0, 1]$ and $V \in [-1, 1]$ are enforced via reflection at boundaries during simulation (not via clamping, to preserve mass).

### 2.4 Emission $p(o_t \mid s_t, c)$

Five observation channels per §2.2, emitting from latent EISV, with class $c$ conditioning emissions only (not transitions). Per-class emission coefficients are fit once from the reference corpus and **frozen before the spike computes $\hat{F}$ on the evaluation slice**.

**C1–C4 — observed EISV channels** (noisy measurements of the latents):

$$o^{\text{chk},E}_t \mid E_t, c \sim \mathcal{N}(E_t,\ (\sigma^{c,E}_{\text{obs}})^2)$$

$$o^{\text{chk},I}_t \mid I_t, c \sim \mathcal{N}(I_t,\ (\sigma^{c,I}_{\text{obs}})^2)$$

$$o^{\text{chk},S}_t \mid S_t, c \sim \mathcal{N}(S_t,\ (\sigma^{c,S}_{\text{obs}})^2)$$

$$o^{\text{chk},V}_t \mid V_t, c \sim \mathcal{N}(V_t,\ (\sigma^{c,V}_{\text{obs}})^2)$$

Per-class observation noise reflects that different classes have different check-in fidelity — residents have more stable measurements than ephemeral-session agents. Emission variances $\sigma^{c,j}_{\text{obs}} \in [0.01, 0.3]$ pre-registered.

**C5 — outcome_event `is_bad`** (binary, when an outcome_event is joined to state row $t$):

$$P(\text{is\_bad} \mid s_t, c) = \sigma\!\left(\beta^c_0 - \beta^c_E E_t - \beta^c_I I_t + \beta^c_S S_t + \beta^c_V |V_t|\right)$$

Rationale: bad outcomes are more likely when latent $E, I$ are low (productive capacity and integrity degraded) and when $S, |V|$ are high (uncertainty and imbalance). Sign pattern pre-registered.

**C6 — event-stream indicators** (Bernoulli per event type, independent given latents):

$$P(\text{circuit\_breaker\_trip in } (t, t + \Delta w] \mid s_t, c) = \sigma\!\left(\xi^{c,\text{cbk}}_0 + \xi^{c,\text{cbk}}_{|V|} |V_t| + \xi^{c,\text{cbk}}_S S_t - \xi^{c,\text{cbk}}_I I_t\right)$$

$$P(\text{stuck\_detected in } (t, t + \Delta w] \mid s_t, c) = \sigma\!\left(\xi^{c,\text{stk}}_0 - \xi^{c,\text{stk}}_E E_t + \xi^{c,\text{stk}}_S S_t\right)$$

$$P(\text{anomaly\_detected in } (t, t + \Delta w] \mid s_t, c) = \sigma\!\left(\xi^{c,\text{anm}}_0 + \xi^{c,\text{anm}}_S S_t + \xi^{c,\text{anm}}_{|V|} |V_t|\right)$$

Three event types (circuit_breaker, stuck, anomaly) each with 3 coefficients (intercept + two EISV features). Sign patterns pre-registered as above.

**Per-class parameter count.** C1–C4: 4 variances. C5: 5 coefficients. C6: 9 coefficients (3 event types × 3 each). **Per-class total: 18.** Plus fleet-wide transition noise (4) and fleet-wide ODE parameters (9, from v6 Appendix A).

**Classes to fit — reduced to 2 in v4** after the 2026-04-23 council audit. The original v3 plan (residents / Claude / Codex / unlabeled, 4 classes) is empirically infeasible on the eval corpus: the pragmatic-lens review ran the SQL and found `Codex_*` has 10 rows across 5 labels, non-residents average 5–10 state rows each, and 81% of eval-window state rows are concentrated in 5 resident agents. Per-class 18-param fits are not identifiable on that distribution.

**v4 class partition:**
- `resident_persistent` — agents tagged `persistent` in `core.agents.tags` (Vigil, Sentinel, Watcher, Steward, Lumen; ~11 agents).
- `session_or_unlabeled` — everything else (session-bounded assistants, ephemeral helpers, unlabeled agents).

**Total params to fit:** 18 × 2 classes + 4 (fleet-wide transition noise) = **40**. Well-identified on the eval corpus.

### 2.5 Fit protocol (pre-registered)

**Reference corpus:** epoch-2, non-archived, tag-populated agent-turns from `core.agent_state` joined against `core.agents.tags`. Time window: **2026-02-20 through 2026-03-20** (30 days, comfortably pre-dating the evaluation slice by a week).

**Estimator:** Expectation-maximization with iterated EKF / UKF smoother on the E-step (v4 upgrade from v3's single-pass EKF):
- E-step: **iterated** extended Kalman smoother or unscented Kalman smoother over the nonlinear v6 ODE transition. Emissions (Gaussian C1–C4, logistic C5–C6) linearized around **posterior mean, not prior mean** (v4 correction per 2026-04-23 pragmatic-lens review: resident agents operate far from the prior mean $\mu_0 = (0.7, 0.8, 0.2, 0.0)$, at e.g. $E \approx 0.95$, $V \approx -0.3$; linearizing at prior mean gives poor fit on 81% of the corpus). Reflection at $V \in [-1, 1]$ handled as post-update clipping plus moment-matching re-projection (preserves Gaussian approximation near boundaries).
- M-step: per-class maximum likelihood over the 18 emission parameters and fleet-wide transition noise. $L_2$-regularized with $\lambda = 0.01$ (pre-registered).

**Convergence:** 50 EM iterations or $|\Delta \log L| < 10^{-4}$, whichever first. If non-convergence, the fit-diagnostic figure (per-class param trajectory across EM iters) is shipped alongside `params.json` with an explicit non-convergence flag; §6 horse race is blocked until operator reviews.

**Split discipline:**
- **Fit split:** 70% of reference-corpus agents (randomly selected, stratified by class; seed pre-registered as 42). Reference window: 2026-02-20 through 2026-03-20.
- **Validation split:** 15%, used for two pre-spike sanity checks (see §2.6).
- **Held-out evaluation split:** 15% + the 30-day forward slice 2026-03-21 through 2026-04-20, not touched until §6 horse race runs.

**ODE-param hash:** `params.json` includes a SHA-256 of the v6 Appendix A ODE parameter tuple $(\alpha, \beta_E, k, \beta_I, \gamma_I, \mu, \lambda_2, \kappa, \delta)$ captured at fit time. Session 2 computes the live hash before dispatch; mismatch aborts the horse race (prevents stale-prior silent errors under the operator's ongoing v6 tuning work, e.g. the pending phase-3 swap).

**Freeze point:** `params.json` + pre-registration record (exact SQL queries, split seed, EM settings, parameter-range claims, ODE-param hash) committed to git. The §6 spike reads that file and does not refit. Any parameter change requires a new pre-registration commit.

### 2.6 Pre-spike sanity checks (hard gates before §6 runs)

**SC1 — parameters in pre-registered ranges.** All fitted emission variances and coefficients must lie within the pre-registered bounds from §2.3–2.4. Out-of-range parameters invalidate the fit; refit with tightened priors or accept the parameter excursion and pre-register its justification.

**SC2 — denoising-collapse check** (v4 addition per 2026-04-23 statistical-lens review). On the validation split, compute the Pearson correlation between per-turn $\hat{F}_t$ and the raw emission residual $\|o^{\text{chk}}_t - \mu_{t|t-1}\|_2$. If $r > 0.9$, the generative model has collapsed to denoising the observed EISV — $\hat{F}$ adds no information beyond what a simple residual-from-ODE-forecast would give. **Stop before the eval slice is touched.** Report $r$ in the results table regardless of outcome, so reviewers can see the model did not trivially re-encode its observations.

## 3. Variational free energy

Under the above model, the variational free energy of the posterior $q(s_t)$ against the generative model at turn $t$ is:

$$F_t = \underbrace{D_{\text{KL}}[q(s_t) \| p(s_t \mid s_{t-1})]}_{\text{complexity}} - \underbrace{\mathbb{E}_{q(s_t)}[\log p(o_t \mid s_t, c)]}_{\text{accuracy}}$$

Standard decomposition. Complexity: how far the posterior has moved from the transition prior. Accuracy (negated): expected log-likelihood of observed outcomes under the posterior.

**Inference:** Mean-field Gaussian posterior $q(s_t) = \prod_j \mathcal{N}(\mu_{t,j}, \sigma^2_{t,j})$. Per-observation update is a Kalman-filter-style closed-form step (linearizing sigmoid emissions around the prior mean for tractability). Compute cost: $O(\dim(s) \cdot \dim(o)) = O(24)$ per turn. Cheap.

**Per-turn $\hat{F}$:** scalar; bounded below by the log-evidence $-\log p(o_t \mid c)$.

**Accumulated $\hat{F}$:**

$$V_t \equiv \lambda V_{t-1} + (1 - \lambda) \hat{F}_t$$

This is exactly the v6 §3.2 equation (V-grounded) with $F_t - F_{\text{ref}}$ replaced by $\hat{F}_t$ (the reference is now absorbed into the posterior's prior).

**Identification:** $V$ under v7 = exponentially-weighted accumulated $\hat{F}$ under the minimal generative model. No new scalar; the existing accumulator gets a principled definition.

## 4. Class-conditional $\hat{F}$ for free

Because emissions $p(o_t \mid s_t, c)$ are class-conditional, $\hat{F}$ is automatically class-conditional. Two agents producing the same observation sequence but belonging to different classes receive different $\hat{F}$ values because their expected emission distributions differ.

This is the v6 §5 class-calibration mechanism, re-derived from the variational model. Scale constants $\{S_{\text{scale}}, I_{\text{scale}}, E_{\text{scale}}, \|\Delta\|_{\max}\}$ become summary statistics of the class-conditional emission and prior distributions, not independent tuning parameters. The "class-conditional calibration" contribution of v6 gains a generative-model derivation; it was empirically right but theoretically ungrounded under the pure v6 framing.

## 5. What this does not fix: the $E$ coordinate

$\hat{F}$ formalizes $V$. It does **not** formalize $E$.

Under active inference, the natural candidates for an $E$-like scalar are:

- **Precision of posterior** (inverse posterior variance, $-\log \det \Sigma_q$) — but this overlaps with $S$ (response-distribution entropy), and the paper already uses $S$ for that semantic.
- **Expected free energy of policy** ($G = E_{q(o, s \mid \pi)}[F]$ under a planning policy $\pi$) — but this requires an explicit policy model UNITARES does not ship.
- **Evidence lower bound (ELBO)** on the generative model — which is $-F$, already claimed by $V$.

Honest conclusion: **$E$ has no FEP grounding under path (d).** $E$ must be reframed phenomenologically — "productive capacity," grounded in tempo/throughput proxies per class, honestly tagged as a non-information-theoretic coordinate. The current resource-rate heuristic continues; the paper stops claiming $E = -F$ in any sense.

v7 §3 (Information-Theoretic Grounding) contracts: $S, I, V$ have FEP / Shannon grounding; $E$ does not. This is more honest than v6 and carries a real cost — the paper loses the "all four coordinates are information-theoretic quantities" clean-table claim. The gain is that the claims it does make are ones the code can back up.

### 5.1 §3 coordinate-table rewrite is required under both paths, not just (d)

v6 §3.1 presents a coordinate table (at `unitares-v6.tex:634–647`) that lists *both* $E$ as "negative variational free energy $-F$" *and* $V$ as "accumulated free-energy residual." These cannot be simultaneously true under any coherent generative model: $E$ would be $V$'s time derivative, which the v6 ODEs do not say. The table is internally inconsistent regardless of what UNITARES ships.

**Under path (d):** $V$ becomes $\hat{F}$-debt under the §2 generative model; $E$ goes phenomenological. Coordinate-table rewrite: $E$ row gains a "productive-capacity (non-information-theoretic)" label; $V$ row gains a "$\hat{F}$-debt under minimal generative model, §2" label.

**Under path (b):** Both $E$ and $V$ go phenomenological. Coordinate-table rewrite: both rows lose their FEP labels; $V$ gains a "damped accumulator of $E{-}I$ gap, phenomenological" label; the v6 §3.2 V-paragraph's Friston derivation is deleted.

Either way, the v6 §3.1 table and the §3.2 $E$/$V$ paragraphs are load-bearing for paper v7 surgery. Estimating the (d) path as "rewrites §3.1" is correct; estimating the (b) path as "no paper work beyond related-work relabeling" is wrong — (b) requires the same table rewrite, with more grounding text deleted rather than reparented.

## 6. Validation protocol (the spike): predictive horse race — v4 narrow design

The v2 draft was a correlation test (rejected as unfalsifiable). The v3 draft was a 15-cell predictive horse race (rejected by the 2026-04-23 council as data-infeasible: 3 of 5 targets had $<2$ active agents in the eval window, making group-by-agent CV degenerate). **v4 re-scopes to a narrow design** with two well-supported targets at one pre-registered primary horizon. The trade is resolution for executability: the (d) vs (b) binary is unchanged, but the resolution distinguishing "full (d)" from "scope-limited (d)" shrinks to 1-vs-2 targets, which is an honest reflection of what the data can support.

### 6.1 Targets (two, well-supported)

Two DB-verified targets with adequate per-agent coverage in the eval window (2026-03-21 → 2026-04-20):

| Target | Source | Eval count | Unique agents |
|---|---|---|---|
| `outcome_is_bad` | `audit.outcome_events WHERE is_bad = true` | 239 | ~15 (residents + session) |
| `stuck_detected` | `audit.events WHERE event_type = 'stuck_detected'` | 2,729 | varies |

The v3 target list (also including `circuit_breaker_trip`, `anomaly_detected`, `lifecycle_paused`) was dropped because those targets have fewer than $\sim$150 positive events on $\geq 2$ distinct agents in the eval slice. They are reported in the results artifact as diagnostic context but do not enter the decision rule.

**Primary horizon:** $k = 20$ turns (pre-registered). Supporting horizons $k \in \{5, 40\}$ are computed and reported but do not enter the decision rule.

### 6.2 Comparators

The horse race compares $\hat{F}$ to **two baselines** (v4 adds a second baseline after the 2026-04-23 council audit raised concerns about $|\Delta\eta|$ historical pullability):

- **B1 — BED $|\Delta\eta|_t$** if historically pullable per-agent-turn. Session 1 verifies pullability as a pre-condition; the calibration-gap component of BED is in-process per the schema audit, so the full BED scalar may only be partially reconstructable. Session 1 reports which BED components are recoverable from `audit.outcome_events.detail` + `core.agent_state.state_json` and whether the pooled $|\Delta\eta|$ can be computed from historical fields.
- **B2 — raw-EISV logistic** (always pullable). A univariate-per-feature logistic with features $(E, I, S, V, \text{phi}, \text{risk\_score})$ from `state_json`. This is the "simple baseline derivable from the same observations as $\hat{F}$" comparator. It is the fallback if B1 is not reconstructable.

### 6.3 Horse race

For each target × comparator: fit the comparator as a logistic regression on $(\hat{F}_t, \text{comparator}_t)$ pair, on the held-out eval split. Evaluate AUC-ROC.

**Cross-validation:** 5-fold CV **grouped by agent** for `outcome_is_bad` (agent-diverse enough to support it). For `stuck_detected`, if grouped-by-agent CV produces folds with zero positives on test, fall back to **stratified temporal CV** (time-ordered folds). Per-target CV choice pre-registered with justification.

**Record per target, per comparator:**
- $\text{AUC}(\hat{F})$
- $\text{AUC}(\text{comparator})$
- $\Delta\text{AUC} = \text{AUC}(\hat{F}) - \text{AUC}(\text{comparator})$
- 95% bootstrap CI on $\Delta\text{AUC}$ (agent-level bootstrap, 1000 resamples; time-block bootstrap for temporal CV)

**Total cells: 2 targets × (B1 if available else B2) = 2 head-to-head comparisons.**

### 6.4 Decision rule (v4)

Decisions are made on the primary-comparator table (B1 if pullable, else B2). Supporting-horizon tables and the unused comparator appear in the results as diagnostic evidence but do not drive the verdict.

**Win condition (per cell):** $\Delta\text{AUC} \geq 0.03$ with **lower bound of the 95% CI strictly greater than zero** (equivalent to a one-sided test at $\alpha = 0.025$). With only 2 cells, multiple-comparisons correction is handled by requiring strict CI-above-zero, which is already conservative; no explicit FDR correction is needed (and BH at q=0.05 across 2 cells degenerates to the same rule).

**Non-regression guardrail:** on any cell that does not meet the win condition, the lower bound of the 95% CI for $\Delta\text{AUC}$ must be $\geq -0.02$. A signal that wins narrowly on one target while materially regressing on the other does not earn path (d).

**Classification:**
- **Path (d) full-earn** — both targets meet win condition + non-regression. Commit v7 to (d); §3 coordinate-table rewrite per §5.1(d).
- **Path (d) scope-limited** — exactly one target meets win condition with **tighter** $\Delta\text{AUC} \geq 0.05$ (tighter bar reflects the narrower win); other target meets non-regression. v7 claims $\hat{F}$-grounding for the winning target's failure class; see §6.5 for coherent-subset pre-registration.
- **Path (b)** — neither target meets win condition, OR either target regresses below the guardrail, OR SC2 (denoising-collapse sanity check, §2.6) tripped. Demote FEP to related-work / inspirational. §3 coordinate-table rewrite per §5.1(b).

### 6.5 Coherent-subset pre-registration (for scope-limited (d))

The theory-lens council review (2026-04-23) flagged that a scope-limited result reads as paper-writable only if the winning target corresponds to a coherent failure class, not a scattered subset. With two targets this reduces to a simple mapping:

- **`outcome_is_bad` wins alone** → coherent claim: "$\hat{F}$ captures outcome-quality surprise; $V$-debt accumulator is grounded for task-success prediction." Paper frame: variational grounding of outcome-quality governance.
- **`stuck_detected` wins alone** → coherent claim: "$\hat{F}$ captures agent-paralysis surprise; $V$-debt accumulator is grounded for stuck-state prediction." Paper frame: variational grounding of temporal-stability governance.

Both framings are paper-publishable as scope-limited wins. They are pre-registered here so the §6.4 classification does not require post-hoc interpretation.

### 6.6 Prior estimate (v4)

$P(\text{path (d) full-earn, both targets}) \approx 0.25$.
$P(\text{path (d) scope-limited, one target}) \approx 0.35$.
$P(\text{path (b), neither target wins or collapse}) \approx 0.40$.

Downward revision from v3 (full-earn $\approx 0.40$) because the narrow spike has stricter per-target power requirements and the scope-limited bar is tighter ($\Delta\text{AUC} \geq 0.05$, not $\geq 0.03$). The (b)-fraction rises because the SC2 denoising-collapse failure mode is a new (b) verdict path that didn't exist in v3.

### 6.7 Spike cost estimate (v4)

- **Session 1 (fit):** 2 sessions realistically, per the pragmatic-lens review. 115k fit-window rows through nonlinear iterated-EKF + EM over 40 parameters + SC1/SC2 sanity checks is not a one-session job.
- **Session 2 (horse race):** 1 session. Tight 2-cell table with well-specified decision rule.
- **Total: 3 sessions end to end.** Each session produces an artifact: the frozen `params.json` + pre-registration record, and the AUC comparison table + verdict.

## 7. Risks and open questions

### What kills path (d)

1. **$\hat{F}$ ties or loses the horse race.** Per §6.4 decision rule. If $\hat{F}$ doesn't beat $|\Delta\eta|$ at forward-predicting audit events, BED is already doing the predictive work and FEP formalization adds no information.
2. **Class-specific coefficients don't stabilize.** If the per-class emission coefficients bounce across production windows, the generative model is under-specified relative to the observable channels — needs richer state dimension or richer observables. Expands v7 scope beyond what the paper can absorb.
3. **Tag-discipline blocker (S8a).** 96% of active agents lack class tags. Per-class fitting requires the class partition. Either (a) path (d) waits on S8a Phase-1 default-stamp rollout, or (b) the spike uses the known-class subset (residents + `Claude_*`-labelled) and the v7 paper is explicit about the class-coverage caveat.

### What (d) does *not* determine

R1 (behavioral-continuity verification, per `plan.md`) is independent. Path (d) makes variational identity verification over $q(s_t \mid \text{trajectory})$ a **viable candidate solution** for R1 — the same generative model that grounds $\hat{F}$ can, in principle, evaluate whether a declared-lineage agent's trajectory is consistent with its claimed parent's posterior. That is one candidate R1 solution among several (behavioral signature matching, substrate-earned three-condition check, etc.). **R1 stays open regardless of the spike outcome.** A (d) win in v7 should not precommit R1's shape.

### Resolved in v3

- **Latent dimensionality** (v2 Q1): frozen at **4-dim, aligned to EISV** (not a separate decomposition). See §2.1.
- **Class-conditioning location** (v2 Q2): frozen at **emissions only, fleet-wide transitions**. See §2.3–2.4.
- **Migration posture** (v2 Q3): frozen at **additive sidecar**. $\hat{F}$-grounded $V$ is computed alongside the v6 $V$ accumulator; it does not influence governance decisions until the horse race earns it.

### Resolved in v4 (post-2026-04-23 council)

- **Acceptance threshold** (v3 Q1): $\Delta\text{AUC} \geq 0.03$ / CI strictly above zero / both targets for full-earn; $\geq 0.05$ / one target for scope-limited. Non-regression guardrail $\geq -0.02$. Narrower target set (2 vs 5) makes threshold less about multiple-comparisons and more about per-cell effect size.
- **Class partition reduction** (v3 implicit): 5 classes → 2 (`resident_persistent` / `session_or_unlabeled`). Driven by eval-corpus data sparsity, not preference. Reduces 76 params to 40; matches what the data can identify.
- **Channel dropout acceptance** (v3 Q3): 5 observation channels accepted for v7.0 scope; dropped channels (primitive_feedback, watcher_finding, per-agent calibration) noted as v7.1 / v8 instrumentation work. Narrowing to 2 targets reflects the same acceptance.
- **Observer-vs-agent framing** (new): $\hat{F}$ is explicitly the governance observer's free energy over the agent, not the agent's own variational free energy. §0 leads with this; §3 of paper v7 rewrites lead with this regardless of spike outcome.

## 8. Next step

If this spec reads as roughly-right: run the two-session sequence.

- **Session 1**: Execute §2.5 fit protocol. Produce `data/v7-fhat/params.json` committed to git. Artifact: the frozen parameter file + a short report on fit convergence and stability.
- **Session 2**: Execute §6 horse race. Produce AUC comparison table per §6.3, decision against §6.4 rule. Artifact: `<not-produced>/docs/ontology/v7-fhat-spike-results.md` (planned per this doc; never produced as a separate file — Session 1b results landed inline in §2.6 and the S12 row of `plan.md`, where SC2 tripped and FEP was redirected per spec §5.1(b)).

If the spec needs re-scoping first: redirect on the §7 open questions before Session 1. Particularly the latent-dimensionality choice — that determines the state-space of everything downstream.

## 9. Change log

- **v6 (2026-04-23):** **Session 1b executed; SC2 tripped; operator selected path (b).** Session 1b (an internal session, parent `3f2df228-…`) ran the v4 + v5-amendment fit protocol on master `fdc2d180`. EM converged at iter 20/50 (log L 18,409 → 30,343); SC1 passed (all 22 params within pre-registered bounds, C5 sign pattern intact); **SC2 tripped at Pearson r = 0.9949 on 952 validation rows**, confirming denoising-collapse per spec §2.6. Root cause: after v5 dropped C6 (the only asymmetric-information channel), the emission model reduced to C1-C4 direct EISV measurements plus sparse C5, making F̂_t structurally collinear with ‖o_chk_t − μ_{t|t-1}‖₂. Eval slice untouched per halt discipline; Session 2 did **not** run. Operator (operator, 2026-04-23) selected **R1** (accept path (b) early) over R2 (wait for C6 maturity / broaden channels) and R3 (run anyway — not recommended). **Resolution:** v7 demotes FEP from load-bearing grounding to adjacent/inspirational framing; V's FEP-grounding retires alongside E's (spec §5.1(b)). Blocked, not disproven — the minimal generative model is too thin under the current observation-channel geometry; C6 / primitive-feedback / per-agent-calibration instrumentation could re-open the question in v7.1/v8. Artifacts frozen at `data/v7-fhat/{params.json, session1-report.md, figures/em_convergence.png, fit/}`. Downstream paper-positioning + plan.md updates landed alongside this change-log entry.
- **v5 (2026-04-23):** Post-Session-1a amendment. Production-DB audit at Session 1a surfaced five blockers against v4: C6 event channels (`circuit_breaker_trip`, `stuck_detected`, `anomaly_detected`) did not exist in `audit.events` during the reference window (first appearances April 2026), the "epoch-2" corpus filter returned zero rows, BED `|Δη|` is not vector-reconstructable from the audit log, and the Session 2 target `stuck_detected` is produced by a single agent in the eval window. Operator (the operator) selected Option X from `data/v7-fhat/session1a-findings.md` §4. **v5 deltas applied on top of v4**, recorded as a binding amendment in `docs/ontology/v7-fhat-spec-v5-amendment.md`: C6 emission channel dropped (per-class 18 → 9, total 40 → 22); reference epoch filter corrected to epoch-1; B1 comparator struck, B2 sole; targets 2 → 1 (`outcome_is_bad` only, `stuck_detected` diagnostic); decision rule simplified to one cell; ODE parameters bound to `data/v7-fhat/ode_params.json` (SHA-256 `dee1182cd109c4a3d2999f21168a3093b9be8660765cd0d34f8c4337fce9751e`). Transition dynamics, estimator, SC1/SC2 gates, class partition, and horse-race shape unchanged. Session 1b executes against v4+v5-amendment.
- **v4 (2026-04-23):** Subagent council review (three lenses: statistical rigor, pragmatic shipping, paper theory) surfaced structural issues in v3 — class partition empirically degenerate (81% of eval state rows from 5 residents; Codex labels with 2 rows each), 3 of 5 targets with <150 positives or <2 active agents making CV degenerate, EKF linearized at prior mean far from resident operating points, decision rule ambiguous, multiple-comparisons not handled, observer-vs-agent framing lived as caveat instead of premise. v4 re-scopes narrower by design: class partition 5 → 2 (`resident_persistent` vs `session_or_unlabeled`, 40 params instead of 76); targets 5 → 2 (`outcome_is_bad`, `stuck_detected`, both well-supported); primary horizon 1 (k=20 turns, pre-registered); EKF upgraded to iterated/UKF with posterior-mean linearization; ODE-param hash in `params.json` to catch stale priors under operator tuning; SC2 denoising-collapse sanity check before eval; dual comparators B1 (BED if pullable) and B2 (raw-EISV-logistic, always pullable); decision rule rewritten as 2-cell table with CI-strictly-above-zero (no FDR needed); non-regression guardrail tightened to $\geq -0.02$; coherent-subset pre-registration for scope-limited (d); observer-vs-agent framing lifted to new §0 and committed as paper §3 rewrite lead regardless of spike outcome. Time budget corrected to 3 sessions (from 2). Prior redistributed: full-earn 0.25 (from 0.40), scope-limited 0.35 (from 0.30), (b) 0.40 (from 0.30).
- **v3 (2026-04-23):** Schema-verified observation channels against the live governance DB; dropped v2's non-pullable channels (primitive_feedback user corrections, Watcher findings, per-agent calibration state) and replaced with five DB-verified channels (observed EISV × 4, outcome is_bad, three event-stream indicators). Adopted GPT's latent-dim call: latents are now **the EISV coordinates themselves**, not a separate 4-dim decomposition. Transition prior is the **v6 ODE discretized** (load-bearing — makes the v6 dynamics the prior on the generative model). Class-conditioning moved to emissions only (fleet-wide transitions and ODE parameters). Migration posture locked as additive sidecar. Added non-regression guardrail to §6.4 decision rule (lower CI $\geq -0.015$ on losing targets, per GPT's call). Reference corpus and eval-slice windows shifted to avoid v6 §11.6 overlap. Per-class parameter count reduced from 104 (v2) to 76 (v3). Forward-prediction targets updated to DB-verified event types: `outcome_is_bad`, `circuit_breaker_trip`, `stuck_detected`, `anomaly_detected`, `lifecycle_paused`.
- **v2 (2026-04-23):** Expanded §2 to full closed-form parameterization with pre-registered ranges and fit protocol; replaced §6 correlational test with predictive horse race against BED on forward audit-event prediction; added §5.1 clarifying that §3 coordinate-table rewrite is required under both (d) and (b); softened R1 coupling to "viable candidate solution, not THE solution"; walked prior from 0.7+ down to 0.60 per reverse-engineering-vs-forward-modeling distinction.
- **v1 (2026-04-23):** Initial draft, superseded by v2.

---

