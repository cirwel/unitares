# Resident Validation Cohort

**Created:** June 14, 2026
**Last Updated:** June 14, 2026
**Status:** Experimental

---

## Purpose

Long-running residents validate UNITARES differently from cron probes. Cron can prove that scheduled checks run; residents can prove whether durable identity, heartbeat, prediction, outcome recording, calibration, and governed recovery improve over time.

This v0 cohort keeps residents low-authority. A resident may observe, emit findings, leave KG sediment, or request dialectic review. It may not deploy, merge, force-push, or roll back production by itself.

## V0 primitive

`scripts/diagnostics/resident_validation_tick.py` emits one deterministic `resident_validation_tick` envelope. A supervisor, launchd job, BEAM process, or Hermes cron can call it repeatedly to form a long-running stream.

The tick includes:

- resident identity and role
- heartbeat cadence and next due timestamp
- prospective prediction plus confidence
- observation text
- bounded authority metadata
- stable tick id for dedupe and comparison

The same CLI can emit `process_agent_update` kwargs with `--process-update-kwargs`; this is the handoff shape for UNITARES calibration and trajectory storage.

## Validation roles

Recommended first cohort:

| Role | Purpose | Authority |
|---|---|---|
| `dogfood_probe` | Notice fresh friction and schema/runtime drift | observe, emit finding |
| `steward` | Route findings to issue/KG/dialectic layers | observe, emit finding, leave KG note, request dialectic |
| `builder` | Attempt small bounded tasks with prospective predictions | observe, emit finding; no merge/deploy |
| `reviewer` | Evaluate resident outputs and policy ambiguity | observe, request dialectic |

## Boundary

This primitive is not itself a resident supervisor. It is the one-tick measurement contract that a supervisor can call. BEAM/Plexus remains the preferred future lane for true supervision, leases, revocation, and governed effect custody; UNITARES remains the durable truth layer for identity, EISV, KG, dialectic, calibration, and outcomes.

## Smoke command

```bash
python3 scripts/diagnostics/resident_validation_tick.py \
  --cohort-id rv-2026-06 \
  --resident-id resident-dogfood-1 \
  --resident-name 'Resident Dogfood Canary' \
  --role dogfood_probe \
  --cadence-seconds 600 \
  --tick-index 1 \
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
  --observation 'Finding queue inspected; no mutation required.' \
  --prediction 'No dialectic escalation should be needed in the next horizon.' \
  --confidence 0.68
```
