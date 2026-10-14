# Resident Validation Cohort

**Created:** June 14, 2026
**Last Updated:** October 13, 2026
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

## Verification

Rechecked on October 13, 2026 against
`scripts/diagnostics/resident_validation_tick.py` and
`src/evaluation/resident_validation/model.py`. Both examples above print JSON
without server writes. Existing tests in
`tests/evaluation/resident_validation/` cover envelope construction, authority
bounds, CLI output, and the related runner/invocation contracts.
