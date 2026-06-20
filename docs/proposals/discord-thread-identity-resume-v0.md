# Discord BEAM thread identity — resume-per-thread (v0)

**Created:** June 18, 2026
**Status:** Orchestrator + reference-hook side merged (#834). The **fail-closed
guard** (anchor honored only with an explicit orchestration marker) was mirrored
into the gov-plugin hook, and dispatch_beam now provisions the marker alongside
the anchor. Strict-mode rollout added one narrow server-side first-bind carve-out:
`orchestrated=true` plus an `agent:/thread-*` anchor may mint and persist the
anchor binding after a resume miss, while a bare anchor still refuses under
`STRICT_IDENTITY_REQUIRED`.
**Operator decision (2026-06-18):** *one id per thread (resume)*, fix in *this
repo (orchestrator + server)*; *fail-closed, no council* (the guard re-applies the
ratified anti-siphon principle, it is not a new ontological claim).
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

## The mechanism

`handle_onboard_v2` defaults `resume=True` (`handlers.py:1670`). A stable
`client_session_id` resolves to the **same** governance UUID across processes:

1. **Turn 1** — onboard under anchor `agent:/thread-<id>`, `resume=True`,
   `orchestrated=true`, no binding yet → PATH 2 fail-closed
   `session_resolve_miss` → strict Path B recognizes the orchestrated thread
   anchor declaration → falls through to the create-finisher → mints +
   **binds** the session under the normalized anchor key (`agent:_thread-...`).
2. **Turn 2..N** — same anchor, `resume=True` → PATH 1/2 hit → returns the turn-1
   UUID. One id for the whole thread.

The strict-mode server carve-out is intentionally narrow: without
`orchestrated=true`, the same resume miss still returns
`lineage_declaration_required` instead of minting.

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
   `UNITARES_CLIENT_SESSION_ID` is set **and** the orchestration signal is present
   (see fail-closed guard below), the helper resumes under the anchor (sends
   `client_session_id`, **omits** `force_new`, declares **no** lineage) instead of
   the default fresh-mint posture. Additive and gated: with no anchor (or no
   orchestration signal), behavior is byte-identical to before. An explicit
   `force_new` still wins (a deliberate clean break).

## Fail-closed: why a bare anchor is not enough

**The same onboard hook is the onboarder for *normal interactive* sessions, not
just `claude -p` children.** So "resume whenever `UNITARES_CLIENT_SESSION_ID` is
set" is too blunt: if that var ever leaked into a normal session's environment (a
stray global `export`, an inherited shell), the session would silently flip into
resume mode — and two normal sessions that happened to share the leaked value would
resume onto **one** governance UUID. That is exactly the ghost-siphon the v2
ontology removed name-claim resolution to prevent (multiple subjects collapsing
onto one identity by a weak shared signal).

The guard: resume is honored only with an **explicit positive orchestration
signal** — `UNITARES_ORCHESTRATED=1`, set by the spawner *alongside* the anchor.
The orchestrator/dispatch KNOWS its children are orchestrated headless turn-children,
so it declares it; an interactive session never carries the marker. Absent the
marker, the anchor is ignored and the hook mints exactly as today. So a leaked
anchor on an interactive session is a **no-op, never a siphon** — the dangerous
direction is structurally impossible, which is why this needs no council (it
re-applies a ratified principle, fail-closed).

- Orchestrator: `agent_runner.ex` provisions `UNITARES_ORCHESTRATED=1` *with* the
  anchor (one unit — both or neither).
- Reference hook: `run_onboard(orchestrated=...)`; the anchor wins only when
  `orchestrated` is true. The CLI derives it from `--orchestrated` /
  `UNITARES_ORCHESTRATED` (affirmatives only — `_env_truthy`).
- Defense-in-depth for the real plugin: it should set `orchestrated=True` only for
  **headless `-p`** children (its own structural discriminator from the hook input),
  so interactive sessions are immune even if the marker leaked too.

## Remaining cross-repo follow-ups (not in this PR)

The common contract is: **the `claude -p` child receives BOTH
`UNITARES_CLIENT_SESSION_ID` (the per-conversation anchor) AND
`UNITARES_ORCHESTRATED=1` (the fail-closed marker) in its env.** How they *reach*
the child depends on the spawn path:

- **Dispatch / Discord-bridge worker** (separate repo) — two spawn paths, same
  env-var destination:
  - *HTTP-API spawners* (drive the orchestrator via `POST /v1/agents`): pass the
    `client_session_id` field; the orchestrator provisions both env vars.
  - *Direct-Port spawners* (e.g. `dispatch_beam`'s `Dispatch.AgentPort`, which
    opens a BEAM `Port` to `claude -p` and does **not** call `/v1/agents`): set both
    on the Port spawn —
    `env: [{"UNITARES_CLIENT_SESSION_ID", "agent:/thread-#{thread_id}"}, {"UNITARES_ORCHESTRATED", "1"}]`.
    `AgentPort` already threads `env:` into `port_opts` and it composes with the
    stdin-redirect wrapper, so the child's session-start hook inherits them. Same
    no-forging boundary: anchor only, the child still self-onboards.
- **gov-plugin session-start hook** (`unitares-governance-plugin`): mirror the
  `onboard_helper.py` contract — resume under the anchor only when the orchestration
  signal is present (and, defense-in-depth, the session is headless). The plugin
  hook is the actual onboarder for `claude -p` children; `onboard_helper.py` is the
  in-repo reference implementation.

**Sequencing:** the env vars are a no-op until the gov-plugin hook reads them, so:
gov-plugin hook mirrors the helper → the worker (HTTP or direct-Port) injects the
anchor + marker.

## Honesty boundary

This confers a **resumed process-instance identity within one conversation**, keyed
on a caller-supplied anchor — *not* a cross-process *strong* credential. Resume by
`client_session_id` stays weak/medium under the #425/#810 taxonomy (the anchor is a
copyable string). Earning a genuine `strong` tier for orchestrated children is the
separate, deferred `orchestrator-vouched-identity-v0.md` track. Resume-per-thread
is the right tool for "one id per Discord conversation"; it does not and should not
claim attestation.
