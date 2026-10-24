# GitHub outcome observer, v0 (timeline-only stage)

**Created:** 2026-10-23
**Status:** Draft, design only. Nothing is built. Written under two operator directions of 2026-10-23: the live portal (KG `2026-10-23T11:39:51.328628+00:00`) and "mistakes are not negative by default; failure signals are never a kill gate" (KG `2026-10-23T12:51:38.289377+00:00`). Merging records the design; it authorises no build by itself. Stage 1 (§6) starts only after the operator rules on the write path (§8.1), and then covers stage 1 only. The stage 1 build is a `capability` PR by what it contains. Under option B it adds an operator-credential route, which sits in the operator-reserved auth column, so it stays a draft until that ruling.
**Related:** `portal-live-timeline-v0.md` (the pane that shows these events), PR #3045 (`GET /v1/timeline`), PR #3049 (check-in report excerpts), `eisv-outcome-grounding-stop-rule-v0.md` (the registered read this stays out of).

## 1. Problem

Agents report their own work through check-ins. The evidence about that work mostly lives on GitHub: CI results on the PR, review records from the review gate, and whether the change merged. None of it reaches UNITARES today unless an agent records it by hand.

The 2026-10-23 session that built #3045–#3050 shows the gap. UNITARES recorded 45 check-ins for that agent up to 12:53 UTC. 44 were hook readings of turn shape (`substrate_interpretation`) and one was the agent's own report. All of them read `approve`, risk 0.00–0.29. The PR record for the same hours holds 22 review findings fixed before merge, one CI failure (a 1400-byte budget breach), and one corrected claim about a local test run. A reader of UNITARES alone would see a quiet session. A reader of GitHub would see the real one.

Neither hook readings nor self-report carry that evidence. Self-report is the right source for claims and intent. It is the wrong source for evidence about mistakes: an agent cannot report a mistake it does not know it made. Evidence should arrive on its own, and a mistake should show up as a contradiction between a claim and a result, not depend on a confession.

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
- **Structured review records.** `scripts/dev/review_gate.py` posts PR comments that open with `<!-- unitares-review v1 key=… verdict=CLEAN|FINDINGS|FAILED findings=N disposed=0|1 reviewer=… head=… -->` (marker `unitares-review v1`, `review_gate.py` `MARKER`). `FAILED` means the review was unavailable or incomplete. `disposed` is a flag, not a count. The reviewed commit is the separate `head=` attribute, which legacy records lack. A disposition is a new record with the same key and head as the review it answers. The gate admits a marker comment only from an owner, member or collaborator, or from the registered App (`trusted_review_comment`); a pasted marker from anyone else is ignored.
- **CI.** GitHub check runs and workflow runs per PR head, each with a run id and attempt number, readable with any GitHub token that can read the repository.
- **Ingestion routes.**
  - `POST /v1/harness/outcome` writes `audit.outcome_events` under an operator-asserted `agent_uuid`. It feeds outcome analysis, so stage 1 does not use it (§5).
  - `POST /api/findings` takes findings from "any producer (a resident, a doctor check, a host hook)": `type` must end in `_finding`, `severity` is one of `info|low|medium|warning|high|critical`, and `fingerprint` dedups within a 30-minute in-memory window that a server restart clears (with an optional `change_token` for emit-on-change). It persists to `audit.events` through `broadcast_event` and appears on `/ws/eisv`.
- **Readers.** `GET /v1/timeline` (#3045) serves six kinds. Lifecycle, knowledge and lineage come from `audit.events` by type, which is the path a new kind would extend; check-ins come from `core.agent_state`, outcomes from outcome events and dialectic from session rows. Check-in excerpts on `/v1/timeline` are `portal-live-timeline-v0.md` §4 item 1, a separate PR. The Timeline tab (#3047) is in review. The live pane in `portal-live-timeline-v0.md` consumes `/ws/eisv`, and its adapter maps only the frame types it lists.

## 4. Design

A small observer polls GitHub for open and recently merged PRs whose body carries the `UNITARES agent:` footer, and emits one event per new fact. Every type is `github.*`:

| Event | Fields | Fingerprint |
|---|---|---|
| `github.pr_opened` / `github.pr_merged` / `github.pr_closed` / `github.pr_reopened` | pr, repo, head_sha, agent_uuid | the open is `repo#pr:opened` (opening is not a GitHub timeline event); each later change is `repo#pr:state:<GitHub timeline event id>`, so a close, reopen and second close are each their own fact. The `closed` event that accompanies a merge is folded into `pr_merged`. The stage 1 GitHub JSON fixtures confirm both API facts |
| `github.ci_result` | pr, head_sha, workflow or app, run or check id, attempt where the source has one, conclusion, failed_jobs | Actions: `repo#pr:ci:run_id:attempt`; other check apps: `repo#pr:check:check_run_id:conclusion`, because an app may rewrite a completed check run with a new conclusion. Emitted at completion, so a re-run on the same head is its own fact. The repository's own `review` check is skipped: `review_record` already carries what it reports |
| `github.review_record` | pr, head_sha (from `head=`, else null), key, verdict, findings, disposed, reviewer | `repo#pr:review:<comment id>`: every record comment is its own fact, so a disposition, a second model family on the same head, a FAILED record and its retry, and a re-run on an unchanged diff all stay. Only comments that pass the gate's `trusted_review_comment` rule are read. The fields are the marker's raw values; the gate's own reading adjusts some (a FAILED record with partial findings counts as FINDINGS, a disposition without complete entries as undisposed), so the event says `raw: true` and the reader applies the gate's rules or links to the gate's check |
| `github.claim_checked` | pr, head_sha, checkin_state_id, excerpt, ci_conclusion, status (`open` or `closed`), closed_by | derived (stage 2): an agent-authored check-in by the PR's agent, its excerpt claiming success, paired with the next CI result on that PR. A pair that agrees is not emitted. A pair that disagrees opens, and closes when a later agent check-in or PR-body edit corrects the claim or a later run on the PR passes. Only a pair still open when the PR merges is the "not corrected" case of §2. Fingerprint `repo#pr:claim:<checkin_state_id>:<status>`, so the opening and the closing are two facts. The window and the claim pattern are telemetry settings, not verdicts |

Every event carries `agent_uuid` (the subject) separately from the observer's own id (the producer). Dedup must be durable: the observer keeps the set of fingerprints it has emitted in its own state store, and under option B the route also refuses a repeated fingerprint. The server's 30-minute in-memory window is not relied on. Without durable dedup, a 5-minute poll would write each fact again every 30 minutes and after every restart, each time as a permanent `audit.events` row.

**Where it runs.** An optional member under `agents/github_observer/`, public surfaces only (`federation-component-boundaries-v0.md`): it reads GitHub with a token the deployment supplies and writes through one UNITARES route. It never reads core tables by SQL. It is generic: any install whose agents sign PRs can run it with its own token, and it never branches on agent or resident names. The maintainer's deployment supplies its App runner's token. A deployment schedules it, a launchd job for the maintainer. It is never part of the server process.

**Write path: the open decision in §8.**
- **A.** Post through `POST /api/findings` as `github_ci_finding`, `github_review_finding` and so on. This needs no server change. It costs semantics: everything on that route reads as a finding (something wrong). That runs against §2, and a passing CI run is not a finding. Findings also reach the Discord bridge, and the live pane discards `*_finding` frames, so A gives no live view without a further pane change.
- **B.** A narrow route, `POST /v1/external/events`, gated by the operator credential and accepting only `github.*` types with a subject `agent_uuid`. It writes `audit.events`, refuses a repeated fingerprint, and broadcasts. It is one new route and one allow-list, with neutral semantics. The credential gate makes it an operator-reserved auth decision.

The recommendation is **B**. A is a usable interim if the operator prefers no server change.

**Reading.** `/v1/timeline` gains a seventh kind, `external`, read from `audit.events` like lifecycle and knowledge. Once the pane exists (`portal-live-timeline-v0.md` M1, unbuilt as of this draft), its frame→mark adapter (§3.3) gains a mapping for `github.*` frames, with cases in the shared parity fixture (§3.1). That proposal's §3.7 calls the adapter replaceable, so the mapping lives in whatever replaces it. Both views draw these events as marks on the agent's row. A mistake renders as its path: a failing `ci_result` linked to the next passing run on the same PR, a `review_record` with its later disposition, a `claim_checked` pair with whatever closed it. Nothing is drawn in alarm colours.

## 5. What it stays out of

- **EISV and the registered read.** No `audit.outcome_events` rows and no `/v1/harness/outcome` calls in stage 1. The 12-01 read's data boundary is already fixed at 2026-10-15T13:45Z (`eisv_ablation_matrix.py`), so rows written now cannot enter it either way. The reason to stay out is what comes after: the stop rule treats labels from a new trusted verification source as a possible new label channel that needs its own registration, and outcome rows also feed calibration and the unregistered instruments. Promotion to graded outcomes is a separate operator decision after the 12-01 read reports (§8).
- **Enforcement.** No pause, verdict, trust-tier or risk input. Events are a record for people and agents to read.
- **Wave 3 measured paths.** The observer calls no dialectic code. If option B is chosen, the new route calls `broadcast_event` as an existing function and does not modify it.
- **Writing to GitHub.** A read-only token. The observer comments on nothing, labels nothing and merges nothing.

## 6. Stages

1. **Stage 1, after the §8.1 ruling.** The observer with `pr_*`, `ci_result` and `review_record` events via the chosen write path, durable dedup, the trusted-comment filter, unit tests against recorded GitHub JSON fixtures, a dry-run mode that prints events without writing, the `external` kind in `/v1/timeline` (read-only), and the pane adapter mapping if the pane has landed by then (otherwise it follows with the pane). The deployment schedule lives in the operator's deploy repo.
2. **Stage 2.** `claim_checked`, once `/v1/timeline` returns check-in excerpts (`portal-live-timeline-v0.md` §4 item 1) and stage 1 has run for a week. The observer reads excerpts there, never from the database. Its input is agent-authored check-ins only, which were 1 of 45 rows in the motivating session, so its yield depends on agents reporting for themselves. The pattern and window are set from what the week shows.
3. **Stage 3, operator decision.** Derived measures per agent: escaped defects (a fix PR that cites an earlier merged PR), time to correction, and repeats of the same failing check. These are telemetry with no authority.
4. **Stage 4, operator decision after the 12-01 read.** Whether any of this becomes graded outcomes, registered as a new label channel under the stop rule's terms.

## 7. Success criteria

- Every agent-signed PR in the window has its CI conclusions and review records on the agent's timeline row within one poll interval.
- Re-polling produces no duplicates, including across an observer or server restart: durable fingerprint dedup is tested, and a CI re-run and a review disposition on the same head each appear once.
- A marker comment from an untrusted author is ignored.
- An unsigned PR, or one with a uuid unknown to UNITARES, is skipped and counted, never guessed at.
- Dry-run output matches what a write would store.
- No row lands in `audit.outcome_events`, and the registered-read manifest is untouched.
- A reader can follow one mistake from introduction to fix on the timeline without opening GitHub.

## 8. Open decisions for the operator

1. **Write path:** B (new narrow route with an operator-credential gate, recommended) or A (reuse `/api/findings`). Stage 1 waits on this ruling.
2. **Poll interval and repositories:** the default is the repos the supplied token covers, every 5 minutes.
3. **Escape definition for stage 3:** a fix PR citing a merged PR, a revert, or a reopened issue.
4. **Promotion to outcomes (stage 4):** after the 12-01 read, whether CI and review results become graded outcomes, and under what label rules.

## Review record

- 2026-10-23: drafted in the session that built #3045–#3050. Facts about `/api/findings`, `record_event` dedup, the review marker and `/v1/harness/outcome` were read from source on master that day.
- 2026-10-24: one independent fresh-context review (Claude subagent) checked §3 against source and found 13 issues, all addressed in this revision: durable dedup instead of the server's 30-minute window; per-run and per-record fingerprints; the full marker grammar and the trusted-comment filter; the excerpt read named as stage 2's precondition; §1's check-in counts split by author; `claim_contradicted` replaced by a neutral `claim_checked` pair that closes on correction; the pane adapter added to stage 1; the authorisation tied to the §8.1 ruling; §5's stop-rule reason corrected; the reader list corrected; event names unified under `github.*`; and a generic token input instead of the maintainer's runner.
- 2026-10-24: the same reviewer's full second pass confirmed all 13 fixed and found 7 more (none P0/P1), addressed here: review records fingerprinted by comment id with their fields marked raw; the index row matched to the new status line; the event table's header repaired; PR state, non-Actions checks and `claim_checked` given complete fingerprints; the token wording made generic; the pane dependency and section references corrected; and the capability class stated by content, with the ruling governing only draft status.
- 2026-10-24: a targeted third pass confirmed the 7 fixed and found 2 keying gaps (P3), addressed here: a rewritten check run keyed by its conclusion, with the `review` check skipped, and PR-open and merge-close keying stated.
