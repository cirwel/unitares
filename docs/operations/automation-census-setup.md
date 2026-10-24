# Automation Census — agnostic setup

The automation census is the discovery layer behind the dashboard's **Automations**
registry. It is an *observability* tool, not a scheduler: it reads where your
automations are already defined, normalizes them into one schema
(`unitares.automation_census.v1`), and writes a snapshot the dashboard renders.
Nothing in it runs or schedules your jobs.

For launchd, configuration and execution are separate evidence. The collector
reads `launchctl list` and the user domain's disabled overrides. `active` means
loaded, not healthy; a loaded periodic job can legitimately have no PID.
An enabled plist absent from the scheduler is `inactive`; an unavailable
scheduler read is `unknown`. Nonzero last exits on idle jobs and unconfirmed
running receipts enter `needs_attention`. A previous exit on a currently
running service is historical, not evidence that its current process failed.
Operator classifications (including intentional pauses) remain available,
but cannot erase the execution evidence in `notes`.

Run receipts retain their original timestamps. An old `running/` receipt
cannot override a newer terminal receipt in `latest/`. A running receipt with
no launchd process is reported as unknown completion, never invented success
or failure. The census itself may appear running in the snapshot it is in the
process of writing; inspect its subsequent completion receipt when auditing it.

For the dependency-ordered operational closure plan, immutable safety surfaces
and registered adaptive-versus-fixed evaluation, see
[Bounded fix-loop readiness](bounded-fix-loop-readiness.md).

Tool: `scripts/ops/unitares-automations` (single file, Python 3.11+ stdlib only —
no dependencies). Run it from a checkout or install it on `PATH`. No particular
installation layout is required. A deployment can opt into a symlink integrity
check by setting `UNITARES_AUTOMATION_INSTALL_SOURCE` to its maintained source
file; failures appear as `self:` warnings in the census.

## What shows up automatically

Each collector degrades gracefully — an absent source yields zero items, never an
error. Most collectors skip it silently. Three warn:

- `crontab`, when its command is missing or `crontab -l` exits non-zero with a
  message (including the usual `no crontab for <user>`)
- `systemd`, when `systemctl` reports an error or, on Linux, is missing
- `remote:<host>` (only when `UNITARES_CENSUS_REMOTE_HOST` is set), when the host
  is unreachable or its remote `crontab -l` / `systemctl` call fails

So a host only "sees" the schedulers it actually has:

| Source | Discovers | Portable? |
|---|---|---|
| `crontab` | your user crontab | any unix |
| `github-actions` | `.github/workflows/*` under `--projects-root` | any repo host |
| `systemd` | `--user` + system timers via `systemctl list-timers` | **Linux** (see caveat) |
| `launchd` | `~/Library/LaunchAgents/*`, reconciled with the user domain's loaded/disabled state and latest exit code | macOS only |
| `claude` | `~/.claude/tasks` (scheduler `claude-tasks`) | Claude Code users |
| `codex` / `hermes` | `~/.codex/automations`, `~/.hermes/cron/jobs.json` | those tools only |
| `claude-hooks` | hook commands in `~/.claude/settings.json` / `settings.local.json` and installed Claude/Codex plugins' hook declarations, plus orphaned executables in `~/.claude/hooks` and plugin `hooks/` dirs | Claude Code / Codex plugin users |
| `remote:<host>` | crontab + systemd timers on the ssh host named in `UNITARES_CENSUS_REMOTE_HOST` (opt-in; unset = skipped) | any host reachable by non-interactive `ssh` |
| `external` | anything you declare by hand (see escape hatch) | any |

Anything *not* in that list (Kubernetes CronJobs, Airflow DAGs, Jenkins, Temporal,
…) is still representable via the **external escape hatch** below — it just isn't
auto-discovered.

On macOS, a plist on disk is configuration, not proof that launchd loaded it.
The collector reads `launchctl list` (rather than `launchctl print`, whose
environment block can contain credentials) and `launchctl print-disabled`.
It reports an enabled plist absent from the loaded domain as `inactive`, a
provably missing executable/interpreter script as `missing`, and the latest
launchd exit as `last_status`. The normalized `scheduler_state`,
`scheduler_last_exit_code`, and `scheduler_pid` fields retain that provenance.
Wrapper receipts still supply timestamps and messages, but cannot overrule a
newer contradictory launchd exit. A disproven `running` receipt is flagged as
stale while its completion remains `unknown`; if its scheduler definition is
gone, it remains visible as an `orphaned` attention row instead of disappearing
from the census.

## Setup (any host)

1. **Get the tool onto `PATH`** (or run it from the repo):
   ```bash
   ln -sfn "$PWD/scripts/ops/unitares-automations" ~/.local/bin/unitares-automations
   ```
   A symlink follows updates to the source checkout. To check that this link
   still points at the intended source, set `UNITARES_AUTOMATION_INSTALL_SOURCE`
   to the absolute source path in the environment that runs the census.
   The check reports a missing source, missing or broken link, installed copy,
   or a link to a different source. It does not repair or write the installation.
   Leaving the source unset disables this check.
2. **Point it at your world.** These locations are env-overridable:
   | Env var | Default | Purpose |
   |---|---|---|
   | `--projects-root` (flag) | `~/projects` | where to scan for GitHub Actions |
   | `UNITARES_AUTOMATION_STATE_DIR` | `~/.local/state/unitares-automations` | snapshot (`last.json`) |
   | `UNITARES_AUTOMATION_OVERRIDES` | `~/.local/share/unitares/automation-overrides.json` | your classifications |
   | `UNITARES_AUTOMATION_EXTERNAL` | `~/.local/share/unitares/automation-external.json` | hand-declared automations |
   | `UNITARES_AUTOMATION_INSTALL_SOURCE` | unset (check disabled) | expected source file for the optional install check |
   | `UNITARES_AUTOMATION_INSTALL_PATH` | `~/.local/bin/unitares-automations` | symlink to check when an expected source is supplied |
3. **Generate the snapshot** the dashboard reads:
   ```bash
   unitares-automations census --write --quiet     # writes last.json
   ```
   Schedule it (cron / systemd timer / launchd) so the registry stays fresh; the
   dashboard shows the snapshot age and flags it stale.
4. Mount the endpoint. The shipped default mounts no route packs, so without
   this step `/api/automations` answers 404. Declare the handler in a route-pack
   manifest, for example `~/.config/unitares/route-packs.json`:
   ```json
   {
     "schema": "unitares.route-packs.v1",
     "packs": {
       "automation-census": [
         {"path": "/api/automations",
          "handler": "/path/to/unitares/scripts/ops/automation_census_route.py:http_automations",
          "methods": ["GET"]}
       ]
     }
   }
   ```
   Then set both `UNITARES_ROUTE_PACKS_FILE` (the manifest's path) and
   `UNITARES_ROUTE_PACKS=automation-census` in the governance server's
   environment. A pack name with no manifest entry mounts nothing. See "Route
   packs" in `docs/operations/resident-roster.md`.
   The endpoint (`/api/automations`) serves `last.json` from
   `$UNITARES_AUTOMATION_STATE_DIR` when that is set in the governance server's
   environment, else from `~/.local/state/unitares-automations/last.json`. Set
   the same `UNITARES_AUTOMATION_STATE_DIR` for the census writer and the
   server, or set `UNITARES_AUTOMATION_CENSUS_PATH` to the full path of
   `last.json`; when set, it takes precedence over the state dir.

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
