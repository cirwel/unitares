# Relational calibration maturity-capacity read v0

Status: frozen feasibility preregistration; documentation only

Date: 2026-08-10

Contract: `relational-calibration-maturity-capacity-v0`

## Question and boundary

This one-time read asks only:

> At a fixed future cutoff, does the federation contain enough recently active,
> behaviorally mature, provenance-compatible identities to make the private
> relational-calibration cohort mathematically plausible?

Capacity is necessary, not sufficient. This read does not measure relational
forecast skill, empathy, qualia, sentience, consent, caller-proven identity,
role availability, independence, or implementation readiness. A mature EISV
profile is a versioned measurement condition, not a judgment of a participant.

Merging this document must not add or enable a collector, endpoint, schema,
table, queue, feature flag, dashboard, scheduled job, writer, participant list,
or enrollment path. The query below reads existing state rows and returns only
aggregate funnel counts. It is not authorization to run the pilot.

Even `capacity_ready` leaves the privacy architecture, adversarial test, mutual
consent, operator authorization, and separate implementation PR required by
[`relational-calibration-pilot-v0.md`](relational-calibration-pilot-v0.md)
blocked.

## Prior reconnaissance, not a result

A read performed before this contract was frozen, at
`2026-08-10T20:52:13.942985Z`, found:

- 77 identities had ever reached a persisted behavioral `is_baselined = true`
  state;
- 362 identities had a latest non-synthetic state in the trailing 30 days;
- 31 of those latest states were baselined and had all four Welford counts at
  least 25;
- fewer than 10 also carried a complete `eisv.telemetry.v1` envelope with the
  frozen behavioral provenance tuple; and
- fewer than 10 additionally had no missing derivation inputs.

These observations informed this design, so they cannot be presented as a
confirmatory result. They are honest reconnaissance and already imply that a
cohort requiring 100 distinct observers and 100 distinct subjects could not be
formed from the current mature supply, even if the two role sets overlapped
perfectly. The future read tests whether that premise changes under one frozen
window and predicate; it does not erase knowledge of the earlier counts.

## Frozen boundary

| Field | Frozen value |
|---|---|
| Cutoff (`as_of`) | `2026-09-10T00:00:00Z` |
| Lookback | exactly 30 days |
| Included time interval | `2026-08-11T00:00:00Z <= recorded_at <= 2026-09-10T00:00:00Z` |
| Unit | one latest non-synthetic state row per `identity_id` as of the cutoff |
| Tie-break | greatest `recorded_at`, then greatest `state_id` |
| Envelope schema | `eisv.telemetry.v1` |
| Primary source | `behavioral` |
| Behavioral observation source | `behavioral` |
| Derivation | `behavioral_sensor` / `behavioral_sensor.v1` with no missing inputs |
| Behavioral baseline | `is_baselined = true`, confidence at least `0.8`, target `30`, every Welford count at least `25` |
| V formula | version `2` in both the persisted behavioral state and telemetry envelope |

These values mirror the deployed contracts in
[`src/behavioral_state.py`](../../src/behavioral_state.py) and
[`src/eisv_telemetry.py`](../../src/eisv_telemetry.py): baseline confidence is
zero below five updates, reaches the `is_baselined` threshold of `0.8` at about
update 25, and reaches full confidence at the 30-update target. The envelope
health field paths follow
[`src/eisv_telemetry_health.py`](../../src/eisv_telemetry_health.py).

The read may run after the cutoff, but it must remain anchored to this cutoff
and must ignore later rows. It must execute once against one transactionally
consistent database snapshot. This document must merge before the cutoff; if it
does not, the contract is `contract_unreadable` and a new future cutoff is
required. There is no automated retry or recurring job.

If a listed field is absent, has the wrong JSON type, or carries another
version, that row fails closed at the corresponding funnel stage. If deployed
semantics change before the cutoff without versioned provenance that this
contract can distinguish, the result is `contract_unreadable`; no analyst may
repair the predicate after inspecting counts.

## Frozen funnel

The output stages are monotonic:

1. `recent_any`: latest non-synthetic state falls in the fixed 30-day window.
2. `recent_baselined`: adds the persisted server `is_baselined` flag.
3. `mature_profile`: adds baseline confidence, target, update, and all four
   Welford-count checks from the same state row.
4. `schema_ready`: adds the versioned telemetry envelope.
5. `measurement_complete`: adds a measurement ID, timestamp, primary source,
   and numeric `E`, `I`, `S`, and `V`.
6. `provenance_compatible`: adds the frozen primary/observation/derivation/V
   source-version tuple and envelope-side maturity agreement.
7. `strict_compatible`: adds an empty derivation `missing_inputs` array. This is
   the capacity decision count.

Only `strict_compatible` feeds the gate. Earlier stages diagnose attrition; they
cannot be substituted for the final count.

`synthetic IS NOT TRUE` is the only durable fixture exclusion available on the
historical state row. Current agent handles, labels, and lifecycle metadata are
mutable and are not frozen evidence of whether an old row was a fixture, so the
query does not use them. The result is therefore an upper bound: any unmarked
fixture, unavailable participant, failed strong-identity proof, refusal, block,
or role conflict can only reduce the enrollable population.

## Frozen SQL

The merge commit pins the exact query text and is the contract digest. The
operator records that commit SHA alongside the result. The query must not be
edited in place; any correction requires a new document version and a new
future cutoff.

```sql
WITH params AS (
    SELECT
        TIMESTAMPTZ '2026-09-10T00:00:00Z' AS as_of,
        INTERVAL '30 days' AS lookback
),
latest AS MATERIALIZED (
    SELECT DISTINCT ON (s.identity_id)
        s.identity_id,
        s.state_id,
        s.recorded_at,
        s.state_json
    FROM core.agent_state s
    CROSS JOIN params p
    WHERE s.synthetic IS NOT TRUE
      AND s.recorded_at <= p.as_of
    ORDER BY s.identity_id, s.recorded_at DESC, s.state_id DESC
),
extracted AS MATERIALIZED (
    SELECT
        l.*,
        p.as_of,
        p.lookback,
        l.state_json->'behavioral_eisv' AS behavioral,
        l.state_json->'eisv_telemetry' AS envelope
    FROM latest l
    CROSS JOIN params p
),
flags AS MATERIALIZED (
    SELECT
        e.*,
        e.recorded_at >= e.as_of - e.lookback AS recent,
        CASE
            WHEN jsonb_typeof(e.behavioral #> '{warmup,is_baselined}') = 'boolean'
            THEN (e.behavioral #>> '{warmup,is_baselined}')::boolean
            ELSE false
        END AS baselined,
        CASE
            WHEN jsonb_typeof(e.behavioral #> '{warmup,baseline_confidence}') = 'number'
            THEN (e.behavioral #>> '{warmup,baseline_confidence}')::double precision >= 0.8
            ELSE false
        END AS baseline_confident,
        e.behavioral #>> '{warmup,baseline_target}' = '30' AS baseline_target_v0,
        CASE
            WHEN jsonb_typeof(e.behavioral->'updates') = 'number'
            THEN (e.behavioral->>'updates')::integer >= 25
            ELSE false
        END AS updates_ready,
        CASE
            WHEN jsonb_typeof(e.behavioral #> '{baseline_stats,E,count}') = 'number'
             AND jsonb_typeof(e.behavioral #> '{baseline_stats,I,count}') = 'number'
             AND jsonb_typeof(e.behavioral #> '{baseline_stats,S,count}') = 'number'
             AND jsonb_typeof(e.behavioral #> '{baseline_stats,V,count}') = 'number'
            THEN (e.behavioral #>> '{baseline_stats,E,count}')::integer >= 25
             AND (e.behavioral #>> '{baseline_stats,I,count}')::integer >= 25
             AND (e.behavioral #>> '{baseline_stats,S,count}')::integer >= 25
             AND (e.behavioral #>> '{baseline_stats,V,count}')::integer >= 25
            ELSE false
        END AS welford_ready,
        jsonb_typeof(e.envelope) = 'object'
            AND e.envelope->>'schema' = 'eisv.telemetry.v1' AS schema_v1,
        coalesce(
            nullif(e.envelope->>'measurement_id', '') IS NOT NULL
            AND nullif(e.envelope->>'observed_at', '') IS NOT NULL
            AND nullif(e.envelope #>> '{measurement,primary,source}', '') IS NOT NULL
            AND jsonb_typeof(e.envelope #> '{measurement,primary,values,E}') = 'number'
            AND jsonb_typeof(e.envelope #> '{measurement,primary,values,I}') = 'number'
            AND jsonb_typeof(e.envelope #> '{measurement,primary,values,S}') = 'number'
            AND jsonb_typeof(e.envelope #> '{measurement,primary,values,V}') = 'number',
            false
        ) AS measurement_fields_complete,
        coalesce(
            e.envelope #>> '{measurement,primary,source}' = 'behavioral'
            AND e.envelope #>> '{measurement,behavioral,observation_source}' = 'behavioral'
            AND e.envelope #>> '{measurement,behavioral,v_formula_version}' = '2'
            AND e.envelope #>> '{measurement,behavioral,warmup,is_baselined}' = 'true'
            AND e.envelope #>> '{measurement,behavioral,warmup,baseline_target}' = '30'
            AND e.behavioral->>'v_formula_version' = '2'
            AND e.envelope #>> '{derivation,kind}' = 'behavioral_sensor'
            AND e.envelope #>> '{derivation,formula_version}' = 'behavioral_sensor.v1',
            false
        ) AS source_version_ready,
        coalesce(
            jsonb_typeof(e.envelope #> '{derivation,missing_inputs}') = 'array'
            AND jsonb_array_length(e.envelope #> '{derivation,missing_inputs}') = 0,
            false
        ) AS derivation_complete
    FROM extracted e
),
funnel AS MATERIALIZED (
    SELECT
        f.*,
        f.recent AND f.baselined AS f_recent_baselined,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v0 AND f.updates_ready
            AND f.welford_ready AS f_mature_profile,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v0 AND f.updates_ready
            AND f.welford_ready AND f.schema_v1 AS f_schema_ready,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v0 AND f.updates_ready
            AND f.welford_ready AND f.schema_v1
            AND f.measurement_fields_complete AS f_measurement_complete,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v0 AND f.updates_ready
            AND f.welford_ready AND f.schema_v1
            AND f.measurement_fields_complete
            AND f.source_version_ready AS f_provenance_compatible,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v0 AND f.updates_ready
            AND f.welford_ready AND f.schema_v1
            AND f.measurement_fields_complete
            AND f.source_version_ready
            AND f.derivation_complete AS f_strict_compatible
    FROM flags f
)
SELECT
    p.as_of,
    count(*) FILTER (WHERE f.recent) AS recent_any,
    count(*) FILTER (WHERE f.f_recent_baselined) AS recent_baselined,
    count(*) FILTER (WHERE f.f_mature_profile) AS mature_profile,
    count(*) FILTER (WHERE f.f_schema_ready) AS schema_ready,
    count(*) FILTER (WHERE f.f_measurement_complete) AS measurement_complete,
    count(*) FILTER (WHERE f.f_provenance_compatible) AS provenance_compatible,
    count(*) FILTER (WHERE f.f_strict_compatible) AS strict_compatible
FROM params p
LEFT JOIN funnel f ON true
GROUP BY p.as_of;
```

## Result packet and privacy

The reviewer records only:

- contract name and merge commit SHA;
- query execution timestamp and frozen `as_of`;
- the seven monotonic aggregate stages;
- the capacity status below; and
- any `contract_unreadable` reason.

No identity, handle, label, timestamp distribution, source subgroup, row,
vector, baseline profile, or participant list is released. Public reporting
renders any positive count below 10 as `<10`; the independent reviewer may use
the transient exact aggregate only to apply the frozen gate. There is no second
query with a shifted cutoff or filter, preventing a differencing series.

## Decision rule

Apply the rule to `strict_compatible` only:

| Count | Status | Meaning |
|---:|---|---|
| `< 100` | `not_feasible` | The mathematical minimum of 100 distinct participants in each role cannot be met; no collector or enrollment work proceeds. |
| `100..199` | `fragile` | The minimum may be possible only with maximal role overlap and little attrition headroom; no collector or enrollment work proceeds. |
| `>= 200` | `capacity_ready` | There is nominal headroom for up to 50% loss under full role overlap, or for disjoint 100-person role sets before other losses; this still grants no implementation authority. |

The 200-identity readiness threshold is deliberately above the protocol's
corrected 100-observer/100-subject minima. With only 100 compatible identities,
every identity would need to serve in both roles at the two-contribution limit,
with zero loss to identity, consent, role, source, or privacy gates. Capacity is
not robust at that arithmetic boundary.

## Stop and reopen rule

After the one read:

- `not_feasible` or `fragile` closes this capacity attempt. Do not build a
  collector, loosen maturity, mix source versions, shorten the activity window,
  or repeat the query to seek a passing snapshot.
- `capacity_ready` permits only the already-required independent privacy and
  adversarial architecture work to be considered. It does not permit live
  collection, recruitment, or enrollment.
- `contract_unreadable` closes the read without a capacity conclusion.

A later capacity read requires a new version, a new future cutoff, and a stated
new premise such as a shipped provenance rollout or materially larger mature
population. The earlier result remains visible; it is never overwritten.
