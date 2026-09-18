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

The checked-in plist + templates under `scripts/ops/` ship an **empty** roster;
the canonical fleet (`Lumen,Vigil,Sentinel,Watcher,Chronicler`) appears
only as a comment example. A deployment sets these to its own roster, or leaves
them empty for a residentless install.

### Non-obvious consequence: privileged tags

The roster is not only a classification hint — it is the **only** sanctioned way
an identity acquires `persistent` and `autonomous`. Both are in `PRIVILEGED_TAGS`
(`src/mcp_handlers/lifecycle/mutation.py`) and the server refuses
self-assignment, because they confer auto-archival immunity and loop-detection
exemption. The grant happens in `src/grounding/onboard_classifier.py`, which
stamps `RESIDENT_DEFAULT_TAGS` at mint time when the onboarding `name` matches
the roster **exactly**.

So a resident onboarded under a name that is not in the roster comes up
untagged, and the orphan sweep archives it — silently, later, after anything
anchored to it has started attributing to a ghost. If you are adding a resident,
add it to the roster and restart the governance server *before* it first
onboards. `scripts/ops/provision_doctor_identity.py` is the worked example: it
refuses to write its anchor when the read-back shows the tags were not granted.

### Anchoring a resident that was minted without the SDK

The SDK writes `~/.unitares/anchors/<name>.json` at first onboard and resumes
from it in every later process with
`identity(agent_uuid=..., continuity_token=..., resume=true)` (PATH 0: the
token's signature and `aid` claim are the ownership proof; its `exp` is
deliberately ignored, see `extract_token_agent_uuid`). A rostered identity minted
any other way — a `start_session` call from an orchestrator, a session whose
name was added to the roster afterwards — has no such file. Its identity is
carried only by a process-local session binding, which works until the first
process boundary after `UNITARES_IDENTITY_STRICT=strict` /
`STRICT_IDENTITY_REQUIRED=true` lands: the binding-only resume is refused with
`lineage_declaration_required`, and the two alternatives the refusal names
(`force_new=true`, `parent_agent_id=...`) mint a successor rather than resume
the identity. Any baseline keyed on the original UUID is lost either way.

`scripts/ops/provision_resident_anchor.py` is the operator-side repair. Run it
where the governance server's environment is available, so the signing secret
(`UNITARES_CONTINUITY_TOKEN_SECRET`, else `UNITARES_HTTP_API_TOKEN`, else
`UNITARES_API_TOKEN`) matches the server's:

```bash
python3 scripts/ops/provision_resident_anchor.py --agent-uuid <UUID> --name <name>          # dry run
python3 scripts/ops/provision_resident_anchor.py --agent-uuid <UUID> --name <name> --apply
```

It reads the identity back (must be `active`, carry `persistent` +
`autonomous`, and have a server label equal to `<name>` lowercased, because the
FILENAME is what `resident_progress.resolve_resident_uuid` looks the anchor up
by), mints a token bound to the UUID, proves the server ACCEPTED that token as
the thing that resolved the resume, and only then writes the anchor (`0600`,
via a temp file created private and fsync'd, so the token is never briefly
world-readable). It never mints an identity and never writes tags. An anchor
that already names the same UUID is left alone, exit 0. An anchor naming a
different UUID is refused unless you pass `--replace-identity`, which prints the
displaced UUID: a silent repoint is how a resident forks.

⛔**A matching UUID is not proof, which is why the verify checks more than the
UUID.** `UNITARES_IDENTITY_STRICT` defaults to `log`, and in that mode a PATH 0
resume whose token fails its ownership check logs, broadcasts
`identity_hijack_suspected`, and resumes anyway — with the correct UUID in the
response. Reproduced against v2.22.1 with an expired token: `success: true`,
`resumed: true`, matching UUID, and alongside them
`proof_origin: "server_inferred"`, `caller_proven: false`,
`session_resolution_source: "agent_uuid_direct_fastpath"`, plus an explicit
`identity_warnings` entry `continuity_token_invalid`. A verify that compared
only the UUID therefore returned green for a token the server had rejected,
usually because the operator's signing secret differed from the server's, and
deferred the failure to the resident's next session. The script now requires
`proof_origin: "caller_asserted"` (or a continuity-token
`session_resolution_source`) and refuses on that warning.

On a UDS deployment a `persistent` resident attests by peer credential rather
than by token, and the SDK deliberately writes a UUID-only anchor for it
(`_save_session`). The script matches that and writes no token there, rather
than leaking a bearer credential into a file designed to hold none.

The anchor is a credential: it stays outside git and outside any session record.
A resident resuming from it should present the fresh token the resume returns on
its first check-in if that check-in is refused with a resume miss (the SDK's own
one-retry rebind), and write that fresh token back to the anchor at session end.

One side effect worth knowing: an anchor for a `persistent` UUID satisfies the
dedicated-substrate condition in `evaluate_substrate_earned`, which is one of
the exemptions the strict write gate honours. The file is a write-gate
credential, not merely a lookup hint.

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

Several residents may name the same `source`. The probe fetches once per
distinct `(source, window_seconds)` pair and keys results and errors on that
pair, not on the source name. Residents that share both the source and the
window share one fetch; a different window gets its own fetch and its own
counts. A failed fetch marks only that pair's residents `source_error`.

### A malformed entry costs that resident, not the roster

An entry that cannot be built is **skipped with a `WARNING` naming the label
and the error**, and the rest of the roster loads. Grep the server log for
`resident-progress manifest entry` after editing the manifest — a skipped
resident is simply never probed, which otherwise looks identical to a resident
that is quiet.

An entry is skipped when it is missing `source`, `metric`, `window_seconds` or
`threshold`; when `window_seconds`, `threshold` or `expected_cadence_s` is not
a number (`expected_cadence_s` may be `null`, meaning event-driven, but must
otherwise be positive); or when `source` or `metric` is not a string. A
manifest whose top level is not a JSON object — a bare array, say — loads as
empty, the same as an absent file.

It degrades rather than refusing to start because of *where* the registry is
built. It is built at module import, and every importer imports the module
lazily from inside an already-running server: the supervised
`progress_flat_probe_task`, `/v1/progress_flat/recent`, and the silence
detector's event-driven check. A raise therefore could not fail the start it
would need to fail — it passed config load and server start, and only then
stopped progress probing for **every** resident, leaving a background-task
crash line as its only trace.

That last importer is why the blast radius exceeded the probe.
`background_tasks._get_expected_interval` consults `is_event_driven_label`
first, inside a `try/except Exception: pass`. That `except` swallowed the
import crash and fell through to the cadence fallbacks, so an event-driven
resident carrying an `autonomous` tag went from exempt (no expected interval)
to one of 300s — and the silence detector escalates
`lifecycle_silent_critical` at 5x the interval. One unrelated typo in the
manifest turned a quiet-but-healthy event-driven resident into a recurring
false critical page.

`ResidentConfig` itself still raises on a bad cadence. The skip is the manifest
parser's policy for untrusted deployment config, not a relaxation of the
dataclass guard, so a bad cadence constructed in code still fails loudly.

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
