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

## Calibration note

Each named resident becomes its own N=1 calibration class. If you add a
resident to the roster, it must also have class-conditional scale constants in
`config/governance_config.py` (`DELTA_NORM_MAX_BY_CLASS`,
`HEALTHY_OPERATING_POINT_BY_CLASS`, etc.) — `tests/test_grounding_scale_constants.py`
enforces this. Residents with no constants fall back to fleet defaults via the
`.get(agent_class, *_DEFAULT)` lookups, so an *unnamed* agent is always safe;
the constraint only applies to names you place in the roster.

## Cross-package contract

The env var **name** (`UNITARES_RESIDENTS`) is the contract between core and the
SDK — the standalone SDK cannot import from `src/`. Both sides parse it
identically (`parse_resident_roster`). `agents/sdk/tests/test_substrate_emission.py`
and `tests/test_grounding_class_indicator.py` pin the parsing and the env var
name on each side.
