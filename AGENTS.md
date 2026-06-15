# AGENTS.md — unitares

Bootstrap for Codex (and other non-Claude) sessions in this repo. This file is the **machine-facing Codex bootstrap**. Content below the `SHARED CONTRACT` markers is kept byte-identical with `CLAUDE.md` — CI (`scripts/dev/check-shared-contract.sh`) enforces parity. Edit shared rules in **both** files; edit the Codex preamble here only.

For the human-facing Codex quickstart, see `CODEX_START.md`.

The installable Codex/Claude adapter bundle is canonical in the companion `unitares-governance-plugin` repo. This file only governs how Codex should behave while working directly inside the `unitares` server repo.

## Codex-specific wiring

Codex has no hook system analogous to Claude's. **Nothing is automatic.** You decide when to check in, diagnose, and surface Watcher findings. The expected Codex baseline is one real check-in per assistant turn, with extra check-ins around high-risk or long-running work. Do not confuse that with per-tool or per-edit check-ins; those are usually noise.

### Slash commands (`commands/*.md`)

- `/governance-start` — onboard or resume; refreshes local continuity state
- `/checkin` — governance update for the current turn, plus meaningful milestones
- `/diagnose` — identity, state, and operator diagnostics
- `/dialectic` — structured review
- `/closeout` — final workspace hygiene check; reports dirty files, Git delivery state (local vs pushed/merged), and repo-rooted processes; can stash/stop when cleanup is requested

Raw tool flow when slash commands are unavailable: `start_session(force_new=true, parent_agent_id=<prior uuid if continuing>, spawn_reason="new_session")` → save `agent_uuid` + `client_session_id` → `sync_state(response_text, complexity, client_session_id=...)` once per assistant turn and at meaningful milestones → `check_working_state()` for read-only checks → `health_check()` only if system health is suspect. Canonical/raw equivalents are `onboard(...)`, `process_agent_update(...)`, and `get_governance_metrics(...)`.

### Local continuity cache

`.unitares/session.json` is Codex's authoritative local workspace state (not Claude's memory system). It holds `uuid`, `client_session_id`, `session_resolution_source`, and optional short-lived proof material for in-process calls. Helper: `scripts/client/session_cache.py`. On every new session or after a restart, call `onboard(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`.

If `session_resolution_source` falls back to a weak source, rerun `/governance-start` or diagnose explicitly; do not repair it with bare UUID resume.

### Watcher visibility is manual

There is no `PostToolUse` hook to surface findings. To see and close them:

```bash
python3 agents/watcher/agent.py --list-findings --only-open   # list open/surfaced findings
python3 agents/watcher/agent.py --print-unresolved            # print unresolved block without mutating
python3 agents/watcher/agent.py --surface-pending             # print + transition open→surfaced
python3 agents/watcher/agent.py --resolve <fingerprint> --agent-id <your-uuid>
python3 agents/watcher/agent.py --dismiss <fingerprint> --agent-id <your-uuid>
```

Use `--agent-id` when resolving or dismissing so the audit trail stays attributed. `data/watcher/` is gitignored, so commit messages are still useful context when you close a finding.

### What Codex should NOT reference

- `.claude/CLAUDE.md` — Claude-only machine-local overlay.
- `~/.claude/projects/.../memory/MEMORY.md` — Claude's memory system; Codex uses `.unitares/session.json` instead.

### Delivery state is part of closeout

Codex must not leave the operator to infer GitHub state from "tests passed".
Before a final response after edits, run `/closeout` or
`python3 scripts/dev/workspace_closeout.py` and report the delivery line:

- `local_changes` means not committed, not pushed, not merged.
- `unpushed_commits` means committed locally but not pushed.
- `pushed_branch` means pushed, but PR/merge state is not proven by local git.
- `synced_default` means the current checkout is clean and synced with the
  default upstream.

If the operator asks whether something is merged, answer from this delivery
state plus any explicit GitHub check you performed. Do not imply local edits
are merged.

<!-- BEGIN SHARED CONTRACT — keep byte-identical across AGENTS.md and CLAUDE.md; scripts/dev/check-shared-contract.sh enforces parity -->

## Project

UNITARES governance MCP server. Information-theoretic governance framework for AI agents (EISV state vectors, coherence tracking, dialectic resolution, knowledge graph).

## Stack

- Python 3.12+, asyncio
- PostgreSQL@17 + AGE 1.7.0 (Apache Graph Extension) via Homebrew
- Redis (optional session cache)
- Pydantic v2 for parameter validation
- MCP (Model Context Protocol) server

## Setup

1. Install PostgreSQL@17 with AGE extension
2. Create a `governance` database
3. Install dependencies: `pip install -e .`
4. Copy `scripts/ops/com.unitares.governance-mcp.plist` to `~/Library/LaunchAgents/` and fill in paths/tokens (see template comments)
5. Start: `python src/mcp_server.py --port 8767`

## Before Starting Work on a Single-Writer Surface

A single-writer surface is one where only one branch can land at a time without conflict — slot collisions, semantic merge conflicts, or strategy-divergent fixes that obsolete each other. Parallel sessions converging on these produces collisions; the 2026-04-29 migration-drift incident (#236 + #237) is the canonical example.

Before touching one of these, run `gh pr list -R CIRWEL/unitares --search "in:title,body <surface-keyword>" --state open` and the same against `CIRWEL/unitares-governance-plugin` for cross-repo surfaces. If an in-flight PR exists, comment there or branch from its head — do not start a parallel attempt.

Surfaces:

- **Migration slots and migration-drift fixes** — `db/postgres/migrations/`. Now CI-gated by `scripts/dev/unitares_doctor.py`; the doctor fails on slot/name drift, but session-level coordination still avoids wasted parallel work.
- **Identity / onboarding — docs AND implementing code are one coupled surface** — docs (`docs/ontology/identity.md`, `commands/governance-start.md`, `skills/governance-lifecycle/SKILL.md`, the `AGENTS.md`/`CLAUDE.md` shared contract including the `Identity rules:` block below, the `force_new=true` / `parent_agent_id` posture) AND code (`src/mcp_handlers/identity/`, `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/schemas/identity.py`). Treat as a single writer-locked region, not as separate doc/code workstreams. These also flow across two repos (unitares + gov-plugin); check both.
- **`docs/ontology/plan.md`** — chronological state ledger; two sessions appending rows in the same window collide trivially. If a session is already editing it, branch from its head rather than starting parallel.
- **Active proposal/RFC docs in hot phase** — the Plexus / lease-plane / BEAM thread (`docs/proposals/plexus-scope.md`, `surface-lease-plane-v0.md`, `surface-lease-plane-phase-a-plan.md`, `beam-footprint-roadmap-v0.md`, `beam-coordination-kernel.md`). Restructure-during-flight is normal here; same rule as plan.md: branch from another session's head if one is in flight.
- **Large test-layout consolidation** — `tests/` directory. If you're about to delete more than ~200 lines of tests, surface intent in a draft PR or issue first; a stale −3496 diff (`feat/agentskills-compat`) was lost to drift this way.

This section is operator-protective, not session-protective. The deeper fix is on the dispatcher's side: do not launch multiple sessions on the same single-writer surface in the same window.

## Before Committing

- **Run `./scripts/dev/test-cache.sh` before the first commit in a local change sequence** (tree-hash cache — skips if tests already passed against this exact test input tree; use `--fresh` to force a re-run)
- Use `./scripts/dev/test-cache.sh --staged` when validating a staged subset; it hashes the staged commit candidate and refuses to run if unstaged/untracked files would affect pytest
- After a branch is pushed and GitHub CI is running the full gate, do not restart local full `test-cache` runs for every fixup; run focused local tests for the touched behavior, push, and let CI be the final full gate
- **If you edit `AGENTS.md` or `CLAUDE.md`**, also run `./scripts/dev/check-shared-contract.sh` to confirm the shared block stayed in sync
- Fix any test failures your changes introduce — do not commit broken tests
- If you change a function's behavior or signature, update its tests in the same commit
- If you do a mechanical refactor (renames, import changes), update affected test mocks before committing
- The pre-push hook will block pushes with test failures

## Architecture Patterns

- **governance_core lives in this repo** at top-level `governance_core/` (pure Python). Code in `src/` imports it as `from governance_core import X`.
- **LazyMCPServer**: All handler modules import `lazy_mcp_server as mcp_server` from `shared.py` (single definition, no per-file copies). Tests patch `{MODULE}.mcp_server` not `get_mcp_server`.
- **Pydantic validation**: Parameter validation uses Pydantic schemas in `src/mcp_handlers/schemas/`. Legacy `validate_and_coerce_params` is removed.
- **Handler modules**: Each in `src/mcp_handlers/`, decorated with `@mcp_tool`.

## Database

- PostgreSQL@17 on port 5432 with AGE graph extension
- Requires `brew services start postgresql@17`
- Check connectivity: `pg_isready -h localhost -p 5432`
- Do NOT create additional PostgreSQL instances, databases, or migration layers

## Git Rules

- Do not force-push
- Do not run destructive git commands without explicit user approval
- Do not run DROP/TRUNCATE/DELETE on the governance database without explicit user approval
- Do not include Co-Authored-By lines in commit messages

## GitHub Workflow Conventions

Codex and Claude share one delivery contract so concurrent sessions stay predictable. Full reference: `docs/operations/github-workflow-conventions.md`.

- **Branch naming — one pattern, agent-prefixed:** `<agent>/<topic>-<short-id>` where `<agent>` is `claude` or `codex` (self-identifying for parallel attribution). Both `ship.sh`'s `<agent>/auto/<timestamp>-<slug>` and the web harness's `claude/<topic>-<id>` satisfy this shape. Never push to `main`/`master`.
- **Delivery — draft PR for everything:** every session lands its work as a draft PR, regardless of agent and regardless of whether the change is runtime code or docs/tests. The operator is the merge gate. Do NOT direct-push to a shared branch and do NOT enable auto-merge by default. `ship.sh` enforces this: its default `auto` route opens a draft PR for every change (`--direct` opts out for docs/tests-only pushes; `--auto-merge` only when the operator explicitly asks).
- **Mark-ready / merge is a deliberate action:** a draft PR means "visible, not claiming merged." Only mark ready or merge after CI is green and you've confirmed no collision with an in-flight branch (see the single-writer-surface rules above).

## Substrate Tax: anyio-asyncio Coupling

The MCP SDK runs handlers inside an anyio task group. asyncpg and Redis run on Python's asyncio. When a handler `await`s DB/Redis work, the two scheduler models can interact in ways that hold connections across unrelated awaits and amplify latency by orders of magnitude. Measured 2026-05-04 on the governance-MCP request path: KG calls that complete in 21–71ms standalone run at **~4,464ms in-handler** — a ~60× amplification, with the floor sub-100ms and the rest in scheduling / pool-acquisition / event-loop contention. The Sentinel-loop call site (`agents/sentinel/agent.py:416-459`) is mitigated to ">400 cycles, zero failures" via PR #290, but that fix is one workaround at one site, not closure of the bug class.

**These are workarounds, not architecture.** The patterns below accreted from incidents — three over the last year, with new variants emerging on different surfaces (current example: the load_metadata_async N-await loop on observe handlers, see PR #348 follow-up). The bug class is structural to anyio + asyncio + asyncpg / Redis on a shared event loop and does not exist on substrates with per-process scheduling and protocol-level connection checkout (e.g., BEAM / db_connection).

**As of PR #218 (deployed 2026-04-27), `get_db()` returns an `ExecutorPool`-wrapped backend** (`src/db/executor_pool.py`). asyncpg operations run on a dedicated background thread with its own event loop, so the anyio task group never sees an asyncpg await. New handlers can use `async with db.acquire() as conn: await conn.fetchval(...)` directly — no wrapper needed for asyncpg DB work. **Redis async clients are not yet wrapped by ExecutorPool.** Existing Redis `asyncio.wait_for` timeouts in `identity_step.py`, `persistence.py`, and `session.py` remain as a precaution; do not add new ones for asyncpg but leave Redis guards in place.

The three patterns below were the pre-ExecutorPool workarounds. They are **retired for new asyncpg handlers** but remain in the codebase as historical context and where they serve purposes beyond anyio isolation (Redis guards, sync blocking I/O, performance caches):

1. **Read cached data** populated by a background task (e.g., `health_check` reads `deep_health_probe_task`'s snapshot; sticky identity reads a cache pre-warmed by `transport_binding_cache_warmup`).
2. **`run_in_executor` with a sync client** — see `verify_agent_ownership` dispatch at `src/agent_loop_detection.py:403` (synchronous DB-touching function pushed to an executor thread so the anyio task group stays unblocked). The same pattern is used externally by `call_pi_tool` in the `unitares_pi_plugin` package.
3. **`asyncio.wait_for` with a tight timeout** — degrade to a fallback on deadlock instead of hanging the pipeline. See `deep_health_probe_task` at `src/background_tasks.py:545` and `_load_binding_from_redis` at `src/mcp_handlers/middleware/identity_step.py` (500ms budget, returns `None` on timeout).

## Known Test Notes

- Knowledge graph AGE tests require a live AGE connection (errors, not failures, when unavailable)

## STRICT_IDENTITY_REQUIRED (#425 staged rollout)

`STRICT_IDENTITY_REQUIRED=true` flips the dispatch middleware from auto-minting an ephemeral identity for non-`pre_onboard` tools to returning a typed-refusal response (`status: identity_required`). Default is `false` so existing callers (residents, dispatch workers, plugins doing bare `onboard()`) keep working. Per-tool identity requirements are declared via `requires_identity=` on the `@mcp_tool` decorator (see `src/mcp_handlers/decorators.py`).

Rollout sequence:

1. Local dev (your shell) → set the flag, run a session through, watch for typed refusals where you expected work. **Stage 1 run 2026-06-11**: typed refusal works as designed on the MCP transport — but **the flag does not reach the REST surface** (`/v1/tools/call` skips `identity_step`): unbound REST reads still succeed under strict, writes refuse with an off-contract generic `SESSION_ERROR`. Since the real unbound population is REST (dashboard 30s sweep, gateway-proxied connector traffic), a REST-side strict gate + a dashboard-identity decision are prerequisites before stages 2–4 achieve the rollout's goal. **REST gate shipped (inert while the flag is off):** `execute_http_tool` now mirrors the middleware's typed refusal via the single-sourced `identity_bootstrap.strict_identity_refusal_payload`, ahead of Wave-3a routing; credential-bearing and pre_onboard calls pass through. The gate keys on the RESOLVED context binding, never credential presence: the REST route transport-injects a synthetic `client_session_id` into every request, so presence-based bypasses pass all real traffic (council live finding, PR #610). **Action-level classification shipped:** both gates resolve at CALL granularity (`decorators.get_call_identity_requirement` — alias-aware, default_action-aware); the mixed tools declare `pre_onboard_actions` (knowledge: search/get/list/details/stats; agent: list/get; calibration: check; config: get; observe: agent/compare/similar/anomalies/aggregate; dialectic: get/list) so the dashboard's read sweep passes under strict while every write/operator action refuses — a drift-guard test pins that no mutating action can enter an exemption set. Remaining prerequisite: **the dashboard-identity decision (operator)** — under strict its reads now work unbound but its operator WRITE buttons (archive/resume/config-set/dialectic-request) refuse until they carry an operator credential.
2. Lumen (Pi) → flag the Pi env, observe resident agents (vigil/sentinel/watcher/chronicler) for refusals at scheduled boundaries; fix offenders by adding `parent_agent_id` to their bootstrap.
3. Dispatch (the Discord bridge / dispatch worker) → flag, observe per-thread agent spawns.
4. Flip the default in code only after all three burn-ins are clean for ≥1 week.

When investigating a typed-refusal, the response carries `tool`, `hint`, `ontology_ref`, and `rollout_flag` so the cause is structurally identifiable.

## Minimal Agent Workflow

Per identity.md v2 ontology, fresh process-instances mint fresh identity. Lineage is causal — declared only for a genuine spawn or handoff event, never to claim continuity with a co-located predecessor.

Default happy path:

1. `onboard(force_new=true)` with NO `parent_agent_id` → save `agent_uuid` and `client_session_id` from response. A fresh session onboards fresh; co-location in this workspace is not lineage.
2. `process_agent_update(response_text=..., complexity=..., client_session_id=...)` for in-process check-ins
3. On a future process-instance, repeat step 1 — onboard fresh, do not auto-resume and do not declare the prior session as a parent.

Declare lineage (`parent_agent_id`) ONLY for a real causal event:

- **Dispatched subagents** — `parent_agent_id=<dispatcher UUID>, spawn_reason="subagent"`. Usually set automatically by the dispatcher.
- **A deliberate handoff from an EXITED prior session** — `spawn_reason="explicit"`. The successor inherits a predecessor that has finished, not a sibling still running.

Declaring a currently-LIVE agent as `parent_agent_id` is rejected server-side (`lineage_coincidental_rejected`): a live agent is a concurrent sibling, not a predecessor. `subagent` and `compaction` are exempt — their parent is legitimately live. A genuine serial handoff to an exited predecessor is accepted and stays provisional until R1 confirms it.

Friendly workflow aliases: the same happy path can be spelled with task-verb names — `start_session` → `onboard`, `sync_state` → `process_agent_update`, `check_working_state` → `get_governance_metrics`, `search_shared_memory` → `knowledge(action="search")`, `record_result` → `outcome_event`, `request_review` → `dialectic(action="request")`. Same parameters, same identity rules (the registry canonicalizes before the #425 gates judge). Calls made via these names return the normalized agent-experience envelope — `next_action` / `state_summary` / `risk_summary` / `memory_suggestions` / `recovery_hint` first, full canonical payload under `raw_governance` — while canonical names keep the raw response shape byte-identical.

For an explicit handoff (rare — only when a prior session has EXITED):

- **Plugin-loaded sessions** are the canonical path. The `unitares-governance-plugin` SessionStart banner surfaces the cached workspace UUID; Codex sessions get the same hint via `commands/governance-start.md` (S11-a). Treat that UUID as a handoff candidate only if that session has finished — if it is still running, it is a sibling and the server will reject it (`lineage_coincidental_rejected`). The default fresh onboard needs none of this.
- **Server-only sessions** (working directly inside this repo without the plugin's hook chain) have no pre-onboard discovery surface. Onboarding without `parent_agent_id` mints honestly as `lineage_state: no_lineage_declared` — this is the expected default, not a degraded state. The onboard response returns `thread_context.predecessor.uuid` when a session-resolved predecessor exists; that is context, not a lineage instruction — do not declare it as a parent unless it represents a real handoff from an exited session.

Identity rules:

- A fresh process-instance is a fresh agent. Process-instance boundaries are honored.
- `client_session_id` maintains identity within one process; weak across processes.
- `continuity_token` is being narrowed (S1 in `docs/ontology/plan.md`) — present-day external clients can still resume with it, but plugin-internal flows declare lineage instead.
- Substrate-anchored agents (Lumen, the long-lived residents) earn cross-process continuity via the substrate-earned identity pattern in `docs/ontology/identity.md` — they may use a hardcoded UUID across restarts.
- Arg-less `onboard()` with no proof signal triggers the v2 fresh-instance gate — the server flips `force_new=true` and emits a `[FRESH_INSTANCE]` log line (S13).
- Lineage is causal and declared at onboard time. The default fresh session declares no parent. Declare `parent_agent_id` only for a real spawn (`spawn_reason="subagent"`) or a handoff from an exited session (`spawn_reason="explicit"`); declaring a live agent as parent is rejected (`lineage_coincidental_rejected`).

<!-- END SHARED CONTRACT -->
