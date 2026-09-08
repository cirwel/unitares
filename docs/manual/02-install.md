# 2 · Installation

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)

Choose one path:

- **Docker** is the Tier-1 install contract and brings up PostgreSQL, Redis, the
  coordination lease plane, and the server together from a named release.
- **Bare metal** is the advanced macOS operator path. The canonical, maintained
  instructions live in the [install playbook](../install/PLAYBOOK.md); this
  chapter does not duplicate them or present it as an equivalent default.

## 2.1 Docker quickstart

The clone pin below names the latest verified public release, which can lag
the source version while a release is being prepared.

```bash
git clone --branch v2.21.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make coordination-demo
```

After cloning, `docker compose up -d --wait` is the one-command install/start;
there is no separate schema bootstrap. `make coordination-demo` verifies the
live coordination boundary by onboarding two participants, rejecting A's
request-bound attestation when it claims B's UUID, refusing replay of a captured
attestation, acquiring one `maintenance:/` surface, refusing a second holder,
handing ownership over through identity-checked mutations, and releasing it.
The local proof uses one operator and a deployment-specific audience. This
version intentionally permits one trusted issuer because lease rows do not yet
persist issuer-qualified principals; it does not establish cross-operator trust
or outcome benefit.

Run `make demo` next to send six warmup check-ins and print the real governance
API response shape. It verifies identity and telemetry wiring; it does not
exercise self-relative scoring or establish predictive value.

When it completes:

- MCP: `http://localhost:8767/mcp/`
- Dashboard: `http://localhost:8767/dashboard`
- Liveness: `http://localhost:8767/health/live`
- Lease plane: `http://127.0.0.1:8788/v1/health` (bearer-authenticated)

If the default ports are occupied:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 \
  LEASE_PLANE_HOST_PORT=18788 \
  docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
UNITARES_COORDINATION_DEMO_PORT=18788 make coordination-demo
```

### Choosing the advertised tool surface (v2.23.0 and later)

The default is the fourteen-tool `standard` profile: the five-name checkpoint
loop plus shared memory (`search_shared_memory`, `store_finding`,
`update_finding`), structured review (`request_review`), advisory inference
(`consult`), recovery (`self_recovery`), and inspection (`knowledge`,
`describe_tool`, `health_check`). To
choose a different profile, set `GOVERNANCE_TOOL_MODE` in the checkout's `.env`
file before starting the server, then run:

```bash
docker compose up -d --build --wait --force-recreate governance-mcp
```

| Profile | Advertises |
|---|---|
| `minimal` | the five checkpoint names alone |
| `standard` (default) | those five plus shared memory, review, advisory inference, recovery, and inspection |
| `lite` | the above plus the remaining consolidated routers and `list_tools` |
| `full` | every registered tool |

Reconnect the MCP client so it refreshes discovery. Compose explicitly passes
this variable into the server; changing the host environment without
recreating the container does not update a running service.

Profiles affect discovery, not authorization or dispatch: registered names
remain callable by name on every transport, subject to their normal gates. Be
deliberate when narrowing, though, because a schema-driven client offers the
model only the tools discovery returned, so a name that is not advertised is
one such a client cannot reach. The server states its own profile and what it
is withholding in the MCP `instructions` string returned at connect.

v2.22.0 defaults to the five-tool `minimal` profile and has no `standard`. Its
published Compose file does not forward `GOVERNANCE_TOOL_MODE` either, so
selecting a profile there needs the explicit override in the [release
errata](../releases/2.22.0-errata.md). v2.21.0 has an older six-tool `minimal`
profile and registration-time filtering on the HTTP MCP mount. Selecting a
profile on either does not reproduce this surface or its dispatch
compatibility; upgrade the server for those changes.

## 2.2 Bare-metal installation

Follow [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md). It owns the exact
PostgreSQL/AGE/pgvector versions, schema sequence, Python environment, expected
outputs, and failure recovery. Do not copy commands from historical proposals.
The `scripts/install/setup.py` helper only diagnoses and scaffolds this
advanced path; it is not a replacement for the Docker quickstart or playbook.

The production posture uses Redis as the de-facto session and identity store.
The server can boot without it in degraded local-only mode, which is adequate
for the demo but does not preserve production continuity.

## 2.3 Verify and continue

An install is ready when `/health/live` reports alive, the coordination demo
completes its refusal and handoff, the telemetry demo returns six well-formed
decisions, and the dashboard loads. Then continue to
[Running the server](03-running-the-server.md) and
[Integrating agents](04-integrating-agents.md).

Production hardening, bearer rotation, and remote exposure belong in
[Operating](06-operating.md) and the
[operator runbook](../operations/OPERATOR_RUNBOOK.md).

For a shared deployment, replace the Compose-only development Ed25519 seed in
`UNITARES_LEASE_ATTESTATION_SIGNING_KEY`, set a stable
`UNITARES_LEASE_ATTESTATION_ISSUER`, and configure each trusted peer as an
issuer-to-HTTPS `/v1/lease-holder/keys` entry in
`UNITARES_LEASE_TRUSTED_ISSUERS`. Never copy a private seed or continuity token
into the lease plane.

---

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)
