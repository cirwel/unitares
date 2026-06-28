# Continuous verdict blending — so numerical/config drift can't flip a verdict

**Status:** v0.2 — **council pass (2026-06-26) corrected the locus; do NOT implement
the v0 blend as written.** For the Φ→telemetry (EISV) owners. Not a committed change.
**Author:** surfaced from a quickstart-canary investigation, 2026-06-26.
**Why now:** to make dependency updates *free* — i.e. so a verdict cannot flip on a
numerical perturbation.

**Related motivation (separate axis, same locus):** the φ→behavioral weighting this
proposal governs is also the lever that decides how much an *independent* signal counts
against a self-attested verdict. The
[self-report-dependence worked example](../operations/self-report-verdict-dependence-2026-06-28.md)
shows that pre-warmup the verdict is a pure function of caller-reported `ethical_drift`
(a confessed-sabotage and a clean-refactor check-in score identically), and even
post-warmup the behavioral signal is escalate-only. That is *not* the numerical-drift
problem this doc primarily targets, but the v2 verification-weighted behavioral scoring
it defers to is what would close it — keep the two motivations distinct when scoping.

## v0.2 — council fold (READ FIRST; supersedes the v0 locus + corrects a safety flaw)

A three-lane council (architect + live-verifier) reviewed the v0 blend before any
code. Verdict: **right primitive, wrong-and-incomplete locus, and the v0 blend
formula is unsafe.** Three findings, the first two live-verified against the running
governance server:

1. **The demo's 0.15↔0.76 flip is a CONFIG-FLAG difference, not numerical drift.**
   `resolve_verdict_risk` is gated (caller `governance_monitor.py:1269`) by the
   `phi_telemetry_only()` env flag (confirmed `UNITARES_PHI_TELEMETRY_ONLY=1` on the
   live process) and by `behavioral_state.confidence >= 0.3`, where
   `confidence = update_count/10` is **integer-derived** (`behavioral_state.py:283-285`).
   Neither a config flag nor an integer counter is tipped by float/dependency drift.
   So the v0 blend smooths a *flag/maturity* transition — not the drift one.

2. **Where numerical drift ACTUALLY flips a verdict: the φ=0.0 kink**
   (`monitor_risk.py:70-79`). At φ=0.0, `verdict_from_phi` (`governance_core/scoring.py:69`)
   flips caution→high-risk — which pauses unconditionally at `monitor_decision.py:156`
   **with no risk threshold** — *and* `phi_risk` hits exactly 0.70, which equals both the
   CIRS `beta_high` hard-block (`cirs.py:313/353`) and `BASIN_LOW_RISK_FLOOR`
   (`config/governance_config.py:80`, → basin pause `monitor_decision.py:189`). A ΔS≈0.03
   in the ODE → Δφ≈0.015 crosses all three at once. The v0 blend reaches none of them.

3. **The v0 blend formula is unsafe (safety-ontology change).** The Φ floor is a
   *one-sided* operator — `max(phi_risk, behavioral_risk)` / `_more_severe_verdict`
   (`:65-67`) — so behavioral can only ever *add* severity, never erase a worse φ
   (comment `:1264-1267`). The v0 convex blend `(1−a)·max(φ,beh) + a·beh` is
   **symmetric**: at high `authority` a confident behavioral EMA slides resolved risk
   *below* the φ floor — the exact erasure the floor forbids. Any blend MUST stay
   one-sided: `resolved = max(phi_floor_when_φ_high_risk, blend(...))`.

**Corrected fix (what to actually build):**
- **Primary — hysteresis / dead-band at the decision gate** (`monitor_decision.py`,
  at the 0.70 basin floor and the `high-risk` verdict gate): a verdict only flips when
  risk clears the threshold *by a margin*, and falls back only below `threshold−margin`.
  This makes the verdict drift-robust *where it is actually decided*. Reuse the existing
  `BOUNDARY` basin + `margin: tight` + `compute_proprioceptive_margin` machinery
  (`monitor_decision.py:208`, `config/governance_config.py:420`).
- **Secondary — the blend, made one-sided** (see #3) to remove the boolean cliff and
  shrink jump magnitude so a smaller dead-band suffices. Add an *interior* test (rising
  φ-risk + confident-low behavioral → must still floor), not just endpoint tests.
- **Separate track — config-flag drift** (`phi_telemetry_only` differing per
  environment) is NOT a numerical-margin problem; neither blend nor hysteresis fixes
  it. It belongs to the reproducibility/pinning bridge.

**Honest goal restatement:** "no drift can flip a verdict" is unachievable — a pause is
a binary decision over a continuous state (IVT). The achievable goal is **"no drift
within a defined margin flips a verdict, and near-boundary verdicts are marked
low-confidence."** The v0 body below is preserved as the *secondary* (blend) half; the
*primary* (gate hysteresis) is the load-bearing fix and was missing from v0.

## The problem: a hard binary switch in the verdict math

`resolve_verdict_risk` (`src/governance_monitor.py:42-68`) combines the Φ-derived
and behavioral verdict/risk with a **boolean** switch, no blend:

```python
if phi_telemetry and behavioral_verdict is not None:
    return behavioral_verdict, float(behavioral_risk)        # de-escalated (~0.15 in the case below)
return (_more_severe_verdict(phi_verdict, behavioral_verdict),
        max(float(phi_risk), float(behavioral_risk)))         # Φ floors (~0.76)
```

When the switch flips, the returned `risk_score` jumps the **full distance**
between the behavioral estimate and the Φ floor — an ~0.6 jump for the case
below — even though every underlying signal (φ, behavioral risk, EISV) moved
only infinitesimally. A jump that large straddles the pause gate, so a
**verdict flips** (`proceed`↔`pause`).

Two upstream thresholds compound it (same anti-pattern — hard step where a ramp
belongs):
- `governance_monitor.py:1269` — the switch only engages at
  `behavioral_state.confidence >= 0.3` (`confidence = update_count/10`). A step,
  not a ramp. The code's own comment (`:1265-1267`) already notes "a maturing
  behavioral EMA can invert risk during monotonically worsening … check-ins" —
  i.e. this surface is known to be unstable.
- `monitor_risk.py:70-79` — the φ→risk piecewise map is continuous but kinked
  (slope breaks at φ=0.30 and φ=0.0; risk swings 0.30→0.70 across the narrow
  caution band). Secondary, but the same shape.

## Evidence: same demo, two CI builds, verdict flipped

The quickstart demo runs a fixed 7-step trajectory. Two builds, step 6:

| build | E | I | S | V | risk (gated) | latest (raw φ) | verdict |
|-------|---|---|---|---|---|---|---|
| A | 0.85 | 0.71 | 0.47 | 0.14 | **0.15** | 0.76 | proceed |
| B | 0.84 | 0.71 | 0.50 | 0.13 | **0.76** | 0.76 | **pause** |

The raw φ path gives ~0.76 in **both** (continuous; no cliff). The behavioral
path gives ~0.1 in **both** (continuous). The *only* thing that differs is
**which branch of the switch ran** — A took the behavioral (de-escalated)
branch, B took the Φ-floor `max()` branch. That single boolean is the whole
0.15↔0.76 swing.

**Honest scope note:** the exact reason A and B took different branches is NOT
pinned in this writeup — candidates are the `UNITARES_PHI_TELEMETRY_ONLY` flag
(`config/governance_config.py:1034-1044`, wired at `governance_monitor.py:1274`)
resolving differently across the two CI environments, the `confidence>=0.3` step
being crossed a cycle apart, and/or small ODE numerical drift across builds (the
EISV does differ slightly: S 0.47 vs 0.50). It does not matter for the fix: any
of these tips a *binary* switch, and the fix is to make the switch continuous so
none of them can cause a 0.6 jump.

## The fix: confidence-weighted blend instead of a switch

Replace the boolean with a ramp, so the Φ floor fades out smoothly as behavioral
authority rises rather than being discarded all at once:

```python
# authority in [0,1]: 0 at cold start / telemetry-off, 1 when mature / telemetry-on
w        = clamp((behavioral_confidence - 0.3) / (1.0 - 0.3), 0.0, 1.0)
telem    = telemetry_strength()          # ramp the flag, not a bool (0..1)
authority = w * telem
risk     = (1 - authority) * max(phi_risk, behavioral_risk) + authority * behavioral_risk
```

- Cold start / telemetry off → `authority≈0` → Φ floor dominates (the safe prior).
- Mature / telemetry on → `authority→1` → behavioral becomes authoritative.
- A 1σ EISV/φ wobble now moves risk by hundredths, never 0.6 → **no verdict flip
  on numerical or config drift → dependencies become freely updatable.**

Apply the same ramp discipline to the `confidence>=0.3` step (`:1269`) and,
optionally, smooth the caution-band slope in `monitor_risk.py:70-79`.

## Safety constraint (load-bearing — needs the owners + a council pass)

The Φ floor is a deliberate **safety prior**: it exists so a confident-but-wrong
behavioral assessment cannot erase a worse self-attested Φ/drift signal. Blending
it down must NOT blunt genuine high-risk detection. Required before merge:
- Council pass (the EISV/Φ→telemetry owners) on the blend's safety envelope.
- Tests: confirm the blend (a) preserves every existing pause on genuinely
  dangerous states (absolute floors still hard — those stay binary by design),
  and (b) removes the verdict flip on the demo trajectory across a φ/EISV
  perturbation.
- This belongs in the **Φ→telemetry workstream** (it *is* a refinement of
  `resolve_verdict_risk` / #1063), not a standalone change.

## What it buys

The reproducibility bridge (pin deps + base-image digest) makes verdicts
*deterministic per build* — necessary, but it only makes drift *deliberate*. This
blend makes verdicts *robust to drift*, which is what actually lets dependencies
be kept current without each bump being a governance-review event.
