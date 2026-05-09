# CLAUDE.md — unitares

Bootstrap for Claude Code sessions in this repo. Content below the `SHARED CONTRACT` markers is kept byte-identical with `AGENTS.md` — CI (`scripts/dev/check-shared-contract.sh`) enforces parity. Edit shared rules in **both** files; edit the Claude preamble here only.

The installable Codex/Claude adapter bundle is canonical in the companion `unitares-governance-plugin` repo. This file only governs how Claude Code should behave while working directly inside the `unitares` server repo.

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
- **Identity / onboarding — docs AND implementing code are one coupled surface** — docs (`docs/ontology/identity.md`, `commands/governance-start.md`, `skills/governance-lifecycle/SKILL.md`, the `AGENTS.md`/`CLAUDE.md` shared contract including the `Identity rules:` block below, the `force_new=true` / `parent_agent_id` posture) AND code (`src/mcp_handlers/identity/`, `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/schemas/identity.py`). The `wip-from-other-session-pre-merge` stash captured a 19-file rewrite spanning this whole region from one parallel session — treat as a single writer-locked region, not as separate doc/code workstreams. These also flow across two repos (unitares + gov-plugin); check both.
- **`docs/ontology/plan.md`** — chronological state ledger; two sessions appending rows in the same window collide trivially (72 file-touches in the trailing 21 days as of 2026-05-03, highest in the repo). If a session is already editing it, branch from its head rather than starting parallel.
- **Active proposal/RFC docs in hot phase** — `docs/proposals/{plexus-scope,surface-lease-plane-v0,surface-lease-plane-phase-a-plan}.md` and `docs/ontology/beam-coordination-kernel.md`. Restructure-during-flight is normal here; the lease-plane RFC accumulated 21 touches in 21 days, and plexus-scope.md itself originated from a scratch-planning name collision. Same rule as plan.md: branch from another session's head if one is in flight.
- **Large test-layout consolidation** — `tests/` directory. If you're about to delete more than ~200 lines of tests, surface intent in a draft PR or issue first; a stale −3496 diff (`feat/agentskills-compat`) was lost to drift this way.

This section is operator-protective, not session-protective. The deeper fix is on the dispatcher's side: do not launch multiple sessions on the same single-writer surface in the same window.

## Before Committing

- **Run `./scripts/dev/test-cache.sh` before the first commit in a local change sequence** (tree-hash cache — skips if tests already passed against this exact Python tree; use `--fresh` to force a re-run)
- Use `./scripts/dev/test-cache.sh --staged` when validating a staged subset; it hashes the staged Python commit candidate and refuses to run if unstaged/untracked Python files would affect pytest
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

## Substrate Tax: anyio-asyncio Coupling

The MCP SDK runs handlers inside an anyio task group. asyncpg and Redis run on Python's asyncio. When a handler `await`s DB/Redis work, the two scheduler models can interact in ways that hold connections across unrelated awaits and amplify latency by orders of magnitude. Measured 2026-05-04 on the governance-MCP request path: KG calls that complete in 21–71ms standalone run at **~4,464ms in-handler** — a ~60× amplification, with the floor sub-100ms and the rest in scheduling / pool-acquisition / event-loop contention. The Sentinel-loop call site (`agents/sentinel/agent.py:413-450`) is mitigated to ">400 cycles, zero failures" via PR #290, but that fix is one workaround at one site, not closure of the bug class.

**These are workarounds, not architecture.** The patterns below accreted from incidents — three over the last year, with new variants emerging on different surfaces (current example: the load_metadata_async N-await loop on observe handlers, see PR #348 follow-up). The bug class is structural to anyio + asyncio + asyncpg / Redis on a shared event loop and does not exist on substrates with per-process scheduling and protocol-level connection checkout (e.g., BEAM / db_connection). The honest open question — whether/how the governance-MCP surface gets ported — lives in `docs/proposals/beam-footprint-roadmap-v0.md` (read its 2026-05-04 amendment, not the original "bug class closed" framing).

When you write a new MCP handler that needs DB access, pick one of the three patterns below. **Do not treat their accumulation as progress.** Each new pattern is evidence the substrate tax is growing, which is data the open-question doc cares about — surface it there if a fourth shape emerges, don't just add it.

1. **Read cached data** populated by a background task (e.g., `health_check` reads `deep_health_probe_task`'s snapshot; sticky identity reads a cache pre-warmed by `transport_binding_cache_warmup`).
2. **`run_in_executor` with a sync client** — see `verify_agent_ownership` dispatch at `src/agent_loop_detection.py:374` (synchronous DB-touching function pushed to an executor thread so the anyio task group stays unblocked). The same pattern is used externally by `call_pi_tool` in the `unitares_pi_plugin` package.
3. **`asyncio.wait_for` with a tight timeout** — degrade to a fallback on deadlock instead of hanging the pipeline. See `deep_health_probe_task` at `src/background_tasks.py:380` and `_load_binding_from_redis` at `src/mcp_handlers/middleware/identity_step.py` (500ms budget, returns `None` on timeout).

## Known Test Notes

- Knowledge graph AGE tests require a live AGE connection (errors, not failures, when unavailable)

## STRICT_IDENTITY_REQUIRED (#425 staged rollout)

`STRICT_IDENTITY_REQUIRED=true` flips the dispatch middleware from auto-minting an ephemeral identity for non-`pre_onboard` tools to returning a typed-refusal response (`status: identity_required`). Default is `false` so existing callers (residents, dispatch workers, plugins doing bare `onboard()`) keep working. Per-tool identity requirements are declared via `requires_identity=` on the `@mcp_tool` decorator (see `src/mcp_handlers/decorators.py`).

Rollout sequence:

1. Local dev (your shell) → set the flag, run a session through, watch for typed refusals where you expected work.
2. Lumen (Pi) → flag the Pi env, observe resident agents (vigil/sentinel/watcher/chronicler) for refusals at scheduled boundaries; fix offenders by adding `parent_agent_id` to their bootstrap.
3. Dispatch (the Discord bridge / dispatch worker) → flag, observe per-thread agent spawns.
4. Flip the default in code only after all three burn-ins are clean for ≥1 week.

When investigating a typed-refusal, the response carries `tool`, `hint`, `ontology_ref`, and `rollout_flag` so the cause is structurally identifiable.

## Minimal Agent Workflow

Per identity.md v2 ontology, fresh process-instances mint fresh identity. Lineage is declared, not resumed via token.

Default happy path:

1. `onboard(force_new=true, parent_agent_id="<prior UUID if continuing this workspace>", spawn_reason="new_session")` → save `agent_uuid` and `client_session_id` from response
2. `process_agent_update(response_text=..., complexity=..., client_session_id=...)` for in-process check-ins
3. On a future process-instance, repeat step 1 with the new prior UUID — do not auto-resume

Identity rules:

- A fresh process-instance is a fresh agent. Process-instance boundaries are honored.
- `client_session_id` maintains identity within one process; weak across processes.
- `continuity_token` is being narrowed (S1 in `docs/ontology/plan.md`) — present-day external clients can still resume with it, but plugin-internal flows declare lineage instead.
- Substrate-anchored agents (Lumen, the long-lived residents) earn cross-process continuity via the substrate-earned identity pattern in `docs/ontology/identity.md` — they may use a hardcoded UUID across restarts.
- Arg-less `onboard()` with no proof signal triggers the v2 fresh-instance gate — the server flips `force_new=true` and emits a `[FRESH_INSTANCE]` log line (S13).

<!-- END SHARED CONTRACT -->
