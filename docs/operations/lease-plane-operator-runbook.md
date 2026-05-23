# Surface Lease Plane Operator Runbook

Status: **LIVE — Phase A shipped 2026-05-03 (PR #305).** Service `com.unitares.lease-plane` runs on `127.0.0.1:8788` via launchd. Bearer-auth fail-closed (HTTPAuth → 503 if `LEASE_PLANE_BEARER_TOKEN` is unset).

This runbook teaches what the BEAM node does, not Elixir-the-language. PRs will read clearly enough without prior fluency once these terms are familiar.

## What the lease plane is

A standalone Elixir/OTP application running on the governance MCP host (Mac). It owns coordination state for shared mutable surfaces (file paths, dialectic sessions, resident lifecycles, capture windows). Backed by Postgres for durable truth. Single-node by design — there is no Erlang clustering across Mac↔Pi.

It does not own EISV, calibration, KG, or identity issuance. Those stay in Python. (RFC §2 invariant.)

## Vocabulary you'll see in PRs

- **GenServer** — a process that holds state and serves messages from its mailbox one at a time. The "process as actor" primitive. Mailbox-serialized = no two callers stomping its state.
- **DynamicSupervisor** — a supervisor that starts and stops child processes at runtime. The lease plane uses one for per-lease holder processes.
- **Registry** — a process directory. "Find me the holder process for surface X."
- **`:DOWN`** — the message a supervisor or monitor receives when a watched process dies. The corpse-lock fix: when a local lease holder dies, the supervisor sees `:DOWN`, releases the lease, writes the Postgres release row.
- **Oban** — durable job queue. Reaper sweeps, handoff timeouts, audit-outbox drains run as Oban jobs. If the BEAM node restarts, Oban jobs resume from Postgres.
- **PromEx** — Prometheus metrics exporter. Lease-plane metrics flow into the existing Sentinel/dashboard surface.
- **Telemetry** — structured event emission. Lease events fire telemetry; PromEx aggregates, audit-outbox persists.
- **Ecto / Postgrex** — the Postgres ORM and driver. The lease plane talks to the same `governance` database UNITARES uses.

## Start

Render the plist template and load it (one-time install):

```bash
sed -e "s|__UNITARES_ROOT__|$HOME/projects/unitares|g" \
    -e "s|__HOME__|$HOME|g" \
    scripts/ops/com.unitares.lease-plane.plist.template \
    > ~/Library/LaunchAgents/com.unitares.lease-plane.plist
launchctl load ~/Library/LaunchAgents/com.unitares.lease-plane.plist
```

Service auto-starts on boot (`RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=10`). Verify:

```bash
launchctl list | grep com.unitares.lease-plane
tail -f ~/Library/Logs/unitares-lease-plane.log

# Health probe (sources LEASE_PLANE_BEARER_TOKEN from ~/.config/cirwel/secrets.env)
curl -s -H "Authorization: Bearer $LEASE_PLANE_BEARER_TOKEN" \
     "http://127.0.0.1:8788/v1/health"
```

## Stop

Graceful stop releases all *local-holder* leases (the BEAM-monitored ones) by writing release rows. *Remote-holder* leases are unaffected and continue to be tracked via Postgres heartbeat-TTL until their holders re-heartbeat or expire naturally.

```bash
launchctl unload ~/Library/LaunchAgents/com.unitares.lease-plane.plist
# Re-load with launchctl load to restart
```

## Health check

Sentinel monitors the lease plane via `GET /v1/health` (RFC §7.7).

**What the probe is**

A successful `/v1/health` probe returns `{"ok": true, "status": "ok", "protocol_version": "v1.0"}` with HTTP 200, proving:
1. Bandit/Plug router is up
2. `HTTPAuth` plug accepts the configured `LEASE_PLANE_BEARER_TOKEN`
3. The Postgres `governance` connection is alive

**Sentinel alarm rules**

| Condition | Alarm | Action |
|-----------|-------|--------|
| 0 successful probes in last 5 min | `lease_plane.unreachable` | Check `launchctl list com.unitares.lease-plane`; restart via `launchctl kickstart -k system/com.unitares.lease-plane` |
| HTTP 401 on probe | `lease_plane.auth_drift` | Sentinel's bearer token diverged from the lease plane's; re-source `~/.config/cirwel/secrets.env` and `launchctl kickstart` Sentinel |
| HTTP 503 sustained | `lease_plane.db_degraded` | Postgres flapping; check `pg_isready -h localhost -p 5432` |
| Probe latency > 1s sustained | `lease_plane.slow` | Postgres lock contention or backlog; inspect `pg_stat_activity` for stuck transactions on `lease_plane.surface_leases` |

**Probe cadence**: every 30s (matches reaper sweep cadence — no point probing more often than the system's own internal pulse). Alarm thresholds use sliding 5-min windows so a single transient blip doesn't page.

**What the probe does NOT cover**

- Reaper liveness (separate alarm: stale `expires_at < now() - interval '60s' AND released_at IS NULL` count > 0)
- Audit-outbox forwarding (separate alarm: `lease_plane_events WHERE forwarded_at IS NULL` count growing unboundedly)
- Per-surface_kind acquire success rate (telemetry, not a binary alarm)

The healthcheck probe is a binary "is the front door open" signal. Functional health lives in the supplemental rules above.

## Live introspection (the BEAM superpower)

This is the part most worth learning. From your laptop:

```bash
# Connect to the running BEAM node interactively
iex --sname operator --remsh unitares-lease-plane@localhost
```

Once attached, useful commands:

```elixir
# GUI: full supervision tree, mailbox depths, ETS tables, message rates
:observer.start()

# Quick: show the supervision tree as text
:observer_cli.start()    # if observer_cli is added as a dep

# Inspect a specific GenServer's state without restarting it
:sys.get_state(UnitaresLeasePlane.HandoffServer)

# Trace a process's messages live (sparingly — it's heavy)
:dbg.tracer()
:dbg.p(pid, [:m, :c])

# Count active leases right now
UnitaresLeasePlane.Stats.active_lease_count()
```

The point: when something is wrong, you don't add print statements and redeploy. You attach, look, and decide.

## Deprecating a surface_kind (RFC §7.11.2 — R1 canonical path)

The 4-phase deprecation procedure runs through the Python CLI at
`scripts/dev/lease_plane_deprecate.py`. R1 (PR #284) introduced
`deprecate-and-finalize` as the canonical Phase 2+3 super-command; the
standalone `deprecation-sweep` and `deprecation-finalize` subcommands remain
as **operator escape hatches** for emergency partial recovery only.

### Canonical sequence (production deprecation)

```bash
# Phase 0: mark the scheme deprecated (writes deprecated_schemes row,
#   emits lease.deprecation_marked event)
python3 scripts/dev/lease_plane_deprecate.py deprecate <kind> --days 30

# Phase 1 (operator-driven): wait the drain window; verify no Elixir source
#   still references the deprecated scheme (unitares_doctor lint — Phase B prep)

# Phase 2+3: atomic on a single connection, correlated under shared run_id
python3 scripts/dev/lease_plane_deprecate.py deprecate-and-finalize <kind>
```

The super-command runs Phase 2 (sweep — force-release surviving leases) and
Phase 3 (finalize — record `check_migrated_at`) on a single asyncpg
connection in two transactions. Both phases share a `run_id` (uuid4) that
appears in every emitted event payload + every log line, so partial
completion is correlatable in audit queries:

```sql
SELECT event_type, ts FROM lease_plane.lease_plane_events
WHERE payload->>'run_id' = '<uuid-from-stderr-log>'
ORDER BY ts;
```

### Recovery from partial failure

The super-command uses **two transactions on one connection** (operator
decision 2026-05-02): if Phase 3 fails after Phase 2 succeeded, the swept
rows STAY released (no rollback of operator work). The super-command:

1. Emits `lease.deprecation_aborted` event with run_id + reason payload
2. Logs clear "rerun deprecation-finalize <kind>" guidance to stderr
3. Returns exit code 3

The §7.11.4 idempotent-sweep predicate makes the rerun safe. To recover:

```bash
# Fix the underlying issue that caused Phase 3 to fail, then:
python3 scripts/dev/lease_plane_deprecate.py deprecation-finalize <kind>
```

### Escape-hatch sub-commands (DO NOT use in routine deprecation)

Use ONLY when the super-command itself is unavailable or has failed in
ways that prevent normal recovery:

- `deprecation-sweep <kind>` — Phase 2 standalone. Requires
  `LEASE_FORCE_RELEASE_TOKEN`. Idempotent.
- `deprecation-finalize <kind>` — Phase 3 standalone. Used as the canonical
  recovery path after a failed super-command (see "Recovery from partial
  failure" above).

### Audit query: any abandoned deprecations?

Two queries — run both. The first catches abandons where the super-command
emitted the abort event before exiting. The second catches the
SIGKILL-between-phases case (Phase 2 committed, super-command was killed
before Phase 3 could run, no abort event was written).

```sql
-- (1) Explicit abandon: abort event emitted
SELECT
  payload->>'kind' AS kind,
  payload->>'run_id' AS run_id,
  payload->>'reason' AS reason,
  ts
FROM lease_plane.lease_plane_events
WHERE event_type = 'lease.deprecation_aborted'
ORDER BY ts DESC;

-- (2) Implicit abandon: Phase 2 committed but Phase 3 never ran
-- (SIGKILL / power loss / OOM mid-super-command)
SELECT
  surface_kind,
  sweep_completed_at,
  check_migrated_at
FROM lease_plane.deprecated_schemes
WHERE sweep_completed_at IS NOT NULL
  AND check_migrated_at IS NULL
ORDER BY sweep_completed_at DESC;
```

If a row appears in either query for `<kind>`, that deprecation is in
"swept but unfinalized" state. Recovery: rerun
`deprecation-finalize <kind>` (the §7.11.4 idempotent-sweep predicate
makes this safe even if Phase 2 is also re-attempted via the super-command).

### Recovery from SIGKILL mid-Phase-2

If `deprecate-and-finalize` was killed (SIGKILL, OOM, parent-process death)
while Phase 2 was running, the in-flight transaction is rolled back by
Postgres when it detects the dead client. Until that happens, row-level
locks (`FOR UPDATE SKIP LOCKED`) on `lease_plane.surface_leases` rows for
the deprecated kind may be held. To inspect:

```sql
-- Check for stuck backends with active transactions on surface_leases
SELECT pid, state, query_start, wait_event, query
FROM pg_stat_activity
WHERE state IN ('active', 'idle in transaction')
  AND query LIKE '%lease_plane.surface_leases%'
ORDER BY query_start;
```

Postgres has no `idle_in_transaction_session_timeout` by default, so a
stuck backend may persist indefinitely until the operator either: (a)
restores the killed super-command (it'll observe its tx was lost), (b)
manually `pg_terminate_backend(<pid>)` the stuck backend, or (c) restarts
Postgres. Once the stuck backend is gone, rerun `deprecate-and-finalize <kind>`
— Phase 2 will sweep zero rows (idempotent predicate) and Phase 3 will
finalize cleanly.

## LEASE_FORCE_RELEASE_TOKEN — provisioning and rotation (RFC §7.10)

Force-release is a separate authority from regular lease access. It uses its own bearer token, **never** the standard `LEASE_PLANE_BEARER_TOKEN` or `GOVERNANCE_TOKEN`. Spec rationale: a caller with the regular bearer can free its own leases; a caller with the elevated token can free *anyone's* leases. The two privileges are distinct and the tokens must not collapse.

**Where the token lives**

```
~/.config/cirwel/secrets.env    # mode 600, local-Mac-only
LEASE_FORCE_RELEASE_TOKEN=<32-byte-random-hex>
```

This follows the existing `~/.config/cirwel/secrets.env` convention (noun-first, `_TOKEN` suffix; cf. `ZENODO_TOKEN`, `CLOUDFLARE_API_TOKEN`). Mode 600. v0 is **local-Mac-only by design** — there is no off-host force-release path. If the operator is travelling, they SSH to the Mac or wait for the lease's TTL.

**Initial provisioning**

```bash
# 1. Generate a fresh 32-byte hex token (no Anthropic-style 'sk-' prefix; this is internal)
TOKEN=$(openssl rand -hex 32)

# 2. Add to secrets.env (preserve existing keys; do NOT overwrite the file)
printf 'LEASE_FORCE_RELEASE_TOKEN=%s\n' "$TOKEN" >> ~/.config/cirwel/secrets.env

# 3. Verify mode is still 600
chmod 600 ~/.config/cirwel/secrets.env
ls -la ~/.config/cirwel/secrets.env

# 4. Reload the lease-plane LaunchAgent so it picks up the new env
launchctl kickstart -k system/com.unitares.lease-plane

# 5. Confirm the lease plane is back up
curl -fsS -H "Authorization: Bearer $LEASE_PLANE_BEARER_TOKEN" \
  http://127.0.0.1:8788/v1/health
```

**Rotation cadence**

Same as other operator-scoped tokens at `~/.config/cirwel/secrets.env`. No special rotation infrastructure for v0 — it's manual:

```bash
# 1. Rotate
NEW_TOKEN=$(openssl rand -hex 32)
sed -i.bak "s/^LEASE_FORCE_RELEASE_TOKEN=.*$/LEASE_FORCE_RELEASE_TOKEN=$NEW_TOKEN/" ~/.config/cirwel/secrets.env
rm ~/.config/cirwel/secrets.env.bak

# 2. Reload
launchctl kickstart -k system/com.unitares.lease-plane

# 3. Update any local Python clients that had the old token cached
# (LeasePlaneClientConfig.force_release_token — see src/lease_plane/client.py)
```

**Recovery from accidental token leak**

If the token appears in shell history, a screenshot, a pasted log, or anywhere outside `secrets.env`:

1. Rotate immediately (steps above)
2. Audit the force-release event log for unexpected entries:
   ```sql
   SELECT lease_id, surface_id, ts, payload
   FROM lease_plane.lease_plane_events
   WHERE event_type = 'forced'
     AND ts > now() - interval '24 hours'
   ORDER BY ts DESC;
   ```
3. If unexpected force-releases appear, file a `multi-agent-git-reset-incident.md`-style incident note

**What invalidates a token**: only changing the value in `secrets.env` and restarting the lease plane. There is no revocation list, no JWT-style expiry — the lease plane reads `Application.get_env(:lease_plane, :force_release_bearer_token)` once at boot.

## Renames and orphan leases (RFC §7.9)

A file rename, dialectic-session ID rotation, or resident relabel changes a surface's canonical `surface_id`. v0 explicitly does **not** handle rename-aware relocation. Active leases keyed on the old ID become *orphan leases* — the index thinks each entry is unique, but the underlying surface is the same.

**Failure mode**

Agent A holds `file:///Users/cirwel/x.py`. Operator (or another tool) renames `x.py` → `y.py`. Agent A's lease still references `file:///Users/cirwel/x.py` — that path no longer exists. Agent B can now `acquire(file:///Users/cirwel/y.py)` and succeed; the "same surface semantically" is double-leased. The orphan ages out via TTL on its `original_ttl_s` clock — at most 1h per RFC §4.4 hard cap.

**Detection**

```sql
-- Orphan candidates: file:// leases pointing at non-existent paths.
-- Run as the operator (paths must be readable by the running shell).
SELECT lease_id, surface_id, holder_agent_uuid, acquired_at
FROM lease_plane.surface_leases
WHERE released_at IS NULL
  AND surface_kind = 'file'
ORDER BY acquired_at;
```

For each row, check if the path exists:

```bash
psql -h localhost -d governance -t -A -c \
  "SELECT replace(surface_id, 'file://', '') FROM lease_plane.surface_leases \
   WHERE released_at IS NULL AND surface_kind = 'file'" \
  | while read path; do
      [ -e "$path" ] || echo "ORPHAN: $path"
    done
```

**Manual release (operator path)**

If an orphan is blocking work and waiting for TTL is unacceptable:

```bash
# Force-release with the elevated token. The release_reason='forced' in the
# audit record makes this distinguishable from natural release.
curl -fsS -X POST \
  -H "Authorization: Bearer $LEASE_FORCE_RELEASE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"lease_id":"<lease-uuid-from-query-above>","release_reason":"forced"}' \
  http://127.0.0.1:8788/v1/lease/release
```

Or via the Python client (preferred — contract-layer rejection if the token is misconfigured):

```python
from src.lease_plane import (
    LeasePlaneClient, LeasePlaneClientConfig, ReleaseRequest,
)
import os
config = LeasePlaneClientConfig(
    bearer_token=os.environ["LEASE_PLANE_BEARER_TOKEN"],
    force_release_token=os.environ["LEASE_FORCE_RELEASE_TOKEN"],
)
client = LeasePlaneClient(config=config)
result = client.force_release(ReleaseRequest(
    lease_id="<lease-uuid>",
    release_reason="normal",  # pinned to 'forced' on the wire automatically
))
print(result)  # SimpleOk on success, SimpleError otherwise
```

**Why no automatic rename detection in v0**

Content-derived `surface_id` (e.g., file inode + ctime, dialectic-session content-hash) would make renames invisible to the lease layer, but it trades simplicity for robustness and warrants its own RFC. v0 treats the rename gap as a *known and bounded* operational hazard, not an unresolved design question. v1 may revisit.

## Common operations

TBD. Will include:

- **Drain a surface kind** (e.g. release all `dialectic:/` leases held by a specific UUID — for a stuck-agent recovery)
- **Promote a surface kind from advisory to enforcement** (config flag flip, no restart needed; documented in RFC §6.2)
- **Demote a surface kind back to advisory** (single config flag flip; the reversal must be cheap, never a code change)
- **Inspect the audit-outbox backlog** (`SELECT count(*) FROM lease_plane_events WHERE forwarded_at IS NULL`)

## Hot code reload (the BEAM thing that matters operationally)

When a new version of a module is deployed, the BEAM node can swap it in place without dropping leases. This directly addresses `feedback_running-process-vs-master-commit.md` — the running-process-vs-master-commit drift class:

- Old: `ps -o etime` + `git log --since=` to figure out if the resident has the fix you think it has
- New: deploy = module swap = the running node *has the fix*. The "is this code running?" question becomes "what version is loaded?", which `:application.loaded_applications/0` answers directly.

v0 does not *automate* hot-reload deploys. Initial deploys are full-restart. But the capability is the floor, not a feature add.

## When things go wrong

TBD. Will include incident-class playbooks for:

- Lease plane unreachable (callers fall through to advisory-skip; no work blocked, but conflict telemetry stops)
- Postgres flapping (Oban retries the audit-outbox drains; the synchronous lease writes return `service_unavailable` to callers)
- Reaper falling behind (active-lease count grows, expired-but-not-released count grows; Sentinel alerts fire on threshold)
- Audit-outbox backlog growing (UNITARES-side worker stalled or DB partition issue)
- Phantom local holder (`:observer` shows the process alive, but Postgres has no lease row for it — schema invariant violated, file an incident)

## Related

- Existing operator runbook: `docs/operations/OPERATOR_RUNBOOK.md` (Python governance MCP)
