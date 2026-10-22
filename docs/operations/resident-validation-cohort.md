# Resident Validation Cohort

**Created:** June 14, 2026
**Last Updated:** October 20, 2026
**Status:** Experimental

---

## Purpose

Long-running residents validate UNITARES differently from cron probes. Scheduled checks provide execution receipts; resident tick records provide observations and prospective predictions for evaluating durable identity, heartbeat, calibration, and governed recovery over time. Producing a tick alone does not establish improvement.

This v0 cohort keeps residents low-authority. A resident may observe, emit findings, leave KG sediment, or request dialectic review. It may not deploy, merge, force-push, or roll back production by itself.

## V0 primitive

`scripts/diagnostics/resident_validation_tick.py` emits one `resident_validation_tick` envelope. Repeated identical inputs produce identical output when `--observed-at` is fixed; otherwise timestamps use the current time. The tick ID depends only on cohort ID, resident ID, and tick index. A supervisor, launchd job, BEAM process, or Hermes cron can call it repeatedly to form a long-running stream.

The tick includes:

- resident identity and role
- heartbeat cadence and next due timestamp
- prospective prediction plus confidence
- observation text
- bounded authority metadata
- stable tick id for dedupe and comparison

The same CLI can emit `process_agent_update` kwargs with `--process-update-kwargs`. It prints the payload; it does not call UNITARES, bind an identity, record an outcome, or persist server state. A submitting caller must supply its own session binding. `--state-path` optionally appends the selected payload to a local JSONL file.

## Validation roles

Suggested responsibilities (role names are metadata; the CLI does not enforce different permissions per role):

| Role | Purpose | Authority |
|---|---|---|
| `dogfood_probe` | Notice fresh friction and schema/runtime drift | observe, emit finding |
| `steward` | Route findings to issue/KG/dialectic layers | observe, emit finding, leave KG note, request dialectic |
| `builder` | Attempt small bounded tasks with prospective predictions | observe, emit finding; no merge/deploy |
| `reviewer` | Evaluate resident outputs and policy ambiguity | observe, request dialectic |

## Boundary

This primitive creates one measurement record. It neither supervises a process nor enforces external effects. `ResidentProfile` rejects deploy authority and the forbidden effect names `deploy`, `merge`, `force_push`, and `rollback`; its default allowed-effect metadata is shared across all four roles. Supervisors and effect custodians remain responsible for actual scheduling, leases, revocation, and authorization. The CLI does not establish which supervisors are deployed.

## Smoke command

```bash
python3 scripts/diagnostics/resident_validation_tick.py \
  --cohort-id rv-2026-06 \
  --resident-id resident-dogfood-1 \
  --resident-name 'Resident Dogfood Canary' \
  --role dogfood_probe \
  --cadence-seconds 600 \
  --tick-index 1 \
  --observed-at 2026-10-13T00:00:00Z \
  --observation 'No actionable friction observed.' \
  --prediction 'Next tick remains bounded and non-mutating.' \
  --confidence 0.72
```

For UNITARES write-shape smoke:

```bash
python3 scripts/diagnostics/resident_validation_tick.py \
  --process-update-kwargs \
  --cohort-id rv-2026-06 \
  --resident-id resident-steward-1 \
  --resident-name 'Resident Steward Canary' \
  --role steward \
  --cadence-seconds 900 \
  --tick-index 1 \
  --observed-at 2026-10-13T00:00:00Z \
  --observation 'Finding queue inspected; no mutation required.' \
  --prediction 'No dialectic escalation should be needed in the next horizon.' \
  --confidence 0.68
```

## Supervised invocation

The supervised invocation layer turns the resident validation canary runner into a
safe recurring-call target for cron, launchd, or a future BEAM/Plexus
supervisor. It is still intentionally non-actuating: it appends local JSONL tick
state and a local invocation audit row, but it does not submit UNITARES process
updates, open GitHub issues, request dialectic, merge, deploy, force-push, or
roll back anything.

This is the layer between:

1. the one-tick measurement contract (`resident_validation_tick`, above),
2. the stateful local canary stream (`resident_validation_canary`), and
3. a future durable runtime supervisor with leases, revocation, outcome events,
   and governed-effect custody.

### What it owns

`scripts/diagnostics/resident_validation_supervised_invocation.py` owns only:

- a local lock file so overlapping invocations do not run concurrently,
- a per-run maximum tick count,
- local tick JSONL append through `src.evaluation.resident_validation.runner`,
- a local invocation audit JSONL row,
- a privacy-safe stdout acknowledgement for scheduler logs.

The public stdout shape is deliberately constant:

```json
{"event_type":"resident_validation_supervised_invocation","status":"state_appended"}
```

If the local lock is held, stdout remains non-sensitive and the process exits
with `75` (`EX_TEMPFAIL`-style retry hint):

```json
{"event_type":"resident_validation_supervised_invocation","status":"lock_held"}
```

### What it does not own

This layer does not own durable governance truth. UNITARES remains responsible
for identity, EISV trajectory, KG, dialectic, calibration, and outcome records.
A later adapter may submit selected local tick/audit rows through governed write
paths, but this CLI does not do that itself.

It also does not own hot runtime governance beyond the local lock. BEAM/Plexus
remains the future home for supervised processes, leases, revocation, and
governed-effect custody.

### Supervised smoke command

```bash
python3 scripts/diagnostics/resident_validation_supervised_invocation.py \
  --cohort-id rv-2026-06 \
  --resident-id resident-dogfood-1 \
  --resident-name 'Resident Dogfood Canary' \
  --role dogfood_probe \
  --cadence-seconds 600 \
  --observation 'No actionable friction observed.' \
  --prediction 'Next supervised tick remains bounded and non-mutating.' \
  --confidence 0.72 \
  --count 1 \
  --max-ticks-per-run 1 \
  --state-path data/resident_validation/canary.jsonl \
  --lock-path data/resident_validation/supervised.lock.json \
  --audit-path data/resident_validation/supervised_invocations.jsonl
```

### Scheduler boundary

A scheduler may call this CLI on a cadence, but the scheduler is not itself the
resident. The resident identity is the profile named in the tick stream; the
scheduler is only the timer/process harness.

Recommended initial cadence is conservative, for example 10-15 minutes, until
there is enough tick history to evaluate continuity and calibration without
creating noisy local state.

## Verification

Rechecked on October 13, 2026 against
`scripts/diagnostics/resident_validation_tick.py` and
`src/evaluation/resident_validation/model.py`. Both examples above print JSON
without server writes. Existing tests in
`tests/evaluation/resident_validation/` cover envelope construction, authority
bounds, CLI output, and the related runner/invocation contracts.
