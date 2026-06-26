# Continuous verdict blending — so numerical/config drift can't flip a verdict

**Status:** v0 proposal, for the Φ→telemetry (EISV) workstream. Not a committed change.
**Author:** surfaced from a quickstart-canary investigation, 2026-06-26.
**Why now:** to make dependency updates *free* — i.e. so a fresh agent's verdict
cannot flip on a numerical or config perturbation. Today it can.

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
