# 6 · Operating

[← Reading the record](05-reading-the-signals.md) · [Manual index](README.md) · [Next: Troubleshooting →](07-troubleshooting.md)

This chapter is the operator's orientation. The deep, living runbook is [`../operations/OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) — this is the map and the parts most readers actually need.

## 6.1 Database ownership

**PostgreSQL + AGE is the durable system of record for governance data** —
identity records, agent state, audit events, discoveries, dialectic, calibration,
and tool usage. There is no SQLite. Redis is currently the de-facto primary
runtime store for session and identity bindings. The server can start without
it only in degraded local-only mode, which is suitable for a demo but does not
provide production continuity.

```
PostgreSQL+AGE (5432)            Redis (6379)
  durable identity rows            live session bindings
  core.agent_state                 identity continuity
  audit.events / audit.outcome_events
  knowledge.discoveries / governance_graph (AGE)
  core.dialectic_sessions / core.dialectic_messages
  core.calibration
  audit.tool_usage
  lease_plane.* (lease plane state)
```

Operational guidance: do **not** create additional PostgreSQL instances, databases, or migration layers — it's a single-Postgres / schema-isolation model ([`../operations/database_architecture.md`](../operations/database_architecture.md)). The current schema is [`db/postgres/schema.sql`](../../db/postgres/schema.sql) plus [`db/postgres/migrations/`](../../db/postgres/migrations/); the dated (2026-04-10) data dictionary of one deployment, archived at [`../operations/archive/DATA_NOTES.md`](../operations/archive/DATA_NOTES.md), omits schemas added since.

## 6.2 Resident agents

Residents are optional agents that an operator chooses to run alongside the
server. A fresh install has **no configured resident fleet**. Use the public
MCP, REST, or [`unitares-sdk`](../../agents/sdk/) contract to integrate a resident;
its identity, capabilities, and schedule are deployment configuration.

The optional [`reference implementations`](../../agents/README.md) demonstrate
health checks, event monitoring, code watching, and metrics collection. Their
presence in a checkout does not enable those services. Configure the agents you
deploy through `UNITARES_RESIDENTS` and, if needed, a progress manifest
([`../operations/resident-roster.md`](../operations/resident-roster.md)). Keep
actual names, hosts, schedules, and service-manager configuration in your
deployment repository.

## 6.3 Security posture

UNITARES has run in production since November 2025 on a **single-operator fleet**. The deployment defaults assume a single trusted operator network; multi-tenant and internet-facing use needs a harder auth posture. See [`SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md#scope-and-threat-model) for the adversarial boundaries and the open gap in sustained adversarial testing.

- All services bind `127.0.0.1` by default; public exposure is an env-var-gated operator decision ([§3.5](03-running-the-server.md#35-exposing-beyond-loopback)).
- The lease plane **fails closed** if `LEASE_PLANE_BEARER_TOKEN` is unset. The Docker stack supplies a fixed, well-known default token; set your own before exposing port 8788.
- Agent identity uses keyed session bindings (`client_session_id`) and HMAC-signed `continuity_token` proofs (symmetric stack retained intentionally; lease-plane mutations use Ed25519 attestations), separate from transport bearer auth (`UNITARES_MCP_BEARER_TOKENS`).
- The dashboard is served by the governance server at `/dashboard` and reads through its REST API, not PostgreSQL directly; browsers authenticate with the REST token or passkey sign-in ([§3.4](03-running-the-server.md)).

**Multi-tenant or public-facing deployment needs a harder auth posture than the defaults.** Set bearer/OAuth gates and host allowlists before exposing anything. Vulnerability reports: [`../../SECURITY.md`](../../.github/SECURITY.md). Full scope and threat model: [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).

## 6.4 Tuning governance thresholds

Read thresholds with `config(action="get")`; change them with `config(action="set", thresholds={...})` (writes require a bound session whose agent carries the operator-granted `admin` tag; overrides are held in process memory and reset when the server restarts). Defaults and margin computation live in [`config/governance_config.py`](../../config/governance_config.py). Prefer leaving defaults in place until the falsifiability harness ([chapter 5](05-reading-the-signals.md#reading-the-instrument-and-evaluating-its-uses)) tells you a change helps on *your* fleet.

## 6.5 The operations doc map

Most readers can skip these; reach for them when the need is specific.

| Doc | When you need it |
|---|---|
| [`README.md`](../operations/README.md) | Full index of the operations docs |
| [`OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) | The primary production runbook |
| [`deployment-env-overlay.md`](../operations/deployment-env-overlay.md) | Keeping deployment-specific settings outside the checkout |
| [`cloud-deploy-runbook.md`](../operations/cloud-deploy-runbook.md) | Running the server on a cloud host |
| [`RELEASE_PROCESS.md`](../operations/RELEASE_PROCESS.md) | How releases are cut and promoted |
| [`DEFINITIVE_PORTS.md`](../operations/DEFINITIVE_PORTS.md) | Port assignments across services |
| [`database_architecture.md`](../operations/database_architecture.md) | Single-Postgres / schema-isolation model |
| [`schema.sql`](../../db/postgres/schema.sql) | The current database schema; [`database_architecture.md`](../operations/database_architecture.md) gives ownership. The dated 2026-04-10 data dictionary is archived at [`archive/DATA_NOTES.md`](../operations/archive/DATA_NOTES.md) |
| [`DEPLOYMENT_DATA_CAVEAT.md`](../operations/DEPLOYMENT_DATA_CAVEAT.md) | What the cited deployment numbers do and don't mean |
| [`resident-roster.md`](../operations/resident-roster.md) | Configuring the resident set |
| [`branch-hygiene-runbook.md`](../operations/branch-hygiene-runbook.md) | Resident branch-hygiene sweep |
| [`lease-plane-operator-runbook.md`](../operations/lease-plane-operator-runbook.md) | Elixir lease-plane operations |
| [`github-workflow-conventions.md`](../operations/github-workflow-conventions.md) | Branch naming + draft-PR delivery contract |

## 6.6 Multi-agent coordination (advanced)

For fleets that coordinate, the **CIRS protocol** ([`../guides/CIRS_PROTOCOL.md`](../guides/CIRS_PROTOCOL.md)) defines the message types agents use to hand off and synchronize. The **lease plane** (port `8788`) is the Elixir/OTP coordination layer for single-writer surfaces. Both are specialized — you don't need them for a basic governed fleet.

## 6.7 Updates and backups

On a Docker install, `./scripts/unitares update --check` reports whether a newer release is published, and `./scripts/unitares update` moves to it. Before it applies any migration it stops the writers and dumps the database to `~/.unitares/backups/` (or `UNITARES_BACKUP_DIR`); a failed migration or rebuild puts the previous release's code back, unless the attempt advanced the protected dialectic schema (or the schema afterwards cannot be read), in which case it leaves the new release checked out with the writers stopped for you to repair forward. A rollback restores code only, never the database: the script reports which release the code is at and which schema the database is at. [Updating](02-install.md#updating) has the full behavior, and the one-time step for installs made before the command existed.

These dumps are taken only when a release brings migrations. They are gzipped plain SQL, readable only by your account, and nothing prunes them, so clear old ones yourself. They are not a backup schedule; for one, run `pg_dump` on a timer yourself.

---

[← Reading the record](05-reading-the-signals.md) · [Manual index](README.md) · [Next: Troubleshooting →](07-troubleshooting.md)
