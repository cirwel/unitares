# Automation and self-improvement readiness audit — 2026-10-14

## Scope and verdict

This audit starts from `unitares-automations census`, then checks the local
schedulers, process state, logs, PostgreSQL receipts, the Project Mark forecast
trial contract, and the registered self-improvement evaluation.

**Verdict:** the server is healthy, but the automation control plane is not
ready to operate an adaptive autonomous fix loop. It is ready for a bounded
fixed-policy plumbing pilot after the observability and ownership prerequisites
below are closed. No actuator, new tool, instrument, model channel, experiment,
or cohort is authorized by this document.

The 2026-10-09 through 2026-10-22 feature freeze permits the census and canary
fixes in this change: they correct existing observability and test behavior.
The freeze continues to bar new capabilities and an RSI actuator.

## Evidence snapshot

- Initial census: 329 rows; 67 launchd rows; five attention rows.
- After removing two already-retired/dead launchd definitions: 327 rows and
  65 launchd rows. Removing the retired Codex release-notes schedule leaves 326
  rows and one active launchd scheduler for that work.
- Live governance `health_check`: fresh cached snapshot, 11 healthy subsystem
  checks, no degraded or failing subsystem. This endpoint does not inspect
  launchd jobs and therefore does not contradict the scheduler failures below.
- Local scheduler ground truth came from `launchctl list` and
  `launchctl print-disabled`; logs were read from each plist's configured
  stdout/stderr path. `launchctl print` is deliberately not used because it
  exposes job environment values.
- The current local automation override file classifies only three rows. Of 38
  `com.unitares.*` launchd rows after cleanup, 35 are active and all 35 have no
  `owner` or `gate:*` classification. The two classified rows are paused.

## Findings

### Misleading health and stale state

1. `com.unitares.checkin-phase-analysis` was advertised as active and running.
   It was not loaded, its recorded PID was gone, and its last log write was
   2026-09-29. The script describes itself as a self-removing one-shot for PR
   #133; booting itself out interrupted the wrapper before the terminal receipt
   and plist removal completed.
2. `com.unitares.lumen-heartbeat-provisioned` was advertised with a failed
   receipt from 2026-09-11. Launchd's latest result is exit 0. The old wrapper
   receipt was stale and must not overrule the scheduler.
3. `com.unitares.dialectic-canary` had no census health despite launchd exit 1.
   Its logs show repeated successful review creation followed by a false DB
   ground-truth failure. `audit.tool_usage.session_id` now stores a keyed
   `csid:` digest; the canary still queried with the bearer client-session ID.
4. `com.unitares.model-adjudicator` had no census health despite launchd exit 2.
   Its configured script no longer exists. The feature and launchd template
   were intentionally removed in PR #2546, whose deployment note explicitly
   requires unloading this job.

The census fix reconciles plist configuration with loaded/disabled state,
records scheduler provenance and latest exit, detects a provably missing
command, rejects contradictory old receipts, and surfaces dead `running`
receipts whose definitions disappeared.

### Stale jobs and duplicate schedulers

- The retired model adjudicator plist, the incomplete self-removing check-in
  analysis plist, and their two stale `running` receipts were removed from the
  live surfaces. The files were moved to Trash, so the cleanup is recoverable.
- The paused Codex weekly-release-notes schedule was the retired predecessor of
  the active launchd job. Its schedule file was moved to Trash while its output
  and memory files were preserved. The launchd job is loaded, last exit 0, and
  wrote a successful 2026-10-09 result.
- Two active dispatch-BEAM labels share a launcher but have different backend,
  node and state environment keys. They are separate resident processes, not a
  confirmed duplicate scheduler.
- Three paused launchd jobs remain intentionally installed: the registered-read
  discipline keeps `eisv-skeptic-trend` off; `lumen-qa` is held behind its
  replacement-runner machine gate; `review-sweep` is disabled. The review-sweep
  override says to resume after the review-budget policy merges, and PR #2596
  merged on 2026-10-01. Resumption can spend model budget, so the stale note is
  an operator decision, not an automatic cleanup action.

### Broken ownership

All 35 active `com.unitares.*` launchd rows lack an accountable owner and gate
classification in the operator override. This does not mean every job is
unsafe; it means the registry cannot answer who owns a failure or what evidence
closes it. Owner and gate assignment is operator judgment. The audit does not
fabricate those values.

### Forecast trial readiness

The local Project Mark log has 59 resolved data-gate forecasts and 65 resolved
Opus-gate forecasts. The Opus lane has both outcomes and exceeds its registered
minimum of 30. Its Brier score is 0.1102 versus 0.1690 for the in-sample base
rate. The data-gate forecasts do not beat that baseline (0.2450 versus 0.2413).
These are descriptive local-log results, not a UNITARES-value finding.

Of 65 resolved Opus lines, 64 name a UNITARES prediction. All 64 prediction IDs
have durable outcome bindings and landed within one hour (maximum 37.9 minutes),
but only 52 use the exact registered trial tag. Three use the wrong outcome
type, one is calibration-excluded, and four carry at least one forbidden detail
field from the concept/seed/session/path family. Those rows are preserved as
audit evidence but excluded from any clean protocol analysis. No durable record
yet shows that a decision changed because of a UNITARES computation unavailable
from the local JSONL control. The preregistered default is therefore no added
value unless the final read demonstrates such a decision change.

The Project Mark task-development pause of 2026-10-13 remains in force. This
audit adds no model run or trial observation.

### Registered adaptive-versus-fixed evaluation

`docs/proposals/registered/self-improvement-loop-evaluation-v0.md` is
registered but unenrolled. It authorizes no experiment or deployment. The
primary estimand is adaptive minus fixed held-out success under matched budgets;
the no-loop arm cannot establish adaptation. There is currently no immutable
enrollment artifact, frozen corpus, arm isolation, assignment seed, scorer
digest, intervention budget, receipt store, or blinded evaluator. Lane O
operational closure may be piloted, but no self-improvement claim is available.

## Dependency-ordered plan

1. **Finish observation integrity.** Land and deploy the census reconciliation
   and keyed-session canary fix. Require one scheduled canary exit 0 and one
   census snapshot that reports it. Keep `health_check` and automation health
   separate in operator views.
2. **Assign accountability before autonomy.** The operator classifies every
   active UNITARES job with one principal, one `gate:*` class, and a concrete
   verifier/output. Resolve the stale review-sweep decision explicitly. No loop
   consumes unowned jobs.
3. **Close the forecast trial without adding observations.** At or after the
   registered end date, freeze hashes of the local log and the joined outcome
   rows; exclude the four detail-violating rows, the three outcome-type
   violations, and the calibration-excluded row under a written, non-shopping
   rule. Report receipt coverage and whether any UNITARES-only computation
   actually changed a decision. If none did, record the registered negative.
4. **Pilot only a fixed, bounded closure loop.** Inputs are existing failed CI,
   canary, or census receipts. Freeze a small action library (diagnose, add a
   regression test, patch an existing defect, update existing docs, or no-op),
   ordering, thresholds, one-issue/one-branch scope, attempt limit, compute
   budget, wall-clock limit, and rollback rule. Every attempt produces the six
   registered receipts: failure, disposition, persistent update, deployment,
   held-out read, and effect summary. It may open an owned draft PR; it may not
   merge directly or change its own policy.
5. **Demonstrate Lane O before enrollment.** On non-held-out maintenance
   fixtures, show complete receipts, deterministic stop behavior, no cross-task
   memory, no safety-surface edits, and no direct deployment. Fail closed on a
   missing owner, missing external outcome, ambiguous scope, exhausted budget,
   or review/CI failure.
6. **Create a separate immutable enrollment artifact.** Only the operator may
   enroll the registered N/F/A evaluation. Freeze every field in its enrollment
   gate: baseline, corpus and eligibility, held-out manifests and scorer,
   sample size/read date, assignment/bootstrap/randomization seeds, fixed and
   adaptive policy digests, matched intervention budgets, reviewer instructions,
   receipt store, safety stops, evaluator, and publication owner.
7. **Run fixed before adaptive, then preserve isolation.** Validate N and F
   plumbing without reading confirmatory scores. Start A only after F and A
   share the same action mechanism, review gate, model/provider/toolset and
   budget, and only A's predeclared action selection may update from discovery
   outcomes. No prompt, commit, memory, finding, or transcript crosses arms.
8. **One confirmatory read.** Use the registered paired family statistic,
   bootstrap and sign-flip test. Any leak, budget mismatch, missing receipt or
   safety stop invalidates the causal cohort. Report adaptive underperformance
   plainly when the point estimate is negative.

## Immutable safety surfaces

The bounded loop and any future evaluation must treat these as immutable:

- the feature freeze through 2026-10-22 and the ban on creating an RSI actuator,
  new tool/action/alias, instrument, or model channel;
- authentication, identity enforcement, governance-sensitive paths, migration
  slots, registered protocols, stop rules, thresholds, and their read dates;
- model/provider/tool permissions, paid-compute authority, review policy,
  protected branch/merge-queue policy, surface claims, and destructive database
  prohibitions;
- held-out manifests, external scorers, seeds, sample size, arm budgets,
  assignment, isolation boundary, receipt schema, safety stops, and the single
  confirmatory read;
- Project Mark challenger isolation, its fixed content-free check-in text, the
  ban on task/concept/source/trap/prompt/file/lane identifiers and platform
  scores entering UNITARES, and the rule that challenger children receive no
  UNITARES environment or capabilities;
- EISV, coherence, confidence, activity counts, and governance verdicts remain
  telemetry. None may be an endpoint, treatment assignment, merge authority, or
  substitute for externally scored outcomes.
