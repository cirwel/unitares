# EISV telemetry envelope v1

**Status:** additive observability contract

**Policy effect:** none
**Storage:** `core.agent_state.state_json.eisv_telemetry`

The envelope makes the EISV chain inspectable without turning measurement into
judgment:

```text
measurement → derivation → policy evaluation → enforcement result
```

It is written once per check-in inside the existing append-only JSONB state
row. No migration is required. Rows that predate the envelope remain readable
and are labeled `eisv.telemetry.summary.legacy`; missing provenance is reported,
not inferred.

## Full persisted shape

```json
{
  "schema": "eisv.telemetry.v1",
  "measurement_id": "uuid",
  "observed_at": "ISO-8601",
  "measurement": {
    "primary": {"source": "behavioral", "values": {"E": 0, "I": 0, "S": 0, "V": 0}},
    "behavioral": {
      "observation_source": "behavioral_sensor | physical | continuity_fallback | …",
      "raw_observation": {"E": 0, "I": 0, "S": 0},
      "smoothed": {"E": 0, "I": 0, "S": 0, "V": 0},
      "confidence": 0,
      "updates": 0,
      "warmup": {},
      "alphas": {},
      "v_formula_version": 2
    },
    "ode": {"source": "ode_diagnostic", "values": {"E": 0, "I": 0, "S": 0, "V": 0}},
    "submitted_sensor": {"source": "physical | behavioral", "values": {}},
    "submitted_afferents": {
      "schema": "eisv.submitted_afferents.v1",
      "source": "physical",
      "source_field": "afferents | body_anima | anima",
      "measurement_role": "raw_afferents",
      "policy_applied": false,
      "values": {"presence": 0},
      "provenance": {
        "status": "caller_declared | undeclared",
        "source": "caller-declared bounded text",
        "role": "caller-declared bounded text"
      }
    }
  },
  "derivation": {
    "kind": "behavioral_sensor | caller_published_sensor | …",
    "formula": "src.behavioral_sensor.compute_behavioral_sensor_eisv",
    "formula_version": "behavioral_sensor.v1",
    "inputs": {},
    "missing_inputs": [],
    "computed_observation": {}
  },
  "policy_evaluation": {},
  "enforcement": {}
}
```

The behavioral derivation retains only the last ten history values because the
live formula reads only that suffix. Outcome history is capped at twenty and
reduced to the fields used by the formula: outcome type, bad/good flag, numeric
score, and verification source. Free-form outcome detail, response text,
credentials, prompts, and tool payloads are excluded.

### Submitted afferents

`measurement.submitted_afferents` preserves the finite numeric dimensions a
physical caller published before projecting them into EISV. This keeps a
separately published body channel such as presence longitudinally observable without
mislabeling it as Valence or giving it decision authority.

New callers should publish either a flat `sensor_data.afferents` mapping or:

```json
{
  "afferents": {
    "values": {"presence": 0.52},
    "provenance": {
      "schema": "sensor.afferents.v1",
      "source": "substrate_probe",
      "role": "physical_self_sense"
    }
  }
}
```

The existing `body_anima` field and its legacy `anima` alias are compatibility
inputs. The recorder keeps at most 16 dimensions, requires finite numeric
values with safe identifier names, caps names and provenance text, rejects
booleans/strings/nested payloads and secret-shaped names, and retains only
`schema`, `source`, `role`, `scale`, and `units` provenance keys. Values remain
in the caller's native scale; UNITARES neither clamps nor interprets them.
Caller-declared provenance is descriptive, not verification.

This field is deliberately measurement-only: it is assembled before the locked
update, copied into the append-only envelope after policy resolution, and never
passed to the behavioral estimator, ODE, basin classifier, or actuator. Compact
summaries expose only source, field, dimension count/names, and the explicit
`policy_applied` flag—not the values.

`measurement.primary.source` answers which state vector is presented as the
primary reading. `measurement.behavioral.observation_source` answers which
instrument the behavioral estimator actually consumed. They are deliberately
different questions: a physical observation may be smoothed into the primary
behavioral state. The compact projection's `measurement_source` names that
underlying instrument only when behavioral state is primary; during warm-up it
remains `ode_fallback` so the displayed ODE values are not mislabeled.

## Read surfaces

- Full `process_agent_update(response_mode="full")` responses include the
  persisted envelope.
- `GET /v1/agents/{agent_id}/history` returns a compact per-point `telemetry`
  projection. Add `include_telemetry=true` to include each full envelope.
- `/v1/eisv/recent` and WebSocket `eisv_update` events carry only the compact
  summary so the broadcaster ring buffer stays bounded.
- `GET /v1/eisv/telemetry-health?days=30` returns a cached, read-only fleet
  aggregate over measured state rows. The dashboard's **Telemetry** tab uses
  this surface for rollout coverage, primary/consumed source rates, warmup,
  missing inputs, same-row contract checks, strict outcome-linked calibration,
  and policy-requested versus enforcement-applied counts. Windows are clamped
  to 1–90 days and cached for 30 seconds so a monitor view does not turn into a
  database load generator.
- `export_monitor_history(..., "json")` now exposes separate ODE and behavioral
  in-memory streams. It explicitly states that per-row source, policy, and
  enforcement history belongs to the append-only envelopes.
- `eisv_skeptic_report.py` reports envelope coverage and marginal
  source/warmup/enforcement/missingness strata. `eisv_ablation_matrix.py`
  exposes the same strata with `--telemetry-strata` and reproducible cohort
  cutoffs with `--as-of`; enforcement remains an audit stratum, never a
  predictor or causal-prevention claim.

## Telemetry-health semantics

The health report calls a row *covered* only when it carries the recognized
`eisv.telemetry.v1` schema. Pre-envelope rows stay in the state-row denominator
but never receive an inferred envelope, source, warmup phase, policy decision,
or actuator result. This makes rollout progress and the evidence gap visible at
the same time.

Contract violations are limited to invariants inside one persisted observation:
recognized envelopes must carry their measurement, derivation, policy, and
enforcement fields; the policy input must match the stored risk/verdict/source;
applied enforcement must have been requested; request state must match the
policy action; and the behavioral-confidence gate must agree with the primary
source. Malformed and unsupported envelope values are included in the contract
denominator but excluded from rollout coverage. Historical rows are not
re-judged using current risk thresholds.

The dashboard separately publishes the three current numeric vocabularies
(`behavioral_verdict`, `experience_summary`, and `health_status`). Their bands
overlap by design. Policy can also be overridden by legacy coherence-compatibility
or void gates, while health is risk/void-derived; those policy overrides are not
additional health evidence. Different words for the same risk value are visible
but are **not** automatically counted as contradictions.

Outcome bins use only strict external anchors, exclude structurally marked
controlled fixtures, require a prior state at least five minutes before the
outcome, and require that prior state to carry the envelope. Rates are clustered
by prior `measurement_id` (falling back to agent + state timestamp), with sparse
and single-class bins labeled. This is descriptive calibration plumbing, not the
baseline-relative lift test performed by the skeptic/ablation reports.

## What this does not claim

The envelope exposes operational measurements and their provenance. It does
not expose hidden activations, establish machine qualia, verify a
caller-published physical sensor, or validate the behavioral formula. Those are
separate empirical questions. It also does not alter thresholds, verdicts,
gap-suppression, or circuit-breaker behavior. Recording an afferent does not
establish that it is independent, useful, or suitable for promotion into EISV;
those require a separately preregistered incremental-information test.
