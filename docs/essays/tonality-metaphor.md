# Tonality as an Optional Lens on UNITARES

**Status:** Teaching essay. Non-normative. This is not a specification, an
evaluation result, or evidence that the model is correct.

UNITARES contains no musical vocabulary in its running code. The deployed
decision path uses behavioral signals, EISV blends, warmup thresholds,
and self-relative risk scoring. ODE state also supplies structural policy
checks and feeds later behavioral observations. Read
[`EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) for the actual formulas and the
deployed-versus-research boundary.

The tonal analogy is useful for one limited idea: after warmup, some behavioral
measurements are interpreted relative to an agent's own history rather than only
against a fleet-wide reference. In music, an event can also be heard relative to
an established tonal context. The resemblance can help teach self-relative
measurement, but it is not an equivalence between music theory and governance.

## A bounded mapping

| Musical idea | UNITARES analogy | Important limit |
|---|---|---|
| **Establishing a key** | Building an agent-specific behavioral baseline | With the current constants, self-relative scoring becomes available at 25 check-ins (`baseline_confidence >= 0.8`) against a 30-update warmup target. Absolute safety floors still apply. |
| **Playing within a tonal context** | Remaining near the agent's established behavioral range | Near-baseline behavior is not automatically safe or correct; the reference frame describes what is typical for that agent. |
| **Chromatic movement** | A change in the deployed `S` drift blend | `S` is not literal musical distance or Shannon entropy. A deviation may be exploration, noise, or instability; policy must interpret it with other evidence. |
| **Intonation** | Calibration: comparing confidence with recorded evidence | I is a blend, not a claim-accuracy score. Its deployed calibration input is a lifetime fleet strategic aggregate; external outcomes have their own producer and scope. |
| **Modulation** | A possible regime shift in the agent's behavior | Regime telemetry describes change. It does not prove that a new baseline is legitimate or beneficial. |
| **Accumulated tension** | `V`, a separate EMA of the raw `E_obs − I_obs` gap | V has its own smoothing rate and generally differs from current E minus I. Behavioral V contributes to assessment; ODE V is a separate state used by structural checks. Neither is musical tension. |

The most useful distinction is between drift and calibration. A musician may
choose notes outside an established key while remaining precisely in tune.
Likewise, an agent can explore outside its recent behavioral range while still
making well-calibrated claims. Conversely, an agent can behave routinely while
being systematically overconfident. UNITARES represents drift and calibration
with different inputs. The analogy distinguishes those questions; whether a
particular reading diagnoses either condition depends on its inputs and scope.

## Warmup is where the analogy is weakest

You cannot infer an agent-specific reference from a handful of observations.
UNITARES therefore uses fixed thresholds and a mostly server-derived cold-start
prior before its behavioral history is ready. In the current implementation,
[`BASELINE_WARMUP_UPDATES`](../../src/behavioral_state.py) is 30 and
`is_baselined` becomes true when confidence reaches 0.8, which resolves to 25
updates. The exact constants are implementation details and should be read from
the code, not from the metaphor.

The system also retains absolute safety floors after warmup. This matters because
a purely self-relative system could normalize an agent's persistently bad
behavior. A musical key does not have an analogous accountability boundary, so
the comparison stops there.

## Where the metaphor stops

- **The metaphor does not drive verdicts.** Behavioral assessment and ODE
  structural checks feed policy; enforcement applies the resulting action.
- **It is not predictive evidence.** A change in `S` or `V` does not establish
  that an incident will follow or that an intervention prevented one.
- **It is not an adversarial-robustness claim.** A motivated actor may shape the
  monitored signals; the threat model documents the known limits.
- **It is not a one-to-one music theory claim.** Tonal perception depends on
  style, context, expectation, and listener. Terms such as consonance and
  chromaticism are teaching conveniences here.
- **EISV is not literal thermodynamics or information theory on the deployed
  path.** The names preserve the research lineage; the computation document
  identifies what each live value actually contains.

Use this essay to build intuition for reference-dependent measurement, then use
the computation, threat-model, and evaluation documents to judge the system.

## See also

- [`EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) — deployed formulas and target
  semantics.
- [`SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md) — signal anchors
  and known adversarial limits.
- [`trust-contract.md`](../trust-contract.md) — guarantees, non-guarantees, and
  failure posture.

## References

- Meyer, L. B. (1956). *Emotion and Meaning in Music.* University of Chicago
  Press.
- Huron, D. (2006). *Sweet Anticipation: Music and the Psychology of
  Expectation.* MIT Press.
- Koelsch, S., Vuust, P., & Friston, K. (2019). "Predictive Processes and the
  Peculiar Case of Music." *Trends in Cognitive Sciences*, 23(1), 63–77.
