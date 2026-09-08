# Gap-Recovery Arming Semantics — v0

**Status:** Draft / **decision pending — operator call, deliberately not taken here.**
**Scope:** `config.GAP_RECOVERY_ARM_SECONDS`, `config.GAP_RECOVERY_CYCLES`, `config.DT_MAX`, and `_maybe_gap_suppress` in `src/governance_monitor.py`. Server-side only; no schema, no migration.
**Referent for:** the dangling `See task #7 for semantics decision` comment formerly at `src/governance_monitor.py`, which pointed at a tracker item with no trace in the repo or the issue list.

## What is being decided

**How long an absence has to be before the first check-in after it is distrusted.**

That threshold decides how much of the fleet's enforcement is downgraded from `pause` to `proceed`. It is currently 150 seconds — and until the change that ships with this document, nobody had chosen 150 seconds.

## Why the value existed without being chosen

Arming tested `scaled_dt > DT_MAX`.

`DT_MAX = 1.0` is an **Euler stability cap**: the largest single integration step the ODE can take without going numerically unstable. It is a fact about arithmetic. It has no opinion about sleep, laptops, or absence.

But `scaled_dt = elapsed × (DT / DT_EXPECTED_INTERVAL) = elapsed / 150`, so testing `scaled_dt > DT_MAX` is testing `elapsed > 150s`. An integration bound was doing duty as a definition of absence, and the enforcement threshold was the arithmetic residue of two constants picked for an unrelated purpose.

The mechanism was built for MacBook clamshell sleep-wake (`GAP_RECOVERY_CYCLES`, PR #224; the 2026-05-08→12 Lumen/Sentinel/Watcher false high-risk incident; KG discovery `2026-05-15T14:27:26.894282+00:00`). Nothing about that incident implies 150 seconds. A sleep is hours.

## What the data establishes

From the 2026-08-06 falsifiability audit, `docs/ontology/eisv-proprioception-contract.md` rows 24, 35 and 41:

| Measurement | Value |
|---|---|
| Recorded pauses downgraded by `gap_suppress` | **195 of 218 (89.4%)** |
| Median suppressed gap | **300.9s** |
| Firings that were sleep-shaped (>1h) | **4 of 195 (2.1%)** |
| Distribution of the other 191 | 4 under 150s (carryover), 185 in [150s, 334s], 2 in (900s, 1h] |

Row 41's conclusion: *"The deployed mechanism is a cadence gate on enforcement."* A guard built for sleep-wake fires on ordinary 5-minute resident cadence 98% of the time.

Row 35 adds the coupling that makes this sharper than a threshold being merely low: because `effective_dt` is capped at `DT_MAX`, **the integration step is maximal on exactly the check-ins whose pauses get suppressed.** The state jump is largest, and the trust in the resulting verdict is lowest, at the same trigger — *"the amplifier and the disarmer share a trigger."*

`docs/dev/GOVERNANCE_SENSITIVITY.md` already names this pair the highest-leverage single-constant change in the repo.

## What the data does NOT establish

Stated plainly, because the numbers above are easy to over-read:

- **Not that the suppressed pauses were correct.** 89.4% downgraded is a count of verdicts withheld, not of false positives averted or of failures let through. Nothing here grades the suppressed verdicts against outcomes.
- **Not that raising the threshold would improve anything.** The 2026-05 incident is real; the mechanism prevents a class of artifact that was observed live. Raising the threshold restores enforcement in the reopened band *including any false positives that band contains*.
- **Not what the right value is.** The data describes the consequence of the current value. It contains no target. Picking one encodes a judgement about what fraction of ordinary cadence should be treated as absence — which is a posture question, not a derivation.
- **Not a case for removing anything.** Per the measurement-authority rule in `AGENTS.md` / `CLAUDE.md`, none of these counts carries removal authority over any capability. They are telemetry.

## What ships with this document

**The decoupling only.** `GAP_RECOVERY_ARM_SECONDS` is introduced, derived as `DT_MAX * DT_EXPECTED_INTERVAL / DT` = **exactly 150.0s**, so runtime behavior is unchanged — verified by a swept equivalence test across the full gap range including the exact boundary.

What changes is that the threshold is now a named constant meaning one thing, and can be moved without rescaling the ODE's notion of time. Before, deciding "how long is an absence" required editing `DT_EXPECTED_INTERVAL`, which would also have rescaled every integration step for every check-in — the decision was not cleanly available to make.

`dt_saturated` and `gap_recovery_armed` are now computed and logged separately. At the default they coincide; they separate the moment the threshold moves.

## Options

None is recommended here. The consequences are stated so the call can be made on them.

| | Change | Consequence |
|---|---|---|
| **A** | Leave 150s, record it as chosen | Posture unchanged. Converts a de-facto value into a deliberate one, which is most of what was missing. |
| **B** | Raise to a sleep-shaped value (e.g. 1h) | Restores enforcement in [150s, 1h] — the band holding ~98% of current firings. Keeps the guard for the incident it was built for. Largest posture change; the reopened band is un-graded, so expect false positives to reappear along with true ones. |
| **C** | Raise to an intermediate value (e.g. 900s) | The measured distribution has a genuine gap in (334s, 900s] — 0 of 195 firings. A threshold there separates the 185-firing cadence cluster from the 6 longer gaps without asserting that only sleeps count. |
| **D** | Narrow *what* is suppressed rather than *when* | See below. Independent of A–C and composable with any of them. |

### Recommendation (author's, not a decision)

**A for now — hold the posture, revisit once the suppressed band is graded.**

Stated as a choice rather than as a derivation: this is a preference for not moving
live enforcement on data that measures the mechanism's *reach* and not its
*correctness*. The audit establishes that 89.4% of pauses are withheld; it does not
establish that withholding them was wrong. B and C reopen a band nothing has graded,
and would trade a known artifact class for an unmeasured one.

That preference is weak and explicitly not the operator's. The delegation on record
for this decision is real, and A is the option that costs least if it turns out to be
wrong — the decoupling that ships here makes B or C a one-line change whenever the
grading in *What would inform the call* exists, or whenever the operator prefers to
act without it.

## The `risk_pause` sub-question (option D)

Row 24 records an internal inconsistency: the sibling `_maybe_warmup_structural_suppress` **deliberately excludes** `risk_pause` from suppression, commented as *"those reflect real signal"* — while `_maybe_gap_suppress` downgrades `risk_pause` along with everything else.

This reads like a bug and is tempting to fix as one. **It is not a safe fix.** `risk_pause` is the dominant delivered pause type in the audit record, so excluding it from gap suppression would turn enforcement back on across the same 89.4% band — the largest posture change available, arrived at sideways as a consistency cleanup. It belongs to the same operator decision as A–C, not to a tidy-up PR.

Worth noting on either side of the call: the two suppressors have different justifications. Warmup suppression fires only when an *independent* signal (a baselined behavioral state reading `safe`) contradicts the cold structural metric — it has corroboration before it overrides. Gap suppression has no such condition; it downgrades on elapsed time alone. Whether that asymmetry justifies the different treatment of `risk_pause`, or is itself the thing to fix, is the substance of D.

## What would inform the call

Not required before deciding — the operator may set the value arbitrarily — but these would turn a judgement into a graded one:

1. **Grade the suppressed band against outcomes.** `record_result` / `outcome_event` exist; `/v1/enforcement/divergence` (#1528) already separates produced from suppressed from delivered. A shadow pass over the [150s, 334s] cluster would say whether those 185 verdicts were worth withholding.
2. **Separate the two effects at one trigger.** Row 35's coupling means a suppressed pause may be an artifact of the maximal integration step rather than of stale EMA state. Running the first post-gap check-in at a sub-saturating `dt` would tell them apart.
3. **Any change to these constants trips the `governance-sensitive` label** and an advisory comment asking for the expected effect on pause rates (`.github/workflows/governance-sensitivity.yml`). That is the review path this class of change already has.

## References

- `docs/ontology/eisv-proprioception-contract.md` — rows 24, 35, 40, 41 (the audit; rows 24/41 are the measurement, 35 the coupling)
- `docs/dev/GOVERNANCE_SENSITIVITY.md` — the sensitivity inventory entry
- `docs/CHANGELOG.md` — #224 (saturation logging), #1528 (`/v1/enforcement/divergence`)
- `tests/test_gap_suppression.py::TestArmingIsIndependentOfIntegrationDt` — the equivalence and independence pins
