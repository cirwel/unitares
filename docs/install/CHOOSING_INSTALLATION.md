# Choose one primary UNITARES installation

An installation consists of the server, coordination lease plane, database,
cache, and their configuration. Choose one primary installation per machine
and connect clients to its endpoint. Installing a second copy does not migrate
the first copy's data or switch its clients.

## Available paths

- **Docker Compose:** the current published, supported installation and update
  path. It runs the services together and keeps data in Docker volumes. Follow
  the [installation manual](../manual/02-install.md#21-docker-quickstart).
- **Native macOS:** runs services directly on the Mac. Follow the
  [native install playbook](PLAYBOOK.md). The Homebrew tap is still a local,
  unpublished draft tracked in [#2964](https://github.com/cirwel/unitares/issues/2964);
  do not use an invented public tap command.

Homebrew is the intended recommended Mac experience once first-time setup,
startup, backup/migration/update, and recovery are available through the same
CLI and the tap is approved for publication. Until then, its successful local
package validation does not replace the supported Docker installation path.
Docker remains a supported choice for server and advanced deployments.

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

Before switching the primary installation, identify its database and connected
clients, take and verify a backup, stop its writers, and validate the replacement
before moving clients. Retain the old data until that validation passes. Do not
delete Docker volumes or point native services at an existing database as a
shortcut around this process.
