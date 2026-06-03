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

If the newest entry contains `parent_agent_id`, treat it as a lineage candidate, not ownership proof. Ignore any legacy `continuity_token` field for startup; tokens are only for explicit same-live-owner PATH 0 proof rebinds.

Call `onboard()` against UNITARES using the strongest honest mode:

- if this is a fresh process with no prior UUID, pass `force_new=true`
- if this is a fresh process inheriting prior work, pass `force_new=true`, `parent_agent_id=<cached uuid>`, and `spawn_reason="new_session"`
- include `model_type` when the current runtime is clear from context
- do not invent a display name unless the user asked for one

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
  - `/checkin` after meaningful work
  - `/diagnose` if continuity still looks wrong

Do not dump raw JSON unless the user asks for it.
Prefer a short interpreted summary.
