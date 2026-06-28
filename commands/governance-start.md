---
description: "Create or declare lineage for a UNITARES session in Codex"
---

Under the identity ontology v2 (`docs/ontology/identity.md`), a fresh process-instance mints fresh governance-identity. Lineage is declared via `parent_agent_id`, not resumed via token. This command starts a session in that posture.

Start by checking the local workspace cache inventory.

Use the shared helper in this repo:

- `scripts/client/session_cache.py list`

Then run the workspace start guard:

- `python3 scripts/dev/workspace_closeout.py --start-check`

This fails on dirty git state or non-baseline repo-rooted processes from a
prior session, and writes a fresh process baseline only when the workspace is
clean.

It also classifies workspace isolation: editing the shared/main checkout (or the
deploy worktree) instead of an agent-owned linked worktree is surfaced as an
**advisory warning** by default. Add `--require-worktree` to make that a hard
failure (strict mode) once the fleet reliably works in worktrees — the
advisory→strict rollout mirrors the Surface Lease Plane. Rationale:
`docs/proposals/worktree-isolation-vs-lease-default.md`.

A cached `parent_agent_id` from a prior session is context, not a lineage
instruction: co-location in this workspace is not lineage, and the prior session
is almost always still a sibling rather than an exited predecessor. Ignore any
legacy `continuity_token` field for startup; tokens are only for advanced
same-live-owner diagnostic rebinds.

Call `onboard()` against UNITARES using the strongest honest mode:

- default: a fresh session onboards fresh — pass `force_new=true` with no `parent_agent_id`
- declare lineage only for a real causal event: a dispatched subagent (`parent_agent_id=<dispatcher uuid>`, `spawn_reason="subagent"`, usually set automatically by the dispatcher) or a handoff from a finished prior session (`parent_agent_id=<prior uuid>`, `spawn_reason="new_session"`). Declaring a currently-live agent as parent is rejected (`lineage_coincidental_rejected`).
- include `model_type` when the current runtime is clear from context
- do not invent a display name unless the user asked for one

`start_session(...)` is an equivalent alias (same parameters, same rules). Invoking the alias returns the normalized agent-experience envelope — `next_action`/`state_summary` first, `agent_uuid` and `client_session_id` lifted to the top level, and the full canonical payload (including `session_resolution_source` and the other cache fields below) under `raw_governance`.

Do not use bare `identity(agent_uuid=<uuid>, resume=true)`. UUID alone is an unsigned claim and is hijack-shaped under strict identity mode.

Do not use `onboard(continuity_token=...)` as cross-process resume; after S1-c it returns `status=continuity_token_resume_rejected`.

After a successful `identity()` or `onboard()` response:

- create or update a slotted cache using `scripts/client/session_cache.py set session --slot <client_session_id-or-codex-session-id> --merge --stamp`
- keep it compact and machine-readable JSON
- include:
  - `schema_version: 2`
  - `server_url` when known
  - `uuid`
  - `agent_id`
  - `display_name`
  - `client_session_id`
  - `session_resolution_source`
  - `updated_at`
- do not write `continuity_token` or `continuity_token_supported` to the cache

When reporting back:

- say whether the identity was freshly created or created with lineage
- if lineage was declared, name the parent UUID prefix
- show the resolved display name or agent id
- note whether continuity is strong or weak
- mention the next useful command:
  - `/checkin` for the turn baseline, then after meaningful milestones
  - `/diagnose` if continuity still looks wrong

Do not dump raw JSON unless the user asks for it.
Prefer a short interpreted summary.
