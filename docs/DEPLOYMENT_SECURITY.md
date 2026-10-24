# Deployment security for the Compose stack

The default Compose stack is designed for one trusted operator on one host.
The governance server and lease plane are published on host loopback, while
PostgreSQL and Redis remain unpublished on the default Compose network unless
the admin overlay is enabled. These defaults reduce accidental exposure; they
do not make the stack a hardened multi-user or internet-facing deployment.

This page owns the current infrastructure posture of the base
[`docker-compose.yml`](../docker-compose.yml). The
[scope and threat model](SCOPE_AND_THREAT_MODEL.md) owns the product and
adversarial boundaries. The [security policy](../.github/SECURITY.md) owns
vulnerability reporting and report scope. Installation and hosted-deployment
procedures live in the [installation manual](manual/02-install.md) and the
[cloud deployment runbook](operations/cloud-deploy-runbook.md).

## Deployment profiles

| Profile | Host-published services | Intended use |
|---|---|---|
| Base quickstart | Governance MCP on `127.0.0.1:8767`; lease plane on `127.0.0.1:8788` | Local evaluation on a trusted host. It retains published development credentials and is not a shared-host security boundary. |
| Admin/debug overlay | The base ports plus PostgreSQL on `127.0.0.1:5432` and Redis on `127.0.0.1:6379` | Temporary local debugging. Redis still has no authentication, and PostgreSQL keeps its configured credential. |
| Core overlay | Governance MCP only; PostgreSQL remains internal, while Redis and the lease plane are disabled | Reduced-capability evaluation. Its security posture is not a substitute for the default stack's coordination and durable session services. |
| Full-search overlay | Same network topology as the base stack | Adds local embedding dependencies and a read-only scripts mount; it does not harden the base stack. |
| Shared-network or non-loopback deployment | Operator-defined | Not a hardened profile shipped by these Compose files. Follow the exposure checklist below and the hosted-deployment runbook. |

The governance and lease-plane processes listen on all container interfaces so
the host mappings work; it is the host-side mappings that bind to `127.0.0.1`.
Do not describe the containers themselves as loopback-bound.

## Controls enabled by default

### Container process controls

All four base services set `no-new-privileges` and drop every Linux capability.
Redis adds back `SETUID`, `SETGID`, `CHOWN`, and `FOWNER` so its entrypoint can
repair volume permissions and drop to the `redis` user. The PostgreSQL image
ends as its `postgres` user. The governance and lease-plane images declare no
`USER`, so they run as root inside their containers.

### Network publication

PostgreSQL and Redis have no host port in the base file. The governance and
lease-plane ports are published only on host loopback. The
[`docker-compose.admin.yml`](../docker-compose.admin.yml) overlay deliberately
publishes the database ports on loopback for host-side debugging; it does not
add authentication or otherwise harden either database.

### Request gates

The base profile leaves `UNITARES_MCP_BEARER_TOKENS` empty, so `/mcp` is
unauthenticated; its boundary is the loopback host publication and trusted-host
access. When configured, the MCP transport has a bearer gate, and OAuth can gate
the MCP resource as a separate deployment profile. Explicit MCP `Host` and
browser `Origin` allowlists apply independently. REST and WebSocket routes use a
separate local-versus-strict auth policy: a credential does not replace MCP
`Host` validation, and a correct `Host` does not replace authentication.

In local REST posture, loopback and private-network callers may be trusted
without presenting `UNITARES_HTTP_API_TOKEN`. A Compose-bridge caller normally
arrives from such a trusted address, so setting that token alone does not require
it on every REST request. A non-empty `UNITARES_MCP_BEARER_TOKENS` allowlist
turns strict REST on by default; set `UNITARES_MCP_BEARER_TOKEN` to one member
for in-repo callers such as the lease plane, and leave `UNITARES_REST_STRICT`
enabled for an exposed deployment. See [Running the server: exposing beyond
loopback](manual/03-running-the-server.md#35-exposing-beyond-loopback) for the
complete transport procedure.

Except for the static `GET /health` liveness response, the lease-plane HTTP
surface separately requires its bearer and fails requests closed when the
bearer is absent or wrong. Force-release routes require a distinct elevated
bearer that the base Compose file does not configure. The base file supplies a
published development bearer for ordinary routes, so reachability remains the
important boundary until that value is replaced.

### Secret generation and published-default detection

When `UNITARES_CONTINUITY_TOKEN_SECRET` is unset, the governance server creates
and persists its own continuity signing secret in the governance-data volume.
The retired Compose default is ignored rather than reused.

The base stack still publishes two development credentials in source:
`LEASE_PLANE_BEARER_TOKEN` and
`UNITARES_LEASE_ATTESTATION_SIGNING_KEY`. The governance server warns whenever
either remains at its published value. It refuses to start when a published
value is combined with configuration that signals beyond-host exposure, such as
a non-loopback allowed host/origin, an MCP bearer allowlist, a hosted dashboard
RP, or a non-loopback OAuth issuer. The refusal names variables, never values.

`UNITARES_ALLOW_PUBLISHED_DEFAULTS=1` downgrades that refusal to a warning for a
deliberate demo. It is not a production mode. The guard cannot see a Docker host
port mapping widened by hand, and it does not cover the PostgreSQL password,
unauthenticated Redis, or a missing MCP bearer.

### Health information

`/health`, `/health/live`, and `/health/ready` are intentionally shallow and
public. The `health_check` tool and `/health/deep` include component diagnostics
and the `insecure_defaults` check; `/health/deep` is gated by the normal REST
policy, which may admit trusted-network callers without an explicit credential
in local posture.

## Before exposing the stack beyond loopback

Treat any LAN binding, tunnel, reverse proxy, shared host, or public endpoint as
a new deployment profile. At minimum:

1. Set a strong `POSTGRES_PASSWORD` before the first volume initialization. A
   later `.env` edit does not rotate an existing database role.
2. Replace `LEASE_PLANE_BEARER_TOKEN` and
   `UNITARES_LEASE_ATTESTATION_SIGNING_KEY`; choose a stable attestation issuer
   and trusted-issuer mapping.
3. Configure a non-empty `UNITARES_MCP_BEARER_TOKENS`, give in-repo clients a
   matching singular `UNITARES_MCP_BEARER_TOKEN`, and keep strict REST enabled.
   OAuth may additionally gate `/mcp`, but it does not replace this bearer pair
   for strict REST or in-repo clients. If OAuth is enabled, pre-register the
   intended clients and set `UNITARES_OAUTH_DYNAMIC_REGISTRATION=false`; open
   dynamic registration with the default auto-approval does not exclude unknown
   callers.
4. Set exact MCP `Host` and `Origin` allowlists and any required browser CORS and
   dashboard relying-party values. A documented `host:*` entry may cover that
   host's presented port; do not use a broad wildcard instead of naming the
   authority the proxy presents.
5. Keep PostgreSQL and Redis unpublished unless a specific client requires
   direct host access. If they must be reachable, add independent network and
   credential controls rather than treating the admin overlay as hardening.
6. Inspect the rendered Compose configuration and deployed port mappings, then
   verify gated deep health. Do not paste rendered configuration into logs or
   tickets; it contains expanded secret values.
7. Remove `UNITARES_ALLOW_PUBLISHED_DEFAULTS` after any deliberate demo.

The [cloud deployment runbook](operations/cloud-deploy-runbook.md) owns the
full credential, proxy, passkey, and validation procedure. Completing that
procedure does not remove the architectural limitations below.

## Network isolation boundaries

Loopback is a machine boundary, not a user boundary: another process or account
on the same host can connect to a loopback-published service unless host policy
prevents it.

Unpublishing PostgreSQL and Redis has different effects by platform:

- On Docker Desktop, the VM normally hides the container bridge network from
  host processes, so an unpublished service is not directly reachable from the
  host.
- On native Linux Docker, the host namespace can normally route to container
  bridge addresses even when no port is published. Unpublishing removes the
  stable host port and reduces accidental discovery; it is not a strong barrier
  against another local account.

Host firewall and rootless/user-namespace policy are deployment concerns. The
Compose files do not install host-originated traffic rules, request user-
namespace remapping, or declare the default network `internal`.

## Known limitations

- **Root, writable application containers.** `governance-mcp` and `lease-plane`
  run as root inside writable container filesystems. No base service uses
  `read_only: true`. Compose requests no user-namespace remapping; the effective
  host UID boundary depends on the Docker daemon or rootless configuration.
- **Database privilege.** The application and lease plane connect with the
  configured PostgreSQL role, which is the image's superuser role by default.
  Container capability dropping does not reduce database privileges.
- **Incomplete credential coverage.** Redis has no authentication in the base
  stack. PostgreSQL defaults to `postgres`/`postgres`. The startup guard covers
  two published lease/attestation credentials, not every unsafe combination of
  binding, proxy, database, and auth settings.
- **Environment-delivered secrets.** Compose passes credentials through
  environment variables and `.env`; it does not use Docker secrets or another
  external secret store.
- **No general content DLP.** Free-form operator and agent text is not covered by
  a system-wide data-loss-prevention, sensitivity-label, or retention-policy
  layer. Treat it as persistent and secret-bearing unless a specific surface
  documents a bounded, scrubbed representation; a scrubber is defense in depth,
  not permission to submit secrets.
- **Broad governance image.** The governance image includes agent SDK/common
  packages, the dialectic reviewer, configuration, dashboard assets, and skills
  rather than a minimal core-only artifact.
- **Mixed image pinning.** Service images use release or upstream tags with
  `pull_policy: missing`; only the Python base is digest-pinned. Controlled
  deployments should record and verify the image digests they actually run.
- **Optional bind-mount permissions.** Dashboard extensions, an extra metrics
  catalog, and the full-search scripts mount add deployment-controlled files.
  Restrictive ownership can become unreadable after `cap_drop: ALL`; verify each
  enabled mount with the actual host UID and Docker mode.
- **No default egress restriction.** The Compose network is not marked
  `internal`; container outbound access is governed by the Docker host and its
  network policy.
- **Single-operator scope.** The shipped posture is not a multi-tenant security
  boundary. The security policy treats non-loopback exposure and attackers with
  host shell/root access as operator responsibilities.

## Verify the deployed state

Run these checks against the same Compose file set used to start the stack:

```bash
docker compose config --quiet
docker compose ps
docker compose port governance-mcp 8767
docker compose port lease-plane 8788
./scripts/unitares health
```

Use `docker compose config` without `--quiet` only in a private terminal when
you need to inspect expanded port, environment, or mount values; its output may
contain secrets. Inspect the running container controls when needed:

```bash
docker inspect --format '{{json .HostConfig.SecurityOpt}} {{json .HostConfig.CapDrop}} {{json .HostConfig.CapAdd}}' \
  unitares-governance-mcp unitares-lease-plane unitares-postgres-age unitares-redis
```

Read the `health_check` tool or gated `/health/deep` response for component
status and `insecure_defaults`. The shallow public health routes do not expose
those diagnostics. A host-side doctor probe of `localhost:5432` or
`localhost:6379` can report a healthy base Compose deployment as unavailable;
use `docker compose ps`, or enable the admin overlay only when direct host
database access is actually required.

The Docker Quickstart CI workflow exercises all four base services, deep health,
and MCP behavior. It does not validate an operator's host firewall, daemon
configuration, proxy, or secret custody.

## Change history

The current contract above is authoritative. These changes explain its main
boundaries; the PRs and changelog retain the implementation narrative.

| Change | Result |
|---|---|
| [#2580](https://github.com/cirwel/unitares/pull/2580) | Added capability dropping and `no-new-privileges`; stopped publishing PostgreSQL and Redis by default. |
| [#2658](https://github.com/cirwel/unitares/pull/2658) | Tightened browser-origin and host handling. |
| [#2674](https://github.com/cirwel/unitares/pull/2674) | Replaced the published continuity secret with generated persistent key material. |
| [#2957](https://github.com/cirwel/unitares/pull/2957) | Added published-default warnings, exposure detection, and startup refusal. |
