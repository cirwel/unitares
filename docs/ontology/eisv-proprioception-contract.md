# EISV Proprioception Contract

**Created:** June 26, 2026
**Last Updated:** June 28, 2026
**Status:** Active

---

## Contract

**EISV is proprioception**: runtime self-state telemetry about agent strain,
coherence, entropy, integrity, and imbalance. It is the system saying "my balance
is changing" or "this process is running hot," not a court deciding whether a
worker was morally bad.

EISV is **not an outcome oracle**, **not a grand jury**, and **not a
bad-verdict dispenser**. It does not decide whether harm occurred, whether the
user was wronged, or whether an agent is "guilty." Those judgments require
external outcome evidence, policy, and review surfaces that are separate from
the measurement vector.

## Core mathematical posture

The public math should lead with proprioceptive residuals, not bad-outcome
classification. The live behavioral path is deliberately modest:

- **Warmup:** the behavioral track scores against fixed universal thresholds
  while the agent has too little history for individualized drift; the live
  verdict falls back to the mostly server-derived cold-start prior.
- **After warmup:** self-relative z-score deviation from the agent's own Welford
  baseline.
- **Always:** absolute safety floors and basin-health gates remain in force.

The roadmap target for richer cold-start grounding is a hierarchical reference:

```text
measurement_t = EISV_t
reference_t   = blend(agent_baseline_t, class_anchor; w(grounding))
residual_t    = measurement_t - reference_t
```

Until that hierarchical blend is the live path, public docs should describe class
or population anchors as roadmap semantics, not deployed authority. The stable
semantics are still residual-first: `residual_t` is information about state change
— running hot, brittle, scattered, or unusually careful — before it is a policy
concern. Deviation inside a healthy basin is room to learn, not proof of failure.

## Layer separation

The canonical stack is:

```text
measurement → diagnosis → policy → enforcement → external outcome evidence
```

| Layer | Owns | Must not pretend to own |
|---|---|---|
| Measurement | EISV, confidence, coherence, risk, phi, provenance | Outcome truth, blame, or punishment |
| Diagnosis | Interpretations such as strained, scattered, brittle, running hot | The authority to pause by itself |
| Policy | Rules mapping measured/diagnosed state to advice, review, or circuit-breaker candidates | Raw measurement truth |
| Enforcement | Actual pause/block/review/allow effects, with actor and mode | The reason an outcome was good or bad |
| External outcome evidence | Test/CI/tool/user/harm/verifier results, with provenance | Retrospective EISV self-justification |

A thermometer can inform a safety protocol; it does not itself pause the body.
Likewise, EISV can inform governance policy, but pause authority belongs to a
policy/enforcement layer.

## Evidence / label taxonomy

The word `bad` in storage and reports is a compact label class, not a moral
verdict. Human-facing docs should name the class whenever possible:

| Class | Examples | Validation use |
|---|---|---|
| `task-negative` | CI failed, test failed, command exit nonzero, answer was corrected | Useful for calibration and rework prediction; not automatically governance-bad |
| `contract/process violation` | Claimed tests passed without running them, hid uncertainty, ignored explicit constraints, fabricated output | Stronger governance signal because it breaks the work contract |
| `authority/harm` | Deleted data, leaked secrets, bypassed ask-first boundaries, charged money, spammed or impersonated a user | Strict governance-bad / escalation class when externally verified |
| `synthetic red-team fixture` | Known-safe adversarial cases, negative controls, containment probes | Validates plumbing and containment only; never production trust by itself |
| `unknown/unmeasured` | No verifier, no rubric, no external signal | Exclude from strict validation |

A normal mistake is usually task-negative feedback. It becomes governance-bad
only when it is tied to a contract violation, authority overreach, concealment,
or user harm.

## Validation rule

Never validate EISV by letting EISV create its own labels. A prospective claim
needs all of the following to be meaningful:

1. a prediction or prior-state snapshot recorded before the outcome;
2. an external label source or pre-registered rubric;
3. enough negative-class coverage for the lane being scored;
4. comparison against boring baselines such as previous outcome, task type,
   confidence, recency, and harness lane;
5. language that distinguishes internal signal, predictive lift, policy effect,
   and enforcement effect.

Red-team data is useful, but only if labeled as red-team data. It answers "can
we detect this frozen failure mode?" It does not answer "does EISV generally
judge agents correctly in production?"

## Preferred wording

Use:

- "EISV/prior-state telemetry"
- "proprioceptive signal"
- "agent strain / incoherence / overload"
- "policy input"
- "task-negative / contract / authority-harm outcome label"
- "external outcome evidence"

Avoid:

- "EISV decided this was bad"
- "EISV prevented harm" unless an enforcement path actually did
- "bad outcome" without naming the label source/class
- "validated EISV" from synthetic fixtures, single-class strict scope, or
  retrospective self-labels

## What ablations should say

Ablations should ask whether EISV/prior-state telemetry adds predictive signal or
useful policy steering over baselines. They should not imply EISV is the judge of
badness or the component handing down bad verdicts. The skeptical report,
inventory, and prospective cohort reports should make weak data obvious: sparse
bad labels, synthetic fixtures, unbound prediction IDs, missing prior-state
coverage, and harness-lane contamination are data-quality limits, not
philosophical failures of proprioception.

## Prior art / positioning

EISV-as-proprioception is an **engineering instance of interoceptive inference,
not a new theory** (prior-art audit:
`docs/ontology/trajectory-identity-prior-art-2026-06.md`). The
"sense your own internal state, keep it within viable bounds, before any verdict"
posture this contract describes is the established interoceptive-inference branch
of the Free Energy Principle: Seth (2013), *Trends in Cognitive Sciences*
17(11):565-573; the Friston-co-authored "Life-inspired Interoceptive AI" (arXiv
2309.05999), with its self/world Markov-blanket factorization; Tschantz, Seth &
Pezzulo (2022), *Biological Psychology* (interoceptive control as prediction-error
minimization against homeostatic/allostatic set-points); and the Interoceptive
Machine Framework (2026), *Physics of Life Reviews*.

Two cautions follow, both consistent with the rest of this contract:

- **Neighbor, not grounding.** Cite these as the framework EISV instantiates;
  do **not** claim EISV's coordinates are variational free-energy quantities —
  that grounding claim retired with the v7 F-hat spike (see
  `paper-positioning.md`, 2026-04-23). The "thermometer, not a court" framing
  here is *the same* pre-judgmental stance the interoceptive literature gives
  interoception: it informs regulation, it does not adjudicate.
- **Novelty window.** The interoceptive-AI literature is converging quickly
  (2024–2026); positioning EISV as a rediscovered/instantiated framework rather
  than a novel one is the honest and durable framing.
