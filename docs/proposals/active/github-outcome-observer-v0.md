# GitHub outcome observer, v0 (timeline-only stage)

**Created:** 2026-10-23
**Status:** Draft, design only. Nothing is built. Written under two operator directions of 2026-10-23: the live portal (KG `2026-10-23T11:39:51.328628+00:00`) and "mistakes are not negative by default; failure signals are never a kill gate" (KG `2026-10-23T12:51:38.289377+00:00`). Merging this document authorises stage 1 only (§6).
**Related:** `portal-live-timeline-v0.md` (the pane that shows these events), PR #3045 (`GET /v1/timeline`), PR #3049 (check-in report excerpts), `eisv-outcome-grounding-stop-rule-v0.md` (the registered read this stays out of).

## 1. Problem

Agents report their own work through check-ins. The evidence about that work mostly lives on GitHub: CI results on the PR, review records from the review gate, and whether the change merged. None of it reaches UNITARES today unless an agent records it by hand.

The 2026-10-23 session that built #3045–#3050 shows the gap. UNITARES recorded 41 check-ins for that agent, all `approve`, risk 0.00–0.29. The PR record for the same hours holds 22 review findings fixed before merge, one CI failure (a 1400-byte budget breach), and one corrected claim about a local test run. A reader of UNITARES alone would see a quiet session. A reader of GitHub would see the real one.

Self-report is the right source for claims and intent. It is the wrong source for evidence about mistakes: an agent cannot report a mistake it does not know it made. Evidence should arrive on its own, and a mistake should show up as a contradiction between a claim and a result, not depend on a confession.

## 2. Principle: a mistake is a path, not a mark

Recorded as an operator direction on 2026-10-23. The observer and every view of its events follow it.

| Stage | Meaning |
|---|---|
| introduced | someone did real work; carries no weight on its own |
| caught by the agent | self-checking worked |
| caught by review or CI | the gates worked; time to catch is the useful number |
| fixed | the path closed; time to correction is the useful number |
| escaped | merged, deployed or user-visible with the defect: a negative signal |
| repeated | the same class of mistake again: a learning signal |
| concealed or misreported | a claim contradicted by evidence and not corrected: the case accountability exists for |

Failure counts are not a measure. A red CI run on an open PR, a review finding, and a `test_failed` row written during test-first development are all the process working. Nothing in this design pauses, ranks or penalises an agent.

## 3. What exists

- **Attribution key.** Every agent-authored PR body ends with `UNITARES agent: <uuid>` (global contract, operator decisions 2026-10-20 and 2026-10-23). The uuid resolves in UNITARES to the agent's identity.
- **Structured review records.** `scripts/dev/review_gate.py` posts PR comments that open with `<!-- unitares-review v1 key=… verdict=CLEAN|FINDINGS findings=N disposed=M reviewer=… -->` (marker `unitares-review v1`, `review_gate.py` `MARKER`). Dispositions are posted the same way.
- **CI.** GitHub check runs and workflow runs per PR head, readable through `unitares-github --read-only -- gh …` with a repository-scoped App token.
- **Ingestion routes.**
  - `POST /v1/harness/outcome` writes `audit.outcome_events` under an operator-asserted `agent_uuid`. It feeds outcome analysis, so stage 1 does not use it (§5).
  - `POST /api/findings` takes findings from "any producer (a resident, a doctor check, a host hook)": `type` must end in `_finding`, `severity` is one of `info|low|medium|warning|high|critical`, and `fingerprint` dedups (with an optional `change_token` for emit-on-change). It persists to `audit.events` through `broadcast_event` and appears on `/ws/eisv`.
- **Readers.** `GET /v1/timeline` (#3045) and the Timeline tab (#3047) read `audit.events` by type. The live pane in `portal-live-timeline-v0.md` consumes `/ws/eisv`.

## 4. Design

A small observer polls GitHub for open and recently merged PRs whose body carries the `UNITARES agent:` footer, and emits one event per new fact:

| Event | Fields | Source |
|---|---|---|
| `pr_opened` / `pr_merged` / `pr_closed` | pr, repo, head_sha, agent_uuid | PR state |
| `ci_result` | pr, head_sha, workflow, conclusion, failed_jobs | check and workflow runs, once per head and workflow at completion |
| `review_record` | pr, head_sha, verdict, findings, disposed, reviewer | `unitares-review v1` comments |
| `claim_contradicted` | pr, head_sha, checkin_ts, excerpt, ci_conclusion | derived: an agent-authored check-in by the PR's agent within N minutes before a failing `ci_result` on that PR, where the excerpt (#3049) claims success. N and the claim pattern are telemetry settings, not verdicts (§7) |

Every event carries `agent_uuid` (the subject) separately from the observer's own id (the producer), and a fingerprint of `repo#pr@head_sha:event[:workflow]`, so re-polls dedup.

**Where it runs.** An optional member under `agents/github_observer/`, using public surfaces only (federation ring 2): read GitHub through the App runner, write through one UNITARES route. It is generic. Any install whose agents sign PRs can run it, and it never branches on agent or resident names. A deployment schedules it, a launchd job for the maintainer. It is never part of the server process.

**Write path: the open decision in §8.**
- **A.** Post through `POST /api/findings` as `github_ci_finding`, `github_review_finding` and so on. This needs no server change and gives live broadcast. It costs semantics: everything on that route reads as a finding (something wrong). That runs against §2, and a passing CI run is not a finding. Findings also reach the Discord bridge.
- **B.** A narrow route, `POST /v1/external/events`, gated by the operator credential and accepting only `github.*` types with a subject `agent_uuid`. It writes `audit.events` and broadcasts. It is one new route and one allow-list, with neutral semantics.

The recommendation is **B**. A is a usable interim if the operator prefers no server change.

**Reading.** `/v1/timeline` gains a seventh kind, `external`, and the pane draws these events as marks on the agent's row. A mistake renders as its path: a failing `ci_result` linked to the next passing one on the same PR, a `review_record` with its later disposition. `claim_contradicted` is drawn neutrally beside the check-in it refers to, never in alarm colours.

## 5. What it stays out of

- **EISV and the registered read.** No `audit.outcome_events` rows and no `/v1/harness/outcome` calls in stage 1. Adding CI or review results to the outcome stream would widen the label channel that `eisv-outcome-grounding-stop-rule-v0.md` forbids widening, and would be an instrument change under the 2026-10-15 data-boundary rule. Promotion to graded outcomes is a separate operator decision after the 12-01 read reports (§8).
- **Enforcement.** No pause, verdict, trust-tier or risk input. Events are a record for people and agents to read.
- **Wave 3 measured paths.** The observer calls no dialectic code. If option B is chosen, the new route calls `broadcast_event` as an existing function and does not modify it.
- **Writing to GitHub.** Read-only App token. The observer comments on nothing, labels nothing and merges nothing.

## 6. Stages

1. **Stage 1, this authorisation.** The observer with `pr_*`, `ci_result` and `review_record` events via the chosen write path, unit tests against recorded GitHub JSON fixtures, a dry-run mode that prints events without writing, and the `external` kind in `/v1/timeline`, read-only. The deployment schedule lives in the operator's deploy repo.
2. **Stage 2.** `claim_contradicted`, once #3049 excerpts are live and stage 1 has run for a week. The pattern and window are set from what the week shows.
3. **Stage 3, operator decision.** Derived measures per agent: escaped defects (a fix PR that cites an earlier merged PR), time to correction, and repeats of the same failing check. These are telemetry with no authority.
4. **Stage 4, operator decision after the 12-01 read.** Whether any of this becomes graded outcomes, with the instrument-change boundary the registered read requires.

## 7. Success criteria

- Every agent-signed PR in the window has its CI conclusions and review records on the agent's timeline row within one poll interval.
- Re-polling produces no duplicates: fingerprint dedup is tested.
- An unsigned PR, or one with a uuid unknown to UNITARES, is skipped and counted, never guessed at.
- Dry-run output matches what a write would store.
- No row lands in `audit.outcome_events`, and the registered-read manifest is untouched.
- A reader can follow one mistake from introduction to fix on the timeline without opening GitHub.

## 8. Open decisions for the operator

1. **Write path:** B (new narrow route, recommended) or A (reuse `/api/findings`).
2. **Poll interval and repositories:** the default is the repos the App installation covers, every 5 minutes.
3. **Escape definition for stage 3:** a fix PR citing a merged PR, a revert, or a reopened issue.
4. **Promotion to outcomes (stage 4):** after the 12-01 read, whether CI and review results become graded outcomes, and under what label rules.

## Review record

- 2026-10-23: drafted in the session that built #3045–#3050. Facts about `/api/findings`, `record_event` dedup, the review marker and `/v1/harness/outcome` were read from source on master that day.
