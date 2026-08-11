# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

`AGENTS.md` is the machine-facing Codex bootstrap. This file is the human-facing quickstart.

The installable Codex adapter itself is canonical in the companion `unitares-governance-plugin` repo. This document is only the direct-workflow quickstart for operating against the `unitares` server repo.

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly, and check in once per assistant turn as a behavioral baseline. Add milestone check-ins for substantial work; avoid per-tool or per-edit noise.

## Stable Workflow

1. Run `/governance-start`
2. Keep continuity in `.unitares/session.json`
3. Do real work
4. Run `/checkin` once per assistant turn, and after meaningful milestones
5. Run `/diagnose` when continuity or governance state looks wrong
6. Use `/dialectic` when you need structured review
7. Run `/closeout` before saying edited work is done

If you are not using commands directly, the equivalent raw tool flow is:

1. First run or fresh process: `start_session(force_new=true)` and save `agent_uuid` / `client_session_id`
2. Fresh process continuing prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. `sync_state()` once per assistant turn, and after meaningful work
4. Same live owner / proof-owned rebind only: `identity(agent_uuid=..., continuity_token=..., resume=true)`
5. `check_working_state()` for read-only state checks
6. `health_check()` only if the system itself may be part of the problem

Canonical/raw equivalents are `onboard(...)`, `process_agent_update(...)`, and
`get_governance_metrics(...)`. Friendly alias calls return the agent-experience
envelope with `next_action`, compact state fields, and the full canonical
payload under `raw_governance`.

## Codex Reality

- Codex uses slash commands and explicit tool calls, not Claude hooks
- nothing auto-checks in for you; keep the turn-level baseline yourself
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
delivery checks live in [`AGENTS.md`](AGENTS.md). Repository delivery uses a
draft PR and human merge gate; the full contract is
[`docs/operations/github-workflow-conventions.md`](docs/operations/github-workflow-conventions.md).

For the installable client rather than direct repository work, use the
[governance plugin](https://github.com/cirwel/unitares-governance-plugin).

## Scope

This file documents the stable manual Codex path. Older planning docs mention `explicit`, `dogfood-light`, and `dogfood-heavy` modes; treat those as planning terms unless a concrete runtime surface is documented alongside them.

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
