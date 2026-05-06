---
description: "Inspect and respond to an existing UNITARES dialectic session"
---

Start by checking the local workspace cache inventory.

Use the shared helper:

- `scripts/client/session_cache.py list`

If the newest entry contains `parent_agent_id`, use it as expected-actor lineage context, not as proof by itself.

If you need to bind to that exact cached UUID before responding, use `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)` only with a matching current in-process token. Without proof, call `/governance-start` and declare `parent_agent_id=<cached uuid>` if this process is continuing the same work.

Do not rely on weaker fingerprint/session fallback when dialectic authorization depends on the exact actor.

Then inspect the target session with:

- `dialectic(action='get', session_id='<session_id>')`

Use the returned `required_role`, `required_agent_id`, `current_agent_can_submit`, and `recommended_action` fields to decide the next step.

Workflow:

1. If phase is `thesis`: the paused agent should respond via `dialectic(action='thesis', ...)`.
2. If phase is `antithesis` and the current bound agent is the assigned reviewer: respond via `dialectic(action='antithesis', ...)`.
3. If phase is `antithesis` and the current bound agent should answer instead of the assigned reviewer: use `dialectic(action='antithesis', ..., take_over_if_requested=true, takeover_reason='...')`.
4. If phase is `synthesis`: respond via `dialectic(action='synthesis', ...)`.
5. If the session is already resolved/failed/escalated, report that instead of trying to write.

When reporting back:

- state the current phase
- state who is allowed to act next
- if you submitted a response, summarize the position you took
- if you had to take over reviewer ownership, say so explicitly
