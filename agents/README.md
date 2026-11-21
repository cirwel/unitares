# agents/

Reference implementations of Unitares governance agents. These are **not** part of the public contract — the public contract is [`unitares-sdk`](sdk/) (published as its own package). Treat the code under `vigil/` and `sentinel/` as examples of how to build a resident agent, not as load-bearing governance internals.

These implementations are optional. Installing the server does not select a
resident fleet or enable a scheduler. Configure names, capabilities, endpoints,
and schedules for the agents you deploy; keep actual fleet configuration in a
separate deployment repository.

These specialized agents are also distinct from a general-purpose **UNITARES
Resident** product. If that first-party conversational runtime is built, it
belongs in a separate `unitares-resident` repository and must depend only on
the public MCP/SDK contract. It must not import Core internals, access the
governance database directly, or receive a privileged measurement path.

## Layout

| Path        | Role                                                                           |
|-------------|--------------------------------------------------------------------------------|
| `sdk/`      | **Integral.** Public agent-to-governance contract (`unitares-sdk` package).   |
| `common/`   | Shared helpers used by the residents in this tree (findings, config, log). |
| `vigil/`    | Reference **janitorial** resident — runs on a schedule, posts health findings. |
| `sentinel/` | Reference **fleet-monitor** resident — continuous, WebSocket-driven.          |
| `github_observer/` | Optional **observer**, not a resident: reads CI results, review records and state changes on agent-signed pull requests from GitHub. Dry run only until its write route lands (`docs/proposals/active/github-outcome-observer-v0.md`). |
| `governance-reviewer.md` | Claude Code **subagent definition** (frontmatter + prompt, `memory: project`). Not auto-discovered here — install into `~/.claude/agents/` or ship via the gov-plugin (`scripts/dev/sync-plugin-agents.sh`); see the "Subagent definitions and per-agent memory" section of `CLAUDE.md`. |

## Residents that have moved out

A deployment's residents belong in its deployment repository, built on the
public SDK. Two that used to live here moved out on that basis: the
code-watcher resident (Watcher, `agents/watcher/`) and the daily metrics
scraper (Chronicler, `agents/chronicler/`). The residents still in this tree
are slated to follow. Core keeps only what a deployment-owned resident reads
through: the Watcher state-location contract (`src/watcher_state_reader.py`)
and the extra-metrics-catalog mechanism (`UNITARES_METRICS_CATALOG_EXTRA`).

The branch-hygiene sweep that sat beside them (`agents/vigil_hygiene/`) is not
a resident: it is a git tool the workspace closeout script uses, and now lives
at `scripts/dev/branch_hygiene/`.

## Running your own

To deploy your own residents, depend on `unitares-sdk` and follow the `run_cycle` pattern shown in `vigil/agent.py` or `sentinel/agent.py`.

Lumen (the embodied agent) lives in a separate repo (`anima-mcp`) and shows that a resident can run **out-of-tree** — it is declared through `UNITARES_RESIDENTS` like any other. It is *not* an SDK consumer, though: it talks to governance directly and imports nothing from `unitares_sdk`. As of 2026-08, every SDK consumer is in this repo, so the `sdk-package` CI job is what stands in for an outside consumer.

Install it from PyPI (or `-e agents/sdk` from a checkout):

```bash
pip install unitares-sdk
```

See [`sdk/README.md`](sdk/README.md) for pinning to a specific server release, and
for the tag-driven release path.

Declare the resident to a deployment with `UNITARES_RESIDENTS` (names and calibration class) and the `UNITARES_RESIDENT_PROGRESS_MANIFEST` (progress probing). If you want the progress probe to track a metric of your own, ship a source in the `unitares.resident_progress_sources` entry-point group — no change to this repo is required. See [`docs/operations/resident-roster.md`](../docs/operations/resident-roster.md).

## Scheduling optional residents

Choose a supervisor appropriate to the resident's lifecycle: a timer for a
bounded invocation or a service manager for a continuous process. macOS
LaunchAgent templates are available under `scripts/ops/`; adapt only those you
intend to use. A template is not a default fleet or an instruction to enable a
particular resident. Store the configured services and their schedules with
your deployment, and keep `UNITARES_RESIDENTS` consistent with the server.
