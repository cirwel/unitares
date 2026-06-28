# CLAUDE.md — unitares

Bootstrap for Claude Code sessions in this repo. Content below the `SHARED CONTRACT` markers is kept byte-identical with `AGENTS.md` — CI (`scripts/dev/check-shared-contract.sh`) enforces parity. Edit shared rules in **both** files; edit the Claude preamble here only.

The installable Codex/Claude adapter bundle is canonical in the companion `unitares-governance-plugin` repo. This file only governs how Claude Code should behave while working directly inside the `unitares` server repo.

## Execution-cost policy (read first)

- **No metered model-API dependencies.** Do not propose, scaffold, or depend on solutions that require an `ANTHROPIC_API_KEY` or any other paid model API — this rules out `anthropics/claude-code-action` and any CI/automation that calls a metered API. This project targets free / self-hosted execution paths: the local Ollama detector for Watcher, `GITHUB_TOKEN`-only CI, and deterministic CLI tools (ruff, the doctor, the surfacing collectors). If a feature genuinely needs a paid API, surface it as a *deferred, opt-in* option and do not build it inert "just in case."

## Claude-specific wiring

Claude Code runs through a plugin-style harness. The hook lifecycle is owned by the **`unitares-governance-plugin`** repo (canonical for the adapter bundle). This repo does not vendor or wire its own hook chain. The plugin's `hooks/session-start` and `hooks/post-edit` are what fire on Claude lifecycle events — when CLAUDE.md or AGENTS.md describes hook behavior, the source of truth is the plugin.

User-level `~/.claude/hooks/` adds a third layer (auto-test, watcher-hook, watcher-chime, stop-checkin, etc.) wired directly in `~/.claude/settings.json`. Watcher findings that appear at session start or as a chime block originate from that user-level chain, not from the plugin's `post-edit`.

To close a Watcher finding (the agent itself lives in this repo):

```bash
python3 agents/watcher/agent.py --resolve <fingerprint>   # confirmed bug, fixed
python3 agents/watcher/agent.py --dismiss <fingerprint>   # false positive
```

Reference fingerprints in the commit message — Watcher's audit trail lives in commits, not in tracked files (`data/watcher/` is gitignored).

### Session-end auto-stash

`scripts/dev/session_end_stash.py` is a standalone utility that captures uncommitted work into a branch-labeled `git stash` so intent survives session boundaries. It is **not** auto-fired from this repo (no hook chain wires it). Run manually, or wire from `~/.claude/hooks/` if desired:

```bash
python3 scripts/dev/session_end_stash.py
```

### Machine-local overlay

`.claude/CLAUDE.md` is gitignored and layers on deployment-specific details (bind address, LaunchAgent paths, `governance_core` source symlink). Read both files; the overlay wins on conflicts.

### What Claude should NOT reference

- `commands/*.md` — those are **Codex** slash commands, not Claude commands.
- `.unitares/session.json` — that's the Codex continuity cache. Claude's continuity comes from the hook chain + `~/.claude/projects/.../memory/MEMORY.md`.

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
- **Identity / onboarding — docs AND implementing code are one coupled surface** — docs (`docs/ontology/identity.md`, `commands/governance-start.md`, `skills/governance-lifecycle/SKILL.md`, the `AGENTS.md`/`CLAUDE.md` shared contract including the `Strict Identity, Simple Contract` block below, the `force_new=true` / `parent_agent_id` posture) AND code (`src/mcp_handlers/identity/`, `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/schemas/identity.py`). Treat as a single writer-locked region, not as separate doc/code workstreams. These also flow across two repos (unitares + gov-plugin); check both.
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

## GitHub Workflow Conventions

Codex and Claude share one delivery contract so concurrent sessions stay predictable. Full reference: `docs/operations/github-workflow-conventions.md`.

- **Branch naming — one pattern, agent-prefixed:** `<agent>/<topic>-<short-id>` where `<agent>` is `claude` or `codex` (self-identifying for parallel attribution). Both `ship.sh`'s `<agent>/auto/<timestamp>-<slug>` and the web harness's `claude/<topic>-<id>` satisfy this shape. Never push to `main`/`master`.
- **Delivery — draft PR for everything:** every session lands its work as a draft PR, regardless of agent and regardless of whether the change is runtime code or docs/tests. The operator is the merge gate. Do NOT direct-push to a shared branch and do NOT enable auto-merge by default. `ship.sh` enforces this: its default `auto` route opens a draft PR for every change (`--direct` opts out for docs/tests-only pushes; `--auto-merge` only when the operator explicitly asks).
- **Delivery requests authorize delivery:** when the operator asks to ship, finish, deliver, open a PR, or otherwise complete a delivery workflow, Codex may assume branch -> commit -> push -> draft PR is in scope and should not ask for a second confirmation just to push or open the draft PR.
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

## Strict Identity, Simple Contract

Strict identity is a write gate. Reads may work without a bound caller; writes
must be accountable. Agents should not need the full identity ontology for the
normal path.

Operational rules:

1. Start each driver with `start_session(force_new=true)` (`onboard` is the
   canonical tool underneath). Save the returned `uuid` and `client_session_id`.
2. For later check-ins and writes in the same running process, pass
   `client_session_id`. Adapters should do this automatically.
3. To continue prior work in a fresh process, mint fresh and declare the cause:
   `start_session(force_new=true, parent_agent_id=<prior_uuid>, spawn_reason="new_session")`.
   Use this only for a real handoff from a finished predecessor.
4. Short dispatched subagents usually should not onboard. If one needs its own
   identity, use `spawn_reason="subagent"`, set `parent_agent_id=<driver_uuid>`,
   and land at least one real `sync_state()` before exit.
5. Persistent/substrate agents use their dedicated substrate identity pattern.
   Ordinary sessions should not copy that pattern.

Do not do these in normal agent code:

- Bare `onboard()` or `identity()` as a way to guess identity.
- Passing `continuity_token` on every call.
- Treating a display name as identity.
- Declaring `parent_agent_id` just because another session shares the workspace.
- Writing KG notes before searching for an existing entry.

Minimal glossary:

- `uuid`: the server record for this process identity.
- `client_session_id`: the proof string for this running process. Use it on
  writes; do not treat it as cross-process selfhood.
- `parent_agent_id`: a causal pointer to the process whose work this process is
  inheriting.
- `lineage`: "this process inherited work from that one," not "this process is
  that one."
- `continuity_token`: advanced same-live-process rebind proof. Not part of the
  normal workflow.

Friendly workflow aliases: `start_session` -> `onboard`, `sync_state` ->
`process_agent_update`, `check_working_state` -> `get_governance_metrics`,
`search_shared_memory` -> `knowledge(action="search")`, `record_result` ->
`outcome_event`, and `request_review` -> `dialectic(action="request")`.

Shared-memory (KG) write discipline: search before writing. If a related entry
exists, prefer a linked correction or `supersede` over a fresh note. Store when
a future agent would search for this and not already find it: a correction to a
prior conclusion, a non-obvious failure mode plus its fingerprint, or a closed
mystery. Operational runbooks and step lists belong in `docs/`, not KG notes.

<!-- END SHARED CONTRACT -->
