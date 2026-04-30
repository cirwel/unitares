# S21-b items 5 + 6 — Council review (2026-04-30)

**Branch:** `s21b/items-5-6-followup`
**State at review:** uncommitted working-tree changes only, 7 files (+364 / -15). No commits on the branch yet.
**Reviewers (parallel dispatch):**
- `dialectic-knowledge-architect` (ontology coherence)
- `feature-dev:code-reviewer` (adversarial bug-hunt)
- `live-verifier` (runtime ground-truth)

**Verdict:** **NOT ready to merge.** One CRITICAL (auth bypass that defeats item 6's primary purpose), one HIGH (unguarded DB await on the hot path; fleet-wide hang risk under documented anyio-asyncio condition), three factual corrections, three ontology tensions worth surfacing.

---

## CRITICAL — must fix before merge

### 1. PATH 0 passthrough silently bypasses item 6's auth gate

**File:** `src/mcp_handlers/middleware/identity_step.py:430-452`

The PATH 0 branch (triggered when `agent_uuid` is supplied and `name in ("identity", "onboard")`) builds an `identity_result` dict with only `agent_uuid` and `source`. `core_identity_status` is never populated — no `_get_agent_status` call is made here, unlike the sticky-cache path (lines 282-294).

`require_registered_agent` (`agent_auth.py:416-419`) then receives a dict with no `core_identity_status` key, so `_identity_result_status` returns `None`. The fallback at `agent_auth.py:425` reads from in-memory `agent_metadata` — the exact stale-positive source S21-b §2 was designed to retire.

**Trigger:** any `identity` or `onboard` call that supplies a bare `agent_uuid` (with a valid `continuity_token` when strict mode is on, or any UUID when strict mode is off / log). This is the legitimate substrate-anchored agent reconnection path — Lumen, Vigil, Sentinel, Watcher, Chronicler.

**Impact:** an archived agent whose `agent_metadata` row is still active in memory passes auth on its next `identity`/`onboard` PATH 0 call. **Item 6 is defeated for its highest-risk path.** Substrate-anchored residents are exactly the population for which `core.identities.status` divergence matters most (long-lived dict entries, status changed by external admin action).

**Fix:** call `_get_agent_status(_direct_uuid)` inside the PATH 0 branch with the same try / except guard used at sticky-cache lines 282-286, then attach to `identity_result["core_identity_status"]` before calling `_attach_middleware_identity`.

---

## HIGH — must fix before merge

### 2. New `_get_agent_status` await on sticky-cache path has no anyio-asyncio deadlock protection

**File:** `src/mcp_handlers/middleware/identity_step.py:282-286`

```python
try:
    from ..identity.handlers import _get_agent_status
    core_status = await _get_agent_status(cached.agent_uuid)
except Exception as e:
    logger.debug(f"[STICKY] core.identity status lookup failed: {e}")
```

Naked `await` on a DB call. `CLAUDE.md` "Known Issue: anyio-asyncio Conflict" explicitly mandates one of three patterns: cached data, `run_in_executor`, or `asyncio.wait_for` with a tight timeout. The Redis recovery call at `identity_step.py:176` correctly uses `asyncio.wait_for(..., timeout=_REDIS_RECOVERY_TIMEOUT)`. This one doesn't.

**Trigger:** every sticky-cache hit (request 2+ for any agent that has gone through one full resolution cycle). Hot path for all established agents.

**Impact:** under the documented anyio-asyncio deadlock condition (which produced the existing wait_for guards on Redis and asyncpg paths), this await hangs indefinitely, blocking the MCP handler loop for every request from any cached agent. **Fleet-wide hang on every tool call from any agent that has connected once.**

**Fix:** wrap with `asyncio.wait_for(..., timeout=_REDIS_RECOVERY_TIMEOUT)` matching the pattern at line 176. On `TimeoutError`, degrade to `core_status = None` (status gate skips to in-memory fallback, which is the pre-patch behavior and safe for the timeout case).

---

## Factual corrections (live-verifier ground-truth)

### 3. `archived_at` column does not exist on `core.identities`

The actual column is `disabled_at`. Verified via `\d core.identities` and via `agent(get)` API which returns `"archived_at": null` for an agent whose status is `archived` — confirming the field is mapped from elsewhere, not from a column of that name. Any test or query in the diff (or in tests added by the diff) that references `archived_at` directly on `core.identities` will fail at runtime.

### 4. `identity_result.get("status")` fallback in `agent_auth.py:_identity_result_status` is dead code

Neither the current running server's identity_result shape nor the post-merge shape carries a bare `"status"` key. Current shape carries no such key in internal identity_result dicts; post-merge uses `core_identity_status`. The `or identity.get("status")` fallback is inert but misleading — remove it or rename to the actual key.

Confirmed by direct calls against the running server (port 8767):
- `identity()` response: `success, uuid, agent_id, display_name, client_session_id, session_resolution_source, continuity_token_supported, identity_status, bound_identity, identity_resolution_outcome, ownership_proof_version, continuity_token, resumed, auto_bound`. Note: `identity_status` (outer API) is unrelated to the internal `identity_result` dict's status field; the internal dict has no `status` key.
- `onboard(force_new=true)` response: `success, welcome, uuid, agent_id, display_name, is_new, client_session_id, session_resolution_source, continuity_token_supported, date_context, next_step, identity_resolution_outcome, ownership_proof_version, continuity_token`.

### 5. `session_resolution_source` is a per-request ContextVar, not persisted

`session_resolution_source` lives as `_session_resolution_source: ContextVar[Optional[str]]` in `context.py`, emitted per-request only. It is NOT stored in `core.identities.metadata`. Verified via `psql ... "SELECT DISTINCT metadata->>'session_resolution_source' FROM core.identities WHERE created_at >= '2026-04-23'"` — empty result. Values observed in live calls: `explicit_client_session_id_scoped`, `explicit_client_session_id`, `ip_ua_fingerprint`. Any post-merge test asserting DB-side values will need to probe API response, not DB.

---

## Ontology tensions (architect — not blockers; surface as follow-up)

### 6. `_middleware_*` keys threaded through `arguments` dict (layering leak)

`arguments` is the agent-supplied JSON payload — a caller-shaped contract. The diff stores middleware-internal handoff data (`_middleware_identity_session_key`, `_middleware_identity_result`, `_core_identity_status`) into it (`identity_step.py:35-66`, `handlers.py:775-790`, `agent_auth.py:33-56`). The leading underscore is a Python convention, not a transport boundary.

`_clear_middleware_identity` scrubs caller-provided values at the start of `resolve_identity` (`:251`) and tested at `test_dispatch_ephemeral_identity.py:230-256`. The mitigation is correct, but the invariant "no caller can spoof these keys" lives entirely in one scrub call. The right carrier is `ctx` — which is already used in parallel (`ctx.identity_result` is set at `:301, :449, :592`). The reason `arguments` is being used is that handlers receive `arguments`, not `ctx`.

**Will rot when:** a future tool defines a legitimate `arguments` field starting with `_`, or `arguments` is logged / serialized into an audit trail / KG entry. Both are recoverable but neither is structurally prevented.

### 7. `core_identity_status` is a layer-collision name

`identity.md` v2 "Five layers" splits process-instance / substrate / role / memory / behavioral continuity. **There is no "identity status" in v2.** What `core.identities.status` actually tracks is agent-row lifecycle on the substrate layer — active / paused / archived / deleted. That is a substrate-continuity-layer fact about whether the persisted row is still in service, not an "identity" fact.

`core_identity_status` (`resolution.py:624, 773, 917, 1051`; `agent_auth.py:21-29`) elevates a substrate-layer signal into the result of a resolution operation, naming it as if "identity" itself has a status. Under v2 this is the same conflation v1 made.

**Two readings live simultaneously:**
- *Schema-prefix reading* (what shipped): `core_` is a namespace pointing at the `core.identities` table.
- *Ontological reading* (what a future reader will adopt): "identity" has a status.

Honest names: `core_agent_row_status` or `core_lifecycle_status`. **Highest naming-rot risk in the diff.**

### 8. `identity` + `onboard` lumped into `read_only_diagnostic_tools` set name (cosmetic)

`identity_step.py:500-507` extends the no-auto-mint set from `read_only_diagnostic_tools` to also include `identity_lifecycle_tools = {"identity", "onboard"}`. Behavior is correct — closes a real previously-hidden double-mint path where `onboard` calling through middleware on session-resolve-miss would auto-mint, then `handle_onboard_v2` would also try to resolve / mint with different guarantees.

The naming tension: lifecycle tools are not read-only and not diagnostic. The unified concept is "tools that own their own identity decision" or "tools where middleware auto-mint is unsound." Worth a comment or a third predicate name.

---

## Findings flagged below threshold (not blockers)

- **Pre-existing `require_registered_agent` ephemeral / dispatch-minted bypass** carried through the refactor. Not a regression here; flagged for awareness.
- **`_REGISTERED_AGENT_ALLOWED_STATUSES` `"paused"` dual-use** with `check_agent_can_operate` is undocumented and inconsistent. `paused` is allowed at registration but blocked at the circuit-breaker. Future statuses (`degraded`, `recovering`) will silently fail-closed against the allowlist intent. Not introduced by this diff but exposed.

---

## Test gaps

- **PATH 0 archived-agent path** — would have caught CRITICAL #1. Add: `agent_uuid` + valid token + `core.identities.status='archived'` → expect rejection.
- **Sticky-cache `_get_agent_status` timeout/failure** — would have caught HIGH #2. Add: simulated DB timeout → expect `core_status=None`, in-memory fallback, no hang.
- **`_REGISTERED_AGENT_ALLOWED_STATUSES` unknown status** — verify fail-closed on `"degraded"` or any unexpected value.

---

## Recommended fix order

1. **CRITICAL #1** — PATH 0 `_get_agent_status` call (~5 lines + test)
2. **HIGH #2** — `asyncio.wait_for` wrap on sticky-cache `_get_agent_status` (~3 lines + test)
3. **Correction #3** — `archived_at` → `disabled_at` everywhere it appears in the diff or its tests
4. **Correction #4** — drop or rename `identity_result.get("status")` fallback
5. **Ontology #7** (optional but high naming-rot risk) — rename `core_identity_status` → `core_agent_row_status` (or commit to the schema-prefix reading via comment)
6. Tensions #6, #8 — comments / future cleanup; do not block merge

After fixes: re-run council pass on the changed surfaces (#1, #2, and any rename for #7), then ship.

---

## Files referenced

- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/src/mcp_handlers/middleware/identity_step.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/src/mcp_handlers/identity/handlers.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/src/mcp_handlers/identity/resolution.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/src/mcp_handlers/support/agent_auth.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/tests/test_dispatch_ephemeral_identity.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/tests/test_identity_handlers.py`
- `/Users/cirwel/projects/unitares/.worktrees/s21b-items-5-6-followup/tests/test_mcp_utils.py`
