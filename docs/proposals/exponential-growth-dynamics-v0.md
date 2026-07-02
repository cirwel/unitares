# Exponential / growth dynamics for UNITARES — scoping + council review (v0)

**Status:** Site B (cohort priors) landed in shadow, read-only form — the pure primitive
(PR #1334, merged), the per-class aggregation source, and a non-mutating shadow-observe hook
on the cold-start resume path (this PR). Live-apply is the next promotion, gated on
shadow-observe validation data. Sites A and C are **reviewed and rejected** as framed.

## Question

UNITARES is contractive by construction — every place "exponential" appears in the
codebase is damping, not growth (EMA smoothing, `e^{-αt}` contraction bounds in
`governance_core/dynamics.py:485`, the leaky `V` accumulator in `docs/ontology/v7-fhat-spec.md:188`,
retry backoff). The question: can a genuine *compounding* ("exponential") dynamic be
added **without** weakening the governor?

## Framing that survived review

There are two regimes, and only one may carry growth:

- **The governor** — verdict cut, CIRS oscillation governor (`src/cirs.py`), drift→entropy
  coupling. Must stay contractive. Non-negotiable.
- **The substrate it governs** — knowledge graph, lineage, per-agent baselines. Compounding
  is legitimate here and does not touch the verdict path.

## Three candidate sites and the council verdict

A five-lens council (control-theory, safety/governance, codebase-fit, research-theory,
red-team) reviewed the plan against the actual code. Summary:

### Site A — growth term in the ODE E-derivative — REJECTED as framed

Proposed: add `+ρ·E·(1−E)` to `dE/dt`, gated OFF by default, "safe because the ODE is
diagnostic-only." **The council falsified the safety premise.** "Diagnostic-only" is a
runtime *flag posture*, not a structural partition. The ODE `E` leaks into enforcement on
the default config:

- `velocity_risk` is computed from `state.E_history` (the ODE-evolved E) and added to
  `risk_score` even on the behavioral-primary branch (`src/monitor_risk.py`, history
  appended `governance_monitor.py:709`).
- At cold start (behavioral confidence < 0.3, `governance_monitor.py:1306`) the verdict
  falls back to `phi_objective(state.unitaires_state)` which rewards E, and
  `runtime_queries.py:93` sets `primary_source=ode_fallback` — the ODE *is* the reported
  state for low-history agents.
- `coherence(self.state.V)` feeds CIRS.
- `sensor_coupling_enabled()` defaults ON (`governance_core/parameters.py:184`), so the ODE
  is spring-pulled toward the measured curve — which contaminates Site A's own falsification
  signal.

Additionally: the v7-F̂ spike **already ran the rigorous version of this experiment** (an
independent ODE-prior predictor compared against observations) and SC2 tripped at Pearson
r = 0.9949 (denoising-collapse) — the model added no predictive information
(`docs/ontology/v7-fhat-spec.md §9`, which also **retired FEP grounding for E and V**).
Logistic `ρ·E(1−E)` also vanishes as `E→1`, exactly where healthy agents live, so
"divergence stayed low" is guaranteed and proves nothing.

*Salvage bar (not pursued):* only viable rebuilt around a **shadow ODE E** used solely for
`eisv_divergence`, never written to `self.state.E`.

### Site C — v7 class-scale-constants as cohort meta-learning — REJECTED for now

Builds on the FEP machinery `§9` already retired for E and V. Derivation-heavy, least
grounded. Revisit only if the F̂ grounding is re-established.

### Site B — cohort priors for per-agent baselines — ACCEPTED (shadow first)

The one growth-shaped move that is also coherent: warm-start a fresh agent's Welford/EMA
baseline from a **cohort prior** so it starts near-calibrated instead of cold
(`docs/UNIFIED_ARCHITECTURE.md §2`, "~30 check-ins from scratch"). A sharper prior *reduces*
surprise → contractive-compatible → never touches the governor. Note it is a **one-time
cold-start offset, not an exponential** — "slow exponential" was oversold; we keep the honest
label.

**Conditions the council attached, and how the primitive enforces them:**

| Council condition | Enforcement in `src/cohort_prior.py` |
|---|---|
| Seed prior mean/std only; never flip the agent to "baselined" | `WelfordStats.z_score` is inert while `count < 5`. `seed_welford` requires `2 ≤ pseudo_count < 5` and raises otherwise, so a seeded agent **cannot z-score until it logs its own real observations**. Proven by `test_seed_stays_inert_until_agent_earns_it`. |
| Only *widen* the seeded std | `widen ≥ 1.0` enforced; pooled variance combines within-agent + between-agent spread, so the prior is never narrower than a contributor. |
| Small pseudo-count | Default 2 (smallest count with defined variance). |
| One cohort can't *be* the prior | `min_contributors = 2`; under-characterized agents (count < 5) excluded. |
| Shadow / seeded-not-earned | Module is read-only, persists nothing, is wired into no handler. `test_seed_never_persists` guards it. |

**Why this protects the governor:** the seed biases the *mean estimate*, never the
*decision to start deciding*. A bad or cohort-correlated cohort therefore cannot silence a
newcomer's early drift signal — the newcomer stays inert to z-scoring until its own
observations cross the gate.

## What landed here

**Primitive (PR #1334, merged):**
- `src/cohort_prior.py` — pure, read-only `CohortPrior` aggregator + guardrailed
  `seed_welford` / `seed_baseline`. `cohort_prior_enabled()` flag defaults OFF.
- `tests/test_cohort_prior.py` — 15 tests, including the anti-poisoning invariant and a
  guard that keeps `Z_SCORE_ACTIVATION_COUNT` in sync with the real Welford gate.

**Aggregation source + shadow-observe (this PR):**
- `src/cohort_prior_source.py` — bulk-loads stored baselines and groups them by
  calibration class (same `classify_agent` logic the rest of the system uses) into one
  `CohortPrior` per class. Named residents (N=1 classes) correctly get no prior. Adds a
  coarse in-process TTL cache (`get_cached_cohort_priors`) so the cold-start path never
  bulk-loads on every resume, and `observe_seed_gap` describing the seed a class would
  receive.
- `src/db/mixins/baseline.py` — `load_all_behavioral_baselines()`, a read-only bulk SELECT.
- `src/agent_behavioral_baseline.py` — `_maybe_observe_cohort_seed()` on the cold-start
  branch of `ensure_baseline_loaded`. **Shadow-only:** no-op unless the flag is enabled;
  when enabled it *logs* the would-be seed and **never mutates** the fresh baseline
  (`test_flag_on_observes_without_mutating`).
- `tests/test_cohort_prior_source.py` — grouping, per-class build, N=1 exclusion, TTL cache,
  and the flag-off-noop / flag-on-non-mutating shadow-hook invariants.

This is the "build it as a shadow, read-only query first and validate calibration lift
before applying" step. Live-apply is the next promotion, gated on that validation data.

## Deferred (next promotion)

**Live-apply.** Actually seeding the fresh baseline from the class cohort prior on cold
start (still behind `cohort_prior_enabled()`, still a **coupled identity/onboarding
single-writer surface** per `CLAUDE.md`). Promote only once the shadow-observe logs show a
real cold-start calibration lift, and re-check for in-flight work on the baseline/identity
surface before starting. This will also want a background refresh for the prior cache
rather than the lazy TTL rebuild used for shadow.
