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
git clone --branch v3.5.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make coordination-demo
```

### Managed checkout alternative

With Docker running, Git, curl and a POSIX shell, the launcher creates and
starts a shallow release checkout without a manual clone:

```sh
curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/scripts/install/launch.sh | sh
```

It resolves `PUBLISHED_VERSION`, keeps the checkout at `~/.unitares/stack`,
copies `.env.example` to `.env` only when missing, starts the release images,
and requires deep health `healthy` or `moderate`. Python for this health check
runs inside the container. Repeating the command preserves `.env` and the
installed release; Compose leaves unchanged running containers alone. It does
not upgrade an existing installation. A failed start can be retried after
fixing the reported problem. No package is installed or published by this path.
To inspect the script first, download that URL to a file and run `sh` on it.

Set `UNITARES_STACK_DIR` to an absolute path to choose a different directory
(pass it to `sh`, not just to `curl`, when using the pipeline). Set port or
credential overrides in that checkout's `.env` before starting a shared
installation. The launcher refuses containers owned by a different checkout.
The managed clone includes `db/postgres`, which Compose mounts for database
initialization, and the Git metadata used by the existing update command.

After startup, run the coordination acceptance demo using host Python 3
(the demo uses only its standard library). Python 3 and curl on the host are
also needed by the existing update CLI:

```sh
cd ~/.unitares/stack
python3 scripts/demo/coordination_demo.py
./scripts/unitares update --check
```

Use `./scripts/unitares update` from this directory for upgrades, following
the update instructions below; rerunning the launcher never moves Git or runs upgrade migrations.
The launcher URL becomes available when the launcher change lands on master.

On a release checkout, Compose pulls the server, the coordination lease plane
and the database as the images published from that release tag
(`ghcr.io/cirwel/unitares`, `ghcr.io/cirwel/unitares-lease-plane` and
`ghcr.io/cirwel/unitares-postgres`) instead of building them on your machine.
If an image cannot be pulled, for example for a release that predates it,
Compose prints a pull warning and builds that service from source. On a
`master` checkout, run `docker compose up -d --wait --build`: without `--build`
those services would be the last release's images rather than the source you
checked out.

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

The quickstart leaves several capabilities off, among them semantic search, a
model for `consult`, and cross-family review.
[§2.4](#24-quickstart-defaults-and-a-full-setup) lists each one, how to turn it
on, and what is missing without it.

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
so give it both ports. If only one port is moved and nothing in the environment
or `.env` states the other, it refuses to start (exit 2) rather than fall back
to the other default and register its demo agents on whatever server answers
there. If one half really is on its default port, set that port explicitly.

### Choose a model (optional)

UNITARES bundles no model. Naming one turns on two things: `consult` answers
from it, and a `request_review` that no peer has claimed can be taken by the
in-process local reviewer, which drafts the antithesis and a proposed
synthesis. Without a model, `consult` answers "Standard advisory
consultation is unavailable", with `MODEL_NOT_CONFIGURED` when none is named and
`MODEL_PROVIDER_UNAVAILABLE` when the named server does not answer, and
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

The orchestrator is not a model and is not an inference host. It is a separate
BEAM service that starts each CLI as a supervised child, enforces its timeout,
scrubs its environment and stops it. The hosts are the models it runs. It lives
outside the governance server so a long or stuck CLI cannot hold memory or a
request in that process.

The model is reached through one OpenAI-compatible endpoint, named by two
settings:

| Setting | Meaning | Default |
|---|---|---|
| `UNITARES_MODEL_BASE_URL` | Base URL of the model server, including `/v1` | `http://localhost:11434/v1` (Ollama on the same machine) |
| `UNITARES_MODEL_ID` | The default model, used when an agent names none | None. An agent picks with `consult(model=...)` from the models `list_inference_hosts` lists; with no default and none picked, the endpoint's only model is used, else the call is refused with `MODEL_NOT_CONFIGURED` (through v3.4.0 it fell back to `gemma4:latest`) |

Ollama is the server these steps are tested with. Other servers that speak the
same routes (vLLM, LM Studio, llama.cpp's server) can be named the same way,
but they are not yet verified end to end. A URL with no path at all, such as
`http://gpu-box:11434`, gets `/v1` added. A server that requires an API key is
not supported yet: the setting for one comes in a later release.

The quickest way is to run, from the checkout, once your model server is
running and has a model:

```bash
./scripts/unitares model
```

It lists the models the server offers (`GET {base}/models`), writes your choice
to `.env`, rebuilds the server, and checks that the server can reach the model.
`--base-url` points it at a server other than Ollama on this machine. When the
server is Ollama, it also gives Ollama's hints, such as `ollama pull`. `--help`
shows the non-interactive flags; `--no-docker` prints the two settings for a
source install instead. To run it as plain `unitares`, link it onto your `PATH`
once: `ln -s "$PWD/scripts/unitares" ~/.local/bin/unitares`.

To do the same by hand with Ollama on the Docker host, pull a model and name
both in `.env`:

```bash
ollama pull <your-model>
cat >> .env <<'EOF'
UNITARES_MODEL_BASE_URL=http://host.docker.internal:11434/v1
UNITARES_MODEL_ID=<your-model>
EOF
docker compose up -d --build governance-mcp
```

Inside the container, `localhost` is the container itself, so a server on the
Docker host is reached as `host.docker.internal`. On Docker Desktop the
container reaches the host's Ollama as is. On Linux, Ollama listens only on
127.0.0.1 by default: set `OLLAMA_HOST=0.0.0.0` for the Ollama service and allow
port 11434 from the Docker bridge, without exposing it beyond the machine,
because Ollama has no authentication. `--build` matters on an existing
install: the model client is installed when the image is built, and `up -d`
alone keeps an image built before this step. If your `docker-compose.yml` has
no `UNITARES_MODEL_BASE_URL` line, the checkout predates this step. If it has
one but `consult` reports a missing dependency, the image predates it; rebuild
with `--build`.

The older names `UNITARES_OLLAMA_BASE` and `UNITARES_OLLAMA_BASE_URL` (for
the endpoint) and `UNITARES_LLM_MODEL` (for the model) were removed in v3.3.0.
Nothing reads them any more, and nothing warns about them: an install that
still sets only those uses the default endpoint (Ollama on `localhost:11434`)
and has no model. Rename them to `UNITARES_MODEL_BASE_URL` and
`UNITARES_MODEL_ID`, in `.env` or in the LaunchAgent plist. An Ollama root URL
can be moved over as it is: a base with no path gets `/v1` added. Running
`./scripts/unitares model` rewrites `.env` with the new names and removes the old
`UNITARES_OLLAMA_BASE` and `UNITARES_LLM_MODEL` lines.

**Where the prompt goes.** `consult` (and the deprecated `call_model`, removed
no earlier than 2026-12-02) default to `privacy='local'`, and the in-process reviewer, check-in coaching and
knowledge synthesis always count as local. The server sends those only to an
endpoint it classifies as local, and it decides that from the URL alone,
never from DNS:

- an IP address is local when it is loopback, in a private RFC 1918 range, or
  in a network listed in `UNITARES_TRUSTED_NETWORKS` (the same networks the
  server's own access checks trust; list an IPv6 unique-local range there if
  your model server uses one);
- a hostname is local only when it is `localhost`, `host.docker.internal`, or
  listed in `UNITARES_MODEL_LOCAL_HOSTS` (comma-separated). A Compose service
  name such as `vllm`, or a machine on your network such as `gpu-box.lan`, is
  local only once listed there;
- `UNITARES_MODEL_PRIVACY=local` or `external` overrides both, for your own
  server on a public address.

Anything else is external, and a local request to it is refused before
anything is sent, with the error `MODEL_ENDPOINT_NOT_LOCAL` naming the setting
to change. A model on a Tailscale peer (a 100.64.0.0/10 address) is external
until you add that range to `UNITARES_TRUSTED_NETWORKS`. On a source install
that also lets callers from that network reach the REST API without a token;
under Compose, callers arrive through the Docker bridge, so it changes only
which model endpoints count as local.
`consult(privacy='cloud_allowed')` (or the deprecated
`call_model(privacy='auto')`) may still use an external endpoint.

### Reinstalling and side-by-side installs

Compose names the project after the checkout directory (`unitares`), and the
database, Redis and other state live in named volumes under that project name.
Running the clone-and-start command again, even from a fresh clone, reuses
those volumes, so the old database and its data come back. That is what makes
an upgrade keep its data.

- For a clean install, run `docker compose down -v` in the old checkout first.
  This deletes the stack's data.
- To keep the old install and add a second one, set `COMPOSE_PROJECT_NAME` to a
  different value in the new checkout's `.env`. The compose file also sets
  fixed `container_name` values, so run `docker compose down` (without `-v`, which
  keeps the old volumes) in the first checkout before starting the second one.
  `docker compose stop` is not enough: stopped containers keep their names.

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
already applied stay applied, and the error names the backup. The exception is
an attempt that crossed the protected dialectic schema frontier (migrations 077
and 078), or whose schema cannot be read afterwards: then `update` does not
roll the code back, but leaves the target release checked out with the
governance server and lease plane stopped for forward repair. The command exits
non-zero unless deep health comes back healthy or moderate. A checkout already
on the target release goes through the same steps without moving the code.

Releases before this command existed shipped a `scripts/unitares update` that
posted a check-in instead, and the `update` in v3.0.0 to v3.3.0 runs
migrations without the attestation that migrations 077 and 078 require, so
moving from any of them to v3.4.0 or later refuses and rolls back. From any of
those releases, move the code once by hand and let the new `update` do the
rest:

```bash
git fetch --depth 1 --no-tags origin "+refs/tags/vX.Y.Z:refs/tags/vX.Y.Z"
git checkout --detach vX.Y.Z
docker compose up -d --build --wait postgres-age   # only from a release before `update` existed
./scripts/unitares update --to vX.Y.Z
```

From a release before `update` existed, the database starts first so the new
server never runs against the old schema. From v3.0.0 to v3.3.0, leave that
line out: the new `update` stops the server and the lease plane before it
starts the database. `update` then applies the release's migrations and
rebuilds, restarts and health-checks the stack. If it stops before a release's
protected migrations are applied (077 and 078 for v3.4.0), it restarts the
stack from the checked-out Compose file; to run the previous release instead,
check that release out and run `docker compose up -d --wait`.

#### Updating a bare-metal or LaunchAgent install

`update` manages only a Compose stack, so on a [bare-metal install](#22-bare-metal-installation)
or a [LaunchAgent](03-running-the-server.md#37-run-at-login-macos-launchagent)
you run the same steps by hand, from the checkout. Set `DB_POSTGRES_URL` to
the URL your server uses (the playbook's is shown):

```bash
export DB_POSTGRES_URL="postgresql://localhost:5432/governance"
git status --porcelain --untracked-files=no     # must print nothing
git ls-files --others --exclude-standard -- db/postgres   # must print nothing
PREV=$(git rev-parse HEAD)                      # the release you are leaving
TAG="v$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION)"
echo "$TAG"                                     # the newest release; set TAG yourself to pick another
git fetch --depth 1 --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG"
```

The `db/postgres` check matters because the migration tool applies every
`NNN_*.sql` file it finds under `db/postgres/migrations/`, so an untracked file
there would reach the database even though the release does not ship it. Move
or delete anything it lists first.

Confirm this is not a downgrade. The first number is the database's schema,
the second the newest migration the release ships; if the first is larger,
stop, because moving to an older release is not supported:

```bash
psql "$DB_POSTGRES_URL" -Atqc "SELECT COALESCE(MAX(version), 0) FROM core.schema_migrations"
git ls-tree --name-only "$TAG" -- db/postgres/migrations/ \
  | sed -n 's|.*/\([0-9][0-9]*\)_[^/]*\.sql$|\1|p' | sort -n | tail -n 1 | sed 's/^0*//'
```

Stop the server, and the lease plane if you run one, so nothing writes while
the schema changes: unload each LaunchAgent you use, and press Ctrl-C in the
terminal of a server started in the foreground. Note which ones were running;
you start the same ones again at the end. Confirm they are gone by service
registration, process and listening port, not by an unanswered health check,
which a timeout would also produce. Then move the code and its Python
dependencies:

```bash
launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist   # if you use it
launchctl unload ~/Library/LaunchAgents/com.unitares.lease-plane.plist      # if you use it
launchctl list | grep -E 'com\.unitares\.(governance-mcp|lease-plane)'    # must print nothing
pgrep -fl 'src/mcp_server\.py|mix run --no-halt'                          # must print nothing
lsof -nP -iTCP:8767 -iTCP:8788 -sTCP:LISTEN                               # must print nothing
git checkout --detach "$TAG"
source .venv/bin/activate
pip install -r requirements-full.txt -c constraints.txt
```

Back the database up, list the pending migrations, then apply them:

```bash
mkdir -p ~/.unitares/backups && chmod 700 ~/.unitares/backups
( set -o pipefail; umask 077
  pg_dump "$DB_POSTGRES_URL" | gzip > ~/.unitares/backups/unitares-$(date +%Y%m%d-%H%M%S)-before-$TAG.sql.gz ) \
  && echo "backup ok"
python3 scripts/dev/apply_migrations.py --db-url "$DB_POSTGRES_URL"
python3 scripts/dev/apply_migrations.py --db-url "$DB_POSTGRES_URL" --apply --dialectic-writers-quiesced
```

Do not migrate unless the backup printed `backup ok`. `--dialectic-writers-quiesced` is your
statement that the server and lease plane are stopped; migrations 077 and 078
refuse to apply without it, and the tool does not check it for you, so pass it
only once the three checks above printed nothing. Those checks cover the whole
machine, so they also list another UNITARES instance, and the `mix run` pattern
matches any Elixir service. Look at what each listed process is
(`ps -o command= -p <pid>`), and stop anything that writes to this database. The migration tool calls
`psql`, which the Homebrew PostgreSQL install puts on your `PATH`.

If `git diff "$PREV" "$TAG" -- scripts/ops/com.unitares.governance-mcp.plist`
shows changes, carry them into your installed plist by hand before loading it.
Do not render it again with the [§3.7](03-running-the-server.md#37-run-at-login-macos-launchagent)
command: that writes new tokens and secrets and resets the database URL and
any other setting you changed, so clients stop authenticating and the server
can start against a database other than the one you just migrated.

The lease plane runs from whatever checkout its plist's `WorkingDirectory`
names, and the [lease-plane runbook](../operations/lease-plane-operator-runbook.md)
uses a separate deploy checkout for it. If that is not this checkout, move it
to the same release before you load the plist, or the previous release's
lease plane starts over the migrated schema:

```bash
plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.unitares.lease-plane.plist
LP=$(git -C "$(plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.unitares.lease-plane.plist)" rev-parse --show-toplevel)
git -C "$LP" fetch --depth 1 --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG"
git -C "$LP" checkout --detach "$TAG"
```

Then start what you stopped. The lease plane fetches its dependencies and
compiles at boot, so its first start on a new release takes longer. Deep
health should report `healthy` or `moderate`, and the lease plane's
`build_sha` should match the release:

```bash
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist   # or: python src/mcp_server.py --port 8767
launchctl load ~/Library/LaunchAgents/com.unitares.lease-plane.plist      # if you stopped it
./scripts/unitares health
curl -s http://localhost:8767/health/deep
curl -s http://127.0.0.1:8788/health; git rev-parse --short "$TAG^{commit}"   # if you run the lease plane
```

If a migration fails, do not start the previous release over the database
until you know where the schema stopped (the `psql` query above). If it did not
cross 077 or 078 during this update, you can go back with
`git checkout --detach "$PREV"`, reinstall its dependencies and start what
you stopped again. If it crossed either one, or you cannot read the version,
stay on the new release, fix the cause the error names and run the `--apply`
line again; the backup is the way back.

#### Updating the agent plugin

The [governance plugin](https://github.com/cirwel/unitares-governance-plugin)
is installed into each agent client, not the server, so neither path above
touches it. It publishes a new version alongside each server release, whose
release notes name the server version its skills match; update it after the
server. Claude Code
does not auto-update third-party marketplaces unless you turn that on under
`/plugin` > Marketplaces.

```bash
# Claude Code
claude plugin marketplace update unitares-governance
claude plugin update unitares-governance@unitares-governance

# Codex
codex plugin marketplace upgrade unitares-governance
```

In Codex, open `/plugins` and confirm the new version is installed, then open
`/hooks` to review and trust any hooks that changed. In both clients, hooks
load when a session starts: running sessions keep the old ones until you start
a new session (or run `/reload-plugins` in Claude Code).

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
and never shipped in a release; v2.22.1 is the last 2.x release and defaults to
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

To see what the quickstart leaves off and how to turn each part on, read
[§2.4](#24-quickstart-defaults-and-a-full-setup).

## 2.4 Quickstart defaults and a full setup

The Docker quickstart runs the server, PostgreSQL (with the AGE and pgvector
extensions installed), Redis and the lease plane. Several capabilities are off
in it, either because they need something the stack does not start (a model
server, the agent orchestrator, the embeddings package) or because they change
live decisions and wait on an operator. Each row names what the quickstart
does, how to turn the capability on, and what is missing while it is off.

Compose reads `.env` only to fill in variables that `docker-compose.yml` already
names in the `governance-mcp` environment block: the knowledge backend, the
`consult` model and the lease attestation settings. Any other variable in the
table needs an environment entry of its own. Put it in
`docker-compose.override.yml`, which Compose reads automatically:

```yaml
services:
  governance-mcp:
    environment:
      UNITARES_ENABLE_RERANKER: "1"
```

Then recreate the service (`docker compose up -d governance-mcp`), with
`--build` where the row says the image changes. Naming files with `-f` or
`COMPOSE_FILE` turns the automatic override off, so with the full override
below list all three:
`COMPOSE_FILE=docker-compose.yml:docker-compose.full.yml:docker-compose.override.yml`.

Semantic search and hybrid ranking come together in an override file, which
builds a local image with the embeddings package and sets both flags:

```bash
docker compose -f docker-compose.yml -f docker-compose.full.yml up -d --wait --build
```

Pass both files on every later `docker compose` command, or set
`COMPOSE_FILE=docker-compose.yml:docker-compose.full.yml` in `.env`. The file's
header lists what it changes and the one-time re-embed for findings stored
before it.

| Capability | Quickstart default | How to enable | Without it | Source |
|---|---|---|---|---|
| Knowledge graph backend | `age`: findings in relational tables, and the graph over them (`AUTHORED`, `RELATED_TO`, `TAGGED`, `RESPONDS_TO`, `SUPERSEDES`) in AGE. An install that ran `postgres` rebuilds the graph from its rows on the first knowledge-graph access after the switch | On by default. `UNITARES_KNOWLEDGE_BACKEND=postgres` keeps the findings and full-text search without the graph | With `postgres`: no graph edges; a superseded finding is flagged in search but carries no `superseded_by` pointer; no semantic search | `docker-compose.yml`; `src/knowledge_graph.py` (`get_knowledge_graph`); `src/storage/knowledge_graph_age.py` (`load`); `UNITARES_KNOWLEDGE_BACKEND` |
| Semantic search | Off: the image omits `sentence-transformers` and no embedding model is named | `docker-compose.full.yml` (above): it builds with `INSTALL_EMBEDDINGS=1` and sets `UNITARES_EMBEDDING_MODEL=bge-m3`; needs the `age` backend. Findings stored before then have no vector until `scripts/migration/reembed_corpus.py --only-missing` fills them | Search matches terms only; a query worded differently from the finding does not find it | `requirements-docker.txt`; `src/embeddings.py`; `src/storage/knowledge_graph_age.py` (`semantic_search`); `UNITARES_EMBEDDING_MODEL` |
| Hybrid ranking | Off | `docker-compose.full.yml` sets `UNITARES_ENABLE_HYBRID=1`, on top of semantic search | Results come from one ranking (semantic, or full-text when semantic is off), not a fusion of both | `src/retrieval.py` (`hybrid_enabled`); `UNITARES_ENABLE_HYBRID` |
| Cross-encoder reranker | Off, also in `docker-compose.full.yml` | `UNITARES_ENABLE_RERANKER=1`, with `sentence-transformers` installed (the full override's image has it); `UNITARES_RERANKER_MODEL` picks the model. It scores every candidate with a second model on each search | The first-stage order is the final order | `src/reranker.py` (`reranker_enabled`); `UNITARES_ENABLE_RERANKER` |
| Search related-finding expansion | Off | `UNITARES_ENABLE_RELATED_EXPANSION=1`. Read the `related_expansion_enabled` docstring first: it adds each top result's stored `related_to` neighbours, which lets a finding's retrievability grow with its own link count | Results are not padded with related findings | `src/retrieval.py` (`related_expansion_enabled`); `UNITARES_ENABLE_RELATED_EXPANSION` |
| Model for `consult` and the local reviewer | None named | `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL_ID` ([Choose a model](#choose-a-model-optional)), or `./scripts/unitares model` | `consult` answers `MODEL_NOT_CONFIGURED` (no model named, and the endpoint lists no single model to fall back on); `request_review` records the thesis and waits for a peer or the operator | `docker-compose.yml`; `UNITARES_MODEL_BASE_URL`, `UNITARES_MODEL_ID` |
| CLI hosts for `consult(effort='thorough')` and `delegate_inference` | Not available: no Compose service starts the agent orchestrator | Run the agent orchestrator, then set `UNITARES_HOST_ADAPTER_ENABLED=1`, `AGENT_ORCHESTRATOR_BEARER_TOKEN`, and an authenticated Codex, Claude or Antigravity CLI | Those calls fail and say the extension is not configured; `list_inference_hosts` reports it under `extensions.agent_orchestrator` | `src/mcp_handlers/support/inference_registry.py`; `UNITARES_HOST_ADAPTER_ENABLED` |
| Orchestrated and cross-family dialectic review | Off: reviews run in-process | `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW=1` with the agent orchestrator reachable at `AGENT_ORCHESTRATOR_URL`; `UNITARES_DIALECTIC_REVIEWER_HOST` (or `_HOSTS`) picks the reviewer backend | Reviews come from the in-process reviewer (with a model) or a peer; no separately identified reviewer process from another model family. Any dispatch failure falls back to in-process | `src/mcp_handlers/dialectic/orchestrator_dispatch.py` (`orchestrated_review_enabled`); `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW` |
| Lease-holder attestations (Ed25519) | On, signed with a published development seed and issuer `unitares-local` | Set your own `UNITARES_LEASE_ATTESTATION_SIGNING_KEY`, a stable `UNITARES_LEASE_ATTESTATION_ISSUER`, and `UNITARES_LEASE_TRUSTED_ISSUERS` (see §2.3) | With the published seed, anyone holding the compose file can sign an attestation the stack accepts; it is for a loopback install only | `docker-compose.yml`; `docs/DEPLOYMENT_SECURITY.md`; `UNITARES_LEASE_ATTESTATION_SIGNING_KEY` |
| Grounded E/I/S shadow | Off | `UNITARES_GROUNDING_SHADOW=1` | No `grounding_shadow` audit events comparing grounded values with the heuristic ones | `config/governance_config.py`; `UNITARES_GROUNDING_SHADOW` |
| Grounded E/I/S apply | Off | `UNITARES_GROUNDING_APPLY=1`. It changes coherence, E, I and S for every agent; its wake condition is in the [dormant-capability registry](../operations/dormant-capability-registry.md), Theme 6 | Canonical metrics come from the heuristic and ODE path | `config/governance_config.py` (`grounding_apply_enabled`); `UNITARES_GROUNDING_APPLY` |
| Cold-start risk confirmation actuation | Shadow on, actuation off | `GOVERNANCE_COLD_START_RISK_CONFIRMATION_ACTUATION=true`; the flag alone does not actuate until the registry's Theme 6 conditions are met (`actuation_ready=false`) | A fallback-owned `risk_pause` actuates without waiting for adjacent evidence; the shadow still counts what would have been deferred | `config/governance_config.py`; `GOVERNANCE_COLD_START_RISK_CONFIRMATION_*` |
| Verification floor | Shadow on, floor off | `GOVERNANCE_VERIFICATION_FLOOR=true`; council-gated, see `docs/proposals/active/verification-weighted-verdict-v0.md` | The independent verification signal is recorded in shadow and does not change verdicts | `config/governance_config.py`; `src/verification_floor_shadow.py`; `GOVERNANCE_VERIFICATION_FLOOR` |

Two related flags are on by default and need nothing: `UNITARES_PHI_TELEMETRY_ONLY`
(the behavioral verdict is authoritative and Φ is telemetry) and
`GOVERNANCE_NON_AUTHORED_COLD_START_GUARD`. Turning either off is a rollback, not
a setup step; the registry's Theme 6 says what each does. The generated
[flag catalog](../FLAGS.md) lists every variable the server reads.

### A smaller stack: the core profile

`docker-compose.core.yml` starts only the server and PostgreSQL: Redis and the
lease plane do not run. It is opt-in, for trying the server out or for a
machine that cannot spare the two extra services; the default stack stays
`docker-compose.yml` alone.

```bash
docker compose -f docker-compose.yml -f docker-compose.core.yml up -d --wait
```

To keep later commands on the core profile, including `./scripts/unitares
update`, add this line to `.env` instead of passing `-f`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.core.yml
```

What works and what does not, as measured by the Minimal Core Matrix workflow,
which runs this file on every change to it:

| | Core profile |
|---|---|
| Onboard, check-in, knowledge store and search, dialectic requests | Work |
| Identity resumption across a server restart, by a keyed `client_session_id` | Works: that binding is stored in PostgreSQL |
| Non-keyed session keys (`X-Session-ID`, `Mcp-Session-Id`, fingerprint pins), OAuth state, provider cooldowns | Kept in process memory, lost on restart |
| Onboard pins (the continuity a client gets when it does not pass its `client_session_id`) | Not available: the pin has no fallback without Redis, so pass `client_session_id` |
| Surface leases and agent messages (the lease plane) | Not available |

Use the default stack for a deployment that runs for long, serves several
clients, or uses coordination. To move to it, remove the `COMPOSE_FILE` line
and run `docker compose up -d --wait`; the database is the same.

---

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)
