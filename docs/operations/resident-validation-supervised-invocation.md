# Resident Validation Supervised Invocation

**Created:** June 14, 2026
**Last Updated:** June 14, 2026
**Status:** Experimental

---

## Purpose

The supervised invocation layer turns the resident validation canary runner into a
safe recurring-call target for cron, launchd, or a future BEAM/Plexus
supervisor. It is still intentionally non-actuating: it appends local JSONL tick
state and a local invocation audit row, but it does not submit UNITARES process
updates, open GitHub issues, request dialectic, merge, deploy, force-push, or
roll back anything.

This is the layer between:

1. the one-tick measurement contract (`resident_validation_tick`),
2. the stateful local canary stream (`resident_validation_canary`), and
3. a future durable runtime supervisor with leases, revocation, outcome events,
   and governed-effect custody.

## What it owns

`scripts/diagnostics/resident_validation_supervised_invocation.py` owns only:

- a local lock file so overlapping invocations do not run concurrently,
- a per-run maximum tick count,
- local tick JSONL append through `resident_validation_runner`,
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

## What it does not own

This layer does not own durable governance truth. UNITARES remains responsible
for identity, EISV trajectory, KG, dialectic, calibration, and outcome records.
A later adapter may submit selected local tick/audit rows through governed write
paths, but this CLI does not do that itself.

It also does not own hot runtime governance beyond the local lock. BEAM/Plexus
remains the future home for supervised processes, leases, revocation, and
governed-effect custody.

## Smoke command

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

## Scheduler boundary

A scheduler may call this CLI on a cadence, but the scheduler is not itself the
resident. The resident identity is the profile named in the tick stream; the
scheduler is only the timer/process harness.

Recommended initial cadence is conservative, for example 10-15 minutes, until
there is enough tick history to evaluate continuity and calibration without
creating noisy local state.
