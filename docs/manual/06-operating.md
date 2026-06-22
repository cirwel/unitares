# 6 · Operating

[← Reading the signals](05-reading-the-signals.md) · [Manual index](README.md) · [Next: Troubleshooting →](07-troubleshooting.md)

This chapter is the operator's orientation. The deep, living runbook is [`../operations/OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) — this is the map and the parts most readers actually need.

## 6.1 Database ownership

**PostgreSQL + AGE is the single source of truth for all governance data** — identities, agent state, audit events, discoveries, dialectic, calibration, tool usage. There is no SQLite. Redis is a session cache only and the server degrades gracefully without it.

```
PostgreSQL+AGE (5432)            Redis (6379)
  core.identities                  session cache only
  core.agent_state                 (optional; graceful fallback)
  audit.events
  core.discoveries (AGE)
  dialectic.*
  core.calibration
  core.tool_usage
```

Operational guidance: do **not** create additional PostgreSQL instances, databases, or migration layers — it's a single-Postgres / schema-isolation model ([`../operations/database_architecture.md`](../operations/database_architecture.md)). The production data dictionary is [`../operations/DATA_NOTES.md`](../operations/DATA_NOTES.md).

## 6.2 Resident agents

Several long-lived agents run alongside the server. They consume the **same public contract** as any external agent (the [`unitares-sdk`](../../agents/sdk/)) and double as reference implementations and operational hygiene. They are *not* load-bearing governance internals — the public contract is the SDK, not the residents.

| Resident | Cadence | Role |
|---|---|---|
| **Vigil** | scheduled (~30 min) | Janitorial — health checks, KG groundskeeping, test triggers |
| **Sentinel** | continuous (WebSocket) | Fleet monitor — anomaly detection on the live event stream |
| **Watcher** | event-driven | Code-watcher — local-LLM pattern match, wired into Claude Code's PostToolUse hook |
| **Chronicler** | daily | Longitudinal codebase metrics → `metrics.series` |
| **Steward** | in-process | EISV sync across substrates |

The named set is configured via `UNITARES_RESIDENTS` ([`../operations/resident-roster.md`](../operations/resident-roster.md)), not hardcoded. Reference implementations: [`../../agents/README.md`](../../agents/README.md).

## 6.3 Security posture

UNITARES has run continuously in production since November 2025 on a **single-operator fleet**. The threat model is "internal fleet hygiene + honest agent identity," **not** "hostile external clients."

- All services bind `127.0.0.1` by default; public exposure is an env-var-gated operator decision ([§3.5](03-running-the-server.md#35-exposing-beyond-loopback)).
- The lease plane **fails closed** if `LEASE_PLANE_BEARER_TOKEN` is unset.
- Agent identity is bearer-token-based (symmetric stack retained intentionally).
- The dashboard reads PostgreSQL directly with the same auth model as MCP.

**Multi-tenant or public-facing deployment needs a harder auth posture than the defaults.** Set bearer/OAuth gates and host allowlists before exposing anything. Vulnerability reports: [`../../SECURITY.md`](../../SECURITY.md). Full scope and threat model: [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).

## 6.4 Tuning governance thresholds

Read thresholds with `config(action="get")`; change them with `config(action="set", thresholds={...})` (writes are identity-gated). Defaults and margin computation live in [`config/governance_config.py`](../../config/governance_config.py). Prefer leaving defaults in place until the falsifiability harness ([chapter 5](05-reading-the-signals.md#57-dont-trust-these-numbers-blindly)) tells you a change helps on *your* fleet.

## 6.5 Operator constraint: no paid LLM API budget

A standing project constraint worth knowing as an operator: **do not adopt features that require a paid model API** (`ANTHROPIC_API_KEY` etc.). The supported automation paths are free/self-hosted — the local Ollama detector for Watcher, `GITHUB_TOKEN`-only CI, and deterministic CLI tools. Dialectic's "LLM-assisted antithesis" uses a *local* LLM for this reason.

## 6.6 The operations doc map

Most readers can skip these; reach for them when the need is specific.

| Doc | When you need it |
|---|---|
| [`OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) | The primary production runbook |
| [`DEFINITIVE_PORTS.md`](../operations/DEFINITIVE_PORTS.md) | Port assignments across services |
| [`database_architecture.md`](../operations/database_architecture.md) | Single-Postgres / schema-isolation model |
| [`DATA_NOTES.md`](../operations/DATA_NOTES.md) | Production data dictionary |
| [`DEPLOYMENT_DATA_CAVEAT.md`](../operations/DEPLOYMENT_DATA_CAVEAT.md) | What the cited deployment numbers do and don't mean |
| [`resident-roster.md`](../operations/resident-roster.md) | Configuring the resident set |
| [`branch-hygiene-runbook.md`](../operations/branch-hygiene-runbook.md) | Resident branch-hygiene sweep |
| [`lease-plane-operator-runbook.md`](../operations/lease-plane-operator-runbook.md) | Elixir lease-plane operations |
| [`github-workflow-conventions.md`](../operations/github-workflow-conventions.md) | Branch naming + draft-PR delivery contract |

## 6.7 Multi-agent coordination (advanced)

For fleets that coordinate, the **CIRS protocol** ([`../guides/CIRS_PROTOCOL.md`](../guides/CIRS_PROTOCOL.md)) defines the message types agents use to hand off and synchronize. The **lease plane** (port `8788`) is the Elixir/OTP coordination layer for single-writer surfaces. Both are specialized — you don't need them for a basic governed fleet.

---

[← Reading the signals](05-reading-the-signals.md) · [Manual index](README.md) · [Next: Troubleshooting →](07-troubleshooting.md)
