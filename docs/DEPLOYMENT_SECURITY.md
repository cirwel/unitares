# Deployment security: the Compose quickstart's hardening posture

This is the infra/deployment counterpart to
[`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md), which covers the
identity/protocol trust boundary (self-report vs. trusted outcomes, the
symmetric resolution HMAC, single-operator scope). That document is silent on
the Compose stack itself; this one isn't. Read both before treating a fresh
`docker compose up` as more than what it is: **a trusted single-user
development stack, not a hardened local production deployment.**

## What this pass changed

- Every service (`postgres-age`, `redis`, `lease-plane`, `governance-mcp`)
  runs with `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]`.
  Verified against `docker compose up`: all four still start and pass their
  healthchecks (`postgres-age` already runs as the non-root `postgres` user
  per `db/postgres/Dockerfile.age-vector`; none of the four bind a port below
  1024 inside the container, so `CAP_NET_BIND_SERVICE` is never needed).
- `postgres-age` (5432) and `redis` (6379) are no longer published to the
  host by default. Postgres carries a default credential
  (`postgres`/`postgres`, see `.env.example`) and Redis has no
  authentication at all; publishing either meant any other local process or
  OS account could reach them — `127.0.0.1` is a machine boundary, not a
  user boundary. `governance-mcp` and `lease-plane` still reach both over the
  internal Compose network with no change in behavior. To opt back in for
  local debugging (`psql`, `redis-cli`, a GUI client):

  ```
  docker compose -f docker-compose.yml -f docker-compose.admin.yml up
  ```

  See `docker-compose.admin.yml` for what it publishes and why it's opt-in.

## What this pass did not change (known gaps)

- **`governance-mcp` and `lease-plane` still run as root** — neither
  Dockerfile has a `USER` directive. Adding one needs a separate pass:
  `governance-mcp` writes to the `governance-data` named volume
  (`/app/data`) and `lease-plane` compiles/runs an Elixir release, and both
  need their runtime file-permission needs verified against a non-root UID
  before switching, not assumed.
- **The dev-only default secrets are still defaults.** `LEASE_PLANE_BEARER_TOKEN`,
  `UNITARES_CONTINUITY_TOKEN_SECRET`, and `UNITARES_LEASE_ATTESTATION_SIGNING_KEY`
  ship as literal values in `docker-compose.yml`, each commented
  `# Development-only` / `# Known development-only` at its definition.
  `.env.example` already carries `change-me-*` overrides for the bearer and
  continuity secret; rotate all three (and set a real `POSTGRES_PASSWORD`)
  before running this stack anywhere reachable beyond your own loopback.
- **No production-mode boot guard.** Nothing here refuses to start on a
  default credential, an unauthenticated Redis, or a missing MCP bearer —
  the quickstart can be pointed at a non-loopback interface without
  crossing any explicit "you are now in production" gate. `.github/SECURITY.md`
  currently treats a non-loopback bind and local-host/local-process access as
  operator responsibility and out of report scope, respectively; that framing
  is a real decision, not an oversight, but it means the boot path itself
  enforces none of it.
- **No image/repo split.** `governance-mcp`'s image ships the reference
  residents, research apparatus, and operator tooling alongside the core
  server; there's no separate minimal image for someone who only wants the
  governance core.
- **No data-loss-prevention layer.** Check-ins, findings, and dialectic text
  persist as free-form text with no secret-pattern redaction, sensitivity
  labels, or bounded retention.

None of the above is fixed by this pass. They're recorded here so the next
pass doesn't have to re-derive them from a fresh read of the compose file.
