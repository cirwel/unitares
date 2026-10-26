# Homebrew first-run configuration

The [public Homebrew preview](https://github.com/cirwel/homebrew-unitares)
provides the server and native dependencies for Apple Silicon Macs. Install it
with the tap's documented `brew update`, `brew tap`, `brew trust` and
`brew install` steps. Its [first-time setup instructions](https://github.com/cirwel/homebrew-unitares#first-time-setup)
cover deliberate role/database creation and the installed bootstrap; no Docker
or model server is required. Public tap revision `3.5.0_5` includes the native
setup and service-control backports; the server still reports upstream version
`3.5.0`. Use `brew info cirwel/unitares/unitares` to inspect the package revision.
From a source checkout, `--homebrew` selects the installed native package;
a packaged CLI selects it automatically.

```sh
unitares setup --homebrew
unitares setup --homebrew --apply
```

The first command inspects the install without writing files or changing
services. `--apply` creates a missing `$(brew --prefix)/etc/unitares/native.env`
with mode 600. It prompts for the URL of your deliberately chosen PostgreSQL
database without displaying the input. Use a URL with a URL-encoded user,
password and database name on localhost port 5432. It does not create the
PostgreSQL role or database. For automation, supply `DB_POSTGRES_URL` through
a secret environment and add `--non-interactive`; do not put credentials in
command arguments or shell history.

Both writers receive the same database URL and one generated lease bearer.
Redis uses localhost:6379, the server localhost:8767 and the lease plane
localhost:8788. Model endpoint, model ID and resident roster start empty.
Ollama is optional. Setup refuses competing port owners and custom service
environments so an existing Docker/Colima installation cannot be mistaken for
the native services.

Existing configuration is inspected unchanged, including on repeated
`--apply` runs. Credentials are never rotated by setup. An invalid file needs
an explicit repair; setup will not replace it. The file contains secrets: keep
it private and outside Git. Its assignments must be literal values without
shell expansion. Existing configuration takes precedence over shell database
settings.

## Database next step

Setup performs read-only database inspection only when the selected Homebrew
PostgreSQL service is running. It never installs formulae, starts services,
creates databases or applies SQL. A successful setup exit means the inspection
completed, **not** that the server is ready; read its database-readiness line.

- **Unknown:** PostgreSQL is stopped, credentials failed, or inspection failed.
  Start the selected dependency with `brew services start postgresql@17` when
  appropriate, verify the chosen database and credentials, then rerun setup.
  Unknown does not mean empty.
- **No UNITARES migration registry:** this does not establish emptiness. Only
  if you deliberately created a new empty database, use the
  [Homebrew first-time setup](https://github.com/cirwel/homebrew-unitares#first-time-setup) for it. Keep both writers stopped. The
  bootstrap script is `scripts/install/bootstrap_postgres.sh` inside
  `$(brew --prefix)/opt/unitares/libexec/app`; use the installed package's
  Python and PostgreSQL tools. An existing database without the registry may
  belong to another application or have an interrupted base bootstrap. Preserve
  it and inspect its schema or restore a known backup. The migration runner
  cannot repair a missing base schema or migration registry.
- **Existing registry, schema not verified:** inspect the bootstrap stage.
  If the base schema is complete and only numbered migrations remain, follow
  the manual's [database backup and migration steps](../manual/02-install.md#updating-a-bare-metal-or-launchagent-install): back up,
  quiesce both writers and use `scripts/dev/apply_migrations.py`. If the base
  bootstrap itself is incomplete, preserve and inspect it or restore a backup;
  do not treat numbered migrations as a generic repair. Do not rerun fresh
  bootstrap over an existing database. For Homebrew, use `unitares stop` to
  stop the two writers and verify their processes/listeners have stopped.
  Run the database steps with the installed package's Python and scripts;
  do not run the manual's source-checkout Git or pip commands inside a keg.
- **Schema verified:** run `unitares start --homebrew --check`, then
  `unitares start --homebrew`. Startup checks the schema again and waits for
  server deep health.

For commands that accept a database URL, pass its password through libpq's
`PGPASSWORD` environment instead of including it in process arguments. Setup's
own probes do this automatically and suppress raw database error output.

Native upgrades use `unitares update --check` and `unitares update`. The latter
refreshes the tap and explicitly performs the stop, verified backup, package
upgrade, migration and prior-state restoration. A restarted installation must
pass deep health; one that was already stopped remains stopped after a schema
check. Formula install hooks and `unitares start` never apply migrations. An
unfinished journal blocks `unitares start`. If an interruption occurs after the
backup, run `unitares update --resume`; a retry from the backed-up stage creates
fresh backup evidence before changing packages. If identity-count verification
fails, inspect the database and backup first, then explicitly acknowledge the
reviewed result with
`unitares update --resume --accept-identity-change`. Journal and backup paths are
under the Homebrew runtime directory and `~/.unitares/backups` respectively.
Setup does not switch clients, copy a Docker database, or change which
installation owns your data.

If a package build cannot succeed, `unitares update --abort` safely restores the
prior running/stopped state only while every managed package and the schema are
still at their recorded pre-update versions. Once either changed, abort refuses
and forward repair with `--resume` remains mandatory. Direct `brew services`
commands bypass these UNITARES journal checks; use the packaged lifecycle
commands while an update is active.
