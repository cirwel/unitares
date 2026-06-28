# Automation registry overrides

`automation-overrides.json` (in this directory) is a **redacted schema example**
of the operator-authored metadata layered onto the auto-discovered automation
census. It encodes the **verifier-centric** accountability model the dashboard
renders as a role-reversal scorecard. The live census — a deployment's real
classifications — is **not** kept in this public repo (see below).

## Where the real census lives

The overrides encode real operator judgment — *how each automation is grounded
in truth* — so they belong under version control and review like any other gate.
That versioning happens in the operator's **own deployment location** (private),
symlinked into `~/.local/share/unitares/automation-overrides.json`. The copy in
this public repo is a redacted example showing only the schema and a few generic
rows — never one operator's live fleet, paths, or accountable principals.

## The model

- `owner` — the accountable **principal** (a person). Not the verifier. A constant.
- `escalates_to` — the **machine verifier / gate** that grounds the automation in
  truth (CI + migration-preflight, governance verdict + anomaly detection,
  dialectic review, Vigil, a deterministic scrape, …). For an ungated row this
  is honestly `none`.
- `notes: ["gate:<class>"]` — the **gate-class** the dashboard colours:
  - `gate:machine` — a machine check verifies it (green).
  - `gate:human` — a person is the gate; fragile (amber).
  - `gate:ungated` — nothing verifies it; faith-based risk (red).
  - `gate:external` — third-party, not your accountability (grey).
  - (no tag) — `github-actions` default to machine in the dashboard; everything
    else reads as `unclassified`.
- `dashboard_priority` — lower floats higher; the un-inverted (ungated, then
  human) lead the table.
- `description` / `surface_claims` / `expected_outputs` — discovery context.

Entries are keyed by the census `id` (e.g. `launchd:com.example.nightly-backup`).

## How it is consumed

`unitares-automations` reads `UNITARES_AUTOMATION_OVERRIDES` (default
`~/.local/share/unitares/automation-overrides.json`) and applies it on top of
auto-discovery (`load_overrides` / `apply_overrides`). The live default path is a
**symlink to the operator's private overrides file** (not this public copy), so
the running census reads the reviewed file.
`census --write` bakes the result into `~/.local/state/unitares-automations/last.json`,
which the dashboard's `/api/automations` endpoint serves; the 30-minute
`com.unitares.automation-census` job re-applies it.

## To change a classification

Edit the overrides in your private deployment location →
`unitares-automations census --write`. The dashboard scorecard updates on its
next load. Changing the redacted example in this public repo does **not** affect
any live census.

## Known gap

The census tool itself is not yet version-controlled (it lives only at
`~/.local/bin/unitares-automations`). Versioning the tool is a separate
follow-up; the operator's private overrides at least preserve the judgment it
consumes.
