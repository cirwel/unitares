# Assurance case v0: canonical outcome record per prediction binding

**Status:** DRAFT. Nobody outside the authoring session has assessed this case.
Writing it does not authorize an outside assessment, a runtime change, or a
fix to #2247.
**Frozen revision:** `master` at `2be3322b5f8ea90a7e00d3fc51b31fe50659d7f1`.
Every path, test name and default below refers to that revision.
**Origin:** first assurance case recommended in
[`docs/ontology/frontier-brief-2026-09-24.md`](../../ontology/frontier-brief-2026-09-24.md)
§2. The operator delegated the choice on 2026-09-25 (see that note's *Operator
disposition*).
**Form:** narrow, pre-stated claim with evidence, as in OpenAI's 2026-09-22
third-party-assessment principles, which the note summarizes. The case is a
document, not a runtime object.

---

## 1. Claim

> While a prediction binding is retained, the outcome store holds **at most one
> canonical outcome record** per `(agent_id, prediction_id)`. A retry with the
> same request returns that canonical record. A retry with a different request
> is rejected with `PREDICTION_REUSE_CONFLICT` and the canonical outcome ID,
> and writes no new outcome row.

The claim is deliberately narrower than "exactly-once outcome binding". It is
about the **outcome record** only, and makes no claim about the following:

- **Calibration delivery.** Delivering calibration is a post-commit side effect
  and is outside the claim. See limit L1.
- **Behaviour after the binding expires.** An expired prediction ID can start a
  new canonical submission, by design. See limit L2.
- **Outcomes submitted without a `prediction_id`.**
- **Orchestrator spawn idempotency** (#1939, #1942, #1953; migration 068). That
  mechanism also rejects conflicting key reuse, but it is separate and needs its
  own case.

## 2. Threat model

Within scope, the claim must survive these conditions:

| # | Condition | Why it matters |
|---|---|---|
| T1 | A client retries after losing the commit acknowledgement | The classic source of duplicate writes |
| T2 | Concurrent identical submissions | A race between claim and insert |
| T3 | Concurrent **conflicting** submissions for one prediction ID | Two requests contest the canonical record |
| T4 | The outcome insert fails after the claim is taken | A claim with no outcome behind it would block every retry |
| T5 | The server restarts between the original submission and a retry | The replay must not depend on memory held in the process |
| T6 | The outcome partition for the timestamp is missing | Partition routing fails partway through |
| T7 | A retry reuses a prediction ID with a different payload | Silent overwrite, or a second record |

Out of scope: a malicious operator with database write access, a compromised
host, and loss of the database itself, none of which this layer can defend
against. Also out of scope is whether the writer truly owns the `agent_id`. The
claim is about uniqueness per key, not authorship: when there is no session
context, the handler accepts an explicitly supplied `agent_id`, so a caller who
does not own a key could claim it first. Authorship belongs to the identity
layer, not this case.

## 3. Mechanism

- **Ledger.** `audit.outcome_prediction_bindings` (migration 070) is not
  partitioned. Its primary key is `(agent_id, prediction_id)` and it stores a
  `request_digest`. Outcomes live in the range-partitioned `audit.outcome_events`,
  so global uniqueness is enforced on the ledger instead.
- **Atomic commit.** The claim and the partitioned outcome insert commit in one
  transaction (`src/db/mixins/tool_usage.py`).
- **Conflict response.** A digest mismatch returns `PREDICTION_REUSE_CONFLICT`
  with the canonical ID (`src/mcp_handlers/observability/outcome_events.py`).
- **Retention.** `audit.cleanup_outcome_prediction_bindings(p_retention_days
  DEFAULT 365)` deletes a binding only when no matching canonical outcome row
  exists (a `NOT EXISTS` check on `(ts, outcome_id)`). The maintenance path,
  `audit.drop_old_outcome_partitions` (`db/postgres/partitions.sql`, default
  365 days), runs this cleanup in the same transaction as the partition drop.
  So a binding is never deleted while its outcome remains, and on the
  maintenance path it is retired together with its outcome.

## 4. Assumptions

- **A1.** Clients pass the `prediction_id` returned by `process_agent_update` /
  `sync_state`.
- **A2.** PostgreSQL enforces primary-key uniqueness under concurrent inserts
  and commits transactions atomically, as documented.
- **A3.** The operator runs cleanup with the default retention or longer. The
  retention window is a setting the operator controls; a shorter window shortens
  the scope of the claim.
- **A4.** Nothing writes to `audit.outcome_prediction_bindings` or
  `audit.outcome_events` outside the application path.

## 5. Evidence manifest

All tests below run in CI (`.github/workflows/tests.yml`).

- The real-Postgres suite runs in the `tests-u-z-and-agents` shard, which
  includes `tests/*/`, against a `pgvector/pgvector:pg17` service.
- The same suite **skips itself when no test database is reachable**, which is
  the usual case locally. An assessor must confirm from the CI logs that it ran
  and did not skip.

| Evidence | Covers | Backend |
|---|---|---|
| `tests/db/test_outcome_prediction_binding_postgres.py::test_concurrent_identical_submissions_replay_one_canonical_outcome` | T2 | real Postgres |
| `…::test_concurrent_conflicting_submissions_choose_one_canonical_outcome` | T3 | real Postgres |
| `…::test_outcome_failure_rolls_back_claim_on_real_postgres` | T4 | real Postgres |
| `…::test_lost_commit_ack_replays_canonical_through_new_connection` | T1 | real Postgres |
| `…::test_expired_binding_cleanup_allows_new_canonical_submission` | L2 boundary | real Postgres |
| `…::test_cleanup_uses_full_partitioned_outcome_key` | retention safety | real Postgres |
| `…::test_non_exact_score_is_canonical_across_response_row_replay_and_calibration` | canonical score | real Postgres |
| `…::test_ambiguous_commit_currently_delivers_calibration_zero_times` | **pins L1 as current behaviour** | real Postgres |
| `tests/test_outcome_prediction_binding_db.py` (5 tests: atomic commit, rollback, identical-claim read, digest conflict, missing-partition retry) | T4, T6, T7 | in-memory fake |
| `tests/test_outcome_prediction_idempotency.py` (10 tests: lost ack, concurrent identical, conflicting retry, restart replay, storage failure, public response shape) | T1, T2, T5, T7 | in-memory fake |
| `tests/test_outcome_prediction_binding_migration.py` (3 tests, including `test_binding_retention_cannot_outlive_canonical_outcomes`) | retention invariant | static |
| `docs/CHANGELOG.md` entry for #2246 | the change and its stated limit | — |

**Independence caveat.** The same project wrote these tests. The fake-backend
tests show the handler's logic, not database behaviour; only the real-Postgres
suite covers the database. That makes this *developer evidence*. An outside
assessment would have to re-run the tests, and ideally add its own adversarial
cases.

## 6. Known limits

- **L1: calibration can be delivered zero times (#2247, open).** When a commit
  is ambiguous, the outcome can persist while calibration is not delivered. The
  claim is scoped so that this is a disclosed limit and not a counterexample.
  The pinned test above reproduces it; how often it happens in production is
  not measured. **Knock-on effect:** arm D of the registered coordination
  ablation (`accountable-coordination-ablation-v0`) includes outcome binding, so
  L1 is a defect specific to arm D in that study. The enrollment record should
  disclose it as an arm-D-specific limit. It is not a common-mode defect in the
  protocol's sense, which covers repairs applied to every arm.
- **L2: the claim holds only inside the retention window.** After cleanup, the
  same `prediction_id` can start a new canonical submission. That is intended,
  but it means the claim is not "forever".
- **L3: the retention window is operator-configurable.** See A3.
- **L4: no production measurement.** There is no count of observed conflicts,
  replays or L1 occurrences. Following *Measurement authority* in `CLAUDE.md`,
  a zero from an unmeasured surface is not evidence that nothing happened.

## 7. What would falsify the claim

Any of the following, observed at the frozen revision within the retention
window with the assumptions holding:

1. Two rows in `audit.outcome_events` with equal `agent_id` and equal
   non-null `detail->>'prediction_id'` (the handler writes `prediction_id` into
   `detail`; it is not a column of that table).
2. A conflicting retry that writes an outcome row, or returns success.
3. An identical retry whose response differs from the stored canonical record.

## 8. Reproduction

```bash
git checkout 2be3322b5f8ea90a7e00d3fc51b31fe50659d7f1
# Needs PostgreSQL with pgvector and a governance_test database matching
# tests/test_db_utils.TEST_DB_URL (postgres:postgres@localhost:5432/governance_test).
python -m pytest -ra \
  tests/db/test_outcome_prediction_binding_postgres.py \
  tests/test_outcome_prediction_binding_db.py \
  tests/test_outcome_prediction_idempotency.py \
  tests/test_outcome_prediction_binding_migration.py
# Check the -ra summary: the Postgres tests must be reported as passed, not skipped.
```

## 9. Next steps (not authorized here)

- An independent re-run and review of this case.
- Measuring L1's frequency before anyone decides whether to fix #2247 or keep
  it disclosed.
- A separate case for orchestrator spawn idempotency, if wanted.
