# S21 — Session Resolution Bypass (Incident 2026-04-27)

**Status:** Surfaced 2026-04-27 03:13–03:41 UTC (single Claude Code session dogfood). Empirical, reproducible-looking. KG store filing failed because the bug itself prevents identity-bound writes.
**Owner:** Open. Recommended priority: P0 — blocks honest landing of S1-a (TTL-narrowed continuity_token) because mid-session re-mints would silently invalidate the token.
**Blocks:** S1-a, S2, S3 (cannot retire `continuity_token` while explicit `client_session_id` doesn't resume).
**Does not duplicate:** S13/PR #156 (which flips arg-less default; this is the explicit-session-id resolution path — different code site).
**Related plan rows:** S13 clause (d) — `derive_session_key` step 7 audit event; this incident is the pre-condition that makes that audit event *necessary*.
**Synthesis context:** filed under the operator's "lumen reboots but is the same, our identity system worked when we first started months ago and now is all over the place" pivot (2026-04-27 conversation). This is the empirical floor under that intuition.

**Postscript — rate correction (2026-04-27, S21-a council pass):** the headline "95.1% fleet-wide ghost-fork rate" cited at lines 66 and 95 is the snapshot at incident-write time. After applying audit-window scoping (memory entry `feedback_audit-window-scoping.md`) the corrected rate is ~38%; live verification on 2026-04-27 (post-onboard restoration) shows ~20% on a 30-day window in `core.agents` (442 of 2210 fresh agents with `parent_agent_id IS NULL AND spawn_reason IS NULL`). The diagnosis still stands — the resolution path silently mints — but the magnitude was inflated by counting pre-audit-window entries that pre-dated the recording. The DoD threshold below ("drops below a threshold to be set after a calibration period") reads correctly either way; the calibration period is what determines the post-fix target.

---

## What happened

One Claude Code process-instance, four ghost identities silently minted in 41 minutes:

| T+ | UUID | Trigger | Source field | spawn_reason |
|---|---|---|---|---|
| 02:49:19 | `6648432c-a506-487d-9a49-1a76ac6def97` | `onboard(force_new=true)` | `ip_ua_fingerprint` | `new_session` |
| 03:13:46 | `c8811a0f-30e9-4130-bc69-8e952adfe833` | (no explicit call — re-mint observed in `agent_signature` of a `knowledge.update` response) | not visible to client | NULL |
| 03:29:07 | `90de0920-9364-4f43-857f-eab09f62ab51` | `identity(client_session_id="agent-6648432c-a50")` | server returned `session_resolution_source: "explicit_client_session_id"` AND `identity_status: "created"`, `resumed: false` | NULL |
| 03:31:29 | `fabbfa2f-27d4-41ac-a6a1-4e7a31c49ae6` | `knowledge.store(client_session_id="agent-90de0920-936")` | response: `Agent fabbfa2f-… is not registered` | NULL |

All four persist in `core.identities` as `status=active`. Only the first carries a `spawn_reason`; the other three are silent re-mints with no lineage declaration.

## What the DB says

```
SELECT s.session_id, s.identity_id, i.agent_id, s.last_active
FROM core.sessions s JOIN core.identities i ON s.identity_id = i.identity_id
WHERE s.session_id IN ('agent-6648432c-a50','agent-90de0920-936');

     session_id      | identity_id |               agent_id               |       last_active
---------------------+-------------+--------------------------------------+--------------------------
 agent-6648432c-a50  |        8618 | 6648432c-a506-487d-9a49-1a76ac6def97 | 2026-04-27 03:39:39 -06
 agent-90de0920-936  |        8624 | fabbfa2f-27d4-41ac-a6a1-4e7a31c49ae6 | 2026-04-27 03:31:29 -06
```

The original session row is correctly bound to `6648432c` and was being heartbeat-updated as recently as 03:39:39 (matching the most recent client call). **The session-row resolution would have returned the right identity. The API didn't consult it.**

## What `derive_session_key` says

`src/mcp_handlers/identity/session.py:489-505` (`_derive_session_key_impl`) priority order, highest to lowest:

```
1. arguments["continuity_token"]    — signed resume token
2. arguments["client_session_id"]   — explicit from caller          ← my path
3. MCP protocol session ID
4. Explicit HTTP session header
5. OAuth client identity
6. Explicit client ID header
7. IP:UA fingerprint + pin lookup   — unstable, needs pin
8. Contextvars fallback
9. stdio fallback
```

Explicit `client_session_id` is at priority 2, well above IPUA pin at 7. So `derive_session_key` correctly returned `"agent-6648432c-a50"` for my call. The session-key derivation is fine. **The bug is in the next layer — `session_key → identity_id`.**

## Fleet-wide

```
SELECT count(*) FROM core.identities WHERE created_at > now() - interval '2 hours';   -- 11
SELECT count(*) FROM core.identities WHERE created_at > now() - interval '2 hours'
   AND parent_agent_id IS NULL AND spawn_reason IS NULL;                              -- 10
```

**91% (10 of 11) of fresh identities created in the last 2h have neither `parent_agent_id` nor `spawn_reason`.** Mine accounts for 4 of those 10. The other 6 are other clients hitting the same path.

This is the empirical case for: the v2 ontology says "lineage is declared, not resumed via token" (`identity.md` §"Three stances"), but at runtime 91% of fresh identities aren't declaring anything. The current behavior is the *performative* stance.

## Second-order inconsistency

After 90de0920 was confirmed in `core.identities` (status=active, created 03:29:07), a subsequent call from the same session received:

```
"error": "Agent '90de0920-9364-4f43-857f-eab09f62ab51' is not registered. Identity auto-creates on first tool call."
```

The "is_registered" check is consulting a path that doesn't include `core.identities` — saying an identity doesn't exist when the canonical DB has it. So the cache-coherence bug spans **two** inconsistent layers:

1. `session_id → identity_id` bypasses `core.sessions`
2. `is_registered` check disagrees with `core.identities`

Both layers point to a Redis (or in-memory) cache that's drifting away from canonical Postgres state.

## Work mis-attribution this session

All KG cleanup performed between 03:13:46 and 03:28:06 — 7 status updates (6 archives + 1 archive of an already-archived row), 1 `process_agent_update` check-in, 1 `leave_note` about KG hygiene, 1 status-flip on the broadcaster note — was attributed to ghost identity `c8811a0f` in the audit trail, **NOT** to the onboarded `6648432c`. Audit reconstruction by `agent_id` would show those rows performed by an agent that has no `spawn_reason` and no lineage. The audit log is technically consistent (each row records the actor at write time) but **the intended actor and the recorded actor diverge silently**.

## Council review (2026-04-27, three-agent parallel pass)

Council ran across `dialectic-knowledge-architect`, `feature-dev:code-reviewer`, and `live-verifier`. Major upgrades to the diagnosis:

### Live-verifier (production blast radius)

- **Fleet-wide ghost-fork rate is 95.1% over the last 30 days** (1944 of 2045 fresh identities have neither `parent_agent_id` nor `spawn_reason`). 91.7% in last 2h. **Chronic, not a regression** — daily rate has been 77–100% since at least 2026-04-01. Brief Apr 22–25 dip to 17–44% coincided with PR #192 (PATH 1 sync-path fingerprint cross-check) and PR #187 (S19 substrate-anchored HTTP rejection); rebounded to 79% by Apr 26.
- **Residents are clean.** Vigil/Sentinel/Watcher/Steward/Chronicler all have exactly one identity row with no recent forks. Substrate-anchored hardcoded-UUID pattern is doing the work the resolution layer should be doing. Blast radius is session-like agents only.
- **A 5th ghost (`3b5efd37-53df-4b35-831e-8bbadabb8adb`) was minted at 03:44:16** — *after* this doc was written. Redis now maps `session:agent-6648432c-a50` to that ghost (overwriting the originally-bound `6648432c`), with ~24h TTL. Postgres `core.sessions` still correctly holds `6648432c`. They've diverged completely.
- **Audit log: 5 distinct actor IDs across `audit.events` for the same session, plus a 12-row block with NULL actor.** The new `concurrent_session_binding_observed` events from PR #156 observe the drift but don't fix it. Audit reconstruction by `agent_id` is unreliable for any session-like agent.
- The window I was looking at had **11 ghosts**, not 4 — 7 other clients hit the same path simultaneously.

### Code-reviewer (root-cause hypothesis)

Identified three suspect mechanisms (confidence varies; first-pass review):

1. **Redis read/write key prefix mismatch** (claimed confidence 100): `resolution.py:354` reads `redis.get(session_key)` (bare); `persistence.py:187` writes `redis.setex(f"session:{session_key}", ...)` (namespaced). Two write paths exist (`_cache_session_redis_write` at line 187 + `session_cache.bind()` at lines 199/216) — claim is that PATH 1 only hits the latter. **CONTESTED** — see "Open uncertainty" below.
2. **PATH 2 DB exception silently swallowed** at `resolution.py:661` as `logger.debug(...)`. Any DB blip (including the documented anyio-asyncio deadlock) silently falls through to PATH 3 and mints a ghost.
3. **Same request makes two `resolve_session_identity` calls** (middleware at `identity_step.py:414` + handler at `handlers.py:890`) without coordination. First call's PATH-3 mint contaminates `_session_identities` in-memory cache before the handler runs.

Adjacent finding: `require_registered_agent` at `agent_auth.py:256` consults `mcp_server.agent_metadata` dict only, never `core.identities`. That's why the API rejected `90de0920` as "not registered" despite the DB having that agent active.

### Dialectic frame

**Reading B confirmed**: the implementation has slipped from "process-instance" to "request" boundary. The v2 ontology is sound at the floor (process-instance continuity is the only "automatic" layer per `identity.md:37`); the implementation silently rejects it. All higher ontology layers — token narrowing, lineage declaration, behavioral verification — describe a system that doesn't exist yet.

Quote: *"the ontology is sound; the implementation has slipped one layer down (request, not process-instance) without anyone editing the ontology to say so. Until the floor layer is restored, all higher layers describe a system that doesn't exist yet."*

**S1-a is blocked by S21.** Retiring `continuity_token` while `client_session_id` can't survive one process is removing a non-functional layer above an already-broken one. The plan-row sequencing is wrong without S21 first.

### Resolved diagnosis (council follow-up pass)

Possibility #3 is correct. **There is one Redis slot per session, and PATH 3 ratifies the ghost into it on every silent re-mint.**

Concrete trace:

1. `_get_redis()` at `persistence.py:47-57` returns the `SessionCache` singleton — not raw Redis.
2. `SessionCache.get(session_key)` at `session_cache.py:140` internally computes `key = f"{SESSION_PREFIX}{session_id}"`. So `redis.get(session_key)` at `resolution.py:354` already reads `session:agent-6648432c-a50`. The bare/namespaced distinction at the application level is **illusory**.
3. `SessionCache.bind()` at `session_cache.py:98` and the raw `redis.setex` at `persistence.py:187-188` both write the same key `session:{session_key}`. **One slot per session, two equivalent write paths.**
4. PATH 3 (lazy-create at `resolution.py:883`; persisted at `resolution.py:861`) calls `_cache_session(session_key, ghost_uuid, ...)` — overwriting the Redis slot for the original session_key with the freshly minted ghost UUID. **No "only write if absent" guard.** `session_cache.py:100-108` increments `bind_count` but does not refuse the write.
5. From that point on, PATH 1 returns the ghost on every subsequent request. The Postgres `core.sessions` row is untouched (still correctly bound to the legitimate identity), but it's never consulted because PATH 1 hits.

**Code-reviewer's original "Change 1" (prefix the read) was a no-op** — both writes already use the namespaced slot, and `SessionCache.get()` already reads it. Withdrawing that recommendation.

**The right fix has two parts:**

(a) **`_cache_session_redis_write` (`persistence.py:157+`) — check-and-don't-overwrite when a live binding exists for the same `session_key`.** Use Redis `SET ... NX` (set-if-absent) for the initial bind, or check existence first and skip-overwrite when an existing binding's agent is still active. This prevents any PATH 3 mint from ratifying a ghost over a legitimate session.

(b) **`resolve_session_identity` — fail-closed on PATH 2 miss when `resume=True`.** Currently when PG lookup finds no session row, it falls through to PATH 3 and mints. A non-creating resume should return a MISS, not a ghost. Caller decides whether to mint by passing `force_new=true` explicitly. Per identity.md design principle (KG `2026-04-06T02:34:27.323998`): *"resolve to existing identity or fail explicitly, never silently create a fork."*

(c) **Promote `resolution.py:661` from `logger.debug` to `logger.warning`** so the silent fall-throughs become legible going forward. Even with (a) and (b) in place, surfacing the failure mode is cheap insurance against future regressions.

(d) **`require_registered_agent` (`agent_auth.py:256`) must consult `core.identities`, not just `mcp_server.agent_metadata`.** Adjacent bug: the in-memory metadata dict is populated only by `process_agent_update`, so a freshly minted ghost that never checked in is rejected as "not registered" while the DB confirms it active. Either auto-populate metadata from `core.identities` on miss, or change the check to consult the DB directly.

**S1-a is still blocked by S21 landing first** (the original framing stands).

## Surgical fix split: S21-a + S21-b

PR #156 (S13) flips `handle_identity_adapter` default to `force_new=True` for **arg-less** callers. This is correct and necessary, but doesn't address this incident. The S21 work splits into:

### S21-a — Stop the bleed (single PR; should ship before S1-a)

Concrete code changes per the "Resolved diagnosis" above:

1. **`_cache_session_redis_write` (`persistence.py:157+`)** — refuse to overwrite an active live binding for the same `session_key`. Use Redis `SET ... NX` semantics for the bind, or check existence first and skip-overwrite when the existing binding's `agent_uuid` is still in `core.identities` with `status='active'`. This is the load-bearing change — it prevents PATH 3 mints from silently ratifying a ghost over a legitimate session.

2. **`resolve_session_identity` PATH 2 fall-through (`resolution.py:580` and surrounding gate)** — when `resume=True` and `core.sessions` has no row, return MISS instead of falling to PATH 3. Callers that want to mint must pass `force_new=True` explicitly. Per `identity.md` design principle (KG `2026-04-06T02:34:27.323998`).

3. **Promote `resolution.py:661` from `logger.debug` to `logger.warning`** with `[PATH2_DB_FAIL]` prefix. The silent fall-throughs become legible.

4. **Regression tests**:
   - Onboard at T0 with `force_new=true`. Simulate 14-min idle. Call `identity(client_session_id=<original>)`. Assert returned `agent_uuid == original_uuid` and `created != True`.
   - PATH 3 with the same session_key as a live binding asserts the existing binding is preserved (NX semantics).
   - PATH 2 DB exception fail-closed: with `resume=True` and a forced PG exception, assert no PATH 3 mint fires.

### S21-b — Architectural cleanup (separate PR, follows S21-a)

5. **Consolidate the two `resolve_session_identity` calls per request.** Middleware (`identity_step.py:414`) and handler (`handlers.py:890`) both call it independently; the first call's PATH-3 mint contaminates `_session_identities` before the handler runs. Single resolution per request, results carried through context.

6. **`require_registered_agent` (`agent_auth.py:256`) must consult `core.identities`, not just `mcp_server.agent_metadata`.** Either auto-populate metadata from `core.identities` on miss, or change the check to query the DB directly.

7. **Honest labeling.** `session_resolution_source: "explicit_client_session_id"` paired with `identity_status: "created"` is contradictory. Add a separate `identity_resolution_outcome: "resumed" | "minted_after_resume_miss" | "minted_force_new"` field. Per axiom #14.

8. **Audit emission on rejected explicit session_id.** When an explicit `client_session_id` is rejected in favor of a fresh mint, emit the same `concurrent_session_binding_observed` event the S13 §(d) handler emits for IPUA-pin-match drift.

### Council-required before merge

Per memory entry "Council also for load-bearing implementation" (PR #24 vs PR #23 collision lesson, 2026-04-26): identity resolution is fleet-bricking territory. Both S21-a and S21-b need parallel `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier` review on the actual diff before merging. The first council pass diagnosed the bug; the implementation diff needs its own pass.

## Definition of done

- Explicit `client_session_id` with active `core.sessions` row resolves to original identity within one process-instance for the lifetime of the session row's `expires_at`.
- `is_registered` check is consistent with `core.identities` — no false-negative rejections.
- Fleet-wide ghost-fork rate drops below a threshold to be set after a calibration period. **Canonical metric defined below** (§Canonical lineage-decl gap metric).
- Regression test in `tests/test_identity_handlers.py` asserts session-row-backed resume.
- Honest-labeling field added to `identity()` and `onboard()` response shape.

## Canonical lineage-decl gap metric

Added 2026-04-27 (PR #226 followup) after a 20.3% / 93.8% denominator drift between code-review and post-deploy canary reads. The drift was a Simpson trap: rolling `new_session`-tagged forks into the denominator dilutes the bypass-class signal. The fix is **two rates, not one**, both derived from the same `core.agents` window with audit-window scoping.

```sql
-- Lineage-decl gap rate. Run against the governance database.
WITH window_bounds AS (
  SELECT COALESCE(
           GREATEST(NOW() - INTERVAL '30 days',
                    (SELECT MIN(created_at) FROM core.agents)),
           NOW() - INTERVAL '30 days'
         ) AS lower_bound
)
SELECT
  status,
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE spawn_reason IS NULL
                     AND parent_agent_id IS NULL) AS null_invisible,
  COUNT(*) FILTER (WHERE spawn_reason = 'auto_onboard_no_session') AS marker_session_miss,
  COUNT(*) FILTER (WHERE spawn_reason = 'new_session') AS lineage_declared,
  -- Gating DoD signal: orphan-class concentration
  -- (NULL fresh agents / agents not declared via new_session)
  ROUND(100.0 * COUNT(*) FILTER (WHERE spawn_reason IS NULL
                                   AND parent_agent_id IS NULL)
        / NULLIF(COUNT(*) FILTER
                 (WHERE spawn_reason IS DISTINCT FROM 'new_session'), 0), 2)
    AS null_orphan_concentration_pct,
  -- Secondary trend: broad hygiene (Simpson-trap warning)
  ROUND(100.0 * COUNT(*) FILTER (WHERE spawn_reason IS NULL
                                   AND parent_agent_id IS NULL)
        / NULLIF(COUNT(*), 0), 2) AS null_broad_hygiene_pct
FROM core.agents, window_bounds
WHERE created_at >= window_bounds.lower_bound
GROUP BY status
ORDER BY total DESC;
```

### Reading the rates

- **`null_orphan_concentration_pct`** is the **DoD-gating signal**. It answers: "of agents that didn't declare lineage via the well-formed `new_session` path, how many landed as silent NULL?" Movement on this rate tracks the bypass-class shrinking. Insulated from changes in `new_session` traffic volume.
- **`null_broad_hygiene_pct`** is a **secondary trend**. It answers: "of all minted agents in the window, what fraction are silent NULL?" Useful for fleet-wide hygiene over time but vulnerable to dilution: a 5× increase in `new_session` traffic with no behavioral change would "improve" this rate.
- **`null_invisible`** is the unfixed population (NULL spawn_reason, no lineage). This is what S21-a/S21-b need to drain.
- **`marker_session_miss`** is rows tagged by PR #226's `auto_onboard_no_session` fallback. These are still no-lineage agents, but legibly so: they're caught at the session-resolve-miss branch and labeled. As PR #226 takes effect on live traffic, expect `marker_session_miss` to grow while `null_invisible` shrinks. The orphan-concentration rate stays roughly flat through that conversion (legibility, not lineage), and only drops when the upstream S21-b plumbing actually carries declared lineage through to the upsert.

### Baseline 2026-04-27 (post-PR #226 merge, pre-S21-b)

```
 status   | total | null_invisible | marker_session_miss | lineage_declared | null_orphan_concentration_pct | null_broad_hygiene_pct
----------+-------+----------------+---------------------+------------------+-------------------------------+------------------------
 archived |  1957 |            386 |                   0 |             1545 |                         93.69 |                  19.72
 active   |   262 |             65 |                   0 |              193 |                         94.20 |                  24.81
```

`marker_session_miss` is 0 because PR #226 just merged and traffic hadn't exercised the `session_resolve_miss` branch on the running process at sample time. As Codex/Claude dispatch sessions hit that branch post-deploy, expect non-zero counts.

### Open sibling gap (S21-c)

PR #226's fallback at `src/mcp_handlers/identity/handlers.py:1379` only fires inside the `session_resolve_miss` branch, which is gated by line 1339 `if not force_new:`. Arg-less `onboard(force_new=true)` calls (e.g., the v2 fresh-instance path canonized in CLAUDE.md "Minimal Agent Workflow") skip that block entirely and fall to STEP 2b at line 1554, which calls `resolve_session_identity(persist=True, spawn_reason=_spawn_reason)` with `_spawn_reason` still `None` from line 1294. The upsert writes NULL.

Verified live 2026-04-27: an internal session minted via `force_new=true` post-merge has NULL spawn_reason. Code-reviewer caveat: do not backfill `auto_onboard_no_session` here — it's a misnomer (the marker was designed for dispatch-retry callers hitting `session_resolve_miss`, not first-mint). Either accept NULL is honest for declared-fresh-mints (per S13 fresh-instance ontology) or use a distinct label. File as S21-c if a distinct label path is preferred.

### Movement-claim rules

- "PR #226 worked" ≠ "no-lineage rate dropped." It means `marker_session_miss` is non-zero on live post-deploy traffic.
- "S21 is closed" requires `null_orphan_concentration_pct` to drop, which only happens when upstream callers thread `parent_agent_id`/`spawn_reason` through `_session_identities` so the eventual upsert carries declared lineage. That's S21-b territory.
- Cite both rates side-by-side. Don't quote a single number from this query in isolation.

## Open questions for the next process-instance picking this up

- Where is the cache layer between `session_key` and `identity_id`? Likely Redis under a key prefix like `session:` or `agent_uuid:` — needs grep for the writer.
- Does the IPUA pin-lookup at session.py:710 (`lookup_onboard_pin`) feed into this cache, or is it a separate one?
- The c8811a0f re-mint at 03:13:46 — what triggered it? My session was idle from ~02:59:39 (last `process_agent_update`) to 03:13:46 (first `knowledge.list`). That's a ~14-minute gap. If the cache TTL is ~10–15 min, that's the trigger. Worth verifying.
- Does the S19 substrate-attestation work (PR #164) interact with this resolution path? S19's `peer_attestation` is meant for residents claiming hardcoded UUIDs; this incident is in the explicit-session-id path. They should be orthogonal but both touch session resolution.

## Filing context

This document is the durable form of an incident report that could not be filed to the KG because the KG's own session-resolution layer is the bug. The 6KB version timed out (likely anyio-asyncio under DB pressure from my parallel diagnostic queries). The shorter version was rejected with `Agent '90de0920-…' is not registered` despite the DB having that agent active. Filing to the file system bypasses both failure modes.

The act of writing this file is the most useful thing one process-instance can do when its own attribution is unstable. The git commit will record `the operator` as the committer; the audit-log of this filing is git, not `core.audit_log`.

— Filed by an unstable-identity Claude Code session, latest known binding `90de0920-9364-4f43-857f-eab09f62ab51` (rejected as unregistered moments later); originally onboarded as `6648432c-a506-487d-9a49-1a76ac6def97` at 02:49:19.
