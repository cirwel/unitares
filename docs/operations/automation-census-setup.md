# Automation Census — agnostic setup

The automation census is the discovery layer behind the dashboard's **Automations**
registry. It is an *observability* tool, not a scheduler: it reads where your
automations are already defined, normalizes them into one schema
(`unitares.automation_census.v1`), and writes a snapshot the dashboard renders.
Nothing in it runs or schedules your jobs.

Tool: `scripts/ops/unitares-automations` (single file, Python 3.11+ stdlib only —
no dependencies). The operator's live install is a copy on `PATH`
(`~/.local/bin/unitares-automations`); this repo copy is canonical.

## What shows up automatically

Each collector degrades gracefully — an absent source yields zero items, never an
error. Most collectors skip it silently; `crontab` warns when its command is
missing or reports an error, and `systemd` when `systemctl` reports an error or,
on Linux, is missing. So a host only "sees" the schedulers it actually has:

| Source | Discovers | Portable? |
|---|---|---|
| `crontab` | your user crontab | any unix |
| `github-actions` | `.github/workflows/*` under `--projects-root` | any repo host |
| `systemd` | `--user` + system timers via `systemctl list-timers` | **Linux** (see caveat) |
| `launchd` | `~/Library/LaunchAgents/*` | macOS only |
| `claude` | `~/.claude/tasks` (scheduler `claude-tasks`) | Claude Code users |
| `codex` / `hermes` | `~/.codex/automations`, `~/.hermes/cron/jobs.json` | those tools only |
| `claude-hooks` | hook commands in `~/.claude/settings.json` / `settings.local.json` and installed Claude/Codex plugins' hook declarations, plus orphaned executables in `~/.claude/hooks` and plugin `hooks/` dirs | Claude Code / Codex plugin users |
| `remote:<host>` | crontab + systemd timers on the ssh host named in `UNITARES_CENSUS_REMOTE_HOST` (opt-in; unset = skipped) | any host reachable by non-interactive `ssh` |
| `external` | anything you declare by hand (see escape hatch) | any |

Anything *not* in that list (Kubernetes CronJobs, Airflow DAGs, Jenkins, Temporal,
…) is still representable via the **external escape hatch** below — it just isn't
auto-discovered.

## Setup (any host)

1. **Get the tool onto `PATH`** (or run it from the repo):
   ```bash
   install -m 755 scripts/ops/unitares-automations ~/.local/bin/unitares-automations
   ```
2. **Point it at your world.** All locations are env-overridable — nothing is
   hardcoded to one user:
   | Env var | Default | Purpose |
   |---|---|---|
   | `--projects-root` (flag) | `~/projects` | where to scan for GitHub Actions |
   | `UNITARES_AUTOMATION_STATE_DIR` | `~/.local/state/unitares-automations` | snapshot (`last.json`) |
   | `UNITARES_AUTOMATION_OVERRIDES` | `~/.local/share/unitares/automation-overrides.json` | your classifications |
   | `UNITARES_AUTOMATION_EXTERNAL` | `~/.local/share/unitares/automation-external.json` | hand-declared automations |
3. **Generate the snapshot** the dashboard reads:
   ```bash
   unitares-automations census --write --quiet     # writes last.json
   ```
   Schedule it (cron / systemd timer / launchd) so the registry stays fresh; the
   dashboard shows the snapshot age and flags it stale.
4. The dashboard endpoint (`/api/automations`) serves `last.json` from its default
   location (`~/.local/state/unitares-automations/last.json`). It does not read
   `UNITARES_AUTOMATION_STATE_DIR`: if you moved the snapshot, set
   `UNITARES_AUTOMATION_CENSUS_PATH` to that `last.json` in the governance
   server's environment.

## The escape hatch — declare anything

For automations no collector finds, write `automation-external.json` (an array of
census rows). Minimal example:

```json
[
  {
    "id": "k8s:nightly-billing",
    "name": "nightly-billing",
    "source": "external",
    "kind": "cron",
    "scheduler": "kubernetes-cronjob",
    "runner": "k8s",
    "cadence": "0 2 * * *",
    "notes": ["declared manually"]
  }
]
```

This makes the registry complete on *any* stack, even where auto-discovery can't
reach.

## Classification — the verifier-centric model

`automation-overrides.json` (keyed by census `id`) carries the accountability
fields the dashboard scores: `owner` (the constant principal), `escalates_to`
(the machine gate/verifier), and a `gate:<machine|human|ungated|external>` note.
The *model* is general; the *data* is yours — every operator authors their own,
because only you know how each automation is grounded in truth. That's by design,
not a customization to remove.

## Caveat: systemd discovery is unverified on Linux

`collect_systemd` uses `systemctl list-timers --output=json` (systemd 246+) and
parses microsecond-epoch timestamps defensively. It is verified to **no-op
cleanly on non-systemd hosts** (on macOS, which has no `systemctl`, it returns zero items and no warning;
the one `systemctl not found` warning is emitted only on Linux), but the discovery path itself has **not been smoke-tested against a
live systemd host**. First Linux operator to run it should sanity-check the timer
list and open an issue if the `next`/`last`/`activates` fields need adjusting for
their systemd version.
