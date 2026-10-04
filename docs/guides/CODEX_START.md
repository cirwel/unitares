# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

`AGENTS.md` is the machine-facing Codex bootstrap. This file is the human-facing quickstart.

The installable Codex adapter itself is canonical in the companion `unitares-governance-plugin` repo. This document is only the direct-workflow quickstart for operating against the `unitares` server repo.

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly, and check in when there is meaningful agent state to report: typically at most once per assistant turn, plus milestone check-ins for substantial work. Do not manufacture a check-in, and avoid per-tool or per-edit noise. The plugin's Stop hook may add its own `substrate_interpretation` row after a turn; that row is not an agent check-in and does not need echoing (see `AGENTS.md`, *Codex-specific wiring*).

## Stable Workflow

1. Run `/governance-start`
2. Keep continuity in `.unitares/session.json`
3. Do real work
4. Run `/checkin` when there is meaningful state to report (typically at most once per assistant turn), and after meaningful milestones
5. Run `/diagnose` when continuity or governance state looks wrong
6. Use `/dialectic` when you need structured review
7. Run `/closeout` before saying edited work is done

If you are not using commands directly, the equivalent raw tool flow is:

1. First run or fresh process: `start_session(force_new=true)` and save `agent_uuid` / `client_session_id`
2. Fresh process continuing prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="explicit")` — only for a real handoff from a finished predecessor
3. `sync_state()` after meaningful work, typically at most once per assistant turn
4. Same live owner / proof-owned rebind only: `identity(agent_uuid=..., continuity_token=..., resume=true)`
5. `check_working_state()` for read-only state checks
6. `health_check()` only if the system itself may be part of the problem

Canonical/raw equivalents are `onboard(...)`, `process_agent_update(...)`, and
`get_governance_metrics(...)`. Friendly alias calls return the agent-experience
envelope with `next_action` and compact state fields. Read the uuid from
`agent_uuid`: a plain fresh `start_session` (`response_shape: "routine"`) does
not repeat the canonical payload, and any other mint, or `response_mode="full"`,
also carries it under `raw_governance`.

## Codex Reality

- Codex uses slash commands and explicit tool calls; its lifecycle hooks are synchronous and do not imply a continuously running agent
- the plugin's Stop hook may record a `substrate_interpretation` row after a turn; that is the substrate's reading, not an agent check-in, so do not echo it
- agent-authored check-ins are yours to make, and only when there is meaningful state to report
- Watcher findings are manual unless you invoke the watcher CLI yourself
- `.unitares/session.json` is local workspace state; use its `uuid` as a lineage candidate, not a resume credential

## Continuity Model

- `uuid` is an identity anchor, not ownership proof
- `continuity_token` is short-lived ownership proof for same-owner/in-process use, not startup resume
- `client_session_id` is in-session transport continuity metadata
- `parent_agent_id` is how a fresh process declares lineage to prior work
- `session_resolution_source` tells you how the runtime actually resolved continuity
- if continuity falls back to a weak source, rerun `/governance-start`; do not repair it with bare UUID resume

## Operational detail

`.unitares/session.json` is the local cache for `uuid`, `client_session_id`, and
the observed resolution source. It assists the client; the server remains the
source of truth. Do not treat every edit or tool call as a governance event.

The machine-facing rules, Watcher commands, surface-claim procedure, tests, and
delivery checks live in [`AGENTS.md`](../../AGENTS.md). Repository delivery uses a
draft PR, then the merge queue once the owning agent labels it
`approved-to-merge` (a `governance-sensitive` PR still waits for the operator);
the full contract is
[`docs/operations/github-workflow-conventions.md`](../operations/github-workflow-conventions.md).

For the installable client rather than direct repository work, use the
[governance plugin](https://github.com/cirwel/unitares-governance-plugin).

## Scope

This file documents the stable manual Codex path. Older planning docs mention `explicit`, `dogfood-light`, and `dogfood-heavy` modes; treat those as planning terms unless a concrete runtime surface is documented alongside them.

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
