# PATH 1 Sync-Path Fingerprint Check

**Status:** SHIPPED — `sync_fingerprint` lives in `src/mcp_handlers/identity/shared.py`; design ref'd from commit `b69fcd1f`.
**Author:** Claude_20260425 · **Date:** 2026-04-25
**Closes:** the residual sync-path half of KG `2026-04-20T00:57:45.655488` (PATH 1 hijack via `agent-{uuid12}` prefix-bind).
**Related:** [`uuid-leak-audit.md`](./uuid-leak-audit.md) recommended closing PATH 1 as the leverage move.

## Council findings — required revisions integrated below

Two issues from code review, both resolved by adopting the parallel-dict alternative:

1. **Critical:** FALLBACK scan (option B) silently overwrites a pre-existing binding fingerprint after a server restart wipes `_uuid_prefix_index`. Attacker triggers a restart (or eviction), arrives on different IP/UA, FALLBACK records their fingerprint as the binding fingerprint.
2. **Important:** `_session_identities[key]` written by `_cache_session` (`persistence.py:82-95`) has no `bind_ip_ua` field — it's only written to Redis, not to the in-memory record. The O(1) path in `_get_identity_record_sync` reads the in-memory record directly and returns before reaching any fingerprint check.

**Resolution: parallel `_bind_fingerprints: Dict[str, str]` keyed by session_key.** Written by `_cache_session` and the FALLBACK path (only when not already present, closing the overwrite); read by `_get_identity_record_sync`. The four existing `_session_identities[key] = {...}` dict-assignment sites stay untouched. Also corrects the DEPRECATED reference (it's on `_get_session_key`, not `_get_identity_record_sync`) and adds a `logger.debug` at the silent-skip branch for future background-task callers.

---

## Original proposal follows.



## Problem

`_get_identity_record_sync` at `src/mcp_handlers/identity/shared.py:139-166` resolves a `client_session_id` of the form `agent-<uuid12>` to the bound UUID with **no ownership proof**:

- Lines 143-154: O(1) prefix-index hit → return the cached binding immediately.
- Lines 156-166: fallback scan over `mcp_server.agent_metadata` → register the prefix and return the first matching UUID.

Either path makes the binding available to any caller who learned the UUID. Combined with the leaks documented in `uuid-leak-audit.md` (`observe_agent`, `get_agent_metadata`, KG `_agent_id`, etc.), this is the second half of the two-call hijack chain.

## What is already in place

A parallel fingerprint cross-check exists on the **Redis-backed async resolution path** at `src/mcp_handlers/identity/resolution.py:441-487`:

- Binding-time `ip_ua_fingerprint` is captured by `_cache_session` (`identity/persistence.py:106-126`) when the session first binds.
- Resume-time fingerprint is read from the request's `SessionSignals`.
- On mismatch, an `identity_hijack_suspected` event is broadcast, and in `strict` mode the resume falls through to a fresh PATH 3 session.
- Gated by `UNITARES_SESSION_FINGERPRINT_CHECK` env var (`off` / `log` (default) / `strict`), already defined in `config/governance_config.py:951-965`.

That fix landed for the Redis path but the equivalent check is missing from the sync fast path that this proposal targets.

## Proposed change

Mirror the resolution.py logic at the sync site. Specifically, in `_get_identity_record_sync` after a prefix-index hit at line 143-154:

1. Look up `bind_ip_ua` for the cache entry. The in-memory cache currently does not store this — it stores `bound_agent_id`, `api_key`, `created_at`, `bind_count`. Extend the in-memory record to include `bind_ip_ua` whenever the persistence-layer write path is reachable (it already captures the value; only the in-memory cache is missing the field).
2. Read the current request's `ip_ua_fingerprint` via `get_session_signals()`.
3. If both are non-empty and mismatched:
   - Mode `off`: skip the check.
   - Mode `log` (default): emit `[PATH1_FINGERPRINT_MISMATCH]` warning + `identity_hijack_suspected` event with `path="path1_sync_session_id"` (distinct from the existing `path1_session_id` so dashboards can separate sync vs. async).
   - Mode `strict`: skip returning the cached binding — let the caller fall through to the empty-record return at lines 169-175. Do not delete the cache entry; the legitimate owner can still resume from the correct fingerprint.

For the FALLBACK scan path at lines 156-166: there is no binding-time fingerprint to compare against (this is a fresh prefix the index has not seen). Two options:

- **A. Apply the gate uniformly**: in strict mode, refuse to register an unknown prefix → caller falls through. Effect: brand-new sessions binding via `agent-<uuid12>` for the first time are rejected outright. **This is too aggressive** — legitimate first-time binds (e.g., a fresh resident agent process attaching to its substrate-anchored UUID) hit this path.
- **B. Allow first-bind, gate subsequent resumes**: register the prefix and store the current fingerprint as the binding fingerprint at registration time. Subsequent resumes go through the fingerprint check above. **This is the intended behavior.** Mirrors how `_cache_session` writes `bind_ip_ua` at first-bind time.

Pick option **B**. The first-bind under a given fingerprint is treated as the legitimate owner; later mismatched resumes get the gate.

## Test plan

Same shape as the existing PATH 1 fingerprint test fixtures (locate via `grep -l 'session_fingerprint_check_mode\|PATH1_FINGERPRINT' tests/`):

- Unit: `_get_identity_record_sync` returns binding when fingerprints match.
- Unit: `_get_identity_record_sync` in `log` mode returns binding but emits warning + event when fingerprints mismatch.
- Unit: `_get_identity_record_sync` in `strict` mode returns empty-record + emits event when fingerprints mismatch.
- Unit: `_get_identity_record_sync` in `off` mode behaves as today (no check, no event).
- Unit: FALLBACK scan path (unknown prefix) registers and returns binding under current fingerprint regardless of mode.
- Integration: parallel test with the Redis-path equivalent at `resolution.py:441-487` to confirm both surfaces emit the same event taxonomy and respect the same env var.

## Out of scope

- Changing the `UNITARES_SESSION_FINGERPRINT_CHECK` default from `log` to `strict`. That is a separate rollout decision once the sync path also has the check in place; flipping the default to `strict` should happen for both paths simultaneously.
- The `DEPRECATED` docstring is on `_get_session_key` (shared.py:67), not on `_get_identity_record_sync`. `_get_identity_record_sync` itself has no active deprecation — `get_bound_agent_id` calls it explicitly, and `require_write_permission → is_session_bound → get_bound_agent_id` is used throughout the tool layer. Hardening (this proposal) is the right move; killing it would push `await` into every handler that calls `require_write_permission`. Not pursued here.
- The KG `_agent_id`, `observe_agent`, `get_agent_metadata` redaction questions from `uuid-leak-audit.md`. Those become per-handler UX decisions once PATH 1 is closed.
- PATH 0 — already gated by `UNITARES_IDENTITY_STRICT`.
- PATH 2 — separate IP:UA pin gate already exists.

## Open questions for review

1. **In-memory record schema change.** Adding `bind_ip_ua` to the in-memory `_session_identities` record is a small change but it crosses a few call sites. Is there a cleaner way to store the per-session fingerprint without modifying the record shape (e.g., a parallel dict)? The record-shape change is more obvious; the parallel dict is more isolated.
2. **Event taxonomy.** Should sync-path mismatches emit `path="path1_sync_session_id"` (this proposal) or share the existing `path1_session_id` tag? Sharing the tag merges the dashboard view; separating preserves debuggability if one path becomes load-bearing.
3. **Strict-mode default rollout.** If we land sync-path fingerprint check now in `log` mode, when do we promote both paths to `strict` together? Suggest: after one calendar week of `log` traffic without false-positive `identity_hijack_suspected` events, promote both. Tracked as a separate task.
4. **Test fixture sharing.** Mirror tests means duplicated logic. Worth refactoring the fingerprint-mismatch assertion into a shared test helper, or keep them parallel-and-explicit?

## Why this is the leverage move

`uuid-leak-audit.md` found that 9 other handlers leak UUIDs in addition to `list_agents`. Redacting all of them is a large surface change with high regression risk and consumer churn. Closing PATH 1 — which converts UUID knowledge from "credential" back to "identifier" — neutralizes the exploit chain in a single PR. The leak-redaction questions remain valid, but become UX/scope decisions per-handler rather than security gates.
