# Homebrew first-run configuration

The [public Homebrew preview](https://github.com/cirwel/homebrew-unitares)
provides the server and native dependencies for Apple Silicon Macs. Install it
with the tap's documented `brew update`, `brew tap`, `brew trust` and
`brew install` steps. Its [first-time setup instructions](https://github.com/cirwel/homebrew-unitares#first-time-setup)
cover deliberate role/database creation and the installed bootstrap; no Docker
or model server is required. The tap tracks verified releases after
installed-package acceptance; use `brew info cirwel/unitares/unitares` to
inspect the version it currently packages and the version installed locally.
Older `3.5.0` revisions carried reviewed setup and service-control backports
that current releases include upstream.
From a source checkout, `--homebrew` selects the installed native package;
a packaged CLI selects it automatically.

```sh
unitares setup --homebrew
unitares setup --homebrew --apply
```

For a fresh installation with no native configuration, use explicit provisioning:

```sh
unitares setup --homebrew --provision-db
unitares setup --homebrew --provision-db --apply
```

The first command prints the plan without changing files, services or databases.
The second verifies the installed PostgreSQL 17, AGE and pgvector packages and
the selected cluster, starts PostgreSQL and Redis if needed, and creates a
dedicated `unitares` login and database. The application login is **not a
superuser**. The local administrator installs extensions and configures AGE
preload only for that role in that database. Setup bootstraps as the application
role, checks migrations and performs an AGE query before publishing `native.env`
with a generated password and installation ID. Server and lease-plane writers
remain stopped. After successful provisioning, run:

```sh
unitares start --homebrew --check
unitares start --homebrew
```

Provisioning refuses existing configuration, a shell `DB_POSTGRES_URL`, an
existing role or database, active writers, foreign port owners, and an active
Docker `postgres-age`. If Docker is installed, its inventory must be readable.
Redis database 0 must be empty; setup atomically records an installation marker
there so a fresh PostgreSQL database cannot be paired with existing identity
state. Resume accepts only that marker until provisioning is complete. Setup
never adopts or deletes existing data, edits cluster-wide PostgreSQL
configuration, rotates existing credentials, or clears Redis. Dependencies
started by setup remain running if a later step fails.

The private `$(brew --prefix)/var/unitares/native-setup.json` journal retains
credentials and stage evidence; keep it private and outside Git. Normal start,
setup and update refuse an unfinished journal. Resume with:

```sh
unitares setup --homebrew --provision-db --apply --resume
```

Resume verifies the cluster, role/database markers, privileges, configuration
and bootstrap source files. A crash after database creation but before its
ownership comment leaves ambiguous state and requires manual inspection. A
bootstrap interrupted after SQL begins also refuses automatic replay: preserve
the database and journal and use the recovery guidance below. A completed
bootstrap can resume schema/runtime verification and configuration publication
without replaying SQL. There is deliberately no destructive reset/abort flag.
Repeated provisioning after completion preserves the existing configuration.

Without `--provision-db`, the first command inspects the install without writing files or changing
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

Setup without `--provision-db` performs read-only database inspection only when the selected Homebrew
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
`--yes` applies that recorded set, including an empty set, even if time changed, while rechecking
update-journal protections immediately before deletion. Repeat the same policy
options with `--yes`; a different policy is refused, and a later dry run always
replaces an older pending plan. An interrupted deletion must resume with the
recorded policy printed by the command.
Unindexed backups are never deleted. Old kegs remain Homebrew's
responsibility; keep the predecessor until the new installation and backup have
been validated, then use Homebrew's own cleanup procedure when appropriate.

Maintainers hand stable releases to the tap through the protected deterministic
[Homebrew release sync](../operations/homebrew-release-sync.md). It opens a
draft tap PR and cannot merge it; bottle construction and publication remain
separate operator-gated actions.

The release sync signs the deterministic source archive with a GitHub artifact
attestation and verifies its repository, workflow, tag and exact commit before
publication. Manual dispatch must run **at the tag**, for example
`gh workflow run homebrew-release-sync.yml --ref "$TAG" -f tag="$TAG"`.
The tagged commit must contain the attesting workflow; older unsigned assets
cannot acquire this guarantee by running a newer workflow on `master`.

Users can verify a downloaded source archive with a current authenticated GitHub
CLI and an expected full commit obtained independently from the reviewed release:

```sh
gh attestation verify "unitares-$VERSION.tar.gz" --repo cirwel/unitares \
  --signer-workflow cirwel/unitares/.github/workflows/homebrew-release-sync.yml \
  --source-ref "refs/tags/v$VERSION" --source-digest "$EXPECTED_COMMIT" \
  --signer-digest "$EXPECTED_COMMIT" --deny-self-hosted-runners
```

The tap's [verification instructions](https://github.com/cirwel/homebrew-unitares#verify-a-downloaded-package)
cover bottle signatures, complete evidence manifests and recorded build dependencies.
Ordinary `brew install` still performs Homebrew checksum checks; attestation
verification is an additional explicit check. Provenance establishes origin and
integrity, not benign behavior, an independent security audit or reproducibility.
These changes do not publish bottles or enable the protected build environments.
