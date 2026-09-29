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
git clone --branch v3.0.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make coordination-demo
```

On a release checkout, Compose pulls the coordination lease plane as the image
published from that release tag (`ghcr.io/cirwel/unitares-lease-plane`) instead
of compiling Elixir on your machine. If that image cannot be pulled, for
example for a release that predates it, Compose prints a pull warning and builds
the lease plane from source. On a `master` checkout, run
`docker compose up -d --wait --build`: without `--build` the lease plane would
be the last release's image rather than the source you checked out.

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

The same lease plane protects files. With the
[governance plugin](https://github.com/cirwel/unitares-governance-plugin)
installed, Claude Code and Codex take a lease on each file before editing it and
release it afterwards, so a second agent editing the same file in the same
checkout is refused instead of overwriting the first. With the Compose defaults
this needs no setup: the plugin presents the stack's development bearer to the
loopback lease plane. If you set your own `LEASE_PLANE_BEARER_TOKEN` in `.env`,
put the same line in `~/.config/unitares/secrets.env` (or point
`UNITARES_SECRETS_ENV` at a file that has it) so the plugin can present it. When
leases are enabled but would not work, the plugin says so at session start.

Run `make demo` next to send six warmup check-ins and print the real governance
API response shape. It verifies identity and telemetry wiring; it does not
exercise self-relative scoring or establish predictive value.

When it completes:

- MCP: `http://localhost:8767/mcp/`
- Dashboard: `http://localhost:8767/dashboard`
- Liveness: `http://localhost:8767/health/live`
- Lease plane: `http://127.0.0.1:8788/v1/health` (bearer-authenticated)

Postgres and Redis aren't published to the host by default (see
`docs/DEPLOYMENT_SECURITY.md`), so only the governance server and lease plane
ports can collide with something already running. If either default port is
occupied:

```bash
GOVERNANCE_HOST_PORT=18767 LEASE_PLANE_HOST_PORT=18788 \
  docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
GOVERNANCE_HOST_PORT=18767 LEASE_PLANE_HOST_PORT=18788 make coordination-demo
```

The coordination demo talks to both the governance server and the lease plane,
so give it both ports. With only the lease-plane port it falls back to the
default governance port, 8767, and registers its demo agents on whatever server
answers there.

### Choose a model (optional)

UNITARES bundles no model. Naming one turns on two things: `consult` answers
from it, and a `request_review` that no peer has claimed can be taken by the
in-process local reviewer, which drafts the antithesis and a proposed
synthesis. Without a reachable model, `consult` answers "Standard advisory
consultation is unavailable" (`MODEL_PROVIDER_UNAVAILABLE`), and
`request_review` records the thesis and leaves the review
waiting for a peer or the operator. That also happens with a model when the
thesis is about the review system itself (recusal), when the model runs past
the review time budget, or when its synthesis does not approve.

This model is the only one a default install uses. The tools also describe a
stronger lane, `consult(effort='thorough')` and `delegate_inference`, which hand
a brief to a Codex, Claude or Antigravity CLI. That lane is an operator
extension: it runs through the agent orchestrator, which no Compose service
starts, so on a default install those calls fail and say the extension is not
configured. `list_inference_hosts` reports it under
`extensions.agent_orchestrator`. Nothing in this manual needs it.

The quickest way is to run, from the checkout, once Ollama is installed and has
a model pulled:

```bash
./scripts/unitares model
```

It lists the models your Ollama has, writes your choice to `.env`, rebuilds the
server, and checks that the server can reach the model. `--help` shows the
non-interactive flags; `--no-docker` prints the two settings for a source
install instead. To run it as plain `unitares`, link it onto your `PATH` once:
`ln -s "$PWD/scripts/unitares" ~/.local/bin/unitares`.

To do the same by hand, install Ollama on the Docker host, pull a model, and
name both in `.env`:

```bash
ollama pull <your-model>
cat >> .env <<'EOF'
UNITARES_OLLAMA_BASE=http://host.docker.internal:11434
UNITARES_LLM_MODEL=<your-model>
EOF
docker compose up -d --build governance-mcp
```

`UNITARES_OLLAMA_BASE` is the Ollama root URL, without `/v1`; the older name
`UNITARES_OLLAMA_BASE_URL` is still read when it is unset. On Docker Desktop the
container reaches the host's Ollama as is. On Linux, Ollama listens only on
127.0.0.1 by default: set `OLLAMA_HOST=0.0.0.0` for the Ollama service and allow
port 11434 from the Docker bridge, without exposing it beyond the machine,
because Ollama has no authentication. `--build` matters on an existing
install: the model client is installed when the image is built, and `up -d`
alone keeps an image built before this step. If your `docker-compose.yml` has
no `UNITARES_OLLAMA_BASE` line, the checkout predates this step. If it has one
but `consult` reports a missing dependency, the image predates it; rebuild with
`--build`.

### Updating

From the checkout, see whether a newer release is published, then move to it:

```bash
./scripts/unitares update --check
./scripts/unitares update
```

`update` resolves the newest published release (or `--to <tag>`), prints the
installed and target versions, and asks before changing anything. `--yes`
skips the question; without a terminal and without `--yes` it stops rather than
assume an answer. `--check` only reports and changes nothing. The command
manages only this checkout's Docker Compose stack, and it refuses, leaving the
code and the database schema as they were, when:

- tracked files in the checkout have local changes, or `db/postgres` holds
  untracked files (the database container would read a stray `NNN_*.sql` there
  as a migration);
- the database schema is newer than anything the target release knows, so
  moving to an older release is not supported (it learns this only after
  fetching the target's tag and starting the database, which stay fetched and
  running);
- this machine runs UNITARES as a macOS LaunchAgent ([§3.7](03-running-the-server.md#37-run-at-login-macos-launchagent))
  rather than from Compose.

On confirmation it:

1. checks out the target and stops the governance server and the lease plane,
   so nothing writes while the schema changes; it goes on only once both are
   confirmed stopped;
2. starts the database and, if the release has pending migrations, backs the
   database up, then applies them with `scripts/dev/apply_migrations.py` inside
   the database container, so no `psql` is needed on the host;
3. rebuilds and restarts the stack and reports deep health.

The backup is a gzipped plain-SQL `pg_dump` written to
`~/.unitares/backups/unitares-<time>-before-<tag>.sql.gz` (set
`UNITARES_BACKUP_DIR` to put it elsewhere) and readable only by your account.
If the dump fails, nothing is migrated. If a migration or the rebuild fails,
`update` checks the previous release back out and rebuilds it; migrations that
already applied stay applied, and the error names the backup. The command exits
non-zero unless deep health comes back healthy or moderate. A checkout already
on the target release goes through the same steps without moving the code.

Releases before this command existed shipped a `scripts/unitares update` that
posted a check-in instead. From one of those, move the code once by hand and
let the new `update` do the rest:

```bash
git fetch --depth 1 --no-tags origin "+refs/tags/vX.Y.Z:refs/tags/vX.Y.Z"
git checkout --detach vX.Y.Z
docker compose up -d --build --wait postgres-age
./scripts/unitares update --to vX.Y.Z
```

The database starts first so the new server never runs against the old
schema; `update` then applies the release's migrations and rebuilds, restarts
and health-checks the stack.

### Tool discovery (interface 1.13.0 and later)

The current source negotiates one complete catalog, including installed plugin
tools, while advertising a small progressive surface initially. Use
`list_tools(lite=true)` for every capability name, `describe_tool(tool_name=...,
action=...)` for its parameters, and `use_tool(tool_name=..., arguments={...})`
to invoke an omitted capability through its normal gates. Set
`UNITARES_TOOL_ADVERTISEMENT=full` in `.env` to advertise every schema up front,
then recreate the service and reconnect the client. Old `GOVERNANCE_TOOL_MODE`
values are ignored. Action authorization and identity gates are unchanged.

Earlier releases used `minimal`, `lite`, and `full` discovery profiles. The
`standard` profile existed only on unreleased master between #2102 and #2137
and never shipped in a release; v2.22.1 is the latest release and defaults to
`minimal` as described below. Those releases still require their own
configuration instructions; changing an old server's environment does not
install the unified catalog.

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
