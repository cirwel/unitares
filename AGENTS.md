# AGENTS.md — unitares

Bootstrap for Codex (and other non-Claude) sessions in this repo. This file is the **machine-facing Codex bootstrap**. Content below the `SHARED CONTRACT` markers is kept byte-identical with `CLAUDE.md` — CI (`scripts/dev/check-shared-contract.sh`) enforces parity. Edit shared rules in **both** files; edit the Codex preamble here only.

For the human-facing Codex quickstart, see `CODEX_START.md`.

The installable Codex/Claude adapter bundle is canonical in the companion `unitares-governance-plugin` repo. This file only governs how Codex should behave while working directly inside the `unitares` server repo.

## Codex-specific wiring

Codex exposes synchronous lifecycle hooks, but they do not imply a continuously
running agent. The expected state-writing profile is sparse: the agent usually
makes zero or one meaningful `sync_state` call during a turn, and the plugin's
Stop hook may add one automatic `substrate_interpretation` after the turn. Only
the former is an agent-authored check-in. Lazy onboarding can also write a
synthetic bootstrap row; initialization is not a real check-in. PostToolUse
receipts and optional hook-parent heartbeats are audit evidence only — never
agent runtime, progress, or EISV. Do not manufacture per-tool or per-edit
check-ins.

### Slash commands (`commands/*.md`)

- `/governance-start` — onboard or resume; refreshes local continuity state
- `/checkin` — governance update for the current turn, plus meaningful milestones
- `/diagnose` — identity, state, and operator diagnostics
- `/dialectic` — structured review
- `/closeout` — final workspace hygiene check; reports dirty files, Git delivery state (local vs pushed/merged), and repo-rooted processes; can stash/stop when cleanup is requested

Raw tool flow when slash commands are unavailable: `start_session(force_new=true, parent_agent_id=<prior uuid if continuing>, spawn_reason="new_session")` → save `agent_uuid` + `client_session_id` → `sync_state(response_text, complexity, client_session_id=...)` only when there is meaningful agent state to report (typically at most once per assistant turn) → `check_working_state()` for read-only checks → `health_check()` only if system health is suspect. Canonical/raw equivalents are `onboard(...)`, `process_agent_update(...)`, and `get_governance_metrics(...)`.

### Local continuity cache

`.unitares/session.json` is Codex's authoritative local workspace state (not Claude's memory system). It holds `uuid`, `client_session_id`, `session_resolution_source`, and optional short-lived proof material for in-process calls. Helper: `scripts/client/session_cache.py`. On every new session or after a restart, call `onboard(force_new=true)`. Add `parent_agent_id=<saved uuid>, spawn_reason="new_session"` only when this is a real handoff from a finished predecessor, not merely because the cache exists.

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

## Measurement authority — what a number may decide

**A usage count may retire an instrument. It may never retire a capability.**

A zero cannot distinguish four states, and only the last is "no value":

1. **Never surfaced** — nothing put it in a caller's context.
2. **Not reachable** — it errored, or was built and never wired.
3. **Not recorded** — the instrument was blind to the transport.
4. Surfaced, reachable, recorded, and genuinely unused.

Operator, 2026-08-01, on the #1387 adoption gate: *"i think our kill criterion
too aggressive because frequently features are not wired up or buggy … id
rather work and improve on things rather than killing everything."* That gate's
zero was produced three times in one month by surface defects — #1414 (auth
rejected the caller while reporting `tier: strong`), #1424 (`audit.tool_usage`
never recorded `/mcp` or `/sse` at all), #1442 (timeout) — and never once by
real disinterest.

**Rules:**

- ⛔ **Never propose removing, deprecating, or "killing" a capability on a usage
  count.** Report the count as telemetry and say what it does not establish.
- ⛔ **"Voluntary" / "unprompted" / "organic" usage is not a measurable
  quantity.** Every tool call is conditioned on context the operator controls,
  so a zero measures what was injected, not what was valued. A metric whose
  name presupposes agent volition is measuring a state the system cannot have.
  See `scripts/dev/adoption_kpi.py` and the *What the word does NOT claim*
  section of `docs/ontology/eisv-proprioception-contract.md`.
- **Before citing any zero as evidence, name which of the four states above you
  ruled out, and how.** "The producer ran and found nothing" and "the producer
  never ran" are different findings; do not report them with the same sentence.
- **A measurement whose only purpose is to authorize a kill should be deleted.**
  A measurement that informs — volume, trend, bounce, concentration — stays, as
  telemetry, with no removal authority attached to it.
- **A kill retires the LEVER, not the goal.** A fair zero banks the datum and
  moves to the next lever; it does not close the track.
- **State a deciding standard as a choice before applying it, not afterwards as
  "the method."** If a threshold, control, or noise floor is what turns the data
  into a verdict, it is a judgement call and belongs to the operator. Choosing
  it silently and reporting only the conclusion imports an evaluative frame that
  was never agreed.

**Exempt: pre-registered scientific stop rules.** These constrain the *analyst*
against selective re-runs, not the feature, and are the opposite of the failure
above. Do not weaken, re-run, or "refresh" them — see
`docs/proposals/eisv-outcome-grounding-stop-rule-v0.md` and the individuality-v2
criterion honoured 2026-07-30.

## Project

UNITARES governance MCP server. A behavioral governance framework for AI agents (EISV state vectors, coherence tracking, dialectic resolution, knowledge graph). The information-theoretic / free-energy formulation is the research target in Paper v6, not the live decision path — which is behavioral state estimation.

## Stack

- Python 3.12+, asyncio
- PostgreSQL@17 + AGE 1.7.0 (Apache Graph Extension) via Homebrew
- Redis — de-facto primary session/identity store, **not optional** (boots in degraded local-only mode without it, but most live sessions exist only in Redis; see `docs/proposals/redis-retirement-v0.md`)
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

This section protects against wasted parallel work across agents, not just mistakes within one session. The deeper fix is upstream of any single agent: do not run multiple agents on the same single-writer surface in the same window.

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

- **Do not force-push in a way that can lose work.** The rule is about work loss, not about rewriting history as such: amending your own un-merged commit — a message fix, a squash of your own commits — is fine. Push it with **both** `--force-with-lease --force-if-includes`. Both are required, and `--force-with-lease` alone is not the safeguard it is widely assumed to be: it only checks that the remote ref still matches your *remote-tracking* ref, so any `git fetch` since your last integration silently refreshes the lease and the next force-push destroys a colleague's commit without warning. Agent sessions fetch constantly, so this is the normal case, not a corner case. `--force-if-includes` closes it by additionally requiring that what you are about to overwrite is already reachable from your local history. Never force-push a shared or already-merged branch, never force-push over a commit you did not author, and never use bare `--force`.
- Do not run destructive git commands without explicit user approval
- Do not run DROP/TRUNCATE/DELETE on the governance database without explicit user approval
- **Keep harness session-attribution trailers out of commit messages.** `repo-scope.yml` greps the whole `origin/master..HEAD` range for the session-link trailer, so a later commit cannot clear a bad one — the only repair is an amend. Agent harnesses append this trailer by default; strip it before committing. The exact pattern is defined in `scripts/dev/check-repo-scope.sh` and is deliberately not reproduced here, because writing it out trips the guard on this file.

## GitHub Workflow Conventions

Codex and Claude share one delivery contract so concurrent sessions stay predictable. Full reference: `docs/operations/github-workflow-conventions.md`.

- **Branch naming — one pattern, author-prefixed:** `<author>/<topic>-<short-id>` where `<author>` identifies who is making the change — an agent (`claude`, `codex`) or a contributor handle — for parallel attribution. Both `ship.sh`'s `<author>/auto/<timestamp>-<slug>` and the web harness's `claude/<topic>-<id>` satisfy this shape. Never push to `main`/`master`.
- **Delivery — draft PR for everything:** every session lands its work as a draft PR, regardless of agent and regardless of whether the change is runtime code or docs/tests. A human maintainer is the merge gate. Do NOT direct-push to a shared branch and do NOT enable auto-merge by default. `ship.sh` enforces this: its default `auto` route opens a draft PR for every change (`--direct` opts out for docs/tests-only pushes; `--auto-merge` only when a maintainer explicitly asks).
- **Delivery requests authorize delivery:** when a maintainer asks to ship, finish, deliver, open a PR, or otherwise complete a delivery workflow, the working agent may assume branch -> commit -> push -> draft PR is in scope and should not ask for a second confirmation just to push or open the draft PR.
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
   `client_session_id`. Adapters should do this automatically. If your adapter
   does not thread it, you fall back to the weak transport-fingerprint pin and
   can fragment under co-residency — check `session_source`/`tier` in the
   onboard response to confirm you bound as expected.
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

Tag recurring point-in-time snapshots `ephemeral`. This is a claim about the
content's shelf life, never about the writer's. A session that ends in an hour
can still record something permanent, and must not tag it `ephemeral` on the
grounds that the session was short-lived. The test is what the entry *is*: a
reading taken at a moment (weekly triage run, scheduled audit, per-cycle status
note) gets the tag; a claim meant to stay true (a diagnosis, a closed mystery, a
correction to a prior conclusion) never does. Vigil's groundskeeper tags its own
notes this way.

Without the tag, snapshots land as ordinary open entries and never close: a
snapshot has no resolution condition, only a timestamp, so every later sweep
re-reads it as unfinished work. With it, `KnowledgeGraphLifecycle` archives
after seven days — archived, still retrievable via `include_archived=true`,
never deleted (`src/knowledge_graph_lifecycle.py:139`).

Two traps. `EPHEMERAL_TAGS` also contains `test` and `demo`, so a durable
finding *about* the test suite must not carry the `test` tag. And permanence
wins on tie: a `learning` / `pattern` / `root_cause_analysis` / `migration`
type, or a `permanent` / `foundational` / `architecture` / `decision` tag,
overrides an ephemeral tag rather than losing to it — `get_lifecycle_policy`
checks permanence first (`src/knowledge_graph_lifecycle.py:107`).

<!-- END SHARED CONTRACT -->
