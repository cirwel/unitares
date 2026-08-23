# Model/harness provenance and risk cohorts

Status: prospective measurement contract. This surface is descriptive only.
It does not identify an agent, select a verdict, dispatch policy, or establish
that a model caused a risk difference.

## Capture contract

Measured state rows may carry
`state_json.provenance_context.runtime_provenance` with schema
`s22.runtime_provenance.v1`. The envelope keeps three different things apart:

- `model`: identifier, provider, source, whether the reporter presented the
  identifier as exact, the reporting channel, and explicit missing reasons;
- `harness`: type and version, each with its own source and missing reason;
- `adapter`: the integration and version that delivered the observation.

Every envelope also says that it is descriptive context and is not identity
proof, verdict authority, or a policy dispatch key. These flags document the
boundary; no policy code reads the envelope.

Transport adapters can report values with the following headers:

| Header | Meaning |
|---|---|
| `X-Unitares-Model` | Exact identifier exposed by the provider or harness |
| `X-Unitares-Model-Provider` | Provider reported separately from the identifier |
| `X-Unitares-Model-Source` | `provider_reported` or `harness_reported` |
| `X-Unitares-Harness-Type` | Harness family, such as `codex-cli` or `claude-code` |
| `X-Unitares-Harness-Version` | Harness version, when exposed |
| `X-Unitares-Adapter-Type` | Integration that delivered the observation |
| `X-Unitares-Adapter-Version` | Integration version |

The same shape can ride the public `provenance_context.runtime_provenance`
slot for hook/adapter check-ins. Provider- and harness-reported values remain
reported evidence, not cryptographic verification. Legacy flat `model` or
`model_type` values are retained for compatibility but are classified
`caller_declared`, `exact=false`, and cannot enter an exact-model cohort.
User-Agent fallback records only a coarse family such as `gpt-family`; it never
manufactures an exact identifier. Display names are never an input.

Identifiers are bounded and must use a compact identifier character set. URLs,
control characters, credential-shaped values, and oversized values are rejected
instead of truncated. The persisted envelope records why the value is missing
without retaining the rejected text.

## Prospective boundary

There is no safe backfill for historical nulls. A missing versioned envelope
means `legacy_unversioned`, even when the identity label contains a model-like
string. An unknown future schema is also excluded rather than reinterpreted.

Choose the production deployment time of the capture code as the cohort start.
Record that timestamp with every report. Never choose an earlier boundary to
increase sample size.

## Cohort report

Run the read-only report with an explicit timezone-aware capture boundary:

```bash
python3 scripts/analysis/model_risk_cohort.py \
  --capture-start 2026-08-23T00:00:00Z \
  --capture-end 2026-08-30T00:00:00Z \
  --output data/analysis/model-risk-2026-08-30.md
```

The report shows attribution attrition before any risk statistic. Eligible rows
must have a supported versioned envelope, an exact provider/harness-reported
model, and a harness type observed from the harness or its User-Agent. When a
harness version is present, it must have the same observed provenance; a
caller-declared version is excluded. A genuinely missing harness version
remains an explicit `unavailable` stratum and is counted in coverage; it is
never inferred. The report then stratifies risk by:

- exact model, provider, harness type, and harness version;
- explicit behavioral readiness plus update-count bucket;
- task type;
- elapsed exposure window from the process identity's first measured state.

The like-for-like section considers only warm rows and requires at least two
model/harness populations in the same update-count, task, and exposure cell,
each meeting `--min-cell-size`. Reaching that bar means only
`ready_for_descriptive_comparison`. The report always sets
`policy_change_allowed=false`: a policy proposal needs a separate,
preregistered causal design and external review.
