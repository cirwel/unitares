# Glama: private, storage-backed installation

This is an independent image. The existing `Dockerfile` and Compose installation
remain unchanged. Each installation owns its database, identity keys, Redis
bindings and lease plane. Never configure the directory listing with an
operator's production service credentials.

## Public description and capability scope

Suggested directory description:

> UNITARES is a self-hosted federation kernel for AI-agent identity, claims and
> evidence, review, outcomes, and reconstruction. Agents keep their own runtimes
> while sharing an attributed record through MCP. Fresh processes receive fresh
> identities; explicit lineage links inherited work. The private bundle supplies
> durable storage and coordination services. Advisory inference requires a
> configured provider, and completing peer review requires a reviewer.

The [capability and deployment guide](../CAPABILITIES_AND_DEPLOYMENT.md) maps
this framing to existing tools. Reconstruction means client retrieval of
findings, review records, and history, not a new tool or a validated claim of
complete recall. Core versus advanced capabilities are reading paths within one
catalog, not different tool modes or authorization levels.

The listing combines repository material with platform-generated copy and
cached inspection results. Updating this repository does not update all those
surfaces automatically. When refreshing the listing:

1. Correct any claim that restarted agent processes remain the same identity.
   A still-running client's binding surviving a server restart is a different case.
2. Remove blanket claims that no data can leave the deployment: optional
   cloud inference and configured integrations have their own processing policy.
3. Pin the reviewed commit and use the bundled build/environment inputs below;
   do not restore the old bare-process command and dummy database settings.
4. Record build commit, transport, interface version/hash, and dependency health
   alongside discovery. Compare actual names, not an unexplained tool count.
5. Validate durable store/read/restart operations as well as discovery. Report
   inference availability and completed review separately from a stored request.

`glama.json` is maintainer ownership metadata; it is not a capability manifest
or deployment profile. Historical 14/52/no-tools observations do not define
supported editions. The documented failure below demonstrates missing backing
dependencies in one build, not the cause of every count discrepancy.

## Reproduced defect (2026-09-08)

PR #2112 merged at 17:53:23 UTC. The later Glama test
`01a082c2-1edf-71b7-a398-b0df62e990e0` reports success for discovery at commit
`1a336c469fca99606a58ebc787be5c15f5d0dc9f`. Its actual spec is Debian trixie,
Python 3.14, `uv sync`, and
`mcp-proxy -- uv run python src/mcp_server_std.py`. The placeholder configuration
sets `DB_BACKEND`, `DB_POSTGRES_URL` (loopback dummy database), `DB_AGE_GRAPH`, and
`UNITARES_KNOWLEDGE_BACKEND=age`; it starts no backing services.

The instance logs explicitly report missing `asyncpg` and `redis` Python
packages. Claude's existing `glama-unitares-repro` image has the same commit,
command and missing dependencies; no competing Glama branch/PR was found.
A network-isolated reproduction initialized MCP and successfully called
`start_session` with `{"force_new":true}`. This subsequent valid request:

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_shared_memory","arguments":{"query":"glama-isolated-probe"}}}
```

returned this complete application error (MCP `isError` was false):

```json
{"success":false,"error":"Failed to search knowledge: asyncpg is required for PostgreSQL backend. pip install asyncpg","server_time":"2026-09-08T21:00:48.647509+00:00","error_code":"MISSING_REQUIRED","error_category":"validation_error","agent_signature":{"uuid":null}}
```

The generic `is required` message heuristic caused the validation label. It did
not indicate missing call arguments. Installing packages alone would expose the
next missing dependency: no PostgreSQL/Redis service or schema bootstrap.
The saved Glama environment schema also still advertised `UNITARES_DISABLE_ODE`;
the replacement schema removes that ineffective flag without changing ODE code.

## Build and validate locally

```sh
docker build -f Dockerfile.glama -t unitares-glama:local .
python3 scripts/ci/check_glama_bundle.py --image unitares-glama:local
docker build -f docker/glama/Dockerfile.proxy-test -t unitares-glama:proxy-test .
python3 scripts/ci/check_glama_proxy.py
```

The test owns disposable volumes and containers, has no external network or host
port mappings, uses a 2 GiB memory limit, and cleans up its resources. It drives
MCP itself, not just discovery. Service stderr logs remain in ignored
`unitares-glama-test-*.stderr.log` files for diagnosis. Only synthetic test data
is generated. Local isolated database testing was explicitly authorized by the
operator as an exception to AGENTS.md's additional-instance restriction.

For your own private installation:

```sh
docker volume create unitares-private-data
docker run --rm -i --network none --memory 2g \
  --mount source=unitares-private-data,target=/data \
  unitares-glama:local
```

Send MCP JSON-RPC through stdin. stdout contains only MCP messages; all backing
service diagnostics use stderr. Give Docker at least 45 seconds to stop the
container (`docker stop -t 45 <name>`). EOF on MCP stdin also shuts it down.

## Storage and supervision

- `/data/postgres`: PG18 data, owned by `postgres` (0700).
- `/data/redis`: Redis AOF with synchronous fsync, owned by `redis` (0700).
- `/data/unitares`: application files, owned by UID 10001 (0700).
- `/data/secrets.json`: generated DB/HTTP/lease credentials and identity signing
  keys, readable only by root (0600). Persist alongside the data.
- `/data/schema.sha256`: completed bootstrap fingerprint.
- `/data/.bundle.lock`: exclusive whole-volume writer lock.

Root is required for startup ownership management; PostgreSQL, Redis, the HTTP
server, lease plane, and MCP proxy run unprivileged. Services bind only loopback.
The public transport is stdio, proxied to the private HTTP server so the lease
plane and tool calls share one governance runtime. Internal URLs and credentials
are generated by the supervisor and cannot point at an operator database.

Fresh initialization uses the canonical `db/postgres/docker-initdb.sh` ordering,
including extensions, schemas, migrations, partitions and graphs. MCP starts
only after dependencies are ready. A child process exit terminates the container
with nonzero status; startup timeout or failed bootstrap cannot leave a successful
MCP listing. PostgreSQL uses fast, checkpointing shutdown after clients stop.
Redis commits its AOF before exit.

The bundle retains Compose's AGE 1.7.0 and pgvector 0.8.3 versions. Upstream
checks found AGE 1.8.0 (`PG18/v1.8.0-rc0`, marked a release) and pgvector 0.8.6;
upgrading those is separate from this installation repair. No embedding model,
external inference service, worker orchestrator or resident fleet is bundled.
`consult` needs a separately configured inference service; completing a peer
review needs a reviewer. Signed lease acquisition, collision refusal, replay
protection and handoff are included and tested.

## Restart and upgrade policy

A transport/server restart can retain a still-running client's Redis-backed
session binding when it supplies its existing `client_session_id`; it does not
turn a new process into the old identity. Fresh agent processes still onboard
fresh and declare real lineage when appropriate. Session TTLs still apply.

Same-schema image updates reuse the volume. Changed bootstrap/schema inputs,
missing completion markers, or a PostgreSQL major mismatch fail closed before
services start. The image does **not** automatically replay all migrations onto
existing data. An interrupted bootstrap is not silently considered initialized.

For a schema-changing upgrade, retain the old image, stop the entire container,
and back up the complete volume including Redis and identity keys. Use the
repository's existing migration/backup procedures on a disposable copy, validate
that copy with the new application, and explicitly record the new fingerprint
only after validating every required migration and extension. Do not edit the
fingerprint merely to bypass refusal. Until that procedure is completed, use
the previous image with its unchanged volume. Major PostgreSQL upgrades need an
explicit pg_upgrade or dump/restore. Automated in-place schema upgrades are not
provided by this image.

## Glama configuration (review before applying)

Use `docker/glama/build-spec.json` and `docker/glama/environment-schema.json` on
the listing's Dockerfile admin page. Pin `pinnedCommit` to the reviewed commit
before building. The common installer works as a Glama Debian-trixie build step;
CMD runs Glama's `mcp-proxy` outside the supervised stdio bundle. No production
URLs, passwords or placeholder service parameters are needed. Do not run
`uv sync` at runtime, as it would select a different environment.

[Glama hosting](https://glama.ai/mcp/hosting) documents configurable build steps
and CMD, and wraps stdio as Streamable HTTP.
[Persistent storage](https://glama.ai/blog/2025-12-06-mcp-hosting-with-persistent-storage)
is explicitly enabled per deployment at `/data`; that announcement limits it to
paid accounts and describes 1 GB volumes expanding to 10 GB. The current hosting
page describes metered hosting charges. These are platform documentation, not
proof of this account's entitlement or a measured runtime limit.

The signed-in admin showed no release at investigation time. Account deployment
settings subsequently returned Cloudflare 525 errors. Before a live deployment,
verify persistence entitlement/toggle, effective memory limit, UID/root policy,
startup deadline, and volume reuse across redeployments in the actual account.
Local tests do not establish those Glama properties. Do not provision paid
infrastructure without explicit approval.

After the configuration and validation are reviewable, a separately authorized
Glama build test can exercise the same storage flow on the platform. Follow
[Glama's release process](https://glama.ai/blog/2026-03-15-how-to-make-a-release)
only after that passes and publication is explicitly approved. No Glama release
or live deployment is created by this PR or its tests.

## Validation receipt (2026-09-08)

Local ARM64 Docker validation passed with a 2 GiB container memory limit:

- Fresh-volume MCP initialize, onboard, check-in, store, search and ID readback.
- PostgreSQL extensions: AGE 1.7.0 and pgvector 0.8.3.
- Restart with the same volume: finding readback and live-driver identity binding.
- AGE backend write/search, review-request storage, and graph finding readback
  after a database failure/restart.
- Signed two-agent coordination: impersonation rejection, replay rejection,
  exclusive ownership, collision refusal, atomic handoff and release.
- PostgreSQL and Redis process failure: nonzero container exit.
- Incompatible fingerprint and incomplete bootstrap: fail closed, no MCP stdout.
- Glama's mcp-proxy 6.4.3 wrapper over Streamable HTTP: onboard/store/search.
- Six focused supervisor tests passed. The repository's
  `./scripts/dev/test-cache.sh --quick` completed with **15,255 passed, 35 skipped**
  in 486.44 seconds; final supervisor changes also passed the focused tests and
  rebuilt-image smoke checks.

Observed fresh/restart handshake times were approximately 3–6 seconds on this
machine; these are local measurements, not Glama startup guarantees. The CI
workflow repeats the real image and wrapper tests on Linux x86_64. Glama account
execution, cross-image schema migration, automated reviewer workers and external
model inference remain outside this validation receipt.
