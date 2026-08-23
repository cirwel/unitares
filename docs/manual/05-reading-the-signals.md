# 5 · Reading the signals

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)

This chapter is the interpretation guide: what the numbers mean, how they're produced, and how much to trust them. The exact formulas with source references are in [`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md); this is the readable companion.

## 5.1 The pipeline at a glance

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(decisions,     (behavioral_         (behavioral_    (behavioral_          proceed/
 calibration,    sensor.py)           state.py)       assessment.py)        guide/
 drift, tools)                                                               pause/reject
```

The **behavioral EISV** path (EMA observations from grounded signals) is primary and drives verdicts. The **ODE / thermodynamic model** (`governance_core/`) runs in parallel as a research lens and does **not** drive verdicts by default.

The current implementation has three stages: check-ins 1–2 use the Φ
cold-start prior; check-ins 3–24 use the behavioral assessment with fixed
universal thresholds; and check-in 25 enables per-agent **Welford-baseline**
z-scores against a 30-update target. Read the state as telemetry against the
reference available in that stage, not as an outcome judgment. The exact gates
are in [EISV_COMPUTATION.md](../EISV_COMPUTATION.md).

## 5.2 The four dimensions

| Coord | What it reads | Deployed computation (heuristic) |
|---|---|---|
| **E** · Energy | Is work advancing? | weighted blend of recent decision-success, complexity-calibration, outcomes when present, and a legacy `legacy_tanh_v` controller-level term (known compatibility debt) |
| **I** · Integrity | Do claims match results? | calibration accuracy + trend of that same legacy controller scalar (+ outcome consistency); not a pure integrity measurement |
| **S** · Entropy | Drifting from own normal? | drift-norm + regime instability + complexity divergence |
| **V** · Valence | Energy-vs-integrity imbalance | EMA-smoothed `E − I` (derived) |

Three things to internalize:

1. **Each score is self-relative after warmup.** A "low" number is low *for this agent*, not against a global scale. An ultra-stable agent and a noisy one are graded against different baselines.
2. **V is a readout, not a fifth fact.** It's `E − I` smoothed. Its **sign** is the actionable part: `V > 0` = running hot (energetic, claims outrun results); `V < 0` = running careful (coherent but low progress).
3. **The weights are hand-set, not learned.** That's the honest status of the current estimator — see §5.6.

## 5.3 Verdict and margin

The assessment emits an internal `safe` / `caution` / `high-risk` label, which becomes the action the agent reads back:

| Verdict | Internal sense | Agent action | Recovery path |
|---|---|---|---|
| `proceed` | safe | Continue | — |
| `guide` | slightly off | Read guidance, adjust | — |
| `pause` | needs attention | Stop, reflect | self-recovery or dialectic ([§5.5](#55-recovery-self-recovery--dialectic)) |
| `reject` | significant concern | Human input or dialectic | dialectic |

Total risk is the **sum of named components** (`low_E`, `low_I`, high-`S`, `|V|`, …), each with an explicit weight — no sigmoid/phi black box, so you can trace exactly why a verdict fired ([`src/behavioral_assessment.py`](../../src/behavioral_assessment.py)). **Absolute safety floors always apply**, overriding the self-relative baseline; and self-relative deviation risk is *gated by absolute basin health* — inside the healthy basin, deviation from your own norm is treated as information, not danger.

**Margin** (`comfortable` / `tight` / `critical`) rides along with the verdict, telling you how close the agent is to a basin boundary even while the verdict is still `proceed`.

## 5.4 Calibration and its trust boundary

The system tracks whether stated `confidence` matches recorded outcomes — test
pass/fail, command exit codes, lint results, or review labels — fed back via
`record_result` / `outcome_event`. Over time this builds a calibration curve, and
persistent overconfidence can penalize **Integrity** through the check-in
pipeline.

Evidence is not automatically independent merely because it is called an
outcome. CI-, tool-, or operator-authored records provide a stronger anchor than
an outcome authored by the monitored process. If the agent controls both its
confidence and every result record, it can forge a calibrated story. Preserve
provenance and keep high-stakes outcome channels outside the agent's authority.

## 5.5 Recovery: self-recovery → dialectic

When an agent is paused, recovery is a structured escalation:

1. **Self-recovery** — `self_recovery(reflection=..., mode="quick_resume")` when risk is low and no void is active; the agent reflects and resumes. Legacy `C(V)` remains visible as ODE-control diagnostic context, but it does not authorize or deny recovery.
2. **LLM-assisted dialectic** — a configured reviewer model supplies an
   antithesis for single-agent reflection; the default reference path supports a
   local model.
3. **Peer dialectic** — another agent reviews via `dialectic`: thesis → antithesis → synthesis.

Federation comparisons follow the same rule. Peer-similarity scores use E/I/S/V,
controller state, maturity, regime, and verdict where available; legacy
`coherence` is reported with producer/role tags but excluded from ranking. The
old `min_coherence` state-announcement filter is rejected, and the former
coherence-variance `focus_stability` value is explicitly retired rather than
recalibrated around a nearly constant producer.

Full protocol: [`../dev/CIRCUIT_BREAKER_DIALECTIC.md`](../dev/CIRCUIT_BREAKER_DIALECTIC.md).

## 5.6 The knowledge graph as a signal source

The KG isn't just storage — searching it *before* acting is how agents avoid re-discovering known failures, and it's where closed mysteries and corrected conclusions live. Discipline (agent-facing): **search before writing**; prefer a linked correction or `supersede` over a fresh note; store something only when a future agent would search for it and not already find it. Operational runbooks belong in `docs/`, not KG notes. Full operating manual: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).

## 5.7 Don't trust these numbers blindly

The most important interpretation rule. The deployed EISV is **auditable heuristics over observable behavior**, not the information-theoretic quantities (free energy, mutual information, entropy) the paper targets — those become instrumentable only when the inference layer exposes things like token-level logprobs. Every coordinate carries a provenance tier (`e_source`, `s_source`, …) so a heuristic is never laundered as a measurement.

Whether the numbers add useful signal beyond simple baselines is an **open,
measured question.** The
[falsifiability harness](../REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc)
tests ranking (AUC), calibration (Brier), and the selection cost of reporting the
best of several candidates. In the frozen 2026-08-09 trusted-anchor matrix,
every overall scope/window/lead slice is `NOISE-LEVEL` against the
best-of-candidates null (selective p = 0.070–0.567). Unadjusted lift sometimes
appears, usually in `prior_risk`, `prior_s`, or dispersion, but none clears
p < 0.05 after that correction. There is **no demonstrated prevention**. A
`NOISE-LEVEL` slice does not separate "no association" from "too small a cohort
to see one"; the
[power audit](../operations/falsifiability-power-audit-2026-08-23.md) measures
which applies here. Run both yourself before treating EISV as load-bearing.

For optional intuition (not a spec),
[`../essays/tonality-metaphor.md`](../essays/tonality-metaphor.md) uses a bounded
tonal analogy for self-relative measurement and names where the comparison stops.

---

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)
