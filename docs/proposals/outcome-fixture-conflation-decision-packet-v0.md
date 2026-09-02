# Decision packet: `calibration_excluded` is read as a fixture marker, so every outcome-grounded instrument has been blind to live outcomes since 2026-08-09

**Status:** decision packet, raised 2026-09-02, **awaiting operator selection**. No
code in this PR. Shape follows the contract in
[`operator-decision-packet-v0.md`](operator-decision-packet-v0.md).
**Class:** authority (what a pre-registered instrument counts as evidence).
**Reversibility:** the predicate change is reversible; a disclosed protocol
deviation is not.
**Blast radius:** surface (the evaluation instruments), with one fleet-level
consequence: the 2026-12-01 registered read.
**Raised by:** `da6974df-ca2a-4119-b26d-2311a3151d32` (a Claude Code session,
2026-09-02). Not yet council-reviewed; the review record will be attached to the
PR that carries this file.
**Default if silent:** the instruments stay blind. The 2026-12-01 read reports
condition 3 (`Bad clusters >= 150`) from a cohort that an instrument flag has
been emptying since 2026-08-01, and the legacy-coherence dependency shadow never
reaches its 150-cluster floor.

## The question, as a fork

An outcome row posted without a caller-supplied confidence is stamped
`calibration_excluded` (correct: a scraped confidence must not train
calibration). That flag is also in the structural fixture set, so the same row
is dropped from **every** validation instrument as if it were synthetic fixture
traffic. Since the flag started being stamped on live traffic (PR #1445,
2026-08-01), nearly every trusted exogenous outcome has been written and then
discarded before any read.

**Which of these does UNITARES count as validation-visible evidence?**

1. Only rows whose confidence was caller-bound (today's semantics), with the
   producers fixed to bind one.
2. Every trusted exogenous row, with calibration eligibility tracked as its own
   flag, applied to new rows only.
3. Every trusted exogenous row, applied to the rows already written, with the
   registered read's deviation disclosed before the read.
4. Nothing changes.

Options 1 and 2 are not mutually exclusive with 3 in code; they are exclusive as
the decision about what the registered cohort contains.

## Finding

### Mechanism, verified in the tree at `origin/master` `24cfaa32`

| Step | Where |
|---|---|
| No caller confidence and no registry-bound `prediction_id` ⇒ `prediction_source` is a fallback ⇒ `scraped_confidence` ⇒ `detail.calibration_excluded = true` | `src/mcp_handlers/observability/outcome_events.py:420-441` (PR #1445) |
| `calibration_excluded` is a member of `_CONTROLLED_FIXTURE_FLAGS`, so `is_structurally_controlled_fixture()` returns true for the row | `src/grounding/outcome_anchors.py:57-63`, `:95` (added by PR #1562) |
| Every validation instrument applies that predicate before counting | `scripts/analysis/eisv_ablation_matrix.py:562`, `eisv_skeptic_report.py:1710`, `legacy_coherence_dependency_shadow.py:538`, `prospective_prediction_cohort.py`, `outcome_inventory.py`, `src/eisv_telemetry_health.py` |

Issue #1790 (2026-08-21) described exactly this conflation and named the fix:
count `calibration_excluded` as its own attrition bucket instead of folding it
into fixture attrition. PR #1793 closed the issue by making the attrition
*visible* (inventory counters, a write-time `validation_visibility` warning on
`/v1/harness/outcome`) and left the exclusion in place. The warning is only seen
by a caller that reads the response; the hook producers do not.

### Measurement, deployment database, 2026-09-02

Rows are `audit.outcome_events` with `verification_source = 'external_signal'`
unless stated. The trusted-anchor predicate is the shared one in
`src/grounding/outcome_anchors.py` (`anchored_outcomes_predicate`).

| Window | Trusted-anchor rows | Agents |
|---|---|---|
| 90 days | 1,400 | 147 |
| 21 days | 816 | 88 |

| Month | `calibration_excluded` true | false |
|---|---|---|
| 2026-06 | 0 | 1,715 |
| 2026-07 | 0 | 274 |
| 2026-08 | 1,184 | 18 |
| 2026-09 (to date) | 8 | 0 |

| Relative to the frozen 2026-08-09 read | Rows | Excluded |
|---|---|---|
| Before the cutoff | 2,196 | 187 |
| After the cutoff | 1,005 | 1,005 |

| Instrument, 21-day window | Result |
|---|---|
| Structural fixture predicate applied to the 816 trusted rows | 816 excluded, 0 kept |
| Key that flips the verdict when removed | `calibration_excluded` (only that key) |
| `legacy_coherence_dependency_shadow.py --window-days 21` | 0 outcomes fetched, both scopes |
| `outcome_inventory.py --window-days 21` | `fixture_rows_excluded: 2661`, `calibration_excluded_only: 1555`, strict outcomes visible: 5 |
| `prediction_binding` on the 816 rows | 696 `prev_confidence_fallback`, 120 `audit_trail_fallback`, 0 registry (live re-verification 2026-09-02; an earlier draft read 154 from the unfiltered 850-row `external_signal` set) |
| Explicit fixture flags set on those rows | 0 |

The rows are not fixture traffic. Their `detail` carries the pytest command
lines of agent worktrees doing real work on this repository.

**Which of the four states this is.** The producer ran and the rows exist
(not state 1 or 2). The instrument's predicate discards them before counting:
state 3, *not recorded*. It is not state 4. A zero from any of these
instruments over this period therefore carries no information about label
supply, and must not be cited as such.

### What it reaches

- **Legacy-coherence dependency shadow** (prospective contract of 2026-08-12,
  `legacy-coherence-dependency-ablation-v0.md`). The envelope is emitting on
  6,762 of 8,215 state rows in the last 7 days, and the read joins 0 outcomes.
  Its 150-cluster floor cannot accrue. This is the gate on the coherence
  producer migration.
- **The registered 2026-12-01 read** (`eisv-outcome-grounding-stop-rule-v0.md`).
  The cohort is `--anchor-scope trusted --exclude-harness-lanes beam` plus the
  script's fixture exclusion, which is the same predicate. The frozen
  2026-08-09 read is unaffected (187 of 2,196 excluded, all of them stamped by
  the same rule). Every trusted row since the cutoff is excluded, so condition 3
  will be reported from a cohort emptied by an instrument flag, and a FAIL on it
  would be closure by bookkeeping, not by label supply. The doc's own rule says
  a condition-3 failure must be described as insufficient eligible evidence, not
  disproof; under this defect it is not even that.
- **Inventory and telemetry health** report the attrition, so they are honest
  about it, but they count 5 strict outcomes in 21 days.
- **The revenue-engine pilot.** If its label ingestion posts `external_signal`
  rows without a registry-bound prediction, those labels will be invisible to
  the same instruments. Not verified here; the pilot owners should check before
  the first episode.

## Options

| # | Option | Consequence | Tradeoff |
|---|---|---|---|
| 1 | **Keep the semantics, fix the producers.** `~/scripts/hooks/outcome-tracker.sh` (the only producer of these rows; it POSTs `agent_uuid`, `outcome_type`, `session_id`, `verification_source`, `detail` to `/v1/harness/outcome`) and any SDK caller bind each row to the agent's registered `prediction_id` from its last check-in instead of letting the server scrape a confidence. | Rows written after the fix are validation-visible and calibration-eligible. No instrument or protocol changes. | Not free: `/v1/harness/outcome` forwards `confidence` but not `prediction_id` (`src/http_routes/substrate.py:365-377`), so the endpoint needs a small change to pass `prediction_id` through to `outcome_event`, and the hook has to learn the agent's current prediction id, which it does not see today. Does nothing for the 1,005 rows already written. An unfixed producer stays invisible with a success response. A test hook has no confidence of its own, so sending one would be the scraped-confidence defect moved client-side; binding is the only honest form. |
| 2 | **Split the flags, prospectively.** Remove `calibration_excluded` from `_CONTROLLED_FIXTURE_FLAGS`; fixtures keep their explicit flags; the calibration trainer keeps honouring `calibration_excluded`. Instruments count new rows from the deploy date. | Every instrument regains live evidence going forward without touching any cohort already read. The registered read's frozen slice is untouched; its post-freeze accrual starts at deploy. | The 08-01 to deploy rows stay excluded by a rule everyone now agrees is wrong. Still a change to the registered read's sampling frame for future rows, so it is disclosed. |
| 3 | **Split the flags, retroactively.** As 2, and the rows already written since 2026-08-01 count too. Disclosed in the stop-rule doc as a third protocol deviation, before the read, with this packet as the record. | The largest evidence gain, and the kill criterion is decided by label supply rather than by a flag. The excluded rows were never part of any discrimination read, so including them cannot be steered by a seen result. | Touches a pre-registered cohort after data exists. Defensible only because the defect is mechanical and disclosed before the read, the same class as the two 2026-08-23 disclosures; a reviewer may still count it against the read's confirmatory standing. |
| 4 | **Nothing, or the env escape hatch** (`UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE=1`). | The flag re-enables training calibration on scraped confidences, the defect #1445 removed. | Rejected as written; recorded so nobody rediscovers it as a fix. |

## Recommendation (author's lean, non-binding, not yet council-sourced)

Do **1** regardless of the fork: it needs no operator decision, only the small
endpoint change and the hook change named above, and it is the producer-side
hygiene the schema was designed for. Choose **3** for the
instruments, with the deviation disclosed in the stop-rule doc before the read:
the rows exist, satisfy the trusted-anchor predicate, were never seen by a
discrimination read, and are excluded only by a flag whose meaning is
"do not train calibration on this". Letting that flag decide condition 3 would
make the kill criterion a bookkeeping outcome, which the measurement-authority
rules in `CLAUDE.md` forbid. If the operator wants zero retroactivity on the
registered read, **2** is the fallback and 1 still applies.

## Disconfirmers

- If the 816 rows carried caller confidences after all, the `prediction_binding`
  distribution above would show `registry` or `argument` bindings. It shows 0.
- If the ablation matrix did not apply the fixture predicate, the registered
  read would be unaffected. It applies it at
  `scripts/analysis/eisv_ablation_matrix.py:562`.
- If the rows were genuine fixtures, they would carry the explicit fixture flags
  or the controlled test names. They carry neither; their `detail` is agent test
  runs.

## Evidence

PRs #1445, #1562, #1790, #1793, #1831, #1855. Docs:
`eisv-outcome-grounding-stop-rule-v0.md` (pre-registered gate and the two
existing disclosures), `docs/ontology/falsification-design-system-audit-2026-08-23.md`
(target-matched sampling frame), `legacy-coherence-dependency-ablation-v0.md`,
README "Current claim status".

Queries used (read-only, against the deployment database):

```sql
-- trusted-anchor rows, 21d / 90d
SELECT count(*), count(DISTINCT o.agent_id)
FROM audit.outcome_events o
WHERE o.ts > now() - interval '21 days'
  AND (o.verification_source = 'external_signal')
  AND (o.eisv_e IS NOT NULL
       AND coalesce((o.detail->>'snapshot_missing')::boolean, false) = false);

-- excluded by month
SELECT to_char(ts,'YYYY-MM'),
       count(*) FILTER (WHERE (detail->>'calibration_excluded')::boolean IS TRUE),
       count(*) FILTER (WHERE (detail->>'calibration_excluded')::boolean IS FALSE)
FROM audit.outcome_events
WHERE ts > '2026-05-01' AND verification_source = 'external_signal'
GROUP BY 1 ORDER BY 1;

-- split at the frozen read
SELECT CASE WHEN ts < '2026-08-09' THEN 'before' ELSE 'after' END,
       count(*) FILTER (WHERE (detail->>'calibration_excluded')::boolean IS TRUE),
       count(*)
FROM audit.outcome_events
WHERE ts > '2026-05-01' AND verification_source = 'external_signal'
GROUP BY 1;
```

The predicate test applied `src.grounding.outcome_anchors.is_structurally_controlled_fixture`
to each of the 816 rows and removed keys one at a time to find the one that
flips the verdict.
