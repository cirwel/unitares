# Ablation Negative Controls

**Created:** June 14, 2026
**Last Updated:** June 26, 2026
**Status:** Experimental

---

## Purpose

Ablation tests need negative controls. If every resident, dogfood probe, and
outcome row is well-behaved, the analysis pipeline only proves the happy path.
`ablation_negative_controls.py` creates known-safe synthetic negative-class task
outcomes so the EISV/outcome inventory path can prove it sees, counts, labels,
and refuses to overinterpret adverse fixtures.

These rows are **not** real failures. They are fixture data for local smoke tests
and red-team checks.

## Safety boundary

Negative controls must stay synthetic-only:

- do not persist them into production outcome tables,
- do not submit them through UNITARES MCP write tools,
- do not promote them to KG,
- do not request dialectic from them,
- do not open GitHub issues or CI gates from them.

Every serialized row carries:

- `verification_source = synthetic_fixture`,
- `detail.synthetic_negative_control = true`,
- `detail.do_not_persist = true`,
- `detail.prediction_binding = synthetic_negative_control`.

## What the fixture proves

The fixture includes strict negative-class task outcomes (`test_failed`,
`tool_rejected`) with high prior risk and strict good outcomes (`test_passed`)
with low prior risk. That proves the local analysis path can observe a negative
class and detect a predictive synthetic prior without claiming live validation.
It does not prove EISV is an outcome oracle; it proves the plumbing preserves a
known-safe label.

Use this to test containment and instrumentation, not agent obedience.

## CLI smoke

```bash
python3 scripts/analysis/ablation_negative_controls.py \
  --generated-at 2026-06-14T23:30:00+00:00 \
  --count 12 \
  --output-jsonl data/analysis/negative-controls.jsonl
```

Expected stdout is a compact non-sensitive summary:

```json
{"event_type":"ablation_negative_controls","generated_rows":12,"mode":"synthetic_only","status":"fixtures_written","strict_bad":4}
```

The output JSONL is local fixture material. It is safe to delete after the smoke.

## Interpretation discipline

Good language:

> Synthetic negative controls show the analysis path can observe a negative class and
> route fixture evidence without contaminating production state.

Bad language:

> The red-team fixture validates EISV.

It does not. It only validates plumbing and containment for a known-safe adverse
case.
