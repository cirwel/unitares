# Agent Orchestrator: Durable Spawn Idempotency

The agent orchestrator accepts an optional `Idempotency-Key` on
`POST /v1/agents`. Migration 068 gives keyed spawns a PostgreSQL replay window
that survives orchestrator restarts. The window is at least 24 hours and grows
to cover a caller-requested longer runtime plus the result-retention window.

## Deployment order

1. Apply `db/postgres/migrations/068_agent_orchestrator_idempotency.sql` through
   the normal repository migration workflow.
2. Configure one of these URLs, in precedence order:
   `AGENT_ORCHESTRATOR_DATABASE_URL`, `GOVERNANCE_DATABASE_URL`, or
   `DB_POSTGRES_URL`.
3. Restart the agent orchestrator.
4. Call authenticated `GET /v1/health` and require:
   `idempotency.backend = "postgres"`, `idempotency.durable = true`, and
   `idempotency.available = true`.

The orchestrator uses the existing governance Postgres database and a dedicated
`orchestration` schema. Do not create another database or migration system.

If the URL, connection, or migration is absent, keyed spawns return
`idempotency_unavailable` and no process starts. Unkeyed spawns retain their
existing behavior. There is no silent Redis or in-memory production fallback.

## What is stored

`orchestration.spawn_idempotency` contains only:

- the SHA-256 hash of the caller's idempotency key;
- the SHA-256 digest of the canonical material spawn specification;
- the server-minted execution ID;
- `reserved` or `started` state; and
- reservation, start, and expiry timestamps.

It never stores the raw key, command, arguments, environment, secrets, or
captured output. An indexed sweep removes expired rows.

## Replay outcomes

| Durable row | Retry result | Why |
|---|---|---|
| No row / expired row | Reserve a new execution ID and spawn | This caller owns the new reservation. |
| Same digest, `started` | Return the original execution ID | The process was successfully opened before the earlier response was lost. |
| Different digest | `idempotency_conflict` | One key cannot name two materially different requests. |
| Same digest, `reserved` | `idempotency_outcome_unknown` | A crash may have occurred between database reservation and OS process start; duplicating the process is unsafe. |

Postgres and an OS process cannot participate in one atomic transaction. The
`reserved` response is therefore deliberately conservative: it preserves
at-most-once safety when the start outcome cannot be proven. The execution ID
is returned with the error for diagnosis.

Restart replay does not resurrect a terminated process and does not make its
captured output durable. It guarantees that a retry does not silently mint a
second execution for the same retained key and material request.
