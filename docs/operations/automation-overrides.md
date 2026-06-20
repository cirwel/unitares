# Automation registry overrides

`automation-overrides.json` (in this directory) is the operator-authored metadata
layered onto the auto-discovered automation census. It encodes the
**verifier-centric** accountability model the dashboard renders as a
role-reversal scorecard.

## Why this is version-controlled

The census tool (`unitares-automations`, currently standalone at
`~/.local/bin/`) and this overrides file used to live entirely outside git. The
overrides encode real judgment — *how each automation is grounded in truth* — so
it belongs under review like any other gate. This file is canonical; the live
path is a symlink to it.

## The model

- `owner` — the accountable **principal** (Kenny). Not the verifier. A constant.
- `escalates_to` — the **machine verifier / gate** that grounds the automation in
  truth (CI + migration-preflight, governance verdict + anomaly detection,
  dialectic councils, Vigil, a deterministic scrape, …). For an ungated row this
  is honestly `none`.
- `notes: ["gate:<class>"]` — the **gate-class** the dashboard colours:
  - `gate:machine` — a machine check verifies it (green).
  - `gate:human` — you are the gate; fragile (amber).
  - `gate:ungated` — nothing verifies it; faith-based risk (red).
  - `gate:external` — third-party, not your accountability (grey).
  - (no tag) — `github-actions` default to machine in the dashboard; everything
    else reads as `unclassified`.
- `dashboard_priority` — lower floats higher; the un-inverted (ungated, then
  human) lead the table.
- `description` / `surface_claims` / `expected_outputs` — discovery context.

Entries are keyed by the census `id` (e.g. `launchd:com.unitares.vigil`).

## How it is consumed

`unitares-automations` reads `UNITARES_AUTOMATION_OVERRIDES` (default
`~/.local/share/unitares/automation-overrides.json`) and applies it on top of
auto-discovery (`load_overrides` / `apply_overrides`). The live default path is a
**symlink to this repo copy**, so the running census reads the reviewed file.
`census --write` bakes the result into `~/.local/state/unitares-automations/last.json`,
which the dashboard's `/api/automations` endpoint serves; the 30-minute
`com.unitares.automation-census` job re-applies it.

## To change a classification

Edit `automation-overrides.json` here → PR → merge → `git pull` in the deploy
worktree → `unitares-automations census --write`. The dashboard scorecard
updates on its next load.

## Known gap

The census tool itself is not yet version-controlled (it lives only at
`~/.local/bin/unitares-automations`). Versioning the tool is a separate
follow-up; this file at least preserves the operator judgment it consumes.
