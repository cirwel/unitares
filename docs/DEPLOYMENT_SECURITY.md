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
  authentication at all. `governance-mcp` and `lease-plane` still reach both
  over the internal Compose network with no change in behavior. To opt back
  in for local debugging (`psql`, `redis-cli`, a GUI client):

  ```
  docker compose -f docker-compose.yml -f docker-compose.admin.yml up
  ```

  See `docker-compose.admin.yml` for what it publishes and why it's opt-in.

## What un-publishing actually buys you (read before relying on it)

The honest version, not the elevator pitch: **on Docker Desktop (macOS,
Windows), un-publishing is a real boundary** — the Desktop VM hides the
container bridge network from the host, so another local process or OS
account genuinely cannot reach an unpublished port. **On a native Linux
Docker host, it's weaker than "127.0.0.1 is a machine boundary, not a user
boundary" makes it sound.** Docker's bridge network is directly routable
from the host's own network namespace by design — any local process,
privileged or not, can typically reach a container's bridge IP
(`docker inspect <container> --format '{{.NetworkSettings.IPAddress}}'`)
whether or not its port is published, because Docker does not firewall
container-to-host-namespace traffic by default. Un-publishing there mainly
removes discovery via a well-known port (`5432`/`6379`) in favor of a bridge
IP an attacker has to enumerate; it is not a hard access boundary. Closing
that gap for real needs a host-originated-traffic rule, not a forwarding one
— Docker's `DOCKER-USER` chain hooks `FORWARD` and only sees traffic routed
*through* the host (container-to-container across networks, or external
traffic), not a host process connecting directly to a bridge IP, which is
`OUTPUT`-chain traffic. An `iptables`/`nftables` rule hooked on `OUTPUT` with
UID matching (e.g. `iptables -A OUTPUT -d <bridge-subnet> -m owner ! --uid-owner
<docker-daemon-uid>` or nftables `meta skuid`) is what would actually block
another local account from reaching the bridge subnet; this pass does not
add one.

**Applied consistently, this also means the ports still published by default
(`governance-mcp` 8767, `lease-plane` 8788) are not meaningfully harder to
reach than Postgres/Redis were** — `UNITARES_MCP_BEARER_TOKENS` is empty by
default (any local caller can drive governance writes, mint identities, read
the knowledge graph), `LEASE_PLANE_BEARER_TOKEN` defaults to the literal
string `unitares-local-lease-plane` (public in this repo and in
`docker-quickstart.yml`), and `UNITARES_LEASE_ATTESTATION_SIGNING_KEY`
defaults to a public, sequential-byte Ed25519 seed — so an attacker who can
already reach the loopback interface can forge attestations offline. This
pass hardens the network-exposure layer for two services; it does not close
the credential layer for any of the four. That's the same shape of gap
`.github/SECURITY.md` already names out of scope ("issues that require root
or local-shell access to the host"), stated here so it doesn't read as
solved by this PR's title.

**A web page is not a local caller.** The trusted-network bypass on REST and
`/ws/eisv` trusts the source address, and a browser on the operator's machine
connects from loopback (or, under Compose, from the bridge gateway) whichever
page asked. The bypass therefore does not apply when the request names a
browser `Origin` outside localhost, `UNITARES_MCP_ALLOWED_ORIGINS` and the
dashboard passkey origin, or a `Host` that is a dotted DNS name not listed in
`UNITARES_MCP_ALLOWED_HOSTS` (the DNS-rebinding shape). IP literals, `localhost`
and dotless names such as the `governance-mcp` service need no listing.
The Host rule applies to every caller, not only browsers: the server cannot
tell a rebound page from a script by its Host alone. So any client, `curl` and
the SDKs included, that addresses the server by a dotted hostname (for example
`server.home.arpa` or a Tailscale MagicDNS name) needs that host in
`UNITARES_MCP_ALLOWED_HOSTS`, or a bearer or passkey session. A browser opening
the dashboard on a LAN address or a hostname also needs its origin in
`UNITARES_MCP_ALLOWED_ORIGINS`, which `/mcp` already required.

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
- **`postgres-age`'s superuser role is the same role the app connects as.**
  The official Postgres image makes `POSTGRES_USER` (`postgres` by default) a
  superuser; `governance-mcp` and `lease-plane` both connect as that role.
  Code execution in either container reaches `COPY ... TO PROGRAM`-style
  superuser SQL against Postgres regardless of that container's own
  `cap_drop`/`security_opt` — `cap_drop: ALL` constrains the container's
  Linux capabilities, not what its already-privileged DB session can do.
  Scoping the app to a non-superuser role with least-privilege grants is a
  separate, unstarted pass.
- **No `read_only: true` on any service.** `governance-mcp` and `lease-plane`
  run as root (see above) with a writable filesystem; code execution in
  either can rewrite the application itself, and `restart: unless-stopped`
  brings the same, now-modified, writable layer back up. `cap_drop: ALL`
  doesn't prevent this — plain file writes to files you own don't need a
  capability.
- **No user-namespace remapping.** UID 0 inside `governance-mcp` and
  `lease-plane` is UID 0 on the Docker host (no `userns-remap`, no rootless
  Docker), so a container escape from either is a host-root escape. The
  capabilities that matter most for escapes (`SYS_ADMIN`, `SYS_PTRACE`,
  `SYS_MODULE`, `DAC_READ_SEARCH`) were never in Docker's default set to
  begin with, so `cap_drop: ALL` narrows an already-narrow escape surface
  more than it closes the root-execution gap above.
- **Optional host-mount paths haven't been tested under `cap_drop: ALL`.**
  `UNITARES_DASHBOARD_EXT_DIR` and the metrics-catalog/route-pack mount
  points ask an operator to bind-mount a host directory into a root
  container. If that directory carries restrictive permissions (owned by
  another UID, mode `0700`), a root process with every capability dropped
  can fail to read it — `CAP_DAC_OVERRIDE`, which `cap_drop: ALL` removes, is
  exactly what lets root bypass an ownership mismatch. This wasn't exercised
  here because it's an opt-in path outside the default quickstart; a sibling
  PR (#2578) hit the same *shape* of regression (two live import breakages)
  in a different narrowing of this image's build, on the same kind of
  opt-in, non-default path a plain `docker compose up` doesn't cover.

## Side effects to expect from this pass

- **Host-side tooling that defaults to `localhost:5432`/`localhost:6379` now
  reads a healthy Compose install as down.** `scripts/dev/unitares_doctor.py`
  is the clearest case (`check_postgres_running`, `check_redis_continuity`,
  `check_governance_database`, `check_pg_extensions` all default to a
  loopback DB URL); the reference agents and `scripts/analysis/*` tools that
  default the same way are written for this operator's native/Homebrew
  deployment (see `CLAUDE.md`'s `HOST_BINARIES`), not the Compose quickstart,
  so they were never exercising this path. `docker compose ps` /
  `docker inspect --format '{{.State.Health.Status}}' <container>` is the
  correct way to check a Compose install's Postgres/Redis health now; the
  admin overlay is the way to make host-side tools that expect a loopback
  connection see them too. A deeper fix (teaching the doctor to notice a
  healthy Compose-managed container instead of reporting a false failure)
  is real and worth doing, but is its own pass against a file with enough
  standing caution around it that it doesn't belong bundled into this one.
- **`POSTGRES_HOST_PORT`/`REDIS_HOST_PORT` no longer do anything on a plain
  `docker compose up`** — they only take effect with the admin overlay now.
  `.github/CONTRIBUTING.md`, `docs/manual/02-install.md`, and
  `docs/REVIEWER_GUIDE.md` are updated in this pass to stop suggesting them
  as the fix for a `5432`/`6379` collision on a plain `docker compose up`,
  since that collision can no longer happen there.

## Related work

[PR #2578](https://github.com/cirwel/unitares/pull/2578) hardens a different
dimension of the same fresh-install audit this pass responds to — the local
session-file permissions (`scripts/unitares` writing
`~/.unitares_session.json` world/group-readable) and the Docker build
context leaking `.unitares/`/`secrets/` via a missing `.dockerignore` entry.
It does not touch `docker-compose.yml` or either Dockerfile's `USER`
handling.

None of the gaps above are fixed by this pass. They're recorded here so the
next pass doesn't have to re-derive them from a fresh read of the compose
file.
