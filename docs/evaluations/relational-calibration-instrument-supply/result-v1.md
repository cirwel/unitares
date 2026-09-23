# Relational calibration instrument-supply read v1 — registered result

**Status: `instrument_supply_not_ready`.** The one authoritative read found
`strict_supply = 0`, below the frozen threshold of 200. Under the contract's
stop rule this closes the attempt: no maturity, duration, hour-bucket, alpha,
value, timestamp or ID check is loosened, and the query is not repeated. A later
supply read needs a new version, a new future cutoff and a stated new premise.

This is a count of instrument supply only. It says nothing about participant,
principal or federation capacity, which the contract never measured.

## Frozen inputs and execution

- Contract: `relational-calibration-instrument-supply-v1`
  ([`relational-calibration-maturity-capacity-v1.md`](../../proposals/registered/relational-calibration-maturity-capacity-v1.md))
- Merge commit (contract digest): `af2a7ea980d40f5b0d87613ba15ef1c7e0221ea6`
  (#1593, merged 2026-08-11T04:49:59Z, before the 2026-08-18 deadline)
- Frozen SQL: extracted from master and confirmed byte-identical to the merge
  commit's text; SHA-256 of the extracted block begins `9c536f48e69cc6da`
- Frozen `as_of`: `2026-09-17T00:00:00Z`
- Execution: `2026-09-23T23:43:57Z`, once, in a single
  `REPEATABLE READ READ ONLY` transaction against the maintainer deployment's
  `governance` database, at the operator's direction

## Funnel

| Stage | Count |
|---|---:|
| `recent_any` | 953 |
| `behavioral_mature` | 38 |
| `temporal_established` | 0 |
| `schema_ready` | 0 |
| `measurement_complete` | 0 |
| `instrument_compatible` | 0 |
| `same_row_consistent` | 0 |
| `strict_supply` | 0 |

No positive count falls below 10, so no cell is rendered as `<10`. No row,
identity or distribution was retained beyond these aggregates.

## Disclosures

- **Late execution.** The contract requires the read "at or after the cutoff";
  it ran six days after. The query pins `as_of` as a literal and reads only
  rows recorded up to it, so the delay changes no predicate. It was late because
  no job or owner had been scheduled to run it; an audit on 2026-09-23 found it
  had never run.
- **Retention.** The query reads rows present at execution. Retention on
  `core.agent_state` removes old rows without an archive, so any deletion
  between the cutoff and execution could only lower counts. The window
  (2026-08-18 to 2026-09-17) held 29,601 rows when checked earlier the same
  day; the observed retention loss has been in months-old history, not the
  most recent month. Deletion inside the window cannot be ruled out from the
  database alone, but it would take the loss of temporally established runs
  for at least 200 identities, against 38 that reached behavioral maturity, to
  change the classification.
- **No `contract_unreadable` condition was found.** The query failed closed at
  the temporal stage as specified; it did not error.
