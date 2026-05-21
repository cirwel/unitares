# Security Policy

## Reporting a vulnerability

**Please use [GitHub Private Vulnerability Reporting](https://github.com/cirwel/unitares/security/advisories/new)** to report security issues. This routes the report privately to the maintainer and lets us coordinate a fix before public disclosure.

Do **not** open a public issue or PR for a security bug.

If GitHub PVR is unavailable to you (e.g., you don't have an account), open a public issue stating only *"contact requested — security"* without details, and I'll reach out for a private channel.

## What's in scope

UNITARES is a runtime governance MCP server for AI-agent fleets. In-scope vulnerabilities include:

- Authentication / identity bypasses in the MCP handlers, the gateway (port 8768), the lease plane (port 8788), or the dashboard
- Bearer-token handling, session-cache leakage, or cross-agent identity confusion
- Injection / RCE through MCP tool inputs, audit-log entries, or knowledge-graph writes
- SQL injection, AGE injection, or other database-layer issues
- Sensitive data exposure in audit logs, telemetry, or the dashboard
- Resource exhaustion / DoS against any bound service

**Out of scope:**
- Issues that require root or local-shell access to the host running the server (e.g., reading `data/` on disk)
- Vulnerabilities in dependencies — please report those upstream first; we'll bump pinned versions once a fix is available
- Configurations that bind services to non-loopback interfaces — the default config binds to `127.0.0.1` (see `MCP_CLIENTS.md`); exposing publicly is operator responsibility

## Supported versions

This project is pre-1.0; only the current `master` branch and the most recent tagged release receive security fixes. Tagged releases pre-`v2.13.0` are not supported.

## Disclosure timeline

- I aim to acknowledge reports within **3 business days**.
- For confirmed issues, I'll work toward a fix and coordinate disclosure — typically a **90-day** maximum window before public disclosure, shorter for actively-exploited issues.
- Credit will be given in the release notes unless the reporter prefers otherwise.

## Production deployment note

UNITARES has been running continuously in production since November 2025 — but on a **single-operator fleet**. The threat model has been "internal fleet hygiene + honest agent identity," not "hostile external clients." Multi-tenant or public-facing deployment will benefit from harder auth posture than the current defaults (bearer tokens, loopback binding, schema-isolated Postgres). See [`docs/integration/MCP_CLIENTS.md`](docs/integration/MCP_CLIENTS.md) and [`docs/operations/OPERATOR_RUNBOOK.md`](docs/operations/OPERATOR_RUNBOOK.md).
