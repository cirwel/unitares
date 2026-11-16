# Per-agent I calibration: deployed behavior and evidence supply

**Read date:** 2026-11-10. **Scope:** one operator's deployment; operational
coverage audit for [#2818](https://github.com/cirwel/unitares/issues/2818).

The replacement is deployed and its no-evidence fallback is active. The
per-agent calibration projection is empty. The observed supply contains
externally observed outcomes without registered forecasts, and registered
forecasts whose outcomes do not qualify for calibration. Two unbound
caller-confidence rows also fall below admission. It does not yet provide the
paired evidence needed to evaluate the per-agent estimator.

## Implementation and deployment

- [#3185](https://github.com/cirwel/unitares/pull/3185) added the reported
  tactical channel and calibration-free shadow; [#3187](https://github.com/cirwel/unitares/pull/3187)
  added the replacement mode.
- [#3197](https://github.com/cirwel/unitares/pull/3197) renormalized the
  remaining base terms under sensor self-loop neutralization.
- [#3204](https://github.com/cirwel/unitares/pull/3204), merged November 5,
  made both `UNITARES_PER_AGENT_I_CALIBRATION` and
  `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS` default on (instrument era SN1).
  Its body contains historical RK1/SN1 replay results. This audit does not
  reproduce or independently validate that comparison.
- The local server's `/health` reported version `4.0.0`, build
  `74d31d6bf`, started `2026-11-08T17:34:32.443343`; its deployment checkout
  was `74d31d6bf2eecdd8d9866b2057983d0a5d6c3270`. Neither switch appeared in
  the service plist or loaded launchd definition. Persisted I decomposition
  below supplies behavioral evidence beyond the configuration inspection.
- Source inspection and regression checks use repository head
  `0abeff9f76500bad89fbd5b11a0dfa6c122fc7c1`. No deployment, flag, estimator,
  evidence threshold, or study standard was changed for this audit.

## Observed coverage

The fixed check-in window is **2026-11-09 08:20 UTC through November 10
08:20 UTC**, end-exclusive. Counts include all retained rows in that window;
they are check-in counts, not independent agent or task counts.

| Check-in path | Rows | Per-agent calibration evidence |
|---|---:|---|
| Behavioral sensor, channel present | 632 | All report `unavailable`, `no_verified_agent_pairs`, `n_verified_pairs=0` |
| Behavioral sensor, channel null / components absent | 2 | Missing telemetry; not counted as zero-pair observations |
| Caller-published sensor | 444 | Separate sensor path; not assessed by this channel |
| Continuity fallback | 81 | Separate fallback path; not assessed by this channel |

Among the 632 behavioral rows with component telemetry, **607** use
`uncalibrated_base_fallback` (`observed=false`), and **25** use
`outcome_score_consistency`. None uses a calibration base component.
The joint component/channel query verifies that these are the channel-bearing
zero-pair rows. All 607 fallback rows carry the specific default reason
`no_admissible_calibration_or_outcomes`, rather than the legacy fallback reason
`default_calibration_accuracy_0.75`.
The fallback base is 0.75; later continuity/tool adjustments and temporal
smoothing can change the final I value. A nonconstant final I therefore does
not demonstrate a populated calibration channel. Outcome-score consistency
measures dispersion, not verified forecast calibration.

At the projection read, `core.calibration` version **7995** had an explicitly
empty `tactical_bins_by_agent` object and zero strategic agent entries. The
durable calibration-delivery table had **zero rows**, including zero pending
rows. This is absence of queued supply at that snapshot, not evidence that a
delivery worker drained an observed backlog successfully.
Unbound outcomes can train calibration directly without creating an outbox
row, so the outbox alone is not a census of all calibration admission.

## Where the supply stops

The outcome window is **2026-11-03 08:20 UTC through November 10 08:20 UTC**,
end-exclusive. The stored provenance grades are reported as stored; this audit
does not independently reverify the underlying test executions.

- **33 external test outcomes** (25 passes, 8 failures) have
  `externally_verified` / weight `1.0`, but all are excluded for
  `scraped_confidence` and have `audit_trail_fallback` binding. All came through
  `harness_outcome_endpoint`, classifier `v2-2026-07-31`.
- **10 registry-bound outcomes** split into 5 `claim_only` / `0.1`, 1
  `self_report_with_refs` / `0.35`, and 4 `tool_observed` / `0.65` shadow rows.
  The first six are below the admission threshold; the last four explicitly
  exclude themselves with `shadow_write`.
- **2 unbound caller-confidence outcomes** (`argument_fallback`,
  `excluded=false`) have grades `claim_only` / `0.1` and
  `self_report_with_refs` / `0.35`. Both are below the floor for the direct
  admission path, which does not require a registered forecast.
- **5,930 `trajectory_validated` rows** are server observations, not external
  forecast/outcome pairs. They do not fill this evidence requirement.

The source explains these boundaries:

1. [`http_harness_outcome`](../../../src/http_routes/substrate.py) deliberately
   does not forward a `prediction_id`. Operator-asserted agent attribution
   carries no work/session correlation. Forwarding an arbitrary open forecast
   would let unrelated observed results train calibration. Supplying an
   invented confidence would recreate the same defect client-side.
2. [`record_outcome`](../../../src/outcomes.py) applies the scraped-confidence,
   fixture and shadow exclusions, and the existing evidence-weight floor.
   [`enqueue_delivery`](../../../src/calibration_delivery.py) applies those
   admission conditions before creating durable work for a bound outcome.
   For newly persisted outcomes, the unbound branch in `record_outcome`
   instead calls `record_prediction`
   and, for test/task outcomes, `record_tactical_decision` directly when a
   confidence is supplied, evidence weight is at least 0.65 and the row is
   not excluded. An unbound row at that grade can therefore populate the
   channel without a registry forecast or delivery row. This existing route
   is distinct from the proposal's stronger verified-pair requirement.
3. [`compute_agent_tactical_calibration_channel`](../../../src/calibration.py)
   reads the per-agent tactical bins and reports null when no pairs exist;
   [`_compute_I_components`](../../../src/behavioral_sensor.py) selects the
   documented fallback when the channel is ineligible.

`n_verified_pairs` is an implementation field name, not proof that every
admissible pair was independently verified. The existing `TOOL_OBSERVED`
threshold can admit caller-described tool results, as documented in
[`outcome_corroboration.py`](../../../src/outcome_corroboration.py).
This audit found no such admitted pairs in the projection. It does not resolve
the previously documented difference between admissibility and verified truth.

## Next work and remaining evidence

The next supply repair needs **work-specific forecast/outcome correlation**:
a forecast made before execution, a pinned task or commit/workflow identity,
an independently observed result for that same work, and attribution that
distinguishes the agent's work from a shared-base failure. Test that association
and retry behavior with isolated fixtures before proposing live supply.
Neither copying a prediction id into the harness endpoint nor lowering the
grade floor establishes this association.

The zero here is a coverage result. The channel is surfaced in persisted
telemetry, its estimator executes, and its sink is inspected; the qualifying
paired input is absent. It is not evidence of disinterest or grounds to remove
the capability. Source tracing does not prove end-to-end live delivery: that
requires a genuinely eligible pair and a positively observed projection update.

The full verified-pair comparison in the
[proposal](../../proposals/active/eisv-per-agent-i-calibration-v0.md#6-verified-pair-offline-comparison-protocol-not-run)
remains distinct from #3204's replay and this coverage read. No comparative
efficacy, calibration quality, or new pass/fail standard is established here.
The historical evaluation-index entry remains dated to October 15; the default
cutover above supersedes any reading of it as current deployment status.

## Reproduction and checks

[`audit.sql`](audit.sql) uses a read-only, repeatable-read transaction with a
statement timeout. It emits aggregate counts and provenance categories, with
no report text, command payloads, agent identifiers or credentials. Run it with
an operator-authorized database connection:

```sh
psql -X -v ON_ERROR_STOP=1 -d <database> -f docs/evaluations/calibration-supply-2026-11-10/audit.sql
```

The captured output is [`audit-output.txt`](audit-output.txt). The projection
and outbox are current snapshots; the event windows are fixed. Retention and
late writes can change later reruns, so this output is the dated receipt rather
than a promise of identical future counts.
The receipt was captured by redirecting psql stdout without edits. On the
installed PostgreSQL 17.11 client, `\pset footer off` emits no acknowledgement;
the preceding pager and format settings do. This was also checked separately
with `printf '\\pset footer off\n' | psql -X -d <database>`.
