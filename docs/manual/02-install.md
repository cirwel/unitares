# 2 · Installation

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)

Choose one primary installation:

- **Apple Silicon Mac: Homebrew preview.** The published native tap packages the
  server, lease plane, PostgreSQL 17 with AGE/pgvector, and Redis. Database setup
  and backed-up upgrades remain explicit; Docker is not required.
- **Docker Compose:** the supported container path with automatic fresh database
  initialization and a backed-up update command.
- **Source checkout:** the advanced macOS operator/developer path in the
  [source install playbook](../install/PLAYBOOK.md).

## 2.0 Homebrew preview for Apple Silicon Macs

```sh
brew update
brew tap cirwel/unitares
brew trust cirwel/unitares
brew install cirwel/unitares/unitares
brew pin unitares unitares-lease-plane
```

The published tap revision `3.5.0_5` includes native CLI backports beyond the
upstream v3.5.0 archive. The running server still reports `3.5.0`; inspect the
package revision with `brew info cirwel/unitares/unitares`.

Continue with the tap's [first-time database and configuration steps](https://github.com/cirwel/homebrew-unitares#first-time-setup).
Package installation does not create a role/database or apply bootstrap SQL.
`unitares setup --apply` prepares missing private configuration, preserving any
existing file. Only after the selected database is verified should you run
`unitares start --check`, `unitares start`, and `unitares health`.

The [Homebrew setup guide](../install/HOMEBREW_SETUP.md) explains readiness and
recovery boundaries. The tap's [upgrade procedure](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery)
is stop/backup/migrate/restart; `unitares update` manages Docker Compose only.
No model connection, Ollama instance or resident roster is enabled by default.
The preview builds from source, has no bottles, and requires the Homebrew
owner's logged-in macOS session. Intel macOS and Linux are outside its scope.
Installing Homebrew alongside Docker does not copy data or switch clients;
see [Choosing an installation](../install/CHOOSING_INSTALLATION.md).

## 2.1 Docker quickstart

The clone pin below names the latest verified public release, which can lag
the source version while a release is being prepared.

```bash
git clone --branch v3.5.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make coordination-demo
```

Cloning an annotated release tag with `--depth 1` prints a harmless Git notice
(`warning: refs/tags/vX.Y.Z … is not a commit!`).

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

Start and verify the core installation first. A fresh install has no default
model connected; Ollama is optional. An unrelated Ollama installation on the
same machine does not become UNITARES's model just because it serves one model.
Agents can still discover available models and explicitly choose one for a call.

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

This model is the only one a default install uses. The stronger CLI lane
(`consult(effort='thorough')` and `delegate_inference`, reported under
`extensions.agent_orchestrator` by `list_inference_hosts`) requires the separate
BEAM agent orchestrator service, which no Compose service starts by default (see
[§2.4](#24-quickstart-defaults-and-a-full-setup)).

The model is reached through one OpenAI-compatible endpoint, named by two
settings:

| Setting | Meaning | Default |
|---|---|---|
| `UNITARES_MODEL_BASE_URL` | Base URL of the model server, including `/v1` | Explicit model requests use `http://localhost:11434/v1` when unset. Set this explicitly to enable automatic selection of the endpoint's sole served model. |
| `UNITARES_MODEL_ID` | The default model, used when an agent names none | None. An agent can pick with `consult(model=...)`. Otherwise use this configured default, or the sole model at an explicitly configured endpoint; without either, return `MODEL_NOT_CONFIGURED`. |

Ollama is the server these steps are tested with; other OpenAI-compatible
servers (vLLM, LM Studio, llama.cpp's server) can be named the same way (a URL
with no path gets `/v1` added; API-key-protected servers are not supported yet).
From the checkout, once your model server is running and has a model:

```bash
./scripts/unitares model
```

It lists the models the server offers (`GET {base}/models`), writes your choice
to `.env`, rebuilds the server, and checks that the server can reach the model
(`--base-url` points at another server; `--no-docker` prints the settings for
Homebrew or source installs, where Homebrew uses
`$(brew --prefix)/etc/unitares/native.env`; link `./scripts/unitares` to
`~/.local/bin/unitares` to run it as `unitares`). After configuring local
inference, `consult(effort='thorough', allow_degraded=True)` permits standard
local advice when the thorough lane is unavailable (`allow_degraded` is false by
default and never grants cloud-provider access).

To configure Ollama on the Docker host manually:

```bash
ollama pull <your-model>
cat >> .env <<'EOF'
UNITARES_MODEL_BASE_URL=http://host.docker.internal:11434/v1
UNITARES_MODEL_ID=<your-model>
EOF
docker compose up -d --build governance-mcp
```

Inside the container, the Docker host is reached as `host.docker.internal` (on
Linux, set `OLLAMA_HOST=0.0.0.0` for Ollama and allow port 11434 from the Docker
bridge only). Pass `--build` when enabling a model on an existing install. The
legacy `UNITARES_OLLAMA_BASE`, `UNITARES_OLLAMA_BASE_URL`, and
`UNITARES_LLM_MODEL` names were removed in v3.3.0; nothing reads or warns about
them, so rename them to `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL_ID` in
`.env` or in the LaunchAgent plist (`./scripts/unitares model` rewrites `.env`
only).

**Where the prompt goes.** `consult` (and the deprecated `call_model`, removed
no earlier than 2026-12-02) default to `privacy='local'`, and the in-process reviewer, check-in coaching and
knowledge synthesis always count as local. The server classifies an endpoint as
local from the URL alone, never from DNS:

- an IP address is local when it is loopback, in a private RFC 1918 range, or
  in a network listed in `UNITARES_TRUSTED_NETWORKS` (add `100.64.0.0/10` there
  for a Tailscale peer, or an IPv6 unique-local range; note that on a source
  install, adding a range to `UNITARES_TRUSTED_NETWORKS` also lets callers from
  that network reach the REST API without a token, whereas under Compose callers
  arrive through the Docker bridge so it changes only which model endpoints count
  as local);
- a hostname is local only when it is `localhost`, `host.docker.internal`, or
  listed in `UNITARES_MODEL_LOCAL_HOSTS` (comma-separated, e.g. `vllm` or
  `gpu-box.lan`);
- `UNITARES_MODEL_PRIVACY=local` or `external` overrides both.

Anything else is refused for local requests with `MODEL_ENDPOINT_NOT_LOCAL`
before anything is sent. `consult(privacy='cloud_allowed')` (or the deprecated
`call_model(privacy='auto')`) may use an external endpoint.

### Reinstalling and side-by-side installs

Compose names the project after the checkout directory (`unitares`) and keeps
state in named volumes under that project name, so upgrades and fresh clones
reuse existing data:

- For a clean install (deleting stack data), run `docker compose down -v` in the
  old checkout first.
- For a side-by-side install, set `COMPOSE_PROJECT_NAME` in the new checkout's
  `.env` and run `docker compose down` (without `-v`) in the first checkout so
  fixed `container_name` values do not collide (`stop` is not enough, because
  stopped containers still hold their fixed `container_name` values, and
  `restart: unless-stopped` revives un-stopped containers when the Docker daemon
  restarts).

### Updating

From the checkout, see whether a newer release is published, then move to it:

```bash
./scripts/unitares update --check
./scripts/unitares update
```

`update` resolves the newest published release (or `--to <tag>`), prints the
installed and target versions, and asks before changing anything (`--yes` skips
the prompt; `--check` only reports). It manages only this checkout's Docker
Compose stack and refuses, leaving the code and the database schema as they
were, when:

- tracked files have local changes, or `db/postgres` holds untracked files;
- the database schema is newer than anything the target release knows, so moving
  to an older release is not supported (it learns this only after fetching the
  target's tag and starting the database, which stay fetched and running);
- this machine runs UNITARES as a macOS LaunchAgent ([§3.7](03-running-the-server.md#37-run-at-login-macos-launchagent)).

On confirmation it checks out the target, stops the governance server and lease
plane, starts the database, and — if the release has pending migrations — writes
a `0600` backup to `~/.unitares/backups/unitares-<time>-before-<tag>.sql.gz` (or
`UNITARES_BACKUP_DIR`) and applies them with `scripts/dev/apply_migrations.py`
inside the database container, then rebuilds, restarts, and verifies deep health
(`healthy` or `moderate`). If the dump fails, nothing is migrated. If a
migration or rebuild fails before crossing the protected dialectic schema
frontier (migrations 077 and 078), `update` checks the previous release back out
and rebuilds it; if the attempt crossed 077 or 078 during this update (or the
schema version cannot be read afterwards), it leaves the target release checked
out with the writers stopped for forward repair. A checkout already on the
target release goes through the same steps without moving the code.

When upgrading from v3.3.0 or earlier, fetch and check out the target tag first
so the new `update` script runs with the 077/078 quiescence attestation:

```bash
git fetch --depth 1 --no-tags origin "+refs/tags/vX.Y.Z:refs/tags/vX.Y.Z"
git checkout --detach vX.Y.Z
docker compose up -d --build --wait postgres-age   # only from a release before v3.0.0
./scripts/unitares update --to vX.Y.Z
```

Because `vX.Y.Z` is already checked out before `update` runs on this manual path,
if `update` stops before the release's protected migrations are applied (077 and
078 for v3.4.0) it restarts the stack from the checked-out Compose file; to run
the previous release instead, check that release out and run
`docker compose up -d --wait`.

#### Updating a bare-metal or LaunchAgent install

`update` manages only a Compose stack, so on a [bare-metal install](#22-bare-metal-installation)
or a [LaunchAgent](03-running-the-server.md#37-run-at-login-macos-launchagent)
run the same steps by hand from the checkout. Set `DB_POSTGRES_URL` to your
server's database URL, verify a clean checkout and no untracked files under
`db/postgres` (the migration runner applies every `NNN_*.sql` file under
`db/postgres/migrations/`), and fetch the target release:

```bash
export DB_POSTGRES_URL="postgresql://localhost:5432/governance"
git status --porcelain --untracked-files=no     # must print nothing
git ls-files --others --exclude-standard -- db/postgres   # must print nothing
PREV=$(git rev-parse HEAD)                      # the release you are leaving
TAG="v$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION)"
echo "$TAG"                                     # the newest release; set TAG yourself to pick another
git fetch --depth 1 --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG"
```

Confirm this is not a downgrade (stop if the database's current version on the
first line exceeds the target release's newest migration on the second):

```bash
psql "$DB_POSTGRES_URL" -Atqc "SELECT COALESCE(MAX(version), 0) FROM core.schema_migrations"
git ls-tree --name-only "$TAG" -- db/postgres/migrations/ \
  | sed -n 's|.*/\([0-9][0-9]*\)_[^/]*\.sql$|\1|p' | sort -n | tail -n 1 | sed 's/^0*//'
```

Stop the server and lease plane, confirm they are gone by service registration,
process, and listening port, then update the checkout and virtualenv:

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

Back up the database, inspect pending migrations, and apply them (do not migrate
unless the backup prints `backup ok`, and pass `--dialectic-writers-quiesced`
only after verifying all writers to this database are stopped):

```bash
mkdir -p ~/.unitares/backups && chmod 700 ~/.unitares/backups
( set -o pipefail; umask 077
  pg_dump "$DB_POSTGRES_URL" | gzip > ~/.unitares/backups/unitares-$(date +%Y%m%d-%H%M%S)-before-$TAG.sql.gz ) \
  && echo "backup ok"
python3 scripts/dev/apply_migrations.py --db-url "$DB_POSTGRES_URL"
python3 scripts/dev/apply_migrations.py --db-url "$DB_POSTGRES_URL" --apply --dialectic-writers-quiesced
```

If `git diff "$PREV" "$TAG" -- scripts/ops/com.unitares.governance-mcp.plist`
shows changes, merge them into your installed plist by hand rather than
re-rendering from [§3.7](03-running-the-server.md#37-run-at-login-macos-launchagent)
(re-rendering overwrites tokens, secrets, and `DB_POSTGRES_URL`). If the lease
plane runs from a separate deploy checkout (see the
[lease-plane runbook](../operations/lease-plane-operator-runbook.md)), advance
that checkout to `$TAG` before loading its plist:

```bash
plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.unitares.lease-plane.plist
LP=$(git -C "$(plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.unitares.lease-plane.plist)" rev-parse --show-toplevel)
git -C "$LP" fetch --depth 1 --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG"
git -C "$LP" checkout --detach "$TAG"
```

Restart the services and verify deep health (`healthy` or `moderate`) and the
lease plane's `build_sha`:

```bash
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist   # or: python src/mcp_server.py --port 8767
launchctl load ~/Library/LaunchAgents/com.unitares.lease-plane.plist      # if you stopped it
./scripts/unitares health
curl -s http://localhost:8767/health/deep
curl -s http://127.0.0.1:8788/health; git rev-parse --short "$TAG^{commit}"   # if you run the lease plane
```

If a migration fails, do not start the previous release over the database until
you know where the schema stopped (the `psql` query above). If it did not cross
077 or 078 during this update, you can roll back with
`git checkout --detach "$PREV"`, reinstall its dependencies, and restart what you
stopped; if it crossed either one during this update (or you cannot read the
version), stay on `$TAG`, fix the reported cause, and rerun `--apply` (or restore
from the backup).

#### Updating the agent plugin

The [governance plugin](https://github.com/cirwel/unitares-governance-plugin)
is installed into each agent client rather than the server; update it after
upgrading the server, then start a new session (or run `/reload-plugins` in
Claude Code; in Codex, review updated hooks under `/hooks`):

```bash
# Claude Code
claude plugin marketplace update unitares-governance
claude plugin update unitares-governance@unitares-governance

# Codex
codex plugin marketplace upgrade unitares-governance
```

### Tool discovery (interface 1.13.0 and later)

The server negotiates one complete catalog, including installed plugin tools,
while advertising a small progressive surface initially. Use
`list_tools(lite=true)` for every capability name, `describe_tool(tool_name=...,
action=...)` for its parameters, and `use_tool(tool_name=..., arguments={...})`
to invoke an omitted capability through its normal gates. Set
`UNITARES_TOOL_ADVERTISEMENT=full` in `.env` to advertise every schema up front,
then recreate the service and reconnect the client. Old `GOVERNANCE_TOOL_MODE` values are ignored, and action authorization and
identity gates are unchanged. Earlier 2.x releases (`v2.22.1` is the last 2.x
release) used `minimal`, `lite`, and `full` discovery profiles: `v2.22.0`
defaults to the five-tool `minimal` profile and requires the explicit override
in the [v2.22.0 release errata](../releases/2.22.0-errata.md) to forward
`GOVERNANCE_TOOL_MODE` under Compose, while `v2.21.0` uses an older six-tool
`minimal` profile with registration-time filtering on the HTTP MCP mount.
Upgrade the server for the unified catalog and dispatch compatibility.

## 2.2 Bare-metal installation

This is the source-checkout path. Packaged Mac installs use the
[Homebrew preview](#20-homebrew-preview-for-apple-silicon-macs) above.

Follow [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md). It owns the exact
PostgreSQL/AGE/pgvector versions, schema sequence, Python environment, expected
outputs, and failure recovery. Do not copy commands from historical proposals.
Bootstrap is for a new, empty database. If it stops partway through, follow
[interrupted native bootstrap recovery](../install/PLAYBOOK.md#recovering-an-interrupted-native-bootstrap)
instead of rerunning it; use the migration runner for existing databases.
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
| Model for `consult` and the local reviewer | None named | `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL_ID` ([Choose a model](#choose-a-model-optional)), or `./scripts/unitares model` | `consult` answers `MODEL_NOT_CONFIGURED` (no model chosen and no explicitly configured endpoint with a sole served model); `request_review` records the thesis and waits for a peer or the operator | `docker-compose.yml`; `UNITARES_MODEL_BASE_URL`, `UNITARES_MODEL_ID` |
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
