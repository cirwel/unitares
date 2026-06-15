---
description: "Manual UNITARES governance check-in for the current turn"
---

Before calling tools, check the local workspace cache inventory.

Use the shared helper in this repo:

- `scripts/client/session_cache.py list`

If the newest entry contains `parent_agent_id`, treat it as local lineage context for attribution, not proof that the current process owns that UUID.

If current binding is unclear, call `identity()` first to inspect the active binding.

If you must rebind to a cached UUID, include a matching current in-process `continuity_token`: `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)`. Do not use legacy cache files as token sources.

If this is a fresh process and no ownership proof is available, use `/governance-start` to mint a fresh identity with `parent_agent_id=<cached uuid>` rather than bare UUID resume.

If no local continuity state exists and the current identity is unclear, use `/governance-start` first.

Call `process_agent_update` for the current agent once per assistant turn to establish a behavioral baseline. Also call it after meaningful milestones, before/after high-risk work, or when uncertainty/drift shows up.

Inputs:

- `response_text`: concise summary of what was actually accomplished
- `complexity`: estimate `0.0-1.0`
- `confidence`: honest estimate `0.0-1.0`
- use the active session binding or `client_session_id`; do not auto-inject `continuity_token` into `process_agent_update`
- use `response_mode="mirror"` by default for Codex

Guidelines:

- Do not check in after every trivial edit or tool call.
- Prefer one baseline check-in per assistant turn.
- Add a check-in for meaningful milestones, completed steps, or decision points.
- If you had to rebind with `identity()`, only use that restored binding when the response shows strong/proof-owned continuity.
- If recent local edit context exists, use it to improve the summary, but do not report raw file churn as if it were real progress.
- If deterministic results already happened in the workflow, mention them concretely instead of speaking in generalities.

After the call:

- report the verdict
- report identity-assurance or continuity warnings when they are surfaced
- report margin or edge warnings when present
- report any guidance briefly
- report the mirror question when present
- if verdict is `pause` or `reject`, recommend `request_dialectic_review`
- if verdict is `guide`, summarize the guidance and adjust behavior
