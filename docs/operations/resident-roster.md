# Resident roster (`UNITARES_RESIDENTS`)

The set of **named resident agents** for a deployment is configuration, not a
hardcoded fleet. It is declared via the `UNITARES_RESIDENTS` environment
variable, read by both the governance server and the agent SDK.

## Why this exists

UNITARES ships with reference resident agents (Vigil, Sentinel, Watcher,
Chronicler, plus the embodied Lumen on the canonical deployment). Earlier these
names were hardcoded in two places — `src/grounding/class_indicator.py`
(`KNOWN_RESIDENT_LABELS`) and `agents/sdk/.../​_substrate.py`
(`KNOWN_RESIDENT_NAMES`). That baked one operator's fleet into the framework,
so a fresh install inherited identities (and an N=1 calibration class for
`Lumen`) that did not exist on that machine.

The roster is now read from `UNITARES_RESIDENTS`, **empty by default**. A fresh
install therefore has *no* named residents: every agent classifies by tag
(`embodied` / `persistent` / `ephemeral`) or falls through to the `default`
calibration class. Named residents are an opt-in specialization, not a baked-in
fleet.

## Format

Comma-separated labels, matching the `name` each resident onboards with
(capitalized per the identity rules):

```
UNITARES_RESIDENTS=Vigil,Sentinel,Watcher,Chronicler
```

Unset or empty ⇒ no named residents.

**Order matters.** The roster is an ordered list, and `/v1/residents` presents
residents in the order declared here. Declare them in the order you want the
dashboard to render. (Before 2026-08-18 presentation order came from a
hardcoded list of this operator's six residents, so any other deployment's
roster ordered to an empty list while the response still reported
`source: "known-residents"`.)

## Where to set it

The value **must be consistent** across the processes that classify or emit for
residents:

- **Governance server** (`com.unitares.governance-mcp.plist`) — classifies
  every agent, so it needs the full roster.
- **Each resident agent** (`com.unitares.{vigil,sentinel,sentinel-beam,chronicler,vigil-hygiene}.plist`)
  — the SDK gates substrate-state emission on the resident's own name being in
  the roster.

The checked-in plist + templates under `scripts/ops/` set the canonical fleet
(`Lumen,Vigil,Sentinel,Watcher,Steward,Chronicler`). A different deployment
edits these to its own roster, or clears them for a residentless install.

## Two neighbouring env vars that are NOT this one

- **`UNITARES_RESIDENT_AGENTS`** — a route-local override for `/v1/residents`
  only, for when the dashboard should show a different set than the calibration
  roster. It takes precedence over everything else on that endpoint and affects
  nothing else. Most deployments leave it unset; `UNITARES_RESIDENTS` is the
  knob you want.
- **`UNITARES_RESIDENT_SILENCE_SECONDS`** — per-label dashboard silence
  thresholds, `label=seconds` comma-separated
  (e.g. `vigil=2400,sentinel=900,lumen=600,watcher=86400,chronicler=108000`).
  Empty by default. This is a **fallback that should shrink to nothing**: the
  generic path is a `cadence.*` tag on the agent, which sets the threshold with
  no label lookup at all. Tag the agent rather than adding an entry here. Both
  unset ⇒ 30 minutes.

## Keeping shipped source roster-neutral

`scripts/dev/check_fleet_identity_leak.py` (wired into pre-commit and the
`Repo Scope Guard` workflow) fails the build if a resident name appears as a
string literal in `src/` or `agents/sdk/src/`. Read the roster instead.

Provenance in a **comment** is deliberately not flagged — a note explaining that
a threshold has its value because of what a particular resident did on a
particular date is the reason the constant is what it is, and deleting it would
make the code less honest without making it more portable.

The guard also prints the couplings that already exist and have not been fixed
(currently `src/agent_lifecycle.py` and `src/http_routes/vigil.py`) on every
run, passing or failing. It does not silence them: a guard that reported
"clean" over known coupling would be the same instrument-optimism failure it
exists to catch.

## Calibration note

Each named resident becomes its own N=1 calibration class. If you add a
resident to the roster, it must also have class-conditional scale constants in
`config/governance_config.py` (`DELTA_NORM_MAX_BY_CLASS`,
`HEALTHY_OPERATING_POINT_BY_CLASS`, etc.) — `tests/test_grounding_scale_constants.py`
enforces this. Residents with no constants fall back to fleet defaults via the
`.get(agent_class, *_DEFAULT)` lookups, so an *unnamed* agent is always safe;
the constraint only applies to names you place in the roster.

## Resident-progress manifest (`UNITARES_RESIDENT_PROGRESS_MANIFEST`)

The resident-**progress probe** (liveness/output monitoring in the server's
background tasks) has its own roster, because it needs more than a name per
resident: a metric source, window, threshold, and heartbeat cadence. It is
loaded from a JSON manifest pointed to by `UNITARES_RESIDENT_PROGRESS_MANIFEST`,
**empty by default** (no residents probed).

The canonical fleet ships as `config/resident_progress.example.json`. Point the
env var at it (or a deployment-specific copy):

```
UNITARES_RESIDENT_PROGRESS_MANIFEST=/path/to/unitares/config/resident_progress.example.json
```

Set this on the **governance server** plist (the probe runs there). Each entry's
`source` must match either a first-party source built in `src/background_tasks.py`
(`kg_writes`, `watcher_findings`, `eisv_sync_rows`, `metrics_series`,
`sentinel_pulse`, `agent_checkins`) or a third-party source discovered via entry
point (below). Labels are lowercase to match the anchor filenames under
`~/.unitares/anchors/`.

### Bringing your own progress source

A deployment running an out-of-tree resident needs a metric that says whether
that resident is making progress — and until it has one, it can name the
resident in the manifest but every tick resolves to an error. Third-party
sources are therefore discovered from the
`unitares.resident_progress_sources` entry-point group. In your distribution:

```toml
[project.entry-points."unitares.resident_progress_sources"]
my_source = "mypkg.sources:MySource"
```

The target is called with the server's db handle and must return an object
satisfying `ResidentProgressSource` (`src/resident_progress/sources.py`) —
a `name` attribute and `async def fetch(resident_uuids, window) -> dict[str, int]`.
Issue **one batched query** covering all passed UUIDs; the probe groups
`(source, window)` pairs and calls each group once, so per-resident fanout
would multiply against the whole roster.

Three rules, all enforced at load:

- **The entry-point name, `source.name`, and the manifest's `source` field must
  be the same string.** A mismatch is rejected rather than silently re-keyed —
  that is how a source ends up installed but referenced by nothing.
- **First-party names win.** A plugin claiming `kg_writes` is rejected; it could
  otherwise redefine what "Vigil made progress" means with identical-looking
  snapshot rows.
- **A broken plugin is skipped, not fatal.** It is logged at WARNING as
  `[PROGRESS_FLAT] source plugin rejected: …` and the probe starts without it.
  Check the server log after installing one — a source that never registers
  presents as a resident that never progresses.

Install the distribution into the **governance server's** environment (the probe
runs in-process there, issuing SQL against the governance DB). No plist edit is
needed; installing is sufficient. Set
`UNITARES_RESIDENT_PROGRESS_PLUGINS=0` to disable discovery entirely.

This differs from `VIGIL_CHECK_PLUGINS` below, which uses colon-separated module
paths and lets a bad plugin raise. Vigil runs `--once` on a timer so a crash
retries next cycle; the progress probe is a long-lived task that is not
restartable, so it contains failures and reports them instead.

`UNITARES_RESIDENTS` (names/calibration) and this manifest (progress probing)
are related but distinct: a deployment that runs residents typically sets both,
listing the same residents in each.

## Vigil health-check targets

Vigil's health checks are pluggable (`VIGIL_CHECK_PLUGINS`, see
`agents/vigil/checks/registry.py`). The built-in checks are governance health,
resident-tag hygiene, and plugin-hook liveness; the **Lumen/anima health check
is an external plugin**, not shipped in this repo — a residentless install
simply doesn't register it (Vigil reports Lumen as `not configured` and
healthy, so nothing breaks).

Any health check a deployment registers — its own `redis`, `gateway`, etc. —
now gets full per-service bookkeeping (`{svc}_healthy` / `_detail` /
`_up_cycles` / `_down_streak`) and outage/recovery/sustained-outage change
notes, the same treatment governance and Lumen get. No service names are
hardcoded into the change-detection path.

## Cross-package contract

The env var **name** (`UNITARES_RESIDENTS`) is the contract between core and the
SDK — the standalone SDK cannot import from `src/`. Both sides parse it
identically (`parse_resident_roster`). `agents/sdk/tests/test_substrate_emission.py`
and `tests/test_grounding_class_indicator.py` pin the parsing and the env var
name on each side.
