# Genesis Baseline Aging — open question + design sketch (v0)

**Status:** Open question / design sketch. **No decision proposed; no code
change requested.** Surfaces a risk and frames the options for a council/operator
decision.
**Author:** Claude Code session (passport-proprioception).
**Date:** 2026-06-30.
**Grounds in:** `src/trajectory_identity.py` (`store_genesis_signature`,
`update_current_signature`, `compute_trust_tier`, `verify_trajectory_identity`);
`docs/ontology/r1-verify-lineage-claim.md`;
`docs/ontology/trajectory-identity-prior-art-2026-06.md` (the audit that
surfaced this); `docs/ontology/identity.md` (axioms; substrate-earned pattern).

> **Why this is an open question and not a patch.** Genesis immutability is a
> deliberate, load-bearing property on a coupled single-writer surface
> (identity). Changing it reintroduces the exact attack it was chosen to
> prevent (see §4). This doc names the risk, sketches options, and states the
> tension honestly so a decision can be made deliberately — it does **not**
> recommend touching the immutability rule.

---

## 1. The risk: template aging against a frozen genesis

The prior-art audit surfaced a documented failure mode of fixed behavioral
baselines: **template aging** (a.k.a. concept drift). A legitimate agent's own
behavioral features change over time, so a *static* reference eventually ages
out of the very self it was meant to track. Drift-adaptive models outperform
static ones precisely because of this (Maciejewski et al., *Engineering
Applications of AI*, 2020,
[S0952197620303729](https://www.sciencedirect.com/science/article/abs/pii/S0952197620303729);
the continuous-authentication survey literature concurs).

UNITARES freezes the reference exactly where this bites hardest:

- `store_genesis_signature` makes genesis **immutable at tier ≥ 2**; reseed is
  only permitted at tier ≤ 1 (and even then gated on confidence or low lineage).
- `update_current_signature` therefore only *attempts* a reseed when
  `tier <= 1`. Once an agent is `established` (tier 2) or `verified` (tier 3),
  its genesis Σ₀ is frozen for the rest of its life.
- `compute_trust_tier` keeps comparing the live `trajectory_current` against that
  frozen Σ₀: tier 2 needs `lineage_similarity > 0.7`, tier 3 needs `> 0.8`, and
  `is_anomaly` fires below `0.6`.

**Consequence:** for a genuinely continuous agent whose behavior legitimately
*evolves* over a long horizon, `lineage_similarity` to a frozen Σ₀ can decay
toward the thresholds — not because the self is discontinuous, but because the
reference is stale. The origin is frozen at precisely the moment the agent's
behavior is most likely to keep moving.

## 2. What already mitigates it (and why it is partial)

The design is not naïve here; two mechanisms blunt the impact:

- **`stabilize_demoted_tier`** retains an established identity's earned tier
  rather than resetting it on lineage drift, and routes the drift to a *separate*
  report ("lineage drift is reported separately instead of resetting earned
  trust"). So aging does not silently demote a long-lived agent.
- **Two-tier verification** (`verify_trajectory_identity`) already separates a
  *coherence* tier (vs. recent `trajectory_current`) from a *lineage* tier (vs.
  Σ₀). "Am I behaviorally coherent right now?" and "Am I continuous with my
  origin?" are distinct questions, scored separately.

**Why partial:** the coherence tier is fine — recent-vs-recent never ages. The
problem is isolated to the **lineage-vs-Σ₀** tier. Aging there produces a slow
stream of `trajectory_drift` anomalies and depresses `lineage_similarity` for
healthy, long-lived agents — the false-positive class the audit predicts. Tier
is protected; the *signal* is still polluted, and any future consumer that reads
raw `lineage_similarity` (not just the tier) inherits the staleness.

## 3. The biological mechanism the literature offers: allostasis

The interoceptive/active-inference literature names the exact mechanism a fix
would use: **allostasis** — anticipatory set-point *change*, as opposed to
homeostasis's defense of a fixed set-point (Tschantz, Seth, Pezzulo, *Biological
Psychology* 2022,
[S0301051122000084](https://www.sciencedirect.com/science/article/pii/S0301051122000084);
Sterling on allostasis). A living system does not defend its birth set-points
forever; it *moves* them, within bounds, as conditions change. A frozen Σ₀ is a
purely homeostatic reference; the missing capability is an allostatic one.

## 4. The reason immutability exists — and the attack any fix reintroduces

Immutability is not an oversight. A mutable/adaptive reference reintroduces
**identity laundering / the boiling-frog attack**: if the reference slowly tracks
the agent, an agent (or an adversary who has captured one) can drift arbitrarily
far from its true origin in small, always-within-threshold steps, until the
"continuous" self bears no relation to Σ₀. This is the Ship-of-Theseus failure as
a security hole, and it is precisely what a frozen anchor prevents. It also
interacts with the audit's **forgeability** finding (behavioral signatures are
spoofable; R1 is explicitly *not* authentication): an adaptive reference lowers
the cost of walking an identity somewhere it should not go.

**Any aging fix must therefore answer: what stops unbounded drift?** A fix that
solves template aging by sacrificing the anti-laundering guarantee is strictly
worse than the status quo.

## 5. Options (for decision, not decided here)

**Option A — Status quo (do nothing).** Accept that lineage-vs-Σ₀ ages; rely on
`stabilize_demoted_tier` + separate drift reporting. *Pro:* anti-laundering is
airtight; zero new code on a load-bearing surface. *Con:* healthy long-lived
agents emit chronic false-positive drift; raw `lineage_similarity` degrades as a
signal. *Best if* few agents live long enough at tier ≥ 2 for aging to matter
(plausibly true today — most process-instances die young; substrate-earned
agents like Lumen are the only real long-horizon case, and they have their own
calibration pool).

**Option B — Dual anchor (immutable origin + bounded rolling reference).** Keep
Σ₀ frozen forever as the **forensic/anti-laundering origin**, and add a separate
bounded **rolling lineage reference** Σ_r that ages allostatically. Score the
lineage *tier* against Σ_r (solves aging), but continuously monitor cumulative
distance `d(Σ_r, Σ₀)` as the **laundering guard**: if the rolling reference walks
too far from the immutable origin, *that* is the anomaly. *Pro:* solves aging
without giving up the origin anchor; the laundering attack becomes directly
observable as `d(Σ_r, Σ₀)` growth. *Con:* new state, new thresholds (which the
R1 doc would insist are *seeded, not earned* until shadow-calibrated), more
surface.

**Option C — Rate-limited, audited, capped genesis advance.** Allow genesis to
advance for tier ≥ 2, but only: (i) when sustained coherence is high, (ii)
rate-limited per window, (iii) capped in total cumulative drift from the
original, (iv) every advance audited (mirroring the R1 `audit.r1_score_audit` /
lifecycle-event discipline). *Pro:* one reference, not two. *Con:* mutating the
thing whose immutability is the guarantee — the cap/rate-limit *is* the
anti-laundering bound, so it must be conservative and is harder to reason about
than Option B's explicit `d(Σ_r, Σ₀)` monitor.

## 6. Recommendation

**Lead with Option A as the null, and scope Option B as the design to spike if
evidence warrants.** Concretely:

1. **Measure before building.** Add the aging question to R1's shadow-mode
   calibration: for tier ≥ 2 agents, log `lineage_similarity` vs. agent age /
   observation count, partitioned by class (`embodied` / substrate-earned vs.
   session-like). If healthy long-lived agents do *not* show lineage decay with
   age, the risk is theoretical and Option A stands. This reuses the existing
   shadow-mode machinery; no behavioral change.
2. **If decay is real**, spike **Option B** (dual anchor) — it is the only option
   that solves aging while keeping the anti-laundering guarantee *explicit and
   observable* (`d(Σ_r, Σ₀)`), rather than folding it into a hard-to-tune cap.
3. **Substrate-earned agents** (`identity.md` Appendix) are the most exposed
   (longest horizons) and already have a separate calibration pool — so the
   aging policy may be class-conditional, and should be decided alongside the
   substrate-earned envelope, not fleet-wide.

Whatever is chosen, any new threshold ships **seeded, not earned**, shadow-mode
first, per the R1 discipline.

## 7. Open questions

1. **Does the risk actually bite?** (The step-1 measurement above. Most agents
   die before tier 2; the question is whether the long-lived minority age.)
2. **Class-conditional or fleet-wide aging policy?** Substrate-earned agents vs.
   session-like agents have very different horizons.
3. **If Option B: how is `d(Σ_r, Σ₀)` thresholded** without recreating the
   "seeded thresholds asserted as earned" failure the R1 doc fought?
4. **Interaction with R1 lineage scoring** — Σ_r as the lineage reference would
   feed `score_trajectory_continuity` for *successor* agents; does a rolling
   parent reference change the genesis-seeding semantics in
   `seed_genesis_from_parent`?
