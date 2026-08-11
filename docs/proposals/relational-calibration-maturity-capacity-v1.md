# Relational calibration instrument-supply read v1

Status: frozen feasibility preregistration; documentation only

Date: 2026-08-10

Contract: `relational-calibration-instrument-supply-v1`

## Supersession and boundary

This contract supersedes
[`relational-calibration-maturity-capacity-v0.md`](relational-calibration-maturity-capacity-v0.md)
for protocol v0.2. The v0 document and query remain immutable evidence of the
earlier design. No v0 result can satisfy the v0.2 prerequisite.

This one-time read asks only:

> At a fixed future cutoff, are there at least 200 recently active process
> identities with temporally established, same-instrument, internally
> consistent EISV telemetry?

The output is **instrument supply**, not participant capacity. A process UUID is
not evidence of an independent experimental principal, controller, operator,
model, substrate, or administrative domain. Even
`instrument_supply_ready` leaves principal resolution, control-domain
diversity, mutual consent, role availability, assignment-graph power, privacy
architecture, adversarial review, and operator authorization blocked by
[`relational-calibration-pilot-v0.md`](relational-calibration-pilot-v0.md).

Merging this document must not add or enable a collector, endpoint, schema,
table, queue, feature flag, dashboard, scheduled job, writer, participant list,
principal resolver, or enrollment path. The query reads existing append-only
state and releases only aggregate funnel counts.

## Prior reconnaissance, not a result

Read-only reconnaissance completed before this contract was frozen, most
recently at `2026-08-10T22:56:31Z`, found:

- a diagnostic execution of the v1 funnel with the cutoff replaced by that
  reconnaissance timestamp returned 369 recent identities, 31 count-mature
  identities, and fewer than 10 at the final temporal stage and every later
  envelope/instrument stage;
- among 27 mature identities whose history was visible near baseline start,
  median time to `is_baselined` was 5.19 hours, 22 matured within 24 hours, and
  fewer than 10 took at least 24 hours; and
- those 31 identities had no persisted process-binding rows from which an
  independent control domain could be established.

The deployed write limiter permits up to 60 writes per minute, so update count
alone is not temporal establishment. These observations informed v1 and cannot
be presented as confirmatory findings. They also show why this read cannot
honestly be called participant or federation capacity.

## Frozen boundary

| Field | Frozen value |
|---|---|
| Cutoff (`as_of`) | `2026-09-17T00:00:00Z` |
| Lookback | exactly 30 days |
| Included recent interval | `2026-08-18T00:00:00Z <= recorded_at <= 2026-09-17T00:00:00Z` |
| Latest-row unit | one latest non-synthetic row per `identity_id` as of the cutoff |
| Latest-row tie-break | greatest `recorded_at`, then greatest `state_id` |
| Temporal evidence | current uninterrupted exact-instrument run has at least 25 distinct update counters in the lookback, with no counter decrease, spanning at least 24 hours and six distinct UTC hour buckets |
| Envelope schema | `eisv.telemetry.v1` |
| Primary source | `behavioral` |
| Behavioral observation source | `behavioral` |
| Derivation | `behavioral_sensor` / `behavioral_sensor.v1`, history window `10`, no missing inputs |
| Behavioral baseline | `is_baselined = true`, confidence at least `0.8`, target `30`, every Welford count at least `25` |
| V formula | version `2` in persisted state and envelope |
| EMA alphas (`E/I/S/V`) | `0.12 / 0.08 / 0.15 / 0.10` in persisted state and envelope |
| Value agreement | primary, envelope-smoothed, and persisted EISV values agree within `0.0001`; persisted raw, envelope raw, and derivation-computed `E/I/S` agree within the same tolerance |
| Timestamp agreement | valid `observed_at` within five seconds of `recorded_at` |
| Measurement ID | valid UUID, unique among non-synthetic rows through the cutoff |

The state constants are deployed in
[`src/behavioral_state.py`](../../src/behavioral_state.py); the envelope schema,
derivation version, and history window are deployed in
[`src/eisv_telemetry.py`](../../src/eisv_telemetry.py). These links explain the
frozen values but do not allow a later code change to reinterpret this contract.

Twenty-four hours is four times the protocol's longest frozen target offset.
Six UTC hour buckets prevent a single burst at the endpoints from satisfying
the duration rule. A source/configuration mismatch or decreasing update counter
among behavioral measurement rows starts a new run, so rows before a reset
cannot mature a rapidly rebuilt profile. A duplicate counter neither resets the
run nor adds distinct-update evidence. State rows with no behavioral
measurement are ignored rather than misclassified as instrument changes. These
are conservative feasibility predicates, not claims that a 24-hour baseline is
stationary or sufficient for every future horizon.

The authoritative read must execute once, at or after the cutoff, against one
transactionally consistent snapshot. Syntax-only validation before the cutoff
is allowed only in a development transaction and is not a result. The document
must merge before `2026-08-18T00:00:00Z`; otherwise the contract is
`contract_unreadable` and a new future window is required.

If a listed field is absent, has the wrong JSON type, carries another version,
or cannot be compared safely, the row fails closed at the corresponding stage.
If deployed semantics change without versioned provenance that this query can
distinguish, the result is `contract_unreadable`; no analyst may repair the
predicate after seeing counts.

## Frozen funnel

The output stages are monotonic:

1. `recent_any`: the latest non-synthetic row is inside the frozen window.
2. `behavioral_mature`: adds the persisted maturity and Welford predicates.
3. `temporal_established`: adds 25 distinct update counters in the current
   uninterrupted exact-instrument run, no counter decrease, a 24-hour span, and
   six UTC hour buckets in the same lookback.
4. `schema_ready`: adds the versioned envelope.
5. `measurement_complete`: adds typed persisted, primary, and
   envelope-smoothed EISV values plus measurement metadata.
6. `instrument_compatible`: adds the exact source, formula, history-window,
   baseline-target, V-version, alpha, and missing-input contract.
7. `same_row_consistent`: adds value ranges/agreement and timestamp agreement.
8. `strict_supply`: adds global measurement-ID validity and uniqueness. This is
   the decision count.

Only `strict_supply` feeds the gate. Earlier stages diagnose attrition and may
not substitute for it.

`synthetic IS NOT TRUE` remains the only durable fixture exclusion on the state
row. Mutable handles, labels, tags, and present lifecycle state are not frozen
evidence about an old row. The result is therefore still an upper bound: an
unmarked fixture, shared controller, unavailable participant, identity failure,
refusal, block, role conflict, or privacy failure can only reduce enrollment.

## Frozen SQL

The merge commit pins the exact query text and is the contract digest. The
operator records that commit SHA alongside the result. Any correction requires
a new document version and a new future cutoff.

```sql
WITH params AS (
    SELECT
        TIMESTAMPTZ '2026-09-17T00:00:00Z' AS as_of,
        INTERVAL '30 days' AS lookback
),
state_rows AS MATERIALIZED (
    SELECT
        s.identity_id,
        s.state_id,
        s.recorded_at,
        s.state_json,
        s.state_json->'behavioral_eisv' AS behavioral,
        s.state_json->'eisv_telemetry' AS envelope
    FROM core.agent_state s
    CROSS JOIN params p
    WHERE s.synthetic IS NOT TRUE
      AND s.recorded_at <= p.as_of
),
latest AS MATERIALIZED (
    SELECT DISTINCT ON (r.identity_id)
        r.*
    FROM state_rows r
    ORDER BY r.identity_id, r.recorded_at DESC, r.state_id DESC
),
lookback_history AS MATERIALIZED (
    SELECT
        r.*,
        CASE
            WHEN jsonb_typeof(r.behavioral->'updates') = 'number'
            THEN (r.behavioral->>'updates')::integer
        END AS updates,
        CASE
            WHEN jsonb_typeof(r.behavioral->'updates') = 'number'
             AND jsonb_typeof(r.behavioral->'alphas') = 'object'
             AND jsonb_typeof(r.behavioral #> '{alphas,E}') = 'number'
             AND jsonb_typeof(r.behavioral #> '{alphas,I}') = 'number'
             AND jsonb_typeof(r.behavioral #> '{alphas,S}') = 'number'
             AND jsonb_typeof(r.behavioral #> '{alphas,V}') = 'number'
             AND jsonb_typeof(r.envelope) = 'object'
             AND jsonb_typeof(r.envelope #> '{measurement,behavioral,alphas}') = 'object'
             AND jsonb_typeof(r.envelope #> '{measurement,behavioral,updates}') = 'number'
             AND jsonb_typeof(r.envelope #> '{measurement,behavioral,warmup,updates_completed}') = 'number'
             AND jsonb_typeof(r.envelope #> '{derivation,history_window}') = 'number'
             AND jsonb_typeof(r.envelope #> '{derivation,missing_inputs}') = 'array'
            THEN coalesce(
                r.envelope->>'schema' = 'eisv.telemetry.v1'
             AND r.envelope #>> '{measurement,primary,source}' = 'behavioral'
             AND r.envelope #>> '{measurement,behavioral,observation_source}' = 'behavioral'
             AND r.behavioral->>'obs_source' = 'behavioral'
             AND r.envelope #>> '{measurement,behavioral,v_formula_version}' = '2'
             AND r.behavioral->>'v_formula_version' = '2'
             AND r.envelope #>> '{measurement,behavioral,warmup,baseline_target}' = '30'
             AND r.behavioral #>> '{warmup,baseline_target}' = '30'
             AND r.envelope #>> '{derivation,kind}' = 'behavioral_sensor'
             AND r.envelope #>> '{derivation,formula_version}' = 'behavioral_sensor.v1'
             AND (r.envelope #>> '{derivation,history_window}')::integer = 10
             AND jsonb_array_length(r.envelope #> '{derivation,missing_inputs}') = 0
             AND (r.behavioral #>> '{alphas,E}')::double precision = 0.12
             AND (r.behavioral #>> '{alphas,I}')::double precision = 0.08
             AND (r.behavioral #>> '{alphas,S}')::double precision = 0.15
             AND (r.behavioral #>> '{alphas,V}')::double precision = 0.10
             AND r.behavioral->'alphas'
                 = '{"E": 0.12, "I": 0.08, "S": 0.15, "V": 0.10}'::jsonb
             AND r.behavioral->'alphas'
                 = r.envelope #> '{measurement,behavioral,alphas}'
             AND (r.envelope #>> '{measurement,behavioral,updates}')::integer
                 = (r.behavioral->>'updates')::integer
             AND (r.envelope #>> '{measurement,behavioral,warmup,updates_completed}')::integer
                 = (r.behavioral->>'updates')::integer,
                false
            )
            ELSE false
        END AS exact_instrument
    FROM state_rows r
    CROSS JOIN params p
    WHERE r.recorded_at >= p.as_of - p.lookback
      AND jsonb_typeof(r.behavioral) = 'object'
),
sequenced_history AS MATERIALIZED (
    SELECT
        h.*,
        lag(h.exact_instrument, 1, false) OVER (
            PARTITION BY h.identity_id
            ORDER BY h.recorded_at, h.state_id
        ) AS previous_exact_instrument,
        lag(h.updates) OVER (
            PARTITION BY h.identity_id
            ORDER BY h.recorded_at, h.state_id
        ) AS previous_updates
    FROM lookback_history h
),
segmented_history AS MATERIALIZED (
    SELECT
        h.*,
        sum(
            CASE
                WHEN NOT h.exact_instrument
                  OR NOT h.previous_exact_instrument
                  OR h.updates < h.previous_updates
                THEN 1
                ELSE 0
            END
        ) OVER (
            PARTITION BY h.identity_id
            ORDER BY h.recorded_at, h.state_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS instrument_segment
    FROM sequenced_history h
),
current_segment AS MATERIALIZED (
    SELECT DISTINCT ON (h.identity_id)
        h.identity_id,
        h.instrument_segment
    FROM segmented_history h
    ORDER BY h.identity_id, h.recorded_at DESC, h.state_id DESC
),
exact_instrument_history AS MATERIALIZED (
    SELECT h.identity_id, h.recorded_at, h.updates
    FROM segmented_history h
    JOIN current_segment c
      ON c.identity_id = h.identity_id
     AND c.instrument_segment = h.instrument_segment
    WHERE h.exact_instrument
),
history_summary AS MATERIALIZED (
    SELECT
        h.identity_id,
        count(*) AS exact_instrument_rows,
        count(DISTINCT h.updates) AS exact_instrument_updates,
        min(h.recorded_at) AS first_instrument_at,
        max(h.recorded_at) AS last_instrument_at,
        count(DISTINCT date_trunc(
            'hour', h.recorded_at AT TIME ZONE 'UTC'
        )) AS utc_hour_buckets
    FROM exact_instrument_history h
    GROUP BY h.identity_id
),
measurement_id_counts AS MATERIALIZED (
    SELECT
        (r.envelope->>'measurement_id')::uuid AS measurement_id,
        count(*) AS occurrences
    FROM state_rows r
    WHERE pg_input_is_valid(r.envelope->>'measurement_id', 'uuid')
    GROUP BY (r.envelope->>'measurement_id')::uuid
),
flags AS MATERIALIZED (
    SELECT
        l.*,
        p.as_of,
        p.lookback,
        coalesce(h.exact_instrument_rows, 0) AS exact_instrument_rows,
        coalesce(h.exact_instrument_updates, 0) AS exact_instrument_updates,
        coalesce(h.utc_hour_buckets, 0) AS utc_hour_buckets,
        coalesce(m.occurrences, 0) AS measurement_id_occurrences,
        l.recorded_at >= p.as_of - p.lookback AS recent,
        CASE
            WHEN jsonb_typeof(l.behavioral #> '{warmup,is_baselined}') = 'boolean'
            THEN (l.behavioral #>> '{warmup,is_baselined}')::boolean
            ELSE false
        END AS baselined,
        CASE
            WHEN jsonb_typeof(l.behavioral #> '{warmup,baseline_confidence}') = 'number'
            THEN (l.behavioral #>> '{warmup,baseline_confidence}')::double precision >= 0.8
            ELSE false
        END AS baseline_confident,
        l.behavioral #>> '{warmup,baseline_target}' = '30' AS baseline_target_v1,
        CASE
            WHEN jsonb_typeof(l.behavioral->'updates') = 'number'
            THEN (l.behavioral->>'updates')::integer >= 25
            ELSE false
        END AS updates_ready,
        CASE
            WHEN jsonb_typeof(l.behavioral #> '{baseline_stats,E,count}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{baseline_stats,I,count}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{baseline_stats,S,count}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{baseline_stats,V,count}') = 'number'
            THEN (l.behavioral #>> '{baseline_stats,E,count}')::integer >= 25
             AND (l.behavioral #>> '{baseline_stats,I,count}')::integer >= 25
             AND (l.behavioral #>> '{baseline_stats,S,count}')::integer >= 25
             AND (l.behavioral #>> '{baseline_stats,V,count}')::integer >= 25
            ELSE false
        END AS welford_ready,
        coalesce(
            h.exact_instrument_rows >= 25
            AND h.exact_instrument_updates >= 25
            AND h.last_instrument_at - h.first_instrument_at >= INTERVAL '24 hours'
            AND h.utc_hour_buckets >= 6,
            false
        ) AS temporal_ready,
        jsonb_typeof(l.envelope) = 'object'
            AND l.envelope->>'schema' = 'eisv.telemetry.v1' AS schema_v1,
        CASE
            WHEN jsonb_typeof(l.behavioral->'E') = 'number'
             AND jsonb_typeof(l.behavioral->'I') = 'number'
             AND jsonb_typeof(l.behavioral->'S') = 'number'
             AND jsonb_typeof(l.behavioral->'V') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,V}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,V}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,0}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,1}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,2}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,S}') = 'number'
            THEN nullif(l.envelope->>'measurement_id', '') IS NOT NULL
             AND nullif(l.envelope->>'observed_at', '') IS NOT NULL
             AND nullif(l.envelope #>> '{measurement,primary,source}', '') IS NOT NULL
            ELSE false
        END AS measurement_fields_complete,
        CASE
            WHEN jsonb_typeof(l.behavioral->'alphas') = 'object'
             AND jsonb_typeof(l.behavioral #> '{alphas,E}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{alphas,I}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{alphas,S}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{alphas,V}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,alphas}') = 'object'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,alphas,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,alphas,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,alphas,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,alphas,V}') = 'number'
             AND jsonb_typeof(l.behavioral->'updates') = 'number'
             AND jsonb_typeof(l.behavioral #> '{warmup,baseline_confidence}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,updates}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,warmup,updates_completed}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,warmup,baseline_confidence}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,history_window}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,missing_inputs}') = 'array'
            THEN l.envelope #>> '{measurement,primary,source}' = 'behavioral'
             AND l.envelope #>> '{measurement,behavioral,observation_source}' = 'behavioral'
             AND l.behavioral->>'obs_source' = 'behavioral'
             AND l.envelope #>> '{measurement,behavioral,v_formula_version}' = '2'
             AND l.envelope #>> '{measurement,behavioral,warmup,is_baselined}' = 'true'
             AND l.envelope #>> '{measurement,behavioral,warmup,baseline_target}' = '30'
             AND l.behavioral->>'v_formula_version' = '2'
             AND l.envelope #>> '{derivation,kind}' = 'behavioral_sensor'
             AND l.envelope #>> '{derivation,formula_version}' = 'behavioral_sensor.v1'
             AND (l.envelope #>> '{derivation,history_window}')::integer = 10
             AND jsonb_array_length(l.envelope #> '{derivation,missing_inputs}') = 0
             AND (l.behavioral #>> '{alphas,E}')::double precision = 0.12
             AND (l.behavioral #>> '{alphas,I}')::double precision = 0.08
             AND (l.behavioral #>> '{alphas,S}')::double precision = 0.15
             AND (l.behavioral #>> '{alphas,V}')::double precision = 0.10
             AND l.behavioral->'alphas'
                 = '{"E": 0.12, "I": 0.08, "S": 0.15, "V": 0.10}'::jsonb
             AND l.behavioral->'alphas' = l.envelope #> '{measurement,behavioral,alphas}'
             AND (l.envelope #>> '{measurement,behavioral,updates}')::integer
                 = (l.behavioral->>'updates')::integer
             AND (l.envelope #>> '{measurement,behavioral,warmup,updates_completed}')::integer
                 = (l.behavioral->>'updates')::integer
             AND abs((l.envelope #>> '{measurement,behavioral,warmup,baseline_confidence}')::double precision
                     - (l.behavioral #>> '{warmup,baseline_confidence}')::double precision) <= 0.0001
            ELSE false
        END AS instrument_ready,
        CASE
            WHEN jsonb_typeof(l.behavioral->'E') = 'number'
             AND jsonb_typeof(l.behavioral->'I') = 'number'
             AND jsonb_typeof(l.behavioral->'S') = 'number'
             AND jsonb_typeof(l.behavioral->'V') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,primary,values,V}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,smoothed,V}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,0}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,1}') = 'number'
             AND jsonb_typeof(l.behavioral #> '{raw_obs,2}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{measurement,behavioral,raw_observation,S}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,E}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,I}') = 'number'
             AND jsonb_typeof(l.envelope #> '{derivation,computed_observation,S}') = 'number'
            THEN (l.envelope #>> '{measurement,primary,values,E}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,primary,values,I}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,primary,values,S}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,primary,values,V}')::double precision BETWEEN -1 AND 1
             AND (l.behavioral->>'E')::double precision BETWEEN 0 AND 1
             AND (l.behavioral->>'I')::double precision BETWEEN 0 AND 1
             AND (l.behavioral->>'S')::double precision BETWEEN 0 AND 1
             AND (l.behavioral->>'V')::double precision BETWEEN -1 AND 1
             AND (l.envelope #>> '{measurement,behavioral,smoothed,E}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,smoothed,I}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,smoothed,S}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,smoothed,V}')::double precision BETWEEN -1 AND 1
             AND (l.behavioral #>> '{raw_obs,0}')::double precision BETWEEN 0 AND 1
             AND (l.behavioral #>> '{raw_obs,1}')::double precision BETWEEN 0 AND 1
             AND (l.behavioral #>> '{raw_obs,2}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,raw_observation,E}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,raw_observation,I}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{measurement,behavioral,raw_observation,S}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{derivation,computed_observation,E}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{derivation,computed_observation,I}')::double precision BETWEEN 0 AND 1
             AND (l.envelope #>> '{derivation,computed_observation,S}')::double precision BETWEEN 0 AND 1
             AND abs((l.envelope #>> '{measurement,primary,values,E}')::double precision
                     - (l.behavioral->>'E')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,I}')::double precision
                     - (l.behavioral->>'I')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,S}')::double precision
                     - (l.behavioral->>'S')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,V}')::double precision
                     - (l.behavioral->>'V')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,E}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,smoothed,E}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,I}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,smoothed,I}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,S}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,smoothed,S}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,primary,values,V}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,smoothed,V}')::double precision) <= 0.0001
             AND abs((l.behavioral #>> '{raw_obs,0}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,raw_observation,E}')::double precision) <= 0.0001
             AND abs((l.behavioral #>> '{raw_obs,1}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,raw_observation,I}')::double precision) <= 0.0001
             AND abs((l.behavioral #>> '{raw_obs,2}')::double precision
                     - (l.envelope #>> '{measurement,behavioral,raw_observation,S}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,behavioral,raw_observation,E}')::double precision
                     - (l.envelope #>> '{derivation,computed_observation,E}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,behavioral,raw_observation,I}')::double precision
                     - (l.envelope #>> '{derivation,computed_observation,I}')::double precision) <= 0.0001
             AND abs((l.envelope #>> '{measurement,behavioral,raw_observation,S}')::double precision
                     - (l.envelope #>> '{derivation,computed_observation,S}')::double precision) <= 0.0001
            ELSE false
        END AS values_ready,
        CASE
            WHEN pg_input_is_valid(l.envelope->>'observed_at', 'timestamptz')
            THEN abs(extract(epoch FROM (
                (l.envelope->>'observed_at')::timestamptz - l.recorded_at
            ))) <= 5
            ELSE false
        END AS timestamp_ready,
        coalesce(
            pg_input_is_valid(l.envelope->>'measurement_id', 'uuid')
            AND m.occurrences = 1,
            false
        ) AS measurement_id_ready
    FROM latest l
    CROSS JOIN params p
    LEFT JOIN history_summary h ON h.identity_id = l.identity_id
    LEFT JOIN measurement_id_counts m
        ON m.measurement_id = CASE
            WHEN pg_input_is_valid(l.envelope->>'measurement_id', 'uuid')
            THEN (l.envelope->>'measurement_id')::uuid
        END
),
funnel AS MATERIALIZED (
    SELECT
        f.*,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AS f_behavioral_mature,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready AS f_temporal_established,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready
            AND f.schema_v1 AS f_schema_ready,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready
            AND f.schema_v1 AND f.measurement_fields_complete
            AS f_measurement_complete,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready
            AND f.schema_v1 AND f.measurement_fields_complete
            AND f.instrument_ready AS f_instrument_compatible,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready
            AND f.schema_v1 AND f.measurement_fields_complete
            AND f.instrument_ready AND f.values_ready
            AND f.timestamp_ready AS f_same_row_consistent,
        f.recent AND f.baselined AND f.baseline_confident
            AND f.baseline_target_v1 AND f.updates_ready
            AND f.welford_ready AND f.temporal_ready
            AND f.schema_v1 AND f.measurement_fields_complete
            AND f.instrument_ready AND f.values_ready
            AND f.timestamp_ready AND f.measurement_id_ready AS f_strict_supply
    FROM flags f
)
SELECT
    p.as_of,
    count(*) FILTER (WHERE f.recent) AS recent_any,
    count(*) FILTER (WHERE f.f_behavioral_mature) AS behavioral_mature,
    count(*) FILTER (WHERE f.f_temporal_established) AS temporal_established,
    count(*) FILTER (WHERE f.f_schema_ready) AS schema_ready,
    count(*) FILTER (WHERE f.f_measurement_complete) AS measurement_complete,
    count(*) FILTER (WHERE f.f_instrument_compatible) AS instrument_compatible,
    count(*) FILTER (WHERE f.f_same_row_consistent) AS same_row_consistent,
    count(*) FILTER (WHERE f.f_strict_supply) AS strict_supply
FROM params p
LEFT JOIN funnel f ON true
GROUP BY p.as_of;
```

## Result packet and privacy

The reviewer records only:

- contract name and merge commit SHA;
- execution timestamp and frozen `as_of`;
- the eight monotonic aggregate stages;
- the instrument-supply status below; and
- any `contract_unreadable` reason.

No identity, handle, label, relationship, principal, control domain, timestamp
distribution, source subgroup, row, vector, alpha profile, baseline profile, or
participant list is released. Public reporting renders any positive count below
10 as `<10`. The independent reviewer may use the transient exact aggregate only
to apply the frozen gate. No shifted cutoff, alternate filter, or repeat query is
allowed.

## Decision rule

Apply the rule to `strict_supply` only:

| Count | Status | Meaning |
|---:|---|---|
| `< 200` | `instrument_supply_not_ready` | There are not 200 temporally established, same-instrument process identities; no principal-capacity or implementation work proceeds. |
| `>= 200` | `instrument_supply_ready` | Raw measurement supply is large enough to justify the already-required independent-principal feasibility review; this is not enrollment or collection authority. |

Two hundred raw identities do not imply 100 principals in either role. The
separate pre-enrollment artifact must still prove at least 100 experimental
principals in each role, five independently administered control domains, no
domain above 25% of scored dyad endpoints, contribution caps over the transitive
principal join, at least 80% simulated inferential power at a true `0.05`
improvement under the frozen dyadic graph and privacy mechanism, and the
separate full-rule operating curve required by the protocol. Historical absence
of a lineage or binding edge may not be interpreted as independence.

## Stop and reopen rule

After the one authoritative read:

- `instrument_supply_not_ready` closes this attempt. Do not loosen maturity,
  duration, hour-bucket, alpha, value, timestamp, or ID checks and do not repeat
  the query to seek a passing snapshot.
- `instrument_supply_ready` permits only independent-principal, privacy, and
  adversarial architecture review. It does not permit recruitment, enrollment,
  target selection, forecast capture, or runtime implementation.
- `contract_unreadable` closes the read without a supply conclusion.

A later supply read requires a new version, a new future cutoff, and a stated
new premise such as a shipped provenance rollout or materially larger mature
population. Every earlier contract and result remains visible.
