# CLAUDE.md — unitares

Bootstrap for Claude Code sessions in this repo. Content below the `SHARED CONTRACT` markers is kept byte-identical with `AGENTS.md` — CI (`scripts/dev/check-shared-contract.sh`) enforces parity. Edit shared rules in **both** files; edit the Claude preamble here only.

The installable Codex/Claude adapter bundle is canonical in the companion `unitares-governance-plugin` repo. This file only governs how Claude Code should behave while working directly inside the `unitares` server repo.

## Execution-cost policy (read first)

- **The core must run free / self-hosted — no *required* metered model-API dependency.** unitares is user/agent-agnostic, so a metered API (Anthropic / OpenAI / …) must never sit on the *required* default path: the server, CI, and residents must run with no paid key (local Ollama for Watcher, `GITHUB_TOKEN`-only CI, deterministic CLI tools — ruff, the doctor, the surfacing collectors). The guarantee is for *every* installer, not just a funded one: someone with no model budget can always run it. Metered models are **welcome as an opt-in, off-by-default backend** — config-gated (e.g. a `base_url` / key from env) so an operator with credits enables them and an operator without stays free. What's forbidden is *forcing* a paid API on everyone: a hardcoded metered endpoint, a metered CI action (`anthropics/claude-code-action`), or a hard SDK import with no local fallback. Add a metered path only as deferred / opt-in, and never build it inert "just in case." (`scripts/dev/check-repo-scope.sh` Rule 6 enforces this.)

## Claude-specific wiring

Claude Code runs through a plugin-style harness. The hook lifecycle is owned by the **`unitares-governance-plugin`** repo (canonical for the adapter bundle). This repo does not vendor or wire its own hook chain. The plugin's `hooks/session-start` and `hooks/post-edit` are what fire on Claude lifecycle events — when CLAUDE.md or AGENTS.md describes hook behavior, the source of truth is the plugin.

User-level `~/.claude/hooks/` adds a third layer (auto-test, watcher-hook, watcher-chime, stop-milestone-audit, etc.) wired directly in `~/.claude/settings.json`. Watcher findings that appear at session start or as a chime block originate from that user-level chain, not from the plugin's `post-edit`.

**The per-turn check-in is the plugin's, not the user chain's.** A `stop-checkin.sh` exists under `~/.claude/hooks/` but is no longer wired in `settings.json` (verified 2026-08-09; its log stops 2026-08-02). The live per-turn `process_agent_update` for **both** Claude and Codex comes from the plugin's `hooks/post-stop`, which submits `epistemic_class="substrate_interpretation"` — a substrate reading of turn shape, not an agent-authored report. Check `settings.json` before assuming a user-level hook is live; a file on disk under `hooks/` is not evidence that it runs.

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

## Fleet neutrality — what a resident name may decide

**A resident name may be read from config and displayed. It may never be branched on.**

UNITARES installs into deployments that run their own residents, or none.
Which residents exist is deployment configuration (`UNITARES_RESIDENTS`, empty
by default), and shipped source must read that roster rather than naming
anybody. A name has exactly two legitimate jobs:

1. **Display** — dashboard labels, and presentation order, which is the order
   the roster declares.
2. **N=1 calibration partitioning** — each named resident is its own class, so
   the name is a partition key for a scale constant.

Note that the second is a *statistical* key, not a dispatch key: the name
selects a constant, never a code path. A name used as a third thing is the
defect. The discriminator for behavior is always a capability — `embodied`,
`persistent`, `protected`, `cadence.*`, physical-vs-behavioral sensor — never
who the agent is.

**Rules:**

- ⛔ **Never branch on a resident name.** No `if label == "Lumen"`, no dict
  keyed by label, no list of names filtering a roster. Use the tag that
  expresses the property you actually need, and add one if it does not exist.
- ⛔ **Never hardcode a roster.** `scripts/dev/check_fleet_identity_leak.py`
  (pre-commit + the `Repo Scope Guard` workflow) fails on a resident name
  appearing as a string literal anywhere in the shipped artifact — `src/`,
  `governance_core/`, `config/`, `agents/sdk/src/`. Its scope must track
  `[tool.setuptools.packages.find]` in `pyproject.toml`.
- **Provenance in a comment is fine and is deliberately not flagged.** A note
  saying a threshold has its value because of what a resident did on a date is
  the reason the constant is what it is; deleting it makes the code less
  honest without making it more portable.
- **Lead with the definition, keep the origin as a footnote.** A docstring that
  explains a general mechanism by naming the agent it was derived from taxes
  every future reader with a device they do not have. Say what the thing is,
  then say where it came from.
- **The residentless install is the default, so it is the case to test.**
  `tests/conftest.py` declares a roster for the suite, which means nearly every
  test runs in a fleet-present world. New behavior that depends on the roster
  needs a companion assertion in `tests/test_residentless_install.py` for what
  happens when it is empty. That path is what every adopter runs and it is the
  one nothing else exercises.

`agents/` (the reference residents) and `scripts/ops/` (this operator's fleet
control plane) are deliberately NOT agnostic and are outside the guard: a
deploy script for a specific fleet is supposed to name it. The boundary is the
shipped artifact, not the repo.

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
3. Install dependencies: `pip install -e ".[full]" -c constraints.txt`
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
- **Mark-ready and merge are separate deliberate acts:** a draft PR means "visible, not claiming merged." Marking ready is the owning agent's declaration (next bullet); merging is the maintainer's. Either happens only after CI is green and no collision with an in-flight branch (see the single-writer-surface rules above).
- **Readiness is agent-declared, never operator-inferred:** the agent that owns a PR marks it ready itself, once its validation actually passed (CI green, review round joined). Until then, draft means "still working — hands off," even when the diff looks finished. Nobody marks another agent's PR ready on its behalf: a draft whose owner went silent is a question for the owner (KG channel), or an explicit operator override stated in a PR comment — not a silent green button. Ordering the agent knows about ("merge after #N") goes in the PR body so in-order merging acts on declared state. Merging remains the maintainer's act.

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
