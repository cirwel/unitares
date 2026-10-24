# Optional reference monitoring

The reference agents under `agents/` can monitor an explicitly selected
repository, GitHub owner, activity log, and set of services. A new installation
has no GitHub owner or local client-history path selected.

## Repository cleanup

`python3 agents/vigil_hygiene/agent.py --repo /path/to/checkout` is a dry run;
`--live` enables deletions. PR protection is resolved from that checkout's
`origin`, regardless of `GH_REPO` or the CLI's default repository. All open PR
pages are read before the sweep changes anything. Unavailable or malformed PR
results hold the entire sweep. Open PRs protect both local and remote branches;
patch-equivalence, age, clean-worktree, and lease safeguards still apply.

`VIGIL_HYGIENE_GH_BIN` optionally selects an executable that accepts ordinary
`gh` arguments, for installations with a credential-scoping wrapper.

## GitHub traffic

Set `GITHUB_SCRAPE_ORG` to opt Chronicler into an owner's non-archived repository
traffic. With it unset, Chronicler registers no traffic scrapers and makes no
traffic API calls. Its optional example catalog is
`agents/chronicler/metrics_catalog.json`.

The default traffic namespace is `github`: for example,
`github.traffic.views.14d`. To retain existing stored series, set
`CHRONICLER_GITHUB_METRIC_PREFIX` to their existing namespace and provide a
matching server catalog through `UNITARES_METRICS_CATALOG_EXTRA`. Metric names
are stable storage keys; changing them creates different series. The namespace
selects names only; `GITHUB_SCRAPE_ORG` selects the measured owner.

## Hook and draft-PR checks

Vigil's hook divergence check requires both `VIGIL_ACTIVITY_PATH` and
`VIGIL_HOOK_ARTIFACT_PATHS` (colon-separated paths). The activity file must
advance independently of the hooks; the artifact paths must be written when
hooks dispatch. Missing configuration produces `not configured` without
inspecting the filesystem. `VIGIL_CC_HISTORY_PATH` remains an explicitly set
compatibility alias; `VIGIL_ACTIVITY_PATH` takes precedence. Activity and stale
windows remain configurable through `VIGIL_HOOK_ACTIVITY_HOURS` and
`VIGIL_HOOK_STALE_HOURS`.

Set `VIGIL_STALLED_PR_OWNER` to opt into draft-PR checks. Missing configuration
makes no GitHub request and reports an indeterminate observation, preserving a
previous unhealthy verdict rather than claiming recovery.

Additional service checks register through `VIGIL_CHECK_PLUGINS`. State and
uptime are derived from each configured check's `service_key`. An absent
service receives no fabricated healthy state or uptime. Hook liveness has its
own condition key, so it cannot overwrite the governance server's health.

Deployment selections and installers belong in the deployment repository.
Install those settings before upgrading scheduled agents that previously
relied on implicit defaults.

## Scan roots and issue/report destinations

The periodic Watcher sweep requires `UNITARES_WATCHER_SWEEP_ROOTS`. It selects
no workspace by default and exits before creating state or contacting an
inference backend when the setting is absent. Scheduling and root selection
belong in deployment configuration.

The issue surfacer requires `--repo OWNER/REPO` or `DOGFOOD_ISSUE_REPO` before
reading findings or GitHub. Its existing dry-run, reviewed-fingerprint,
checkpoint and deduplication gates still apply.

The unresolved-dialectic report uses an explicit repository named in a PR
reference. To interpret bare `PR #N` references or search merged PR bodies for
answers, set `DIALECTIC_PR_REPO`. Without it, ambiguous subjects remain
unresolved and answer searches are disabled. Supersession still recognizes
explicit session-id citations. Answer caches are bound to the configured
repository; a cache from another scope cannot hide an objection. These are
surfacing rules only: the report never changes protocol state in the database.
