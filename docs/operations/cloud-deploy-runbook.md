# Cloud Deploy Runbook — Governance Stack Off the Home Machine

**Status:** Runbook (v0, 2026-09-01)
**Audience:** Operators whose primary deployment is a single physical machine
(laptop, desktop, home server) that can go offline — travel, relocation, power
loss, hardware failure — and who want the governance stack reachable anyway.

The root `docker-compose.yml` already packages the full stack (Postgres 17 +
AGE + pgvector, Redis, lease plane, governance MCP server), so a cloud
deployment is a hosting decision, not a build project. This runbook covers
choosing a host, hardening the Compose defaults, exposing the server, keeping
off-site backups, and the cutover discipline that prevents two instances from
both believing they are canonical.

## What moves and what does not

- **Moves cleanly:** everything in `docker-compose.yml` — the governance MCP
  server, Postgres/AGE, Redis, and the lease plane. This is the entire
  governed surface an MCP client talks to.
- **Does not move:** the embodiment side (anima-mcp, port 8766) is bound to
  physical hardware and sensors. The governance server runs standalone
  without it; embodied residents pause with their hardware.
- **State does not follow automatically.** A cloud `docker compose up` on
  empty volumes is a fresh, residentless install. Identity history, the
  knowledge graph, and session state stay wherever their Postgres and Redis
  data directories live. Moving them is an explicit migration (below), and it
  requires the source machine to be powered on — plan the dump before a
  planned outage, not during one.

## Choosing a host

- **Small VPS (recommended default).** Any provider with ~4 GB RAM and a
  20 GB disk runs the stack comfortably; typical cost is in the $5–10/month
  range. Plain Docker on a plain Linux host is the least surprising
  environment for this Compose file.
- **Free-tier ARM instances** (e.g. Oracle Cloud's always-free 4-core/24 GB
  shape) match the project's run-free policy and have headroom for a local
  Ollama for the Watcher. Treat them as opportunistic: capacity is scarce in
  popular regions and the images must be arm64-compatible (the Compose stack
  builds from source images that publish arm64 variants).
- **Container PaaS (Fly.io, Railway, Render) works but is fiddlier.** No
  managed Postgres product supports Apache AGE, so Postgres must run as an
  ordinary container from `db/postgres/Dockerfile.age-vector` with a volume —
  at which point a plain VPS is simpler and usually cheaper.

## Deploy

1. Provision the host; install Docker Engine with the Compose plugin.
2. Clone the repository and enter it.
3. `cp .env.example .env` and **replace every development default**. The
   Compose file ships loopback-only dev values that must not survive on a
   host with a public route to it:
   - `POSTGRES_PASSWORD` — non-default.
   - `LEASE_PLANE_BEARER_TOKEN` — unique random value.
   - `UNITARES_CONTINUITY_TOKEN_SECRET` — unique random secret.
   - `UNITARES_LEASE_ATTESTATION_SIGNING_KEY` — fresh 32-byte Ed25519 seed,
     base64url without padding (`python3 -c 'import os,base64;
     print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))'`).
   - `UNITARES_LEASE_ATTESTATION_ISSUER` / `_AUDIENCE` — stable,
     deployment-specific identifiers; never reuse across independent
     deployments.
   - `UNITARES_HTTP_API_TOKEN` and (if external outcome producers post in)
     `UNITARES_OPERATOR_TOKENS`.
4. `docker compose up -d --build` and wait for health checks.
5. Verify: `curl -fsS http://127.0.0.1:8767/v1/tools` returns 200, then an
   MCP client `onboard()` round-trip through the tunnel (next section).

`scripts/ops/rotate-secrets.sh` documents the rotation path once the install
is live.

## Exposure: keep loopback binding, tunnel in

The Compose file binds every published port to `127.0.0.1` on purpose. Keep
that, and make an outbound tunnel the only ingress — do not rebind ports to
`0.0.0.0` or open them in the provider firewall.

- **Cloudflare Tunnel:** install `cloudflared` on the host, attach it to the
  existing tunnel hostname (or a new one) and route it to
  `http://127.0.0.1:8767`. Reusing the hostname that previously pointed at
  the home machine means MCP client configuration does not change at all —
  the cutover is entirely server-side. Run one more route for the lease
  plane (8788) only if remote clients actually need it.
- **Tailscale** is the simpler alternative when the clients are all personal
  devices: join the host to the tailnet and use its tailnet address; no
  public hostname exists at all.

Either way the governance HTTP surface is now reachable from untrusted
networks, which is why step 3 above is not optional: the dev-default signing
key and bearer token are public knowledge (they are in this repository).

## Off-site backups

`scripts/ops/backup_governance.sh` already produces daily compressed
`pg_dump` files with retention, a status JSON, and
`scripts/ops/check_governance_backup_health.sh` for staleness alerts — but it
targets a native (Homebrew) Postgres and writes to a directory on the same
machine. Two gaps to close on any deployment whose machine can be lost:

1. **The backup must leave the machine.** After each dump, sync the backup
   directory to object storage or another host — `rclone sync` to any S3/B2/
   R2-compatible bucket, or `restic` for encrypted deduplicated snapshots.
   Schedule it immediately after the dump job and alert on failure the same
   way the dump alerts.
2. **Redis is state, not cache, and needs its own snapshot.** Most live
   session/identity bindings exist only in Redis (see
   `docs/proposals/redis-retirement-v0.md`). Trigger `BGSAVE` (or rely on
   AOF, which the Compose service enables) and copy the resulting
   `dump.rdb`/`appendonly.aof` off-site alongside the SQL dump.

For a Compose install, run the equivalents inside the containers:

```bash
docker compose exec -T postgres-age pg_dump -U postgres governance \
  | gzip > governance_$(date +%Y%m%d_%H%M).sql.gz
docker compose exec -T redis redis-cli BGSAVE
docker compose cp redis:/data/dump.rdb redis_$(date +%Y%m%d_%H%M).rdb
```

Also include the `governance-data` volume (`/app/data`) — file-backed server
state — in whatever sync job ships the dumps.

**A backup that has never been restored is a hypothesis.** Do one restore
drill per deployment: fresh volumes, load the SQL dump, drop the RDB into
the Redis volume, `docker compose up`, confirm `onboard()` sees the expected
identity history.

## Migration and cutover

The invariant: **exactly one instance is canonical at a time, and the tunnel
hostname points at it.** Two instances that both accept writes diverge in
ways that cannot be merged later — Postgres rows can be reconciled by hand at
great cost; interleaved identity/session state effectively cannot.

- **Planned outage (machine will be off for a while):** dump Postgres and
  Redis on the source machine, restore into the cloud volumes, point the
  tunnel at the cloud host, and stop the source services before it goes
  offline. The cloud instance is now canonical; when the machine returns it
  rejoins as a client or a standby, not as a second writer.
- **Unplanned outage (machine already off):** the dump is unreachable, so do
  not try to impersonate the old instance. Either wait, or bring up a fresh
  cloud install and treat it as a parallel deployment with its own issuer and
  audience. When the machine returns, choose which database wins, migrate
  that one, and retire the other deliberately.
- **Returning home:** the same procedure in reverse, or simpler: leave the
  cloud instance canonical permanently and demote the home machine to the
  embodiment endpoint that is allowed to be offline. That split — stack in
  the cloud, hardware at home — makes future outages a non-event.

## Decommission

When retiring a cloud instance: take a final dump, confirm it restored
elsewhere, remove the tunnel route, `docker compose down -v`, and revoke the
instance's secrets (bearer token, attestation issuer mapping in any lease
plane that trusted it).
