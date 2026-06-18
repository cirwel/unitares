# Discord BEAM thread identity — resume-per-thread (v0)

**Created:** June 18, 2026
**Status:** Orchestrator + reference-hook side SHIPPED in this PR; the gov-plugin
session-hook mirror and the dispatch-worker anchor are the remaining cross-repo
follow-ups (below). No new server behavior — the resume path already exists.
**Operator decision (2026-06-18):** *one id per thread (resume)*, fix in *this
repo (orchestrator + server)*.
**Touches:** identity/onboarding (writer-locked surface — see CLAUDE.md). Operator
is the merge gate.

## The symptom

The Discord "BEAM Claude" runs as an ephemeral agent spawned **per turn** through
`AgentOrchestrator.AgentRunner` — one OS process per turn, wrapping a `Port` to a
`claude -p` runtime. Each fresh process opens a **new** governance MCP session, so
`derive_session_key` falls to the new per-connection MCP session id, and the
session-start onboard mints a **fresh UUID every turn**. Consecutive turns of one
Discord conversation were therefore disconnected identities, not one agent.

## Why this is not just "fresh process = fresh agent"

Per `identity.md` v2, a fresh process-instance *is* a fresh agent — that is correct
for genuinely independent sessions. But the turns of one Discord thread are **one
logical conversation**: the thread is the identity anchor (`thread_identity.py`:
"A thread is the user's conversation — the true identity anchor"). The fix is to
give those turns a **stable session anchor** so they resume one identity, rather
than mint per turn. This is *resume*, not lineage — the turns are the same agent,
not a parent/child chain.

## The mechanism (already supported server-side)

`handle_onboard_v2` defaults `resume=True` (`handlers.py:1670`). A stable
`client_session_id` resolves to the **same** governance UUID across processes:

1. **Turn 1** — onboard under anchor `agent:/thread-<id>`, `resume=True`, no
   binding yet → PATH 2 fail-closed `session_resolve_miss` → falls through to the
   create-finisher → mints + **binds** the session under the anchor key.
2. **Turn 2..N** — same anchor, `resume=True` → PATH 1/2 hit → returns the turn-1
   UUID. One id for the whole thread.

So no server change was needed. The two gaps were purely *plumbing*: nothing
provisioned the anchor, and the reference onboard hook hard-coded `force_new=true`.

## What shipped here

1. **Orchestrator (`agent_runner.ex` + `http_router.ex`).** New top-level spec key
   `client_session_id:` provisions `UNITARES_CLIENT_SESSION_ID` into the child env,
   under the same explicit-wins + validation discipline as `server_url` /
   lineage. A blank or non-string anchor refuses the spawn
   (`{:invalid_client_session_id, _}` → HTTP 422) — a blank anchor would silently
   degrade back to fresh-mint-per-turn, the exact symptom this prevents. The
   `POST /v1/agents` body accepts `client_session_id`. Tests mirror the
   `server-url provisioning` block.

2. **Reference onboard hook (`scripts/client/onboard_helper.py`).** When
   `UNITARES_CLIENT_SESSION_ID` is set, the helper resumes under the anchor
   (sends `client_session_id`, **omits** `force_new`, declares **no** lineage)
   instead of the default fresh-mint posture. Additive and gated: when the env is
   unset, behavior is byte-identical to before. An explicit `force_new` still wins
   (a deliberate clean break).

## Remaining cross-repo follow-ups (not in this PR)

The common contract is: **the `claude -p` child receives `UNITARES_CLIENT_SESSION_ID`
in its env, stable per conversation** (e.g. `agent:/thread-<discord-thread-id>`).
How the anchor *reaches* the child env depends on the spawn path:

- **Dispatch / Discord-bridge worker** (separate repo) — two spawn paths, same
  env-var destination:
  - *HTTP-API spawners* (drive the orchestrator via `POST /v1/agents`): pass the
    `client_session_id` field; the orchestrator provisions `UNITARES_CLIENT_SESSION_ID`
    (this PR).
  - *Direct-Port spawners* (e.g. `dispatch_beam`'s `Dispatch.AgentPort`, which
    opens a BEAM `Port` to `claude -p` and does **not** call `/v1/agents`): set the
    env var directly on the Port spawn —
    `env: [{"UNITARES_CLIENT_SESSION_ID", "agent:/thread-#{thread_id}"}]`. `AgentPort`
    already threads `env:` into `port_opts` and it composes with the stdin-redirect
    wrapper, so the child's session-start hook inherits it. Same no-forging boundary
    as the orchestrator path: anchor only, the child still self-onboards.
- **gov-plugin session-start hook** (`unitares-governance-plugin`): mirror the
  `onboard_helper.py` change — when `UNITARES_CLIENT_SESSION_ID` is present, onboard
  in resume-by-anchor mode rather than `force_new`. The plugin hook is the actual
  onboarder for `claude -p` children; `onboard_helper.py` is the in-repo reference
  implementation of the same contract.

**Sequencing:** the env var is a no-op until the gov-plugin hook reads it, so:
this PR merges → gov-plugin hook mirrors the helper → the worker (HTTP or
direct-Port) injects the anchor.

## Honesty boundary

This confers a **resumed process-instance identity within one conversation**, keyed
on a caller-supplied anchor — *not* a cross-process *strong* credential. Resume by
`client_session_id` stays weak/medium under the #425/#810 taxonomy (the anchor is a
copyable string). Earning a genuine `strong` tier for orchestrated children is the
separate, deferred `orchestrator-vouched-identity-v0.md` track. Resume-per-thread
is the right tool for "one id per Discord conversation"; it does not and should not
claim attestation.
