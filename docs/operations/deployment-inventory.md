# Deployment inventory

Deployment status and dispatch use an explicit operator-owned inventory. The
server repository does not choose an account, host, agent roster, checkout
layout, external service, or deployment script for an installation.

Set exactly one of these before invoking `scripts/ops/deploy-status.sh` or
`scripts/ops/deploy-apply.sh`:

- `UNITARES_DEPLOY_INVENTORY`: a JSON file.
- `UNITARES_DEPLOY_INVENTORY_PROVIDER`: an absolute executable path that prints
  the same JSON. Providers are trusted deployment code; install them from
  reviewed source before selecting them.

An inventory has a version and service array. An explicit empty array monitors
no services; missing or malformed configuration is an error.

```json
{
  "version": 1,
  "services": [{
    "name": "sample-server",
    "label": "org.example.sample-server",
    "checkout": "/srv/sample/deploy",
    "paths": "src config",
    "pickup": "restart",
    "port": "8767",
    "deploy_script": "/srv/operations/deploy-sample.sh"
  }]
}
```

Names are unique identifiers. `checkout`, `deploy_script`, and `status_adapter`
are absolute paths; script and adapter may be omitted where unused. `paths` is
a space-separated list of relative literal pathspecs. `pickup` accepts
`restart`, `restart-DEV`, `hot-reload`, `live-from-checkout`, `library`,
`unregistered`, or `external`. An external row requires a `status_adapter` that
accepts `--repo PATH --base REF` and prints a JSON object containing string
fields `verdict`, `branch`, `commit`, `behind`, and `health`. An adapter failure
makes status fail; local checkout state cannot silently replace remote truth.

`deploy-status.sh --list-checkouts` emits resolved paths without service status,
health, or remote probes. A configured provider may read installed host topology
to resolve its inventory. Consumers must use this interface rather than
parse script source. Both status and dispatch advertise `inventory-v1` through
`--capabilities`, without reading configuration or probing services.

Dispatch follows inventory order and the returned `deploy_script` field.
An absent or non-executable script leaves work pending. Existing safeguards
remain: DOWN services stay stopped, shared scripts cannot wake a DOWN sibling,
a failed deployment holds its shared checkout, and reviewed-revision and schema
checks stay in the component scripts. Explicit `--request-service=NAME` enters
that same dispatch path for configuration-only changes.

Core component deploy scripts require `UNITARES_REPO` and their documented
`UNITARES_*DEPLOY` path. Set those in the operator's reviewed configuration.
External-service wrappers and remote-host adapters belong in that operator's
separate deployment repository; the shared `deploy-lib.sh` provides restart,
locking, revision, and migration safety without copying those implementations.

The optional health watchdog uses the server's port registry. Additional
services require `UNITARES_HEALTH_EXTRA_PROBES`, a TSV file with name, HTTP(S)
URL, accepted comma-separated status codes, and timeout seconds. A configured
unreadable or malformed file is logged as a failure. No external host or agent
is probed by default.


The optional drift doctor separately accepts `UNITARES_DEPLOY_DRIFT_SURFACES`,
a JSON array with `name`, absolute `path`, `branch`, nullable `launchd_label`,
and optional boolean `check_behind`/`check_version` fields. Its read-only deploy
preflights (#3020) come from `UNITARES_DEPLOY_PREFLIGHTS` the same way: a JSON
array of objects with `service`, absolute `plist`, and optional `needles` and
`require_env_keys` (lists of strings), `require_exists`, `check_governance_bearer`
and `check_sentinel_plist` (booleans), and `allow_env`, `governance_plist` and
`uds_socket` (strings). Neither list ships with a default roster. With no
configured surfaces and no preflights it performs no checks and no finding
resolutions; it never reports an empty inventory as a healthy fleet. The one-time plist migration helper also requires explicit
`UNITARES_DEPLOY_MIGRATE_LABELS` alongside source and deployment paths.

## One report across publication and running installations

The existing [distribution registry](distribution-surfaces.toml) owns public
packages, listings and sites; this deployment inventory owns operator-specific
machines and services. Do not copy host names or credentials into the public
registry. Temporary `git surface` claims coordinate edits; they are not an
inventory of published or running software.

From the server checkout, generate a timestamped report:

```sh
python3 scripts/ci/distribution_surfaces.py \
  --report-markdown artifacts/surfaces.md --report-json artifacts/surfaces.json
```

On a maintainer workstation, add `--github-app` to that command
for repository-scoped App reads through the installed runner. CI supplies
its own read-only GitHub token. The installed runner supports an explicit
repository scope, for example:

```sh
unitares-github --repo cirwel/homebrew-unitares --read-only -- \
  gh api repos/cirwel/homebrew-unitares/commits/main --jq .sha
```

The report selects that scope for each cirwel repository. Reads of public
third-party listings use the server repository's App token as a public GitHub
API caller; they gain no installation access to those repositories. Refused
calls stay UNKNOWN, with no retry through a human login. The maintainer's
2026-10-24 live run verified this interface against Homebrew's current head and
its acceptance run, plus the public upstream listing probes.

The existing Positioning Surfaces workflow
publishes the same report in its job summary and retains both files for 30 days.
It runs weekly and supports manual dispatch; no additional scheduler is needed.

Add `--deployments` only on a configured operator host. It calls the existing
`deploy-status.sh --json` with the selected inventory, never deploys or restarts,
and preserves service verdicts separately from health. Remote fetch is not
requested, so comparisons use the inventory's existing Git refs. Keep this
combined report private: service names and health descriptions can identify the
installation. Missing configuration, a failed adapter, and an empty inventory
are UNKNOWN, never proof of a healthy fleet.

A report records when its observations were collected; it is not a permanent
status. OK applies only to the named probe. An HTTP response proves reachability,
not that the page's instructions are current. A delegated check is UNKNOWN until
its own evidence is inspected, and a Homebrew acceptance result must name the
current tap head. The report links each surface's source, refresh path, checks,
and declared dependencies so the next action is discoverable.

When releasing, inspect source, Docker, Homebrew, SDK/plugin compatibility and
installation copy as separate deliveries. Different version numbers are expected;
use COMPATIBILITY.md and package manifests instead of requiring version equality.
Add new public surfaces to the registry in the change that publishes them, and
new running services to the operator inventory. Update the existing workspace
project index to point here rather than maintaining another status table.
