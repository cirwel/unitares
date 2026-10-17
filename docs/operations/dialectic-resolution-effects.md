# Dialectic resolution-effect recovery

A resolved dialectic decision and the runtime change it authorizes are separate
facts. The terminal session row records the decision. The durable
`coordination.dialectic_resolution_effects` row records whether the runtime
change is confirmed applied. Do not infer the second fact from the first.

## Find work without changing it

Run the read-only inventory from the repository:

```bash
python3 scripts/ops/dialectic_resolution_effects.py
python3 scripts/ops/dialectic_resolution_effects.py --json
```

By default, the command uses the same `DB_POSTGRES_URL` setting as the server,
then the legacy `GOVERNANCE_DATABASE_URL`, then the local development DSN. Use
`--dsn` only for an explicit target.

Use `--state pending`, `--state applying`, or `--state unknown_partial` to
filter. The command opens a server-enforced read-only transaction and reports:

- state age and attempt count;
- the exact claimant token, when one exists;
- the last recorded error;
- terminal-session status and phase; and
- both durable paused-agent status copies plus whether the current pause token
  still matches the effect's bound generation.

The inventory is a queue, not proof of what happened in the runtime. Re-read an
individual row through the operator-only action before changing it:

```text
dialectic(action='reconcile_effect', session_id='SESSION_ID')
```

Keep owner and pause tokens in operator channels. Do not copy them into issues,
pull requests, shared knowledge, or general logs.

## State-specific procedure

### `pending`

No worker has claimed the effect. Confirm the terminal decision, participant
identities, bound pause generation, and current paused-agent state. If they are
consistent, explicitly apply it:

```text
dialectic(action='reconcile_effect', session_id='SESSION_ID',
          effect_disposition='apply_pending')
```

This is an execution request, not an acknowledgement. A failed or interrupted
execution becomes `unknown_partial`; do not retry it automatically.

### `applying`

Treat the effect as possibly partly applied. Never replay it. First establish
that the claimant process is no longer running or able to finish. Inspect the
row again and use the exact owner token from that current response—not a saved
or truncated token—to fence that one claim:

```text
dialectic(action='reconcile_effect', session_id='SESSION_ID',
          effect_disposition='mark_uncertain',
          effect_owner_token='EXACT_CURRENT_OWNER_TOKEN',
          reason='claimant stopped; verified by ...')
```

The compare-and-set refuses a changed token or state. A successful fence moves
the row to `unknown_partial`; it does not undo or finish any runtime mutation.

### `unknown_partial`

Inspect the agent's live and durable state, the terminal conditions, and every
runtime mutation those conditions could make. Repair or complete the live state
manually. Only then close the record:

```text
dialectic(action='reconcile_effect', session_id='SESSION_ID',
          effect_disposition='mark_applied',
          effect_updated_at='EXACT_UPDATED_AT_FROM_INSPECT',
          reason='verified/repaired ...; evidence ...')
```

`mark_applied` is an operator attestation supported by the required reason. It
is not machine proof that all conditions ran, and it does not execute or replay
the effect. Copy `effect_updated_at` from the exact `unknown_partial` inspect
response used for the repair; a newer row version is refused even if it is also
`unknown_partial`. If completion cannot be established, leave the row
`unknown_partial` and escalate rather than manufacturing certainty.

## Before closing any row

Verify all of the following:

1. The session is terminal with the decision and participants you intend to
   honor.
2. The paused-agent and identity status copies agree.
3. The live pause generation matches the effect's `pause_token`, or any mismatch
   is understood and included in the repair record.
4. For `applying`, the exact claimant is stopped and fenced before repair.
5. The reason states what was observed and what was repaired; it does not merely
   say "looks good."

Re-run the inventory after action. An empty result means no non-applied effect
rows remain; it does not certify unrelated agent or telemetry state.
