# S21-a — Council Review of the Fix Proposal

**Filed:** 2026-04-27, two passes.

**Pass 1 (this doc, §HIGH/MEDIUM/TEST GAPS below):** review of the proposed fix. `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`, parallel. Six high-severity findings (H1–H6), five medium (M1–M5), three test gaps. Verdict: ship with modifications.

**Pass 2 (§"Adversarial second-pass" near the end):** review of the merged S21-a (PR #221, 2026-04-27 12:18Z). Same agents, prompted explicitly to find what pass 1 missed — adversarial inputs, persisted-state corruption, concurrent writers, deploy-time migration, forensic gaps. Found additional high-severity issues that survived merge. Drives a follow-up scope expansion for S21-b.

The pass 1 prompts were closed-set ("evaluate NX race, TOCTOU, scope, two-callsites, test gaps") which left bug classes outside that frame under-covered. Pass 2 corrects for that. Memory entry "Council prompts must invite adversarial bug-hunting" (2026-04-27) is the durable form of that lesson.

---

## HIGH — must address before merge

### H1. anyio-asyncio violation in Change 1 (code-reviewer)
The proposed "check if existing `agent_uuid` is `status='active'` in `core.identities`, then skip" path adds a new async Postgres call inside `_cache_session_redis_write` at `persistence.py:157+`. That function is invoked from PATH 2 (`resolution.py:617`) and PATH 3 (`resolution.py:861/883`), both inside the anyio task group. CLAUDE.md "Known Issue: anyio-asyncio Conflict" prohibits new `await asyncpg` from handler paths.

**Fix:** PATH 2 already fetches `agent_status` at `resolution.py:607`. Pass it down as a parameter to `_cache_session` → `_cache_session_redis_write`. No new round-trip.

### H2. `_session_identities` in-memory dict overwrite is unguarded (code-reviewer)
NX in Redis blocks one write path. `persistence.py:96` writes to the in-memory `_session_identities` dict unconditionally before the Redis path runs. Even with NX correctly preserving Redis, the in-memory layer is still being overwritten on every PATH 3 mint. Other in-process readers consult that dict.

**Fix:** apply the same "don't overwrite if active binding exists" guard at `persistence.py:96`. Both layers must be gated, not just Redis.

### H3. No eviction of already-installed ghosts (code-reviewer)
NX prevents *future* overwrites. Redis slots already polluted by ghosts (e.g., the active sessions producing the chronic 92.3% rate) are not repaired by the proposal. Live-verifier confirmed slot `session:agent-6648432c-a50` is currently bound to `3fe12516-3f53-4106-b02a-8c8489d71773` (a different agent), TTL ~24h.

**Fix:** S21-a must include either (a) a one-shot Redis sweep at deploy time, (b) explicit eviction of stale entries where the bound agent is `status=archived`, or (c) PR description must call out that 24h TTL drains the existing pollution.

### H4. `resume=True` is the default — fail-closed will brick legitimate first callers (code-reviewer + dialectic)
`handlers.py:858`: `resume = arguments.get("resume", True)`. A first-time caller with explicit `client_session_id="agent-foo"` and no prior session row will hit the new fail-closed gate by default. Live-verifier confirmed external callers exist: dashboard, discord-bridge, plus any HTTP script that "tries `identity()` first."

**Fix:** either change the handler default to `resume=False` and require explicit `resume=True` for continuity intent, OR add a second discriminator (e.g., only fail-closed when a tombstone says the row existed and was bypassed). Also: MISS response should suggest `onboard(force_new=true)` so callers self-heal. Consider env flag `UNITARES_PATH2_FAIL_CLOSED=1` for a one-release canary.

**External-caller audit required in S21-a:** grep `identity(` callsites in `unitares-governance-plugin`, `unitares-discord-bridge`, dashboard JS, `agents/*/agent.py`.

### H5. Plan-doc schema description is wrong (live-verifier)
Plan says join `core.sessions.identity_id` → `core.identities.agent_id (UUID)`. Reality: `core.sessions.identity_id` is `bigint` FK to `core.identities.identity_id` (bigint PK). `agent_id` is a separate `text` column holding the UUID string.

**Fix:** any SQL written off the plan's description will fail. Update plan doc + ensure regression tests use the right column.

### H6. status enum has six values, not three (live-verifier)
Production: `active, archived, deleted` observed; CHECK constraint permits `active, archived, disabled, deleted, waiting_input, paused`. The NX guard says "skip if status='active'" — must explicitly decide what to do for the other four non-active-non-archived states. A `paused` agent's binding should probably be preserved; a `deleted` agent's slot should evict.

---

## MEDIUM

### M1. Redis key format in production
Live keys are `session:{ip}:{port_fragment}:{hash}` (e.g. `session:127.0.0.1:51befd:d0d017f3`), not `session:agent-{uuid[:12]}`. The incident example `session:agent-6648432c-a50` is the *display form* in the doc, not the actual key. Doesn't change the fix logic — NX is still keyed on whatever `session_key` the caller passes — but test fixtures should use realistic key formats.

### M2. Ghost-fork rate is 92.3%, not 95.1%
Re-run on 2026-04-27: 2032 ghosts / 2201 total / 92.3% over 30d. Chronic, unmitigated. Plan-doc figures are stale; phenomenon is real.

### M3. Archived-agent in Redis (TOCTOU)
If Redis holds a binding to an agent that was subsequently archived, naive NX refuses the legitimate re-bind. Need to evict-then-bind when bound agent's status is non-resumable.

### M4. S21-a/S21-b honesty gap (dialectic)
Between merges, master has correct PATH 2 resume *and* `require_registered_agent` (`agent_auth.py:256`) still consulting only `mcp_server.agent_metadata` (S21-b §6). Dogfood will see "not registered" errors after S21-a and conclude the fix didn't work. Either fold §6 into S21-a or call out the residual breakage explicitly in the PR body.

### M5. NX semantics implicitly grant cross-process-instance continuity (dialectic)
NX-preservation is anchored in `core.sessions` (substrate) so it's earned, not performative — but the implementer should write a one-line comment naming this so future readers don't mis-read it as token-style resume.

---

## TEST GAPS

The three proposed regression tests cover (i) 14-min idle resume, (ii) NX preservation, (iii) PATH 2 fail-closed. Missing:

1. **Archived-agent-in-Redis** — NX correctly refuses, leaving the stale binding stuck (issue M3 / H6).
2. **In-memory `_session_identities` overwrite** — verify the dict is also guarded, not just Redis (issue H2).
3. **Middleware + handler double-resolution with first-call MISS** — interaction between `identity_step.py:414` and `handlers.py:890` when the middleware MISS is uncached and the handler re-runs PATH 2 (S21-b defers the consolidation but S21-a's fail-closed change affects both callsites).

---

## VERIFIED

- All six file:line references match: `persistence.py:157` (`_cache_session_redis_write`), `resolution.py:580` (PATH 2 gate), `resolution.py:661` (`logger.debug`), `resolution.py:861/883` (PATH 3 mint), `session_cache.py:98` (bind), `agent_auth.py:256` (`require_registered_agent`).
- `session_resolution_source` already exists in live `identity()` response.
- `identity_resolution_outcome` correctly identified as a proposed addition (S21-b §7).
- One slot per session — single write key confirmed at `persistence.py:188` and `session_cache.py:98`. The "two write paths" concern from the diagnosis is not a separate-key concern; both write the same key.
- All five named ghost UUIDs survive in `core.identities`; four still `active`.

---

## Verdict

**Ship S21-a with modifications.** Required changes:

1. Pass `agent_status` from PATH 2 down to `_cache_session_redis_write` instead of adding a DB call (H1).
2. Gate the in-memory dict write at `persistence.py:96` with the same NX logic (H2).
3. Add explicit ghost-eviction OR document 24h TTL drain (H3).
4. Decide `resume` default semantics + run external-caller audit (H4).
5. Fix plan-doc schema description (H5) + handle full status enum (H6).
6. Either fold S21-b §6 (`require_registered_agent`) into S21-a or explicitly document the residual breakage in the PR body (M4).
7. Add the three missing regression tests.

S21-b row remains correctly scoped — no changes proposed there from this pass.

---

# Adversarial second-pass (post-merge of PR #221)

**Filed:** 2026-04-27, after S21-a merged at 12:18:50Z.
**Why a second pass:** pass 1 prompts gave closed-set failure-mode lists; agents stayed inside those lists. Pass 2 explicitly invited adversarial bug-hunting outside that frame: persisted-state corruption, adversarial inputs, concurrent writers, deploy-time migration, forensic gaps, axiom-#3 honesty.
**Verdict:** S21-a as merged closes the bleed at the *write* path but leaves multiple high-severity gaps in the *read* path, the *deploy-time pollution drain*, the *adversarial-input* surface, and *forensic reconstructability*. These are real and should drive a follow-up PR or a scope expansion for S21-b.

## HIGH — discovered post-merge

### H7. PATH 1 still serves ghosts on pre-existing polluted Redis slots
`src/mcp_handlers/identity/resolution.py:354–557` (PATH 1 fast-path) returns the cached UUID before reaching the new fail-closed gate at line 594. For the ~92% of sessions whose Redis slot already holds a ghost binding (chronic pre-S21-a population), PATH 1 hits, returns the ghost, function exits — fail-closed gate never evaluated. NX prevents *future* PATH 3 overwrites on clean slots; it does not fix populated ghost slots. PR #221 deferred H3 to "TTL drain" — but see H8 for why that assumption is wrong.

**Fix:** at PATH 1 return time, cross-check the cached UUID against `core.sessions` before returning, OR add an explicit Redis sweep to the deploy runbook (`SCAN session:* + DEL where bound_agent_id is archived` — scoped to keys with >1h TTL remaining).

### H8. 348 Redis session keys have no TTL — pollution will not self-drain
`redis-cli` snapshot 2026-04-27: **7,504** `session:*` keys live; **7,154** have TTLs (range 22s–86,285s, median ~13h); **348** are persistent (no TTL). Of the 348 no-TTL keys, **59 are bound to `archived` agents** in `core.identities`. These keys will never expire and will never be evicted by the merged S21-a. The "24h TTL drain" answer in PR #221's H3 deferral is materially incomplete.

**Fix:** S21-a-followup must include a deploy-time sweep that handles no-TTL keys explicitly. Persistent ghost bindings to archived agents are unrecoverable without it.

### H9. Middleware auto-mint defeats the fail-closed gate it was meant to support
`src/mcp_handlers/middleware/identity_step.py:431–445`. PR #221 added a middleware retry on `session_resolve_miss` that calls `force_new=True, spawn_reason="dispatch_auto_mint"`. This was the cited solution to pass-1 H4 ("don't brick legitimate first callers"). Pass 2 finds it is the wrong direction: the fail-closed gate fires, the middleware silently absorbs the MISS, and mints a fresh ghost (with declared `dispatch_auto_mint` lineage). Tool-only callers (any external HTTP client that calls a governance tool without a prior `onboard()` / `identity()`) now get one fresh identity per request — same fleet-wide ghost-fork rate as before, just with declared lineage. Pass-1 H4 explicitly asked for the MISS to be returned to callers so they self-heal; the merged code does the opposite.

**Fix:** gate the dispatch auto-mint behind an env flag (`UNITARES_DISPATCH_AUTO_MINT=1`, default off). When off, the middleware returns the structured `session_resolve_miss` to the caller. Canary the flag on for a release before flipping default.

### H10. `_redis_slot_blocks_overwrite` is fail-open on Redis exceptions — same failure mode as the bug being patched
`src/mcp_handlers/identity/persistence.py:305–306`: `except Exception: ... return False`. Any non-timeout Redis error (RESP protocol error, connection reset, WRONGTYPE, auth failure) silently returns "not blocked" and the PATH 3 overwrite proceeds. The anyio-asyncio deadlock case is handled by the outer `asyncio.wait_for(timeout=1.0s)` (raises `TimeoutError` before `return False` is reached), but transient Redis errors are not. The log level is `debug`, so this failure mode is invisible at default log level.

**Fix:** promote the exception log to `warning`. Add an env flag (`UNITARES_NX_FAIL_CLOSED=1`) that flips the default to `return True` for environments that prefer refusing mints over allowing them on cache read failure.

**Status:** Implemented in `src/mcp_handlers/identity/persistence.py`. Redis guard read failures now emit `[S21A_REDIS_GUARD_READ_FAILED]` at warning level and preserve default fail-open behavior unless `UNITARES_NX_FAIL_CLOSED=1`, in which case `_redis_slot_blocks_overwrite` returns `True` and the guarded Redis write is skipped. Regression coverage lives in `tests/test_identity_session.py`.

### H11. Adversarial `client_session_id="\n\n"` reproduces the original bug post-merge
Live probe via `mcp__unitares-governance__identity` with whitespace-only input: server returned `session_resolution_source: "explicit_client_session_id"` AND `identity_status: "created"` AND `resumed: false` — minted a fresh ghost while claiming the explicit-session-id path. This is the exact shape of the original incident. Reproducible on demand. The NX guard does not fire because the whitespace key has no prior binding. Pass-1 H6 (status enum) and the proposed regression tests do not cover this input class. Also adjacent: 10000-char and `../../etc/passwd` payloads silently dropped (no graceful JSON error, just empty body).

**Fix:** sanitize/reject whitespace-only and over-length `client_session_id` in `derive_session_key`. Per pass-1 plan, the `arguments.get("client_session_id")` extraction at `session.py:564` should `.strip()` and reject empty-after-strip. Add adversarial-shape regression tests covering empty, whitespace-only, oversized, control-character, and path-traversal inputs.

**Status:** Implemented in `src/mcp_handlers/identity/session.py` via `normalize_client_session_id()`, with handler proof gates normalizing `client_session_id` before deciding whether a caller supplied a proof signal. Whitespace-only IDs now fall through instead of marking `explicit_client_session_id`; overlong IDs are bounded; control/path-traversal shapes are sanitized to inert key text. Regression coverage lives in `tests/test_identity_session.py`.

### H12. 38 ghosts have `core.sessions` rows; NX cannot dislodge them
Of the 2032 30d ghosts, **38** have at least one row in `core.sessions` — i.e., they minted *and* bound a session_key in PG. The NX guard refuses to overwrite these from PATH 3. They are blast-resistant to S21-a as merged. Operators have no tool to clear them.

**Fix:** ship a one-shot reconciler in `scripts/ops/` that flips ghost identities meeting (`parent_agent_id IS NULL AND spawn_reason IS NULL AND status='active'`) to `status='archived'` with `spawn_reason='s21_backfill_ghost'`. Without it, the chronic-rate metric never recovers.

## HIGH — forensic / honesty

### H13. `audit.events.session_id` is always NULL — session-level forensic reconstruction is architecturally impossible, not merely degraded
Live verification: `audit.events` rows have `session_id=NULL` universally. The incident doc claimed "the audit log is technically consistent" — true at the row level, but the absence of `session_id` means an operator querying "what happened in session X" against `audit.events` cannot succeed by any query. Only `agent_id` is queryable, which means session-level reconstruction across the bleed period is impossible. S21-b §8 plans audit emission on rejected session IDs — correct direction, but the table needs `session_id` populated on *all* rows, not just rejection events. The 12 NULL-actor rows in the incident window have no discriminating signal.

**Fix:** S21-b §8 must expand to include populating `audit.events.session_id` on all writes, plus a `forensic_origin` JSON field on `core.identities` capturing `(session_key, prior_redis_binding, prior_pg_binding, mint_path)` at mint time. Cheap to add now; impossible to back-fill later — the join key was never recorded.

### H14. S21-a creates a coherent-looking façade over a rotted floor (axiom #3 violation)
Post-S21-a, `identity()` returns the right UUID for resumed sessions, but `require_registered_agent` (`agent_auth.py:256`) still consults only `mcp_server.agent_metadata` (S21-b §6 deferred). External callers see "your session resumed" → "your agent is not registered" within two calls. PR #221's PR body explicitly calls this out as known. Pass-1 M4 noted the symptom; pass 2 names the principle: this is *worse* than the chronic ghost. Today the system is consistently broken; post-S21-a it presents a partial recovery that lies about its own state. Axiom #3: build nothing that appears more alive than it is.

**Fix:** either fold `require_registered_agent` consults `core.identities` into S21-a-followup, OR add a top-level `degraded: true, residual_breakage: ["require_registered_agent_inconsistent"]` field on `identity()` responses until S21-b §6 ships.

## MEDIUM — discovered post-merge

### M6. SessionCache TTL refresh on read may extend ghost lifetime
If `SessionCache.get()` touches TTL on hit (common pattern), every PATH 1 read against a ghosted slot resets the 24h drain clock. H3's "24h TTL drains pollution" assumption fails. **Verify** in `src/cache/session_cache.py` — does the get-path call `EXPIRE` or only `GET`?

### M7. Audit event absent on PATH 2 fail-closed return
`resolution.py:595–610` logs `[PATH2_RESUME_MISS]` at INFO but emits no structured `concurrent_session_binding_observed` event. The fail-closed path is precisely the moment that event is most useful. Cheap addition; pure additive change.

**Status:** Implemented as `session_resolve_miss_observed` in `src/audit_log.py` and emitted from the PATH 2 no-row / PG-exception fail-closed branches. The event carries top-level `session_id` for PostgreSQL audit indexing plus structured details (`reason`, `resolution_source`, `resume`, `force_new`, token presence). It intentionally uses a dedicated event type rather than overloading `concurrent_session_binding_observed`, because a missing session row is not itself evidence of concurrent binding.

### M8. continuity_token + client_session_id PATH 2.8 silent reroute (existing behavior, but now invisible)
When a caller passes both, `derive_session_key` returns the token-derived `session_key`; the explicit `client_session_id` is silently ignored. PATH 2.8 may bind the token's UUID to the token-derived key. Not a S21-a regression, but the new fail-closed gate (`if not token_agent_uuid`) makes this carve-out invisible. Document or surface in response.

### M9. Concurrent same-`session_key` writers — dict-write + Redis-write are not atomic
Two concurrent first-time resolvers can both pass the dict guard, both pass the Redis NX (only one wins Redis), and the loser's dict write may shadow the Redis winner inside one process. The only true serialization is per-session_key mutex or making the dict a write-through cache of the Redis NX result. Lower probability than the others; flag for S21-b's atomicity work.

### M10. Postgres ghost backlog is permanent until archival policy
2032 `active` ghosts in `core.identities` will satisfy `is_registered` checks indefinitely after S21-b §6 lands. No archival policy currently exists. Without one, the chronic-rate metric never goes below the legacy floor.

## Pass-2 verdict

**S21-a as merged is necessary but insufficient.** It closes the *future write* path. It leaves open: the *current read* path on polluted slots (H7), the *deploy-time pollution drain* for no-TTL keys (H8), the *fail-closed gate's effective bypass* by middleware auto-mint (H9), the *Redis-error fail-open* (H10), the *adversarial input* surface (H11), the *blast-resistant 38 ghosts* (H12), and the *forensic reconstructability* of the bleed period (H13). Plus the axiom-#3 honesty gap (H14).

**Recommend opening S21-a-followup PR** (or expanding S21-b scope) covering H7–H14 + M6–M10 before declaring S21 closed. Most are 1–2 file changes each; the deploy-time sweep (H7+H8+H12) is the largest single piece and warrants its own runbook entry.

**Methodological note:** pass 1's prompts were closed-set; pass 2's were explicitly adversarial. Pass 2 found 8 additional high-severity issues that pass 1 missed despite reviewing the same code. The cost of running pass 2 was three parallel agents and ~10 minutes wall time. The cost of *not* running it would have been merging S21-a and discovering H7+H8 in production when ghost rate didn't drop. This pattern (closed-set prompts under-cover; adversarial second-pass finds real bugs) is now durable in the user's memory as "Council prompts must invite adversarial bug-hunting."
