# Cloud Deploy Runbook — Governance Stack Off the Home Machine

**Status:** Runbook (v1, 2026-11-12)
**Audience:** Operators whose primary deployment is a single physical machine
(laptop, desktop, home server) that can go offline — travel, relocation, power
loss, hardware failure — and who want the governance stack reachable anyway.

The root `docker-compose.yml` plus [`docker-compose.cloud.yml`](../../docker-compose.cloud.yml)
package the single-operator stack (Postgres 18 + AGE + pgvector, Redis, lease
plane, governance MCP server, and an optional outbound tunnel profile), so a
cloud deployment is a hosting decision, not a multi-tenant rewrite. This runbook
covers sizing a host, hardening the Compose defaults, exposing the server
(including OAuth public listeners and remote agent tokens), splitting cloud
record storage from local workstation harnesses, keeping verified off-site
backups, and the cutover discipline that prevents two instances from both
believing they are canonical.

## What moves and what does not

- **Moves cleanly (the canonical record plane):** `docker-compose.yml` +
  `docker-compose.cloud.yml` (plus `docker-compose.full.yml` when running
  `bge-m3` semantic/hybrid search) — the governance MCP server, Postgres/AGE,
  Redis, the lease plane, and outbound connector tunnels (`cloudflared`,
  OpenAI `tunnel-client`).
- **Stays on workstations and embodied hardware:**
  - Physical embodiment (`anima-mcp`, port 8766) stays with its sensors and
    display hardware, checking in remotely over HTTPS via
    `UNITARES_HTTP_AGENT_TOKENS`.
  - Workstation CLI orchestrators, host adapters (`claude`, `codex`,
    `antigravity`, `hermes`), local git worktrees, and machine-local health
    observers stay on the workstation, pointed at the cloud server's HTTPS
    endpoint (`UNITARES_SERVER_URL`). When the workstation sleeps, optional
    `consult` callbacks to workstation models degrade gracefully while the
    governance record, knowledge graph, dashboard, and cloud/embodied agents
    remain online.
- **State does not follow automatically.** A cloud `docker compose up` on
  empty volumes is a fresh, residentless install. Identity history, the
  knowledge graph, and session state stay wherever their Postgres and Redis
  data directories live. Moving them is an explicit migration (below), and it
  requires the source machine to be powered on — plan the dump before a
  planned outage, not during one.

## Choosing a host

Size the host to the search profile you run — adding `docker-compose.full.yml`
(`bge-m3` embeddings + hybrid ranking) roughly doubles memory and disk needs
compared with full-text search alone:

| Tier | Compose files | Minimum spec | Why |
|---|---|---|---|
| **Base FTS** | `docker-compose.yml` + `docker-compose.cloud.yml` | **4 GB RAM / 40 GB SSD** | Comfortable for PostgreSQL 18 + AGE, Redis, BEAM lease plane, Python server, and rolling local backup staging. |
| **Full Hybrid (`bge-m3`)** | Base + `docker-compose.full.yml` | **8 GB RAM / 80 GB NVMe** | `governance-mcp` holds ~1.7–2.2 GB RSS with `bge-m3` loaded, `/app/data/hf` caches ~4.3 GB of weights, and long-lived Postgres/AGE clusters plus 14-archive backup retention (`KEEP_COUNT=14`, two weeks on a daily schedule) routinely exceed 20–35 GB. |

- **Plain Linux VPS (recommended default).** Plain Docker Engine on Ubuntu/Debian
  is the least surprising environment for these Compose files. Pick a region near
  your primary workstation so interactive hook and Tailscale round-trips stay
  under ~35 ms.
- **Free-tier ARM instances** (e.g. Oracle Cloud's always-free 4-core/24 GB
  shape) match the project's run-free policy and have headroom for `bge-m3` or
  an optional local Ollama model. Treat them as opportunistic: capacity is
  scarce in popular regions and every image in the stack must be `arm64`
  (the published release images ship `linux/amd64` and `linux/arm64`).
- **Container PaaS (Fly.io, Railway, Render) works but is fiddlier.** No
  managed Postgres product supports Apache AGE, so Postgres must run as an
  ordinary container from `db/postgres/Dockerfile.age-vector` with a persistent
  volume — at which point a plain VPS is simpler and usually cheaper.

## Deploy

1. Provision the host; install Docker Engine with the Compose plugin.
2. Clone the repository at a published release tag (see `README.md#install`) and enter it.
3. `cp .env.example .env`, set `COMPOSE_FILE=docker-compose.yml:docker-compose.cloud.yml`
   (append `:docker-compose.full.yml` for `bge-m3` hybrid search, and add
   `COMPOSE_PROFILES=cloudflare` when running the bundled `cloudflared` service so
   `docker compose up -d` and `./scripts/unitares update` preserve and recreate the
   entire overlay stack), and **replace every development default**.
   The Compose file ships loopback-only dev values that must not survive on a
   host with a public route to it:
   - `POSTGRES_PASSWORD` — non-default; set before the first volume initialization.
   - `LEASE_PLANE_BEARER_TOKEN` — unique random value.
   - `UNITARES_CONTINUITY_TOKEN_SECRET` — not a shipped default. Left empty,
     the server generates one at startup and keeps it under `data/secrets/`
     on the `governance-data` volume, which must travel with a migration.
     Set it only to share a key.
   - `UNITARES_LEASE_ATTESTATION_SIGNING_KEY` — fresh 32-byte Ed25519 seed,
     base64url without padding (`python3 -c 'import os,base64;
     print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))'`).
   - `UNITARES_LEASE_ATTESTATION_ISSUER` / `_AUDIENCE` — stable,
     deployment-specific identifiers; never reuse across independent
     deployments.
   - `UNITARES_MCP_BEARER_TOKENS` — a unique bearer used by every remote MCP
     and REST client. A non-empty allowlist makes REST strict by default; do
     not set `UNITARES_REST_STRICT=0`. This is load-bearing behind a loopback
     tunnel, where the local trusted-network branch would otherwise bypass
     `UNITARES_HTTP_API_TOKEN`.
   - `UNITARES_MCP_ALLOWED_HOSTS` — include the tunnel's public hostname exactly
     as it arrives in the HTTP `Host` header, without a scheme (for example,
     `localhost,127.0.0.1,governance.example.com,governance.example.com:*`).
     Otherwise `/health/ready` can pass while the MCP transport rejects the
     tunnel with 421.
   - `UNITARES_MCP_ALLOWED_ORIGINS` / `UNITARES_HTTP_CORS_EXTRA_ORIGINS` — put
     each browser MCP client's exact origin in **both** lists, including
     `https://` and excluding a trailing slash (for example,
     `http://localhost:8767,http://127.0.0.1:8767,https://governance.example.com`).
     The first list configures MCP transport Origin validation; the second
     configures the outer HTTP CORS preflight. Omitting either blocks the
     browser before it can use MCP. The cloud runtime verifier is not a browser
     and does not prove this CORS configuration.
   - `UNITARES_DASHBOARD_RP_ID` / `UNITARES_DASHBOARD_ORIGIN` — set these to the
     public dashboard hostname and exact HTTPS origin (for example,
     `governance.example.com` and `https://governance.example.com`). Passkey
     enrollment and sign-in are bound to this pair. If `_ORIGIN` is empty it
     defaults to `https://` plus the RP id. With no RP id, passkey sign-in is
     off and its endpoints answer 503 naming these settings.
   - `UNITARES_MCP_BEARER_TOKEN` — the singular client credential used by the
     lease plane for governance REST calls. Set it to one exact member of the
     plural `UNITARES_MCP_BEARER_TOKENS` allowlist above.
   - `UNITARES_OPERATOR_TOKENS` — configure at least one unique, high-entropy
     token to bootstrap the first dashboard passkey. The same operator
     credential is required whenever an operator mints another one-time
     enrollment code, and when external outcome producers post in.
   - `UNITARES_HTTP_AGENT_TOKENS` — optional comma-separated
     `<agent_uuid>:sha256:<64 hex>` digests (passed through by
     `docker-compose.cloud.yml`) so remote embodied or resident agents can call
     `process_agent_update` on `POST /v1/tools/call` without holding a fleet-wide
     bearer (`src/http_agent_tokens.py`).
   - `UNITARES_OAUTH_ISSUER_URL` / `UNITARES_OAUTH_PUBLIC_PORT` / `UNITARES_OAUTH_REQUIRED` —
     optional OAuth 2.1 configuration for hosted connectors such as Claude.ai and
     ChatGPT (`docker-compose.cloud.yml`). Keep `UNITARES_OAUTH_DYNAMIC_REGISTRATION=false`
     and `UNITARES_OAUTH_AUTO_APPROVE=false` in normal operation; enabling both on
     a public hostname lets any caller register a client and obtain a token.
   - Dashboard users authenticate with a passkey-backed dashboard session.
     The RP id and origin above must match the browser-visible deployment.
     Strict REST ignores `UNITARES_HTTP_API_TOKEN`, even on loopback;
     non-browser telemetry and REST clients must send a member of
     `UNITARES_MCP_BEARER_TOKENS` (or a scoped `UNITARES_HTTP_AGENT_TOKENS`
     credential for check-ins).
4. Start the stack and wait for health checks (`docker compose up -d --wait`, or
   add `--build` when using `docker-compose.full.yml` or a source branch; add
   `--profile cloudflare` when running the bundled `cloudflared` container). Do
   not paste `docker compose config` output into tickets or public logs — it
   expands every secret from `.env`.
5. Verify: `curl -fsS http://127.0.0.1:8767/health/ready` returns 200, then
   verify the public tunnel gate and an MCP client `start_session(force_new=true)`
   round-trip (next section).

`scripts/ops/rotate-secrets.sh` rotates secrets for the macOS launchd install
only. On a Compose host, rotate by editing `.env` (the bearer allowlist and
its singular member, the lease-plane bearer and the attestation signing key)
and running `docker compose up -d` to recreate the services.

## Exposure: keep loopback binding, tunnel in

Both `docker-compose.yml` and `docker-compose.cloud.yml` bind published host
ports to `127.0.0.1` on purpose. Keep that, and make an outbound tunnel or
authenticated tailnet HTTPS listener the only ingress — do not rebind ports to
`0.0.0.0`, open `:8767`/`:8788` in the provider firewall, or attach an
unauthenticated tailnet-to-loopback TCP proxy.

- **Cloudflare Tunnel + OAuth public listener (`--profile cloudflare`):**
  `mcp_transport_service.bind_public_socket` (`src/services/mcp_transport_service.py`) binds
  `UNITARES_OAUTH_PUBLIC_PORT` (for example `8772`) to `127.0.0.1` *inside* the
  `governance-mcp` container's network namespace, where a Docker bridge `ports:`
  mapping cannot reach it. In `docker-compose.cloud.yml`, the opt-in
  `cloudflared` service sets `network_mode: "service:governance-mcp"`, sharing
  that container loopback directly:
  1. Set `COMPOSE_PROFILES=cloudflare`, `CLOUDFLARE_TUNNEL_TOKEN`,
     `UNITARES_OAUTH_ISSUER_URL=https://governance.example.com`,
     `UNITARES_OAUTH_PUBLIC_PORT=8772`, and `UNITARES_OAUTH_REQUIRED=1` in `.env`
     (alongside a non-empty `UNITARES_MCP_BEARER_TOKENS` for the main `:8767`
     listener). Persisting `COMPOSE_PROFILES=cloudflare` in `.env` ensures every
     subsequent `docker compose up -d` (including secret rotation and
     `./scripts/unitares update`) recreates `cloudflared` whenever `governance-mcp`
     is recreated.
  2. In the Cloudflare Zero Trust tunnel dashboard, route `governance.example.com`
     to `http://127.0.0.1:8772` with a fallback catch-all rule of `http_status:404`
     (never point the public tunnel at Postgres/Redis or leave an unauthenticated
     listener behind it).
  3. Start with `docker compose up -d --wait`. Note: if `governance-mcp` later
     crash-restarts in place (`restart: unless-stopped` or `docker restart`),
     `cloudflared` stays attached to the old network sandbox until recreated via
     `docker compose up -d --force-recreate cloudflared` (run `cloudflared` on
     the host instead if you want tunnel survival across container crash-restarts).
  4. Sanity-check from outside:
     `curl -i https://governance.example.com/mcp/ -H 'Accept: text/event-stream'`
     must return `401` (OAuth challenge), never `200` or `421`.
- **Host-installed `cloudflared` or secondary connector tunnels (e.g. OpenAI `tunnel-client`):**
  A tunnel daemon installed on the VPS host outside Compose reaches
  `http://127.0.0.1:8767` (host loopback), not container-internal `:8772`. If a
  host-level `cloudflared` is your public OAuth ingress, leave
  `UNITARES_OAUTH_PUBLIC_PORT` unset so OAuth gates `/mcp` on `:8767` directly.
  When using the bundled `cloudflared` container (`COMPOSE_PROFILES=cloudflare`)
  for public OAuth on `:8772`, host `127.0.0.1:8767` remains available on the VPS
  for Bearer-gated local callers or a secondary connector daemon. Reusing the
  public hostname that previously pointed at the home machine means remote MCP
  client URLs do not change at cutover.
- **Tailscale HTTPS (`tailscale serve`):** join the VPS to your tailnet and
  expose `http://127.0.0.1:8767` over `tailscale serve` HTTPS with a non-empty
  `UNITARES_MCP_BEARER_TOKENS` allowlist (required so `/mcp` is Bearer-gated and
  REST stays strict; `UNITARES_HTTP_AGENT_TOKENS` is an optional per-agent
  addition for `POST /v1/tools/call`, never a substitute for the bearer
  allowlist), and add `https://<node>.<tailnet>.ts.net` to
  `UNITARES_MCP_ALLOWED_HOSTS` / `UNITARES_MCP_ALLOWED_ORIGINS`.

Either way the governance HTTP surface is now reachable beyond a single
workstation, which is why step 3 above is not optional: the dev-default signing
key and bearer token are public knowledge (they are in this repository).

### Splitting cloud record from workstation harnesses

When Core moves to a VPS while coding harnesses (`claude`, `codex`,
`antigravity`, `hermes`) and `agent-orchestrator` stay on a laptop or desktop:

- **Client hooks and MCP config:** point `UNITARES_SERVER_URL` (and MCP client
  URLs) at the authenticated HTTPS origin (`https://governance.example.com` or
  your `tailscale serve` HTTPS URL) with a bearer from
  `UNITARES_MCP_BEARER_TOKENS`. This matches the `:443` HTTPS requirement for
  sandboxed cloud coding sessions ([`cloud-session-plugin.md`](cloud-session-plugin.md)).
- **Local per-file edit leases vs. server coordination:** `dialectic_beam` and
  `maintenance` coordination leases run against the co-located `lease-plane`
  container on the VPS. Workstation `pre-edit` file leases can either stay
  disabled (`UNITARES_FILE_LEASES_REQUIRED=0`, relying on worktree isolation) or
  use a workstation-local lease plane for same-disk checkouts.
- **Workstation `consult` callbacks:** if the cloud server delegates advisory
  `consult` calls back to workstation inference or CLI host adapters over
  Tailscale, list `100.64.0.0/10` in `UNITARES_TRUSTED_NETWORKS` (so
  `privacy='local'` classification recognizes the tailnet peer; with
  `UNITARES_MCP_BEARER_TOKENS` set, strict REST remains enforced) and keep short
  connect timeouts so a sleeping workstation fails fast without affecting
  check-ins or knowledge search.

### Bootstrap the first dashboard passkey

After the tunnel is live, use one exact member of `UNITARES_OPERATOR_TOKENS`
to mint the initial 10-minute, single-use enrollment code. Keep the token out
of URLs and shell history; the placeholder below should come from a secret
manager or a non-logged interactive shell:

```bash
read -rsp 'Bootstrap operator token: ' UNITARES_BOOTSTRAP_OPERATOR_TOKEN
printf '\n'
curl -fsS -X POST \
  -H "X-Unitares-Operator: ${UNITARES_BOOTSTRAP_OPERATOR_TOKEN}" \
  https://governance.example.com/auth/enroll
unset UNITARES_BOOTSTRAP_OPERATOR_TOKEN
```

Open `https://governance.example.com/auth/signin?enroll=1`, type the returned
code, and create the passkey before the code expires. The `enroll=1` query is
only a nonsecret UI marker; the code itself is sent in a request header. Sign
in with the new passkey before treating dashboard access as recovered. Keep an
operator token configured if this deployment must mint additional enrollment
codes or accept external operator writes.

## Off-site backups

`scripts/ops/backup_governance.sh` targets native macOS Homebrew PostgreSQL. On
a Compose VPS, use [`scripts/ops/backup_compose.sh`](../../scripts/ops/backup_compose.sh)
together with [`scripts/ops/check_governance_backup_health.sh`](../../scripts/ops/check_governance_backup_health.sh):

1. **All three stateful stores are staged per run and published to `$BACKUP_DIR` (`0600`) only after every artifact verifies** (captures run sequentially against the live stack; for an exact quiescent cutover snapshot, stop writers first as in *Migration and cutover*):
   - `governance_<timestamp>.sql.gz` — `pg_dump` from `unitares-postgres-age`,
     verified with `gzip -t` and pg_dump's completion trailer.
   - `redis_<timestamp>.rdb` — captured after triggering `BGSAVE`, waiting for
     `LASTSAVE` to advance with `rdb_last_bgsave_status:ok`, validating CRC64
     and RDB structure in-container via `redis-check-rdb`, and checking the
     copied file's `REDISdddd` header and `0xff` EOF marker (most live
     session/identity bindings and OAuth client registrations live in Redis).
   - `governance_data_<timestamp>.tar.gz` — streamed directly from `/app/data`
     inside `unitares-governance-mcp` while skipping `/app/data/hf` model
     weights in place, so an auto-generated `continuity_token_secret` travels
     with the dump without copying multi-GB weights onto backup disk.
2. **Encrypt to a public recipient only, and ship off-box immediately.** Keep
   an `age` public key on the VPS (`AGE_RECIPIENT_FILE=/etc/unitares/backup.agekey.pub`)
   and keep the private decryption key *off* the VPS so a compromised host
   cannot decrypt historical dumps (`core.agents` and audit rows can hold
   sensitive operational text). When an `age` recipient is configured,
   `backup_compose.sh` defaults `BACKUP_KEEP_PLAINTEXT=0`, deleting staged
   plaintext inside `.stage_<timestamp>_*` before uploading and publishing `.age`
   archives plus `manifest_<timestamp>.sha256` into `$BACKUP_DIR` (any orphaned
   `.stage_*` directory from a prior crash is swept on the next lock acquisition;
   if the host already holds earlier unencrypted dumps, verify `age -d` on one
   encrypted set first and then run once with `PURGE_PRIOR_PLAINTEXT=1` to
   remove the older plaintext files).
   Use `rclone copy "$BACKUP_STAGE_DIR"` (never `rclone sync`, which would mirror
   local pruning or wipe remote history from a rebuilt host):

   ```bash
   AGE_RECIPIENT_FILE=/etc/unitares/backup.agekey.pub \
   BACKUP_SYNC_CMD='rclone copy "$BACKUP_STAGE_DIR" remote:unitares-backups/' \
     ./scripts/ops/backup_compose.sh

   ./scripts/ops/check_governance_backup_health.sh
   ```

   **Escrow `.env` separately** in your password manager or secret store:
   `backup_compose.sh` backs up container volumes, never `.env` (tunnel tokens,
   OAuth client secrets, bearer allowlists, and `POSTGRES_PASSWORD`).

**A backup that has never been restored is a hypothesis.** Do one restore
drill per deployment on fresh volumes:
1. Restore the PostgreSQL SQL dump (see *PostgreSQL + Apache AGE restore notes* below)
   and unpack `governance_data_<timestamp>.tar.gz` into `/app/data` (`secrets/`).
2. Because `docker-compose.yml` runs `redis-server --appendonly yes` (and Redis 7+
   ignores `dump.rdb` when AOF is enabled with no `appendonlydir`), bootstrap
   Redis from the RDB once before starting normal AOF mode: copy
   `redis_<timestamp>.rdb` into `/data/dump.rdb` on the fresh `redis-data`
   volume, start Redis once with `--appendonly no` (`docker compose run --rm -d --name redis-restore redis redis-server --appendonly no`),
   run `docker exec redis-restore redis-cli BGREWRITEAOF` and wait for
   `aof_rewrite_in_progress:0`, then stop `redis-restore` and bring up the stack
   normally (`docker compose up -d --wait`).
3. Confirm both `docker compose exec -T redis redis-cli DBSIZE` (non-zero session /
   OAuth state) and `start_session` / `check_working_state` against Postgres
   identity history.

### PostgreSQL + Apache AGE restore notes

When restoring a `pg_dump` from a source cluster onto a fresh PostgreSQL
cluster or container:

1. **Source roles:** if the source cluster owned objects or granted read access
   to local roles that do not exist on the target cluster, either pre-create
   those roles (`CREATE ROLE <name>;`) before `psql -f` or restore with
   `--no-owner --no-acl`.
2. **Apache AGE catalog OID drift:** `pg_dump` copies the source database's
   literal `pg_namespace` OID into `ag_catalog.ag_graph.graphid` and
   `ag_catalog.ag_label.graph`, while `CREATE SCHEMA governance_graph` on the
   target cluster allocates a new namespace OID. `GraphMixin` automatically
   reconciles `ag_catalog.ag_graph` and `ag_catalog.ag_label` in-place on the
   first AGE probe or query after restore. That repair needs `UPDATE` on both
   catalog tables. A role without it, such as a native install's `NOSUPERUSER`
   application role, leaves the graph unavailable and its data untouched until
   a role that can update the catalog runs the statement below. Run the same
   statement if you are verifying a restored database directly via `psql`
   before starting the server:

   ```sql
   WITH old_graph AS (
       SELECT g.graphid AS old_id, n.oid AS new_id
       FROM ag_catalog.ag_graph AS g
       JOIN pg_namespace AS n ON n.nspname = g.name
       WHERE g.name = 'governance_graph'
         AND g.graphid <> n.oid
   ), updated_graph AS (
       UPDATE ag_catalog.ag_graph AS g
       SET graphid = old_graph.new_id,
           namespace = old_graph.new_id::regnamespace
       FROM old_graph
       WHERE g.name = 'governance_graph'
       RETURNING g.graphid
   )
   UPDATE ag_catalog.ag_label AS l
   SET graph = old_graph.new_id
   FROM old_graph, updated_graph
   WHERE l.graph = old_graph.old_id;
   ```

3. **Existing findings without `bge-m3` embeddings:** if enabling
   `docker-compose.full.yml` for the first time after restoring a dump from an
   FTS-only deployment, backfill missing vectors once with:
   `docker compose exec governance-mcp python scripts/migration/reembed_corpus.py --only-missing`.

### Redis 7 to 8 rollout

Redis Open Source supports upgrading a standalone Redis 7.x instance to Redis
8. This stack persists session and identity state in the `redis-data` volume,
so upgrade that service during a maintenance window rather than treating it as
a disposable cache:

1. Run `./scripts/ops/backup_compose.sh` and the restore drill above while the
   service is still on Redis 7.
2. Pull the new image and recreate only Redis: `docker compose pull redis &&
   docker compose up -d redis`.
3. Wait for `docker compose exec -T redis redis-cli PING` to return `PONG`,
   then verify that an existing MCP client can resolve its session identity.
4. Keep the copied RDB/AOF artifacts until the deployment has completed a
   normal session-TTL cycle without Redis errors.

The Compose definition tracks `redis:8-alpine` for patch updates within Redis
8. Rollback is an operator decision: stop Redis and restore the pre-upgrade
snapshot into the named volume before starting a Redis 7 image again.

## Migration and cutover

The invariant: **exactly one instance is canonical at a time, and every ingress
tunnel points at it.** Two instances that both accept writes diverge in ways
that cannot be merged later — Postgres rows can be reconciled by hand at great
cost; interleaved identity/session state effectively cannot.

- **Planned cutover (home machine to cloud VPS):**
  1. Stand up and restore a rehearsal dump on the VPS first (without pointing
     production tunnels at it) to verify `bge-m3` / AGE queries, backup health,
     and Tailscale RTT.
  2. Stop every inbound tunnel on the source machine (`cloudflared`, connector
     tunnels) and stop the source `governance-mcp` and `lease-plane` writers.
  3. Take a final PostgreSQL dump, trigger Redis `SAVE`/`BGSAVE`, and copy the
     SQL dump, `dump.rdb`, and `continuity_token_secret` /
     `UNITARES_LEASE_ATTESTATION_SIGNING_KEY` to the cloud host.
  4. Restore into the cloud volumes, verify `/health/ready` and gated
     `/health/deep`, start the cloud tunnels, and repoint workstation
     `UNITARES_SERVER_URL` plus any remote embodied or resident HTTP clients
     (`POST /v1/tools/call`) to the cloud endpoint.
     When the home machine returns, keep its former server writers disabled so
     it rejoins purely as a client/harness host.
- **Unplanned outage (source machine already unreachable):** the live dump is
  unreachable unless an off-site backup from `backup_governance.sh` /
  `backup_compose.sh` is current. Restore from the newest verified off-site
  dump if continuity is acceptable, or bring up a fresh cloud install with a
  distinct attestation issuer/audience and reconcile deliberately when the
  source machine returns.
- **Returning home:** the same procedure in reverse, or simpler: leave the
  cloud instance canonical permanently and keep the home machine as the
  offline-tolerant harness and embodiment workstation.

## Decommission

When retiring a cloud instance: run a final `./scripts/ops/backup_compose.sh`,
confirm it restored elsewhere, remove the tunnel routes, `docker compose down -v`,
and revoke the instance's secrets (bearer tokens, OAuth static client secret,
tunnel token, and attestation issuer mapping in any lease plane that trusted it).
