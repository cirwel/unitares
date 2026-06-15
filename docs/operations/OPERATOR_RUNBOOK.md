# Unitares Operator Runbook

Status: live operator guide. Use for local operational procedures and triage, not for architecture truth.

This is the simplest local operator path for running Unitares governance on the Mac host.

## Start

From the repo root:

```bash
./scripts/ops/start_with_deps.sh
```

This ensures PostgreSQL is reachable at `DB_POSTGRES_URL`, then launches the governance server on port `8767`.

If you already know dependencies are ready and only want the server:

```bash
./scripts/ops/start_server.sh
```

## Stop

```bash
./scripts/ops/stop_unitares.sh
```

This attempts a graceful stop first, then force-kills only if needed, and cleans up the real `data/.mcp_server.*` files in the repo root.

## Health Check

```bash
./scripts/diagnostics/check_health.sh
```

This verifies:

- local HTTP health on `http://127.0.0.1:8767/health`
- whether `data/.mcp_server.pid` maps to a live process
- whether PostgreSQL is reachable at `DB_POSTGRES_URL`

The `health_check()` tool now also returns `operator_summary`:

- `overall_status`
- `failing_checks`
- `degraded_checks`
- `first_action`

Use `first_action` as the initial remediation hint instead of reading every component block first.

For a deeper live read from the running server, call `health_check()` through MCP or the REST tool API. The shell script is meant to answer "is the local instance up at all?" while `health_check()` is the better source for component-level diagnosis such as Redis, calibration DB, knowledge graph, and Pi connectivity.

## Identity Continuity

UUID is an identity anchor, not sufficient proof that the current execution context owns that identity. The `identity()` response includes:

- `identity_status` (`created` or `resumed`)
- `bound_identity` (`uuid`, `agent_id`, `display_name`)
- `session_resolution_source` (for diagnosing unexpected forks)

Standard agent workflow:

1. Fresh process: `start_session(force_new=true)` — save the returned `agent_uuid` / `client_session_id`
2. Fresh process inheriting prior work: `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`
3. Same live owner / proof-owned rebind: `identity(agent_uuid=..., continuity_token=..., resume=true)`
4. `sync_state()` for work logging
5. `check_working_state()` for read-only state
6. `identity()` to confirm current binding

Canonical/raw equivalents are `onboard(...)`, `process_agent_update(...)`, and
`get_governance_metrics(...)`. Use them for older clients or when inspecting
the unwrapped handler payload directly.

`continuity_token` is now a short-lived ownership proof for PATH 0 anti-hijack, not indefinite cross-process continuity. `client_session_id` remains in-session transport continuity metadata. For fresh process instances, prefer lineage declaration over silent UUID resume.

If an agent forks identity unexpectedly, inspect `session_resolution_source` first.

### Legacy Flat Cache Cleanup

S20 retired the shared workspace cache as a taught identity surface. New client
cache writes should use `.unitares/session-<slot>.json`; the flat
`.unitares/session.json` file is read-only legacy unless an operator has
explicitly chosen `--allow-shared` for a substrate-earned single-tenant
deployment.

After upgrading clients past the S20 helper/command changes, inspect each
workspace that may have old cache state:

```bash
WORKSPACE=/path/to/workspace
python3 scripts/client/session_cache.py list --workspace "$WORKSPACE"
```

Entries with `"legacy_flat": true` point at the flat legacy file. If
`"has_legacy_token": true`, the inventory has detected an old token-bearing
cache without printing the token value. Treat that UUID only as a lineage
candidate; do not copy or reuse any token from the file.

If the flat file is not an intentional `--allow-shared` single-tenant cache,
remove only that file:

```bash
rm "$WORKSPACE/.unitares/session.json"
python3 scripts/client/session_cache.py list --workspace "$WORKSPACE"
```

Leave `.unitares/session-*.json` files in place. They are the slotted lineage
inventory used by current startup paths; stale slot pruning is separate from the
S20 migration.

## Expected Endpoints

- MCP: `http://127.0.0.1:8767/mcp/`
- Health: `http://127.0.0.1:8767/health`
- Dashboard: `http://127.0.0.1:8767/dashboard`

## Common Failure Modes

### Stale PID file

Symptom:

- `check_health.sh` reports `PID file: stale or unreadable`

Fix:

```bash
./scripts/ops/stop_unitares.sh
./scripts/ops/start_with_deps.sh
```

### PostgreSQL not reachable

Symptom:

- `check_health.sh` reports PostgreSQL unreachable

Fix:

```bash
pg_isready -d "$DB_POSTGRES_URL"
brew services start postgresql@17
./scripts/ops/start_with_deps.sh
```

### HTTP health down

Symptom:

- `check_health.sh` reports `HTTP: Not responding`

Fix:

```bash
./scripts/ops/start_with_deps.sh
```

If that still fails, inspect:

```bash
tail -f /tmp/unitares.log
```

### Knowledge graph degraded

Symptom:

- `health_check()` reports `knowledge_graph` as degraded or warning
- `operator_summary.degraded_checks` includes `knowledge_graph`

Interpretation:

- If `knowledge_graph.info.error` mentions `graph with oid ... does not exist`, the AGE catalog and schema drifted out of sync.
- Current runtime logic will attempt to repair the AGE graph and rehydrate it from durable PostgreSQL tables.
- If recovery succeeds, `knowledge_graph.status` should return to `healthy` and the discovery counts should be nonzero again.

What to check:

- `health_check().checks.knowledge_graph.lifecycle`
- `health_check().checks.knowledge_graph.info`
- server logs during startup or first KG access

### Weak continuity

Symptom:

- `identity()` shows `session_resolution_source="ip_ua_fingerprint"`
- agents appear to "resume" into unexpected identities

Fix:

- rerun `start_session(force_new=true)` (raw implementation: `onboard(...)`)
- if the process is continuing prior work, include `parent_agent_id=<prior uuid>` and `spawn_reason="new_session"`
- avoid bare `identity(agent_uuid=..., resume=true)`; use a matching `continuity_token` only for same-owner rebinding

### Start script exits unexpectedly

Symptom:

- the wrapper script starts the server and then the process exits during shutdown/cleanup

Interpretation:

- This points to process-management or event-loop cleanup behavior, not necessarily a core governance failure
- confirm with `health_check()` whether a live server is still reachable before assuming the whole stack is down

## Practical Triage Order

When something feels wrong, do the checks in this order:

1. Run `./scripts/diagnostics/check_health.sh`
2. If HTTP is up, call `health_check()`
3. If an agent identity looks wrong, call `identity()`
4. If the issue is governance-state related, call `check_working_state()` (raw implementation: `get_governance_metrics(...)`)
5. Only after that inspect logs or restart services

This order matters because many apparent "agent bugs" are actually continuity or process issues, and many apparent "graph bugs" are now observable directly through `health_check()` without guessing from symptoms.
