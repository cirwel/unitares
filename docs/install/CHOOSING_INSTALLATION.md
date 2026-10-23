# Choose one primary UNITARES installation

An installation consists of the server, coordination lease plane, database,
cache, and their configuration. Choose one primary installation per machine
and connect clients to its endpoint. Installing a second copy does not migrate
the first copy's data or switch its clients.

## Available paths

- **Homebrew preview for Apple Silicon Macs:** the published
  [cirwel/unitares tap](https://github.com/cirwel/homebrew-unitares) installs the
  native server, lease plane, PostgreSQL 17 with AGE/pgvector, and Redis. Docker
  is not required. Follow the tap's install and first-time database steps, then
  the [native setup guide](HOMEBREW_SETUP.md). Services run in the Homebrew
  owner's logged-in macOS account. Intel macOS and Linux are outside this preview.
- **Docker Compose:** the supported container installation and automated update
  path. It runs the services together and keeps data in Docker volumes. Follow
  the [installation manual](../manual/02-install.md#21-docker-quickstart).
- **Native source checkout:** the advanced operator/developer path, maintained
  in the [source install playbook](PLAYBOOK.md). Its manual extension builds
  and Python environment are separate from the packaged Homebrew installation.

Homebrew provides package installation, private configuration and native service
control. Database creation/bootstrap and backed-up upgrades remain explicit
operator steps; `unitares update` is still Compose-only. The tap builds from
source and does not publish bottles. Docker remains supported for container
and server deployments. Choose the path whose operating procedure fits your host.

Installation method and optional capabilities are separate choices. Semantic
search needs embeddings; model-assisted consultation needs a configured model.
Neither capability appears merely by changing Docker to Homebrew. The manual's
[capability table](../manual/02-install.md#24-quickstart-defaults-and-a-full-setup)
names the defaults and the explicit setup for each feature.

## Before creating another installation

The [managed Docker launcher](../../scripts/install/launch.sh) checks for a
Homebrew `unitares` installation, a native environment file, and the standard
native LaunchAgent. It reports discovered paths. A configured native service
blocks creation of a new Docker checkout; a package alone does not, because
its CLI can be used with Docker. An existing managed Docker checkout with its
own `.env` can still be reused after the usual ownership, origin, release and
clean-checkout validation. Its configuration is preserved. The launcher does
not read credentials from native configuration, stop that installation, or
move its data.

Homebrew is located through `PATH` or its standard macOS/Linuxbrew executable
locations. `UNITARES_NATIVE_ENV` can identify a custom native configuration.
These checks do not discover every source checkout, custom service manager,
remote Docker context, or remote server. Review those explicitly before choosing
an installation. An installed package is reported as installed, not as proof
that its service is running.

Existing UNITARES Docker containers also have an owner checkout. The launcher
refuses containers owned by another checkout, or whose ownership is unknown,
before creating its managed directory. Reuse the owning checkout for updates;
changing directories is not a database migration.

For a deliberate native-plus-Docker test, choose two free alternate ports and
pass `--separate` explicitly:

```sh
curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/scripts/install/launch.sh \
  -o launch.sh
GOVERNANCE_HOST_PORT=18767 LEASE_PLANE_HOST_PORT=18788 \
  sh launch.sh --separate
```

Both ports must be distinct, between 1 and 65535, and different from the native
defaults 8767 and 8788. Pick different values if a custom installation already
uses these example ports. This option does not permit taking over another
Docker checkout's fixed container names or volumes.

For a new managed checkout the launcher saves these ports in `.env`, so later
CLI updates keep them. For an existing checkout it requires matching numeric
port assignments in `.env`, optionally surrounded by single or double quotes,
and never rewrites the file. Use `KEY=value` with no `export` prefix or spaces
around `=` or the value. Inline comments and variable expressions are also
rejected because the CLI's port reader does not resolve them. Repeat the same
explicit command to rerun the launcher alongside the native installation.

## Know which server answered

`unitares status` and `unitares status --json` report the endpoint they queried
alongside the running version. A Homebrew CLI can connect to a Docker server or
a remote server; the CLI's package version does not identify that server.
URL credentials and query parameters are omitted from the displayed endpoint.

```sh
UNITARES_URL=http://localhost:18767 unitares status
```

## Control a configured Homebrew installation

The CLI bundled in a Homebrew package selects its native services for `start`,
`stop`, and `logs`. From a source checkout, select them explicitly:

```sh
unitares start --homebrew --check
unitares start --homebrew
unitares logs --homebrew server --no-follow
unitares logs --homebrew lease-plane
unitares stop --homebrew
```

This controls the current macOS user's local Homebrew installation. The
formulae, a private `$(brew --prefix)/etc/unitares/native.env`, and the database
must already be configured. Startup checks port ownership, starts missing
PostgreSQL/Redis services, verifies the installed package's schema without
applying migrations, then starts the lease plane and server and checks deep
health. Existing services are reused. `--check` reports the plan without
changing services; it does not claim that an offline database passed its schema
check.

Stop shuts down the server and lease plane and leaves PostgreSQL/Redis running.
If startup fails, only writers newly started by that invocation are stopped;
shared dependencies and pre-existing writers are retained. Inspect the error
and logs before retrying. Neither start nor stop upgrades packages, creates a
database, changes configuration, or runs migrations.

This initial native path supports local PostgreSQL on 5432, Redis on 6379, and
the packaged server on 8767. Both writers must name the same database with an
explicit URL-encoded username and password. Configure the lease-plane port
consistently in its bind setting and both client URLs. Configuration values
must be single-line literal shell assignments, optionally quoted; shell
expansion is refused. Custom Homebrew per-service environment files and custom
writer commands require their existing service procedure. A shell-only
`UNITARES_NATIVE_ENV` override cannot select a different launchd configuration.

Native upgrades still use the documented stop/backup/migrate/restart procedure.
`unitares update` and Docker model configuration refuse a Homebrew package;
invoke the CLI in the owning Docker checkout to manage that stack.

Before switching the primary installation, identify its database and connected
clients, take and verify a backup, stop its writers, and validate the replacement
before moving clients. Retain the old data until that validation passes. Do not
delete Docker volumes or point native services at an existing database as a
shortcut around this process.
