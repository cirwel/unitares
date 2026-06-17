# Tonality as a Lens on UNITARES

**A teaching metaphor — how key signatures and chromaticism map onto EISV, coherence, and drift.**

This document is a conceptual bridge, not a specification. There is **no musical
vocabulary anywhere in the running code** — UNITARES's native language is
thermodynamic and information-theoretic (energy, entropy, coherence, void,
valence, drift). Nothing here changes a formula or a verdict. It exists because
the tonal metaphor turns out to be unusually faithful to one specific, load-bearing
property of the system, and that makes the model easier to teach.

For what the code actually computes, read [`EISV_COMPUTATION.md`](EISV_COMPUTATION.md);
this doc sits beside it as the intuition, not the mechanism.

## Why the analogy is load-bearing, not decorative

UNITARES does **not** judge an action against an absolute standard of
right-or-wrong. It measures **deviation from a self-established baseline** — an
agent's own characteristic behavior, learned over its check-ins. Risk is a
*relationship to a reference frame*, not a property of the action in isolation.

That is exactly what tonality is. A note is not "dissonant" in the absolute; it
is dissonant *relative to an established key*. The same pitch is the consonant
tonic of one key and a jarring accidental in another. Consonance is relational,
and so is risk. Because both systems are relativistic in the same way, the
mapping is structural rather than cosmetic.

This is not a private coincidence. The claim that musical meaning *is* the
maintenance and violation of expectation under a learned model has a lineage:
Meyer ground affect in the inhibition and resolution of expectation [^meyer];
Huron's ITPRA theory made it a prediction-error account of musical feeling
[^huron]; and the predictive-processing reading of music states the FEP case
directly — a listener is a generative model minimizing surprise, a key is its
prior, chromaticism is precision-weighted prediction error [^koelsch]. The tonal
lens on UNITARES is the same idea pointed at agent behavior instead of pitch.

## The core mapping

| Music | UNITARES | Why it lines up |
|---|---|---|
| **Key signature** — the tonic + the diatonic set, the home reference | **The agent's behavioral baseline** — its characteristic EISV regime | UNITARES measures drift relative to your *own* baseline. The baseline *is* the key. |
| **Establishing the key** — you can't hear "out of key" until a tonic is set | **`settling` / warmup** — `< 3` check-ins, so headroom can't be judged yet | No tonal center → no consonance judgment. No baseline → no margin judgment. |
| **Diatonic / in-key playing** | **`comfortable` margin, low `S`, coherent** | Operating inside your established set is consonant. |
| **Chromaticism** — notes outside the diatonic set | **Semantic uncertainty `S` / drift** | Behavior outside the established set. A little is color/exploration; a lot is instability. |
| **Tension → resolution** — the leading tone pulling home | **`tight` margin + coherence feedback `C(V)`** | Coherence is literally a restoring force toward center — tonal gravity, written as an equation. |
| **Dissonance demanding resolution** | **`critical` margin / risk past threshold** | Past a boundary; resolve or it breaks. |
| **Modulation** — sustained chromaticism that establishes a *new* key | **Regime shift** — the baseline itself moving (`regime_s`) | The moment the old key stops explaining the music but the new one isn't confirmed = tonal ambiguity = regime instability. Re-triggers `settling`. |
| **Atonality / loss of key** | **High `S` + incoherence (`V → −∞`, `C → 0`) → pause → dialectic** | No tonal center at all. The dialectic protocol is re-establishing a key collaboratively. |

## The distinction that earns its own axis

The question pairs *key signatures* with *chromaticism* — and UNITARES happens to
split those onto **two different axes**, which is the most useful part of the lens:

- **Chromaticism is a question of *which note you chose*.** That maps to `S`
  (semantic uncertainty / drift): an *intended* deviation from your characteristic
  set. Playing an F♯ in C major is a decision. Healthy exploration (Energy `E`) is
  chromaticism that resolves; runaway drift is chromaticism that won't.
- **Calibration is a question of *whether you're in tune on whatever note you chose*.**
  That maps to `I` (integrity): `cal_I = clamp(1 − calibration_error)`, claimed
  confidence versus verified outcome. An agent reporting `confidence=0.9` while
  succeeding 50% of the time is **playing in-key but sharp** — right notes,
  mistuned. Integrity drops independent of how chromatic it is being.

So **chromaticism is an `S` question (color); intonation is an `I` question
(accuracy)**. A great improviser is wildly chromatic *and* dead in tune. A poor
one can be rigidly diatonic *and* flat. UNITARES separates exactly these — which
is why an agent can have high exploration and still be trusted, as long as it is
calibrated.

## V as the tension reservoir

The fourth coordinate, `V`, is not an independent dimension — it is the
EMA-smoothed `E − I` imbalance (`src/behavioral_state.py:174-176`), the
"Void" integral. In tonal terms it is the **accumulated harmonic tension**: an
energy surplus without matching integrity (`V` positive and rising) is unresolved
dominant tension building up. The coherence function

```
C(V, Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))
```

(`governance_core/coherence.py`) is the **pull back toward the tonic** — the
feedback that, left to run, resolves the tension. When `V → −∞`, `C → 0`
(incoherent, integrity starved of energy); when `V → +∞`, `C → Cmax`.

The affect, though, is not in the level of `V` but in its *trajectory*. A
suspended chord is not tense because of where it sits; it is tense because of
where it is heading. Three independent vocabularies converge on this: Huron's
account locates musical feeling in the unfolding of expectation rather than any
static chord [^huron]; active-inference models of emotion put valence at the
**time-derivative of free energy** — falling free energy feels good, rising feels
bad [^joffily]; and UNITARES's own contract insists a single check-in is
uninformative (`margin: settling`) because the governed quantity is longitudinal.
Read `dV/dt`, not `V` — the same reason you cannot hear a cadence in one note.

## Where the metaphor stops

A metaphor that explains everything explains nothing, so the boundaries matter:

- **It does not drive verdicts.** Verdicts come from the behavioral assessment
  path (EMA + z-score deviations + weighted thresholds), not from any harmonic
  intuition. The thermodynamic ODE itself runs *in parallel and does not drive
  verdicts by default* (`governance_monitor.py:1013-1017`); the tonal lens sits
  one level further out than even that.
- **EISV is heuristic blends, not measured entropy.** Per
  [`EISV_COMPUTATION.md`](EISV_COMPUTATION.md), `S` is not a literal
  response-distribution entropy on the primary path — so "chromatic density"
  should be read as "drift-blend magnitude," not as a Shannon quantity.
- **The 12-tone grid is not real here.** Music quantizes deviation into discrete
  semitones; UNITARES's `S` and margins are continuous. The grid is a
  storytelling convenience, not a claim about the state space.

Treat this as a way to *feel* the system's relativism — risk as distance from a
self-set key, exploration as chromatic color, calibration as intonation — and
then go read the formulas.

## See also

- [`EISV_COMPUTATION.md`](EISV_COMPUTATION.md) — the actual formulas behind every
  term used above.
- [`trust-contract.md`](trust-contract.md) — what the system guarantees and what
  honest failure looks like.
- [`ontology/identity.md`](ontology/identity.md) — what "an agent" (the thing
  whose key we are tracking) is.

## References

[^meyer]: Meyer, L. B. (1956). *Emotion and Meaning in Music.* University of
    Chicago Press. Affect arises from the inhibition and resolution of
    expectation — a pre-formal expectation/prediction-error account.

[^huron]: Huron, D. (2006). *Sweet Anticipation: Music and the Psychology of
    Expectation.* MIT Press. The ITPRA theory — musical feeling as the trajectory
    of expectation, not any static event.

[^koelsch]: Koelsch, S., Vuust, P., & Friston, K. (2019). "Predictive Processes
    and the Peculiar Case of Music." *Trends in Cognitive Sciences*, 23(1),
    63–77. Music perception as free-energy minimization; key as prior,
    expectation violation as precision-weighted prediction error.

[^joffily]: Joffily, M., & Coricelli, G. (2013). "Emotional Valence and the
    Free-Energy Principle." *PLoS Computational Biology*, 9(6), e1003094. Valence
    as the time-derivative of (variational) free energy.
