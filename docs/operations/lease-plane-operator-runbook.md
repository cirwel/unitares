# Surface Lease Plane Operator Runbook

Status: **STUB — service does not exist yet.** This runbook is shipped alongside the v0 RFC (`docs/proposals/surface-lease-plane-v0.md`) so the operator-facing surface is visible before any code lands. Concrete commands and ports get filled in when the service ships.

The audience is Kenny (operator-as-reviewer, not author). This runbook teaches what the BEAM node does, not Elixir-the-language. PRs will read clearly enough without prior fluency once these terms are familiar.

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

TBD. Likely a launchd plist (`com.unitares.lease-plane`), matching the pattern for Vigil / Sentinel / Chronicler. Service should start automatically on boot and after upgrades.

## Stop

TBD. Graceful stop releases all *local-holder* leases (the BEAM-monitored ones) by writing release rows. *Remote-holder* leases are unaffected and continue to be tracked via Postgres heartbeat-TTL until their holders re-heartbeat or expire naturally.

## Health check

TBD. Sentinel will monitor `GET /v1/lease/status?surface_id=__healthcheck__` (RFC §7.7). Alarm fires if unreachable for >5min.

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

## Common operations

TBD. Will include:

- **Drain a surface kind** (e.g. release all `dialectic:/` leases held by a specific UUID — for a stuck-agent recovery)
- **Promote a surface kind from advisory to enforcement** (config flag flip, no restart needed; documented in RFC §6.2)
- **Demote a surface kind back to advisory** (single config flag flip; the reversal must be cheap, never a code change)
- **Inspect the audit-outbox backlog** (`SELECT count(*) FROM lease_plane_events WHERE forwarded_at IS NULL`)
- **Force-release a lease the operator knows is corpse-held** (last-resort manual override; logged to audit with `release_reason='operator_forced'`)

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

- RFC: `docs/proposals/surface-lease-plane-v0.md` (v0.1, pre-council)
- Pattern precedent: `docs/proposals/path1-sync-fingerprint-check.md` (advisory→strict rollout)
- Existing operator runbook: `docs/operations/OPERATOR_RUNBOOK.md` (Python governance MCP)
- Memory anchors: `feedback_running-process-vs-master-commit.md`, `multi-agent-git-reset-incident.md`, `feedback_check-in-during-long-sessions.md`
