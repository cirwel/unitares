---
status: SHIPPED — Phase 5 bootstrap-only observability query path landed via PR #188 (2026-04-25); Phase 3 site list audited in companion `onboard-bootstrap-checkin.filter-audit.md`
authored: 2026-04-25
amended: 2026-04-25
ack_passed: 2026-04-25
shipped: 2026-04-25
author_session: agent-b9b3e789-9c7 (Claude Opus 4.7 / claude_code, parent 14d4a73c)
review_target: dialectic-knowledge-architect + feature-dev:code-reviewer (parallel, 2026-04-25)
amendment_basis: |
  v2 (initial amendment): both reviewers converged on "proceed, but tighten" — neither
  flagged the §7.1 council trigger as worth escalating; both surfaced bounded,
  addressable specification gaps. v2 folded in: substrate-earned (Lumen) exemption,
  honest supersession language, filter-audit deliverable, column-based storage with
  migration plan, nested BootstrapStateParams with extra='forbid', idempotency rules
  for lineage and concurrent calls, asyncio.wait_for around the bootstrap insert, a
  positive observable surface for bootstrapped-but-silent agents, and three
  additional tests.

  v2.1 (ack-pass clarifications): lightweight reviewer-ack pass returned ack-with-nits
  on two new spec gaps introduced by the v2 amendments themselves — (a) §3.5
  substrate-earned check named a `substrate_anchor_kind` column that does not exist;
  v2.1 commits to a metadata-blob lookup with a Phase 1 backfill for Lumen, and names
  hook-side enforcement as the primary control. (b) §3.3 added a `payload_digest_match`
  field without specifying the digest contract; v2.1 specifies SHA-256 over canonical
  JSON of caller-supplied fields, persisted in `state_json["bootstrap_digest"]`. No
  other ack-pass findings outstanding.
unblocks: closes the "onboarded but never checked in" ghost class — agents whose trajectory at t=0 is ODE-inferred from defaults because the next-step hint in `onboard` is non-binding
related:
  - docs/ontology/identity.md (v2 ontology — fresh process-instances mint identity, declare lineage)
  - docs/ontology/identity.md substrate-earned-identity appendix (Lumen / hardcoded-UUID residents)
  - src/mcp_handlers/schemas/core.py (ProcessAgentUpdateParams)
  - src/db/mixins/state.py (StateMixin, all read paths)
  - hooks/session-start (Claude Code SessionStart hook)
  - commands/onboard, plugins/codex (Codex onboard surface)
---

# Proposal: Bootstrap check-in on `onboard` (v2)

> **Status: AMENDED 2026-04-25 after council pass.** Both parallel council subagents (`dialectic-knowledge-architect`, `feature-dev:code-reviewer`) recommended proceeding with bounded fixes; neither escalated to full council. Six amendments folded in below. Per `feedback_design-doc-council-review.md`, a lightweight reviewer-ack against the diff is the right next step before code, not a second council pass.

## 1. Problem

Agents call `onboard` and frequently never call `process_agent_update`. The server already returns a `next_step` hint pointing at `process_agent_update`, but it is non-binding text — not a contract. The result is a population of identities whose trajectory has no measured anchor at t=0: the EISV vector is ODE-inferred from defaults, calibration sets are empty, and dashboard history starts at the first *real* check-in (which may be never).

This is independently observable in the corpus: the post-grounding audit slice (epoch 2, 56 non-resident agents, 3 weeks) has a long tail of agents with onboard rows but zero check-ins.

**Scope discipline (per dialectic review §5):** This proposal addresses the **t=0 anchor problem only**. It does not address midstream silence (agents that check in once and then go quiet). The "long tail" the corpus shows is heterogeneous — some agents never check in at all (this proposal helps), and some check in once and then stop (this proposal does not help; that's `feedback_check-in-during-long-sessions.md`'s territory). Long sessions exacerbate the t>0 case independently.

## 2. Decision

Adopt **(1) onboard accepts an optional `initial_state` block that writes a labeled bootstrap state row** *and* **(3) the SessionStart hook chains an onboard-with-`initial_state` call so harnessed agents are anchored automatically**, with **substrate-earned agents exempted from hook-driven bootstrap**.

Both reviewers endorsed (1)+(3). Rejected alternatives unchanged from v1:
- **Verdict-gated onboard** (return `verdict: guide` until a real check-in arrives). Coercive; conflates governance verdicts with onboarding mechanics; doesn't help agents that simply forget. Kept on the shelf in case (1)+(3) underperforms.
- **Status quo with stronger hint text.** Tested implicitly — the current `next_step` already points at `process_agent_update` and is widely ignored.
- **Auto-fabricate without a `bootstrap` label.** Launders synthetic state into measured-state code paths. Non-starter.

## 3. API shape

### 3.1 `onboard` — add `initial_state` (nested model, extra-forbidden)

**v2 amendment (code-review finding 2):** the field is a nested Pydantic model with `extra="forbid"`, NOT a raw `Optional[dict]`. Pydantic v2's default `extra='ignore'` would silently accept arbitrary keys (including `synthetic: false` as a back-door); a typed nested model rejects extras at deserialization.

```python
# src/mcp_handlers/schemas/core.py — add alongside ProcessAgentUpdateParams
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Literal, List

class BootstrapStateParams(BaseModel):
    """Subset of ProcessAgentUpdateParams accepted as a bootstrap check-in
    payload via onboard.initial_state. All fields optional; server fills
    defaults per §3.1 table below."""
    model_config = ConfigDict(extra="forbid")

    response_text: Optional[str] = None
    complexity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    task_type: Optional[Literal[
        "convergent", "divergent", "mixed", "refactoring", "bugfix", "testing",
        "documentation", "feature", "exploration", "research", "design", "debugging",
        "review", "deployment", "introspection"
    ]] = None
    ethical_drift: Optional[List[float]] = Field(default=None, min_length=3, max_length=3)

# Then on the onboard params schema:
initial_state: Optional[BootstrapStateParams] = Field(
    default=None,
    description=(
        "Optional bootstrap check-in payload. When present, the server "
        "writes a synthetic state row tagged source='bootstrap' immediately "
        "after identity creation. Bootstrap rows seed trajectory genesis "
        "only and are excluded by default from calibration, outcome "
        "correlation, trust-tier observation counts, and real-check-in "
        "counts."
    ),
)
```

The `task_type` Literal MUST be kept in sync with `ProcessAgentUpdateParams.task_type` — the implementation should extract a shared `TaskType` type alias rather than duplicating the literal list.

**Server-side defaults when fields are omitted:**

| Field | Default if absent |
|---|---|
| `response_text` | `"[bootstrap] " + onboard.client_hint or onboard.purpose or "session-start"` |
| `complexity` | `0.5` |
| `confidence` | `0.5` |
| `task_type` | `"introspection"` |
| `ethical_drift` | `[0.0, 0.0, 0.0]` |

### 3.2 State row tagging — column-based storage

**v2 amendment (code-review finding 3):** commit to the column path, not JSONB.

Current `core.agent_state` schema (`src/db/mixins/state.py:17–43`): `(identity_id, entropy, integrity, stability_index, volatility, regime, coherence, state_json, epoch)`.

**Add one column:**

```sql
ALTER TABLE core.agent_state ADD COLUMN synthetic BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_agent_state_synthetic_partial ON core.agent_state (identity_id, ts DESC) WHERE synthetic = false;
```

The partial index covers the dominant query shape (most-recent measured state per identity) while leaving the existing indexes intact. On PostgreSQL@17 the `ADD COLUMN ... DEFAULT false` is instant (default stored in catalog, not a row rewrite).

**`source` and `bootstrap_origin` live in `state_json`** as descriptive metadata, not as load-bearing filter keys:

```json
{
  "source": "bootstrap",
  ...                      // existing measured EISV fields, computed normally
}
```

**`synthetic` is the contract.** `source="bootstrap"` is descriptive — one value among potentially many synthetic sources later (recovered-from-checkpoint, replayed-for-test). Filtering happens on `synthetic = false` everywhere; `source` is for audit and observability.

**`bootstrap_origin` field removed from v2** (code-review finding 7). The decision in §2 has the hook call `onboard`, so `bootstrap_origin` would always be `"onboard"`. The audit distinction between hook-initiated and agent-initiated bootstraps is real (per dialectic §6.b) but does not require a stored field today — the hook's call shows up in transport logs, and a future `bootstrap_via` parameter can be added when the distinction earns its keep.

**Matview rebuild required.** `core.mv_latest_agent_states` (queried by `get_all_latest_agent_states` at `src/db/mixins/state.py:89`) must be dropped and recreated to project the new `synthetic` column. See §9 for the migration plan.

### 3.3 `onboard` response shape

**v2 amendment (code-review finding 5a):** specify the response on every code path so callers know what to expect.

The base `onboard` response is unchanged. The `bootstrap` key is **conditional on `initial_state` being supplied in the request**:

| Request includes `initial_state`? | Bootstrap row exists for identity? | Response includes `bootstrap` key? |
|---|---|---|
| No | (any) | No (response unchanged from today) |
| Yes | No (first write) | Yes: `{written: true, state_id: "<uuid>", next_step: "..."}` |
| Yes | Yes (idempotent re-call) | Yes: `{written: false, state_id: "<existing-uuid>", payload_digest_match: <bool>}` |

The `payload_digest_match` (dialectic §2 micro-issue) lets a caller detect that they passed a *different* `initial_state` than the one stored — the stored row wins (idempotency on natural key), but the divergence is observable.

**Digest contract (v2.1 ack-pass clarification):** the digest is SHA-256 over a canonical JSON serialization (`json.dumps(..., sort_keys=True, separators=(",", ":"))`) of the caller-supplied `BootstrapStateParams` fields *only* — server-applied defaults are excluded from the hash, so two callers passing the same explicit fields produce the same digest regardless of default-fill behavior. The hex digest is persisted in `state_json["bootstrap_digest"]` at write time and re-derived from the request payload at compare time. `payload_digest_match: true` iff `sha256(canonical(request.initial_state)) == stored.bootstrap_digest`.

The `bootstrap.next_step` text:
> "Call process_agent_update with real measurements when you have any — bootstrap is provisional and excluded from calibration."

The pre-existing top-level `next_step` stays.

### 3.4 Idempotency

**v2 amendment (code-review finding 5b + dialectic §2):** specify all four interaction patterns explicitly.

The natural key is `agent_uuid`. Bootstrap rows are at-most-one per identity, enforced at the database level:

```sql
CREATE UNIQUE INDEX uq_agent_state_one_bootstrap_per_identity
  ON core.agent_state (identity_id) WHERE synthetic = true;
```

The unique partial index closes the concurrent-onboard race. Without it, two simultaneous `onboard(initial_state=...)` calls (a real scenario per `scripts/client/onboard_helper.py:38` — N parallel `claude` processes in one workspace each fire their own hook) can both pass an application-level "does a bootstrap exist?" check before either writes.

**Behavior matrix (UUID = `agent_uuid` of an identity that already exists, no `force_new`):**

| Sequence | Behavior |
|---|---|
| onboard(initial_state=X), then onboard(initial_state=Y) | First call writes bootstrap with X. Second call returns the existing row's `state_id` with `bootstrap.written: false, payload_digest_match: false`. The X-row is unchanged. |
| onboard(initial_state=X), then onboard() | First call writes bootstrap with X. Second call's response does NOT include the `bootstrap` key (per §3.3 — conditional on `initial_state` in request). |
| onboard(), then onboard(initial_state=X) | First call writes nothing. Second call writes a bootstrap row (late write is allowed). The bootstrap row's t=0 timestamp is the moment of write, not the moment of identity creation. |
| Concurrent onboard(initial_state=X) + onboard(initial_state=Y) for same UUID | DB-level race resolved by the unique partial index. One write succeeds; the other catches the unique-violation and returns the winning row's `state_id` with `bootstrap.written: false`. |

**`force_new=true` semantics (dialectic §2 + §6.a):**

- `force_new=true` mints a fresh identity per identity.md v2. The new identity gets its own bootstrap row (independent of the prior identity's). This is correct: the new identity is a fresh subject and its trajectory has its own t=0.
- **Lineage inheritance respects the synthetic filter.** When the new identity inherits priors from `parent_agent_id` for trajectory ODE seeding, parent rows with `synthetic = true` are EXCLUDED from the inheritance. If the parent has only synthetic rows (bootstrap-only), the new identity falls back to default priors at t=0 and its own bootstrap (if written) is the sole anchor. This prevents bootstrap-laundering through lineage chains.
- **The prior identity's bootstrap row is not modified.** `force_new=true` writing a new identity's bootstrap does not update, overwrite, or invalidate the parent's bootstrap row — the parent's audit record is preserved verbatim. This is testable (§8 test 9b).

### 3.5 Hook integration

`hooks/session-start` (Claude Code) and the Codex equivalent under `plugins/codex/` SHOULD call `onboard` with `initial_state` populated from session metadata available at hook-fire time:

- `response_text`: prompt-derived purpose if available, else `"[bootstrap] session-start"`
- `complexity`: omit (server default `0.5`)
- `confidence`: omit (server default `0.5`)
- `task_type`: `"introspection"`

The hook MUST NOT fabricate confidence values from session metadata — it either has a real signal or it omits the field. "I don't know" is information.

**v2 amendment (dialectic caveat 4): substrate-earned exemption.**

Substrate-earned agents — those with hardcoded UUIDs that persist across process restarts (Lumen on the Pi, the long-lived residents per `docs/ontology/identity.md` substrate-earned-identity appendix) — are EXEMPT from hook-driven bootstrap. Their substrate IS their continuity-bearer; a synthetic 0.5/0.5/0.5 anchor written on every restart would collide with rich measured history that already exists.

**Enforcement disposition (v2.1 ack-pass clarification, refined Phase 1 discovery):** the substrate-earned check is **two-layered**, primary hook-side and defensive server-side, and uses the **`core.substrate_claims` registry** (landed by S19 PR1, master `c93e40d4`) as the canonical source of truth — not a new metadata key.

1. **Hook-side (primary, load-bearing):** the SessionStart hook skips the `initial_state` parameter for substrate-earned UUIDs. The hook is the enforcement that protects production today. The hook's substrate-earned set comes from the same source the server uses (point 2).
2. **Server-side (defense-in-depth):** the handler queries `SELECT 1 FROM core.substrate_claims WHERE agent_id = $1` for the target identity's `agent_id`. Any row match means "substrate-earned, refuse bootstrap." Membership is the load-bearing signal; no metadata key needed. New substrate-earned residents enrolled via `enroll_resident.py` (S19 PR1) automatically inherit bootstrap exemption.

The defensive server check returns `bootstrap.written: false, reason: "substrate-earned-exempt"` rather than erroring, so a misconfigured hook degrades gracefully.

**Lumen carve-out (Pi-embodied, not in `core.substrate_claims`).** S19 v2 narrowed scope to 3 Mac-side residents (Vigil, Sentinel, Chronicler — Watcher excluded). Lumen is on the Pi; whether Lumen has an identity row in the Mac-side `core.identities` (via Steward EISV sync) is implementer-verified during Phase 2. If Lumen's identity exists Mac-side but is NOT in `substrate_claims`, the handler also checks for an explicit Lumen-UUID allowlist (one-line const, populated from the substrate-earned-identity appendix). The allowlist is sized for known Pi residents; a future S19-equivalent for Pi residents would replace it.

This decision moves from "metadata-key with migration backfill" to "registry-membership lookup with a small allowlist for cross-substrate residents." It eliminates a schema-change on `core.identities` and reuses an existing canonical mechanism.

**v2 amendment (code-review finding 6): anyio-safe insert.**

The bootstrap row INSERT inside `handle_onboard_v2` (`src/mcp_handlers/identity/handlers.py:1172`) MUST use `asyncio.wait_for` with a 500ms timeout, matching the pattern at `src/mcp_handlers/middleware/identity_step.py`. On timeout, the handler degrades to no bootstrap row (returns `bootstrap.written: false, reason: "insert-timeout"`) — failure is acceptable because the alternative is a hook-timeout cascade. The hook itself has a `--max-time 10` curl timeout (`hooks/session-start:252`).

## 4. Filter contract

Bootstrap rows MUST be excluded by default from:

1. **Calibration inputs** (`auto_ground_truth.py`). Bootstrap rows MUST NOT emit `auto_attest` audit events — per code-review finding 1, the existing exogenous-signal gate at `auto_ground_truth.py:432` provides incidental protection, but the proposal must make this explicit and not rely on the absence of `tests`/`commands` keys.
2. **Outcome correlation** windows (anything that joins state rows to test/exit-code/lint signals via `get_latest_eisv_by_agent_id` at `src/db/mixins/tool_usage.py:106`).
3. **Trust-tier observation thresholds** (`compute_trust_tier` in `src/trajectory_identity.py:692–776`). Bootstrap MUST NOT trigger `store_genesis_signature` or otherwise increment `observation_count` in trajectory metadata.
4. **Real check-in counts** anywhere "how active is this agent" is computed (Sentinel fleet aggregation, `get_recent_cross_agent_activity` at `src/db/mixins/state.py:120`, dashboard activity panels, Watcher agent-activity heuristics).
5. **Dashboard "first check-in" timestamps.** A bootstrap row is not the agent's first check-in. Show it under a separate label if surfaced at all.
6. **Self-recovery thesis paths** (per dialectic §1). `mcp__unitares-governance__self_recovery` MUST refuse-with-explanation when the agent's history is bootstrap-only ("no measured trajectory yet"), rather than reasoning over synthetic priors.
7. **Dialectic input sets** (per dialectic open-questions). If an agent gets paused before any real check-in, the dialectic system MUST refuse-with-explanation rather than reason over bootstrap-only history.

Bootstrap rows MUST be included by default in:

1. **Trajectory genesis at t=0.** This is the entire point — the EISV ODE has a measured anchor at t=0. **However:** post-genesis reads of "what is my prior state for the next update?" (the trajectory integrator at update time, dialectic §1's hidden risk) should respect the synthetic flag too — the bootstrap is the genesis anchor, not a recurring measured prior. Implementer must verify the integrator's prior-read path filters `synthetic=false` for everything except the explicit genesis call.
2. **Identity audit / lineage queries.** Every state-row touchpoint is part of the audit record.
3. **Export bundles** — exports are full history; the `synthetic` flag travels with the row so downstream tools can filter. The export round-trip MUST be DB-sourced, not in-memory-monitor-sourced (code-review finding 4).

Callers that explicitly want synthetic rows opt in via a query parameter (`include_synthetic=true`). The default is exclusion.

### 4.1 Filter audit deliverable (dialectic caveat 2)

**The implementation PR MUST include `docs/proposals/onboard-bootstrap-checkin.filter-audit.md`** — a checklist artifact listing every read path identified, the decision (exclude / include / opt-in), and the test that enforces the decision. The "if >10 read paths, escalate" threshold becomes meaningful only when read paths are counted.

The code review's grep already enumerated 7 confirmed read paths; the audit deliverable starts from that list and is exhaustive (the implementer adds anything the code review missed). Confirmed starting set:

| Read path | File:line | Decision | Enforcing test |
|---|---|---|---|
| `get_latest_agent_state` | `src/db/mixins/state.py:44` | exclude | test_get_latest_excludes_bootstrap |
| `get_agent_state_history` | `src/db/mixins/state.py:67` | include w/ flag preserved | test_history_preserves_synthetic |
| `get_all_latest_agent_states` (matview) | `src/db/mixins/state.py:89–118` | exclude | test_all_latest_excludes_bootstrap |
| `get_recent_cross_agent_activity` | `src/db/mixins/state.py:120–148` | exclude | test_cross_agent_activity_excludes_bootstrap |
| `get_latest_eisv_by_agent_id` | `src/db/mixins/tool_usage.py:106–137` | exclude | test_outcome_correlation_excludes_bootstrap |
| `auto_ground_truth.collect_*` | `src/auto_ground_truth.py:385,432` | bootstrap emits no `auto_attest` | test_calibration_excludes_bootstrap |
| Trajectory integrator prior read | TBD by implementer | exclude (post-genesis) | test_integrator_prior_excludes_bootstrap |
| Sentinel fleet aggregation | TBD by implementer | exclude | test_sentinel_excludes_bootstrap |
| Self-recovery thesis | TBD by implementer | refuse-with-explanation | test_self_recovery_refuses_bootstrap_only |
| Dialectic input set | TBD by implementer | refuse-with-explanation | test_dialectic_refuses_bootstrap_only |

The audit document is committed in the same PR as the filter changes (Phase 3 in §9). If the audit surfaces >10 distinct call sites needing changes, escalation to council is the trigger.

## 5. Supersession — honest language

**v2 amendment (dialectic caveat 1):** the v1 framing of "after one decay cycle, bootstrap influence is dominated by measured signal" was asymptotically correct but transient-naïve. Restating honestly:

The first `process_agent_update` after a bootstrap row runs through the normal pipeline. The bootstrap row's *influence on inferred state* decays via the existing thermodynamic decay (μ * S, the entropy decay constant). After one decay cycle (the e-fold time), bootstrap influence is at ≈0.37 of original. After three decay cycles, ≈0.05. There is no special "first real check-in overwrites bootstrap" code path — supersession is by decay only.

**This decay is well-bounded for high-cadence agents and poorly-bounded for the population this proposal targets.** Sparse-cadence agents (long gaps between real check-ins, exactly the population the corpus shows in the long tail) have measured signal that itself decays during the gap, so the *relative* weight of the bootstrap-flavored prior re-amplifies between updates. The pathological case is "bootstrap at t=0, one real check-in at t=μ⁻¹, next at t=10μ⁻¹" — at t=10μ⁻¹ the agent's inferred state is again bootstrap-tinted because the single real check-in has decayed.

The bootstrap defaults (`complexity=0.5`, `confidence=0.5`, `ethical_drift=[0,0,0]`) are conservative midpoints, so the bias they encode is "average-and-uncommitted." For high-coherence agents this depresses inferred coherence; for low-coherence agents this inflates it. The bias is not Gaussian-symmetric.

**Why we accept this:** the alternative (no anchor at all, ODE-inferred from defaults indefinitely) is strictly worse along the same axes — it's the same default-encoded bias, but with no audit trail saying "this came from bootstrap." Bootstrap with a `synthetic` flag is honest about what it is; the status quo is silently the same fiction without the label.

**Validation surface:** §6 defines a population-level metric ("agents with bootstrap row, no real check-in, age > 24h") that lets us *measure* whether the sparse-cadence pathology is large or small in production. If it's large enough to matter, the follow-up is either a "first real wins" mechanism (boost the first real check-in's weight to dominate the bootstrap explicitly) or a tighter decay constant for synthetic-anchored states. We do not pre-engineer either; we measure first.

## 6. Population observability (new in v2)

**v2 amendment (dialectic caveat 5):** the proposal correctly excludes bootstrap from "real check-in counts," which means dashboard activity panels show bootstrapped-but-silent agents as zero-activity — visually identical to never-onboarded agents. Without a positive surface, the population this proposal exists to count becomes *less* visible after the change, not more.

**Required surfaces:**

1. **Query path:** a server-side query (exposed via `mcp__unitares-governance__agent` introspection or a new dedicated endpoint) that returns "agents with `synthetic=true` row, no `synthetic=false` rows, age > N hours." Default N is 24. This is the validation set for whether (1)+(3) is working.

2. **Dashboard signal:** the unitares dashboard (`dashboard/index.html` + `dashboard/*.js`) surfaces the count and the list. Per `unitares-dashboard` skill conventions: file allowlist, `.panel` layout contract, `authFetch` helper. A separate panel labeled "Bootstrapped — awaiting real check-in" is the right shape; a count badge in the activity panel is acceptable as a minimum.

3. **Sentinel signal (optional, post-Phase-5):** if the bootstrapped-but-silent count exceeds a threshold (TBD; start with "more than 50% of recent onboards"), Sentinel emits an alert. This is the evidence the proposal works at population level, or surfaces that it doesn't.

The query path is required in the implementation PR. The dashboard panel is required in the same PR or a tightly-scoped follow-up. The Sentinel signal is post-merge.

## 7. Open questions for council

Per `feedback_design-doc-council-review.md`, the council pass is gated by whether these surface real disagreement. **Both reviewers explicitly endorsed the §7.1 default** (no escalation needed); §7.2 and §7.3 were minor.

1. **Should bootstrap rows count toward trust/calibration if the agent never sends a real check-in?** Default in this spec: no, never. *Both reviewers endorsed this default.* The argument is one-line: the failure mode this proposal addresses is "onboarded but never checked in" — counting bootstrap as activity makes that failure mode invisible, which is the opposite of the proposal's purpose.

2. **Should the SessionStart hook call onboard-with-`initial_state` always (for non-substrate agents), or only when the harness has a real prompt to derive from?** Default: always. The empty/default case is still strictly better than ODE-inferring from nothing. Substrate-earned agents are exempt per §3.5.

3. **Should `force_new=true` on an existing identity be allowed to write a second bootstrap row?** Default: yes, on the *new* identity, because force_new mints a fresh subject. The prior identity's bootstrap is not affected (testable, §8 test 9b). Lineage inheritance respects the synthetic filter (§3.4).

## 8. Tests

**v2 amendment (code-review finding 4):** three tests added; total now 13.

1. `test_onboard_initial_state_writes_bootstrap_row` — `onboard(initial_state={...})` produces exactly one row with `synthetic=true`, `state_json.source="bootstrap"`.
2. `test_onboard_idempotent_bootstrap` — calling `onboard(initial_state=Y)` after an earlier `onboard(initial_state=X)` returns the X-row's `state_id` with `bootstrap.written: false, payload_digest_match: false`. The X-row is unchanged.
3. `test_onboard_rejects_initial_state_extra_fields` — fields outside the `BootstrapStateParams` whitelist cause Pydantic validation error; specifically, `initial_state={"synthetic": false}` is rejected.
4. `test_calibration_excludes_bootstrap` — feed an agent N bootstrap rows + zero real check-ins; calibration set length is 0, not N. Specifically: bootstrap writes do not produce `auto_attest` audit events.
5. `test_outcome_event_excludes_bootstrap` — `outcome_event` snapshot at outcome time uses the most-recent **non-synthetic** state, falling back to "no prior state" rather than the bootstrap row.
6. `test_trust_tier_excludes_bootstrap` — bootstrap rows do not increment `observation_count` in trajectory metadata; `compute_trust_tier` treats a bootstrap-only agent as having zero observations.
7. `test_export_includes_bootstrap_with_flag_db_sourced` — export bundle preserves `synthetic` and `state_json.source` on bootstrap rows. **Critical:** the test must use the DB-sourced export path (not the in-memory monitor history at `src/mcp_handlers/introspection/export.py:52`, which doesn't see the bootstrap row).
8. `test_first_real_checkin_after_bootstrap` — bootstrap row + one real check-in: trajectory query returns both rows; "first real check-in" timestamp points at the real one; `get_latest_agent_state` returns the real row.
9. `test_force_new_re_bootstraps_new_identity` — `onboard(force_new=true, parent_agent_id=<prior>)` after a bootstrap on the prior identity writes a new bootstrap row on the new identity.
10. `test_force_new_does_not_modify_parent_bootstrap` — same setup as #9; the parent identity's bootstrap row is byte-identical before and after the `force_new` call.
11. **(new)** `test_concurrent_onboard_bootstrap_at_most_one` — two simultaneous `onboard(initial_state=...)` calls for the same UUID; assert exactly one bootstrap row exists after both resolve. Enforced by the unique partial index.
12. **(new)** `test_cross_agent_activity_excludes_bootstrap` — a bootstrap-only agent does not appear in `get_recent_cross_agent_activity` results.
13. **(new)** `test_substrate_earned_exempt` — `onboard(initial_state=...)` for an identity flagged substrate-earned returns `bootstrap.written: false, reason: "substrate-earned-exempt"` and writes no row, even when the hook would otherwise call it.
14. **(new — dialectic open-question)** `test_lineage_inheritance_filters_synthetic` — when `force_new=true, parent_agent_id=<bootstrap-only-parent>`, the new identity's trajectory ODE seeds from defaults (not from the parent's synthetic priors).
15. **(new — code-review finding 4)** `test_session_start_hook_calls_initial_state` — hook integration test (parallel to existing hook tests in `tests/hooks/`); verifies the hook posts `initial_state` and respects the substrate-earned exemption.

Existing tests that touch onboard or filter state rows MUST be reviewed for regression risk; the filter audit deliverable in §4.1 is the artifact that confirms this.

## 9. Implementation order

1. **Schema migration** (one PR, dedicated). 
   - `ALTER TABLE core.agent_state ADD COLUMN synthetic BOOLEAN NOT NULL DEFAULT false;`
   - `CREATE INDEX idx_agent_state_synthetic_partial ON core.agent_state (identity_id, ts DESC) WHERE synthetic = false;`
   - `CREATE UNIQUE INDEX uq_agent_state_one_bootstrap_per_identity ON core.agent_state (identity_id) WHERE synthetic = true;`
   - `DROP MATERIALIZED VIEW core.mv_latest_agent_states; CREATE MATERIALIZED VIEW ...` (recreate with `synthetic` projected). The matview's existing fallback path (`src/db/mixins/state.py:103` try/except) covers the brief recreate window.
   - **No identity-table backfill required** — the substrate-earned check uses the existing `core.substrate_claims` registry (S19 PR1) plus a small Pi-resident allowlist resolved in Phase 2. Phase 1 is pure `agent_state` schema work.
2. **Schema + handler change** for `onboard.initial_state` — `BootstrapStateParams` model in `src/mcp_handlers/schemas/core.py`, INSERT in `handle_onboard_v2` wrapped with `asyncio.wait_for(..., timeout=0.5)`, substrate-earned defensive check.
3. **Filter audit + filter changes.** Produce `docs/proposals/onboard-bootstrap-checkin.filter-audit.md` (the deliverable from §4.1). Add `synthetic = false` filter to every read site enumerated in the audit, with one test per site. **This is the danger step.** If the audit surfaces >10 call sites, escalate to council.
4. **Hook update** — Claude Code `hooks/session-start`, Codex `plugins/codex/...`, dispatch bots if applicable. Substrate-earned bypass at hook level.
5. **Population observability** — query path (§6.1) and dashboard panel (§6.2). Sentinel signal (§6.3) is post-merge.

## 10. Out of scope

- Changing the meaning of `process_agent_update` itself.
- Replacing `client_hint` / `purpose` semantics on onboard. `initial_state` is additive.
- Verdict-gated onboarding (rejected in §2).
- Auto-deriving bootstrap values from session metadata server-side. The agent or the hook supplies them; the server does not infer.
- Backfilling bootstrap rows for historically onboarded agents. New behavior, forward-only.
- "First real wins" mechanism on top of decay (§5). Measure first via §6 metrics; pre-engineer only if pathology is large in production.
- A `bootstrap_via` parameter distinguishing hook-initiated from agent-initiated bootstraps (dialectic §6.b). Add when audit distinction earns its keep.
- `bootstrap_stale: true` sweep for old bootstrap-only rows (dialectic open question). Per-query staleness computation is sufficient for now.
- Changing midstream-silence behavior (per §1 scope discipline). That's `feedback_check-in-during-long-sessions.md`'s scope; not addressed here.
