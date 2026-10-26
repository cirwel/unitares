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
check. Homebrew may update dependencies required by the managed formulae; the
journal records the complete installed-formula inventory before and after the
transaction. Formula install hooks and `unitares start` never apply migrations. An
unfinished journal blocks `unitares start`. If an interruption occurs after the
backup, run `unitares update --resume`; a retry from the backed-up stage creates
fresh backup evidence before changing packages. If the tap target changed, the
retry asks for confirmation (or requires `--yes`) before adopting it. If
`unitares` disappeared from `PATH` during an interrupted Homebrew link step, run
`$(brew --prefix)/opt/unitares/bin/unitares update --resume`. If identity-count verification
fails, inspect the database and backup first, then explicitly acknowledge the
reviewed result with
`unitares update --resume --accept-identity-change`. Journal and backup paths are
under the Homebrew runtime directory and `~/.unitares/backups` respectively.
Setup does not switch clients, copy a Docker database, or change which
installation owns your data.

If a package build cannot succeed, `unitares update --abort` safely restores the
prior running/stopped state while the four managed UNITARES packages and schema
remain at their recorded pre-update versions. Dependency changes made by the
failed Homebrew transaction are retained and recorded in the journal; inspect
them before retrying. Once a managed package or the schema changed, abort
refuses and forward repair with `--resume` remains mandatory. A resumed package
attempt refreshes the tap and adopts a newer repaired formula revision.

Kegs installed before the coordinated updater shipped cannot run these commands.
For that one-time transition, follow the tap's
[manual upgrade and recovery procedure](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery).
Direct `brew services`
commands bypass these UNITARES journal checks; use the packaged lifecycle
commands while an update is active.

Verified updater backups are indexed with their checksum and size. Inspect the
retention plan without deleting anything:

```sh
unitares update --cleanup-backups
```

The default keeps at least the newest three existing, reverified backups, every backup named
by the current update journal, and anything newer than 30 days. Apply that exact
eligible set with `--yes`; adjust the two floors with `--retain-backups N` and
`--older-than-days N`. Cleanup revalidates ownership, mode, size, checksum and
`pg_restore --list`, journals each deletion, and refuses while an update is
unfinished. Missing files do not count toward the retention floor; their stale
index records appear in the dry run and are reconciled only with `--yes`.
The dry run records its candidates and policy in the private cleanup journal;
`--yes` uses that recorded plan even if time or command defaults changed, while
rechecking update-journal protections immediately before deletion.
Unindexed backups are never deleted. Old kegs remain Homebrew's
responsibility; keep the predecessor until the new installation and backup have
been validated, then use Homebrew's own cleanup procedure when appropriate.

Maintainers hand stable releases to the tap through the protected deterministic
[Homebrew release sync](../operations/homebrew-release-sync.md). It opens a
draft tap PR and cannot merge it; bottle construction and publication remain
separate operator-gated actions.
