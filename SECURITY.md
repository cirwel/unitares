# Security Policy

## Reporting a vulnerability

**Please use [GitHub Private Vulnerability Reporting](https://github.com/cirwel/unitares/security/advisories/new)** to report security issues. This routes the report privately to the maintainer and lets us coordinate a fix before public disclosure.

Do **not** open a public issue or PR for a security bug.

If GitHub PVR is unavailable to you (e.g., you don't have an account), open a public issue stating only *"contact requested — security"* without details, and I'll reach out for a private channel.

## What's in scope

UNITARES is a runtime governance MCP server for AI-agent fleets. In-scope vulnerabilities include:

- Authentication / identity bypasses in the MCP handlers, the gateway (port 8768), the lease plane (port 8788), or the dashboard
- Bearer-token handling, session-cache leakage, or cross-agent identity confusion (see [`docs/ontology/identity.md`](docs/ontology/identity.md) for the identity model)
- Injection / RCE through MCP tool inputs, audit-log entries, or knowledge-graph writes
- Escaping the **governed-effect execute plane** (port 8788) — committing a host effect (`agent_spawn` / `file_write` / commit) without passing the strong-tier identity gate, the per-effect governance veto, the bearer token, or lease custody (see the section below; off by default)
- SQL injection, AGE injection, or other database-layer issues
- Sensitive data exposure in audit logs, telemetry, or the dashboard
- Resource exhaustion / DoS against any bound service

**Out of scope:**
- Issues that require root or local-shell access to the host running the server (e.g., reading `data/` on disk). Note: the governed-effect execute plane is *HTTP-reachable* host execution, not local-shell — it is in scope above, not excluded by this clause
- Vulnerabilities in dependencies — please report those upstream first; we'll bump pinned versions once a fix is available
- Configurations that bind services to non-loopback interfaces — the default config binds to `127.0.0.1` (see `MCP_CLIENTS.md`); exposing publicly is operator responsibility

## Governed-effect execute plane

UNITARES ships an optional **governed-effect execute plane** (BEAM, port 8788): an
agent *proposes* an effect — `agent_spawn`, `file_write`, or a file-write `commit` —
and only governance *commits* it. Because that is a host code-execution / file-write
surface, it is treated as privileged:

- **Off by default.** All three execute flags
  (`UNITARES_GOVERNED_EFFECT_EXECUTE_AGENT_SPAWN` / `_FILE_WRITE` / `_FILE_WRITE_COMMIT`)
  default off in code, and the shipped plist templates do not set them — a fresh
  clone or deploy has no execute surface. A fail-closed boot guard refuses to start
  if `COMMIT` is enabled without the dispatch path.
- **Multiply gated when enabled.** Every effect re-certifies **strong-tier identity**
  and passes a **per-effect governance veto** (`POST /v1/effect-veto`) on every path;
  the plane is bearer-gated and loopback-bound by default, with per-class payload
  ceilings and content-hash reversibility/recovery.
- **In scope.** Bypassing any of those gates to commit an unsanctioned effect is a
  security issue — please report it.

The maintainer's own dogfood deployment runs this plane **enabled** (it is off for
everyone else by default, and reachable only via loopback + bearer token there).

## Supported versions

This project is pre-1.0; only the current `master` branch and the most recent tagged release receive security fixes. Tagged releases pre-`v2.13.0` are not supported.

## Disclosure timeline

- I aim to acknowledge reports within **3 business days**.
- For confirmed issues, I'll work toward a fix and coordinate disclosure — typically a **90-day** maximum window before public disclosure, shorter for actively-exploited issues.
- Credit will be given in the release notes unless the reporter prefers otherwise.

## Production deployment note

UNITARES has been running continuously in production since November 2025 — but on a **single-operator fleet**. The threat model has been "internal fleet hygiene + honest agent identity," not "hostile external clients." Multi-tenant or public-facing deployment will benefit from harder auth posture than the current defaults (bearer tokens, loopback binding, schema-isolated Postgres). See [`docs/integration/MCP_CLIENTS.md`](docs/integration/MCP_CLIENTS.md) and [`docs/operations/OPERATOR_RUNBOOK.md`](docs/operations/OPERATOR_RUNBOOK.md).
