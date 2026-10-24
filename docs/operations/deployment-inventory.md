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
