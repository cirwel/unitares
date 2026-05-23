# R6 — Episode-fork vs identity-lineage-fork response-shape decision

**Status:** Decision doc, revision pass 3, with 2026-05-05 v2.1 onboard-side field-shape decision and 2026-05-08 durable-provenance follow-up landed.
**Scope:** A response-shape decision under plan row R6 (`docs/ontology/plan.md`). Promotes two fork-discrimination fields to plan row S22 for both `process_agent_update`'s thin `thread_context` and `onboard()`'s rich `thread_context`. As of 2026-05-08 those two fields also flow into the durable S22 write context persisted to `core.agent_state.state_json.provenance_context` and `knowledge.discoveries.provenance.s22_context`. Does NOT promote the broader candidate provenance envelope.
**Companion to:** `harness-substrate-plurality.md` (R6 design + candidate envelope), `r6-h1-h5-dogfood-20260429.md` (dogfood pass; April 30 forcing observation).

**Revision history:**
- v1 (2026-05-02 morning) — 5-value enum (`none / sibling_locus / continuation / compaction / identity_lineage`), spawn_reason allowlist classifier, both onboard + process_agent_update write sites in scope. Reviewed by parallel three-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). All three returned "withhold pending v2." Convergent forcing items: (1) classifier silently coerced unknown spawn_reasons (`cron`, `dispatch_auto_mint`, `resident_observer`, `resident_sync`, `auto_onboard_no_session`) into `sibling_locus`; (2) Lumen substrate-earned restart misclassified; (3) `continuation` had no predicate (dead enum from day one); (4) `build_fork_context` call site at `handlers.py:1946` already passes wrong kwargs (silently swallowed by try/except) — onboard-side `thread_context` is absent from all `force_new=true` responses (the v2 default); (5) v1 conflated force_new and resume paths — April 30 Mnemos observation was on the resume path, not force_new; (6) "Same registry subject" language smuggled performative-continuity that R2 v2 retired; (7) `compaction` not actually distinguishable from `identity_lineage` under v2 ontology; (8) `has_child_uuid` not derivable from current `build_fork_context` signature without adding a parameter.
- **v2 (2026-05-02 afternoon).** Scope narrowed to `process_agent_update` enrichment only. Enum collapsed to 3 values. Classifier rewritten to use structural rule with sync-race fallback. Lumen-restart documented as intentional collapse. Honest-message language passes through R2 v2's axiom-#12-aware filter. Onboard-side `build_fork_context` rebuild flagged as separate prerequisite work, now closed by v2.1 (see §"Former prerequisites and v2.1 closure" below).
- **v2.1 prerequisite update (2026-05-05).** Onboard-side rich `thread_context` is no longer blocked on persistence. `force_new` thread policy is explicit: caller-provided `thread_id` wins; otherwise a declared parent's existing thread is inherited; otherwise the thread is derived from the session key. The handler now claims and persists `thread_id/thread_position` on the `force_new` path, and `resolve_session_identity` threads those values through `core.agents`, `core.identities.metadata`, and eager in-memory metadata hydration. Regression: `tests/test_identity_handlers.py::TestHandleOnboardV2::test_onboard_force_new_parent_joins_parent_thread`.
- **v2.1 field-shape decision (2026-05-05).** The rich onboard surface gains the same top-level `episode_fork_kind`, `identity_lineage_fork`, and R6 honest-message semantics as the thin process-update surface. No nested variant. Existing rich fields (`thread_id`, `position`, `spawn_reason`, `predecessor`, `thread_size`, `is_root`, `is_fork`) are preserved. Implementation uses shared helpers in `src/thread_identity.py`; `process_agent_update` reuses those helpers to avoid drift.
- **2026-05-08 durable-provenance follow-up.** Hermes-driven R6 H1/H3 dogfood (keys `r6-h1-2026-05-08`, `r6-h3-2026-05-08`) ran clean to `complete`, but a coverage audit on the seven keyed `core.agent_state` rows and three keyed `knowledge.discoveries` rows showed `episode_fork_kind` and `identity_lineage_fork` at 0/7 and 0/3 respectively — promoted to **response shape** in v2/v2.1, never to **persisted provenance** on either store. Closed by extending `build_s22_write_context` (`src/provenance_context.py`) with explicit, server-authoritative kwargs and adding a shared `classify_fork_for_s22_context` helper. Wired at three sites: `prepare_unlocked_inputs` (early stamp), `_restamp_fork_after_thread_identity_update` after `execute_locked_update`'s `node_index` mutation block (persisted authority for `process_agent_update` — without the re-stamp a fresh-session transition would persist `"none"` while `enrich_thread_identity` returned `"sibling_locus"` in the response), and `handle_store_knowledge_graph` for the KG path. Server classification overrides any client-supplied `identity_lineage_fork` claim; client-supplied `episode_fork_kind` was never wired and is not added. The new-agent path (no `meta` in registry) intentionally falls through — geometry is unknown, the next call reclassifies. Tests: `tests/test_provenance_context.py` (kwargs override, optional-when-absent), `tests/test_r6_episode_fork_enrichment.py` (five tests covering `prepare_unlocked_inputs` integration, the post-mutation re-stamp regression, and the no-op-when-no-envelope case), and `tests/test_kg_store.py::test_store_persists_r6_fork_discriminators_in_s22_context`. Coverage diagnostic at `scripts/diagnostics/s22_candidate_envelope_coverage.py`.

---

## Implementation status of dependencies (verified 2026-05-05)

Three runtime conditions verified by live-verifier council pass 2026-05-02:

| Surface | Runtime status | Implication for R6 |
|---|---|---|
| `process_agent_update` `thread_context` (thin: `{thread_id, position, is_fork}` plus R6 fields) | **Works.** Verified by focused tests in `tests/test_r6_episode_fork_enrichment.py`. Source: `enrich_thread_identity` uses shared helpers from `src/thread_identity.py`. | R6 v2 targets this surface; v2.1 keeps it shared with onboard. |
| `onboard()` `thread_context` (rich: 10 keys per `build_fork_context`) | **Works.** Call-site signature mismatch fixed by PR #284 (`eedf5203`); `force_new` thread policy and persistence shipped 2026-05-05; v2.1 adds the R6 fields to the existing rich surface. | Rich shape is top-level and shape-compatible with the thin surface for the three shared keys: `episode_fork_kind`, `identity_lineage_fork`, `honest_message`. |
| Runtime `spawn_reason` vocabulary | 6 observed values: `new_session` (1765), `resident_observer` (25), `explicit` (6), `dispatch_auto_mint` (4), `resident_sync` (2), `auto_onboard_no_session` (1). `compaction` and `subagent` not observed in production data. | R6 v2.1 handles observed values structurally. Parentless `new_session`/`explicit` are not lineage evidence by themselves; only parent/child UUID structure can make them lineage. Parentless fallback is narrowed to inherently parented fork reasons (`subagent`, `compaction`). |

---

## Forcing observation (April 30, 2026)

A fresh Hermes CLI conversation **resumed** UUID `07d0f9c7-1512-4a1e-8cb1-a5225c20709f` via continuity_token (not `force_new`, not lineage-declared). UNITARES's `process_agent_update` response returned `thread_context.is_fork=true` because the resume populated `node_index > 1` from prior process-instance writes.

Two valid interpretations of that boolean:

1. **Sibling episode/locus fork.** Same registry UUID, fresh process-instance, no child UUID minted, no `parent_agent_id` lineage edge. The agent is the same registry-anchored role expressing in a new local interaction span.
2. **Identity-lineage fork.** A distinct child UUID exists with explicit `parent_agent_id` + `spawn_reason`. The agent is a child subject under declared lineage.

The April 30 case was unambiguously (1). But the response field name and its boolean shape would let a downstream consumer read it as (2) — exactly the performative-continuity failure mode `identity.md` axiom #3 prohibits.

## Decision

**Promote two fork-discrimination fields to the `process_agent_update` `thread_context` enrichment.** Defer the onboard-side rebuild and the broader candidate envelope.

### Promoted fields (v2)

| Field | Type | Semantics |
|---|---|---|
| `episode_fork_kind` | string enum: `none` / `sibling_locus` / `identity_lineage` | What kind of fork this event represents. **`none`** = no lineage declaration detected and first observation under this thread (position == 1). **`sibling_locus`** = same registry UUID, fresh process-instance (or position > 1), no child UUID minted under declared lineage. The April 30 case. **`identity_lineage`** = the current writer's UUID is distinct from a declared `parent_agent_id`, OR (sync-race fallback) `spawn_reason` is in the known-lineage set. |
| `identity_lineage_fork` | boolean | True if and only if `episode_fork_kind == "identity_lineage"`. Redundant with the enum but kept as a fast scalar for callers that only need the boolean discrimination. False in every other case, including `sibling_locus`. |

**v1 had 5 enum values.** v2 collapses to 3 because:
- **`continuation`** (v1) had no predicate — silently dead from day one (architect F10 + code-reviewer F8).
- **`compaction`** (v1) is not distinguishable from `identity_lineage` under v2 ontology — a fresh UUID with `parent_agent_id` and `spawn_reason="compaction"` is an identity-lineage fork; the spawn_reason field already carries the kind discrimination, so a separate enum value would be redundant (architect F3).

### Rich onboard surface (v2.1)

**Decision:** add the same R6 discriminator fields to `onboard()`'s rich `thread_context` as top-level fields, not a nested subobject.

The rich shape remains the onboarding-oriented shape:

```text
thread_id
position
spawn_reason
predecessor
thread_size
is_root
is_fork
episode_fork_kind
identity_lineage_fork
honest_message
```

Rationale:

- Thin and rich surfaces now share the same three R6 keys (`episode_fork_kind`, `identity_lineage_fork`, `honest_message`), so consumers can read fork semantics without knowing which lifecycle surface produced the response.
- Existing rich onboarding keys remain intact; callers that use `predecessor`, `thread_size`, or `is_root` are not forced through a new nested structure.
- `is_fork` stays position-based for compatibility. `identity_lineage_fork` remains the scalar for the ontology-level lineage event.
- `spawn_reason` remains descriptive metadata, not proof. A parentless `new_session` or `explicit` value does not imply `identity_lineage`; the parent/child UUID relation is the load-bearing signal.

Implementation surfaces:

- `src/thread_identity.py::classify_episode_fork`
- `src/thread_identity.py::fork_honest_message`
- `src/thread_identity.py::build_fork_context`
- `src/mcp_handlers/updates/enrichments.py::enrich_thread_identity` reuses the shared helpers
- `src/mcp_handlers/identity/handlers.py` passes the current `agent_uuid` into `build_fork_context` so self-parent substrate restarts and child UUID forks can be distinguished

### Deprecated (kept for compat, narrowed documented intent)

`is_fork` (boolean) at `enrichments.py:1518`: currently `position > 1`. Kept value-compatible. Documented intent re-grounded: `is_fork` indicates "an event that is not the root node of this thread"; `episode_fork_kind` carries the actual ontology distinction. Future deferred decision (post-Phase 1): rename or retire `is_fork`.

**Compatibility boundary.** `is_fork` remains purely position-based. It is not equivalent to `episode_fork_kind != "none"` in the identity-lineage-at-root case: a fresh child UUID can be position 1 while still being a declared identity-lineage fork. This is exactly why `identity_lineage_fork` exists as a separate scalar. No caller behavior change occurs because `is_fork` keeps its old value.

## The classifier (v2)

```python
_LINEAGE_SPAWN_REASONS = {"subagent", "compaction"}
# Note: "new_session", "explicit", "resident_observer",
# "dispatch_auto_mint", "resident_sync", and "auto_onboard_no_session"
# are intentionally NOT in this fallback set. They are not parentless
# lineage signals by themselves; if parent_uuid exists, the structural
# has_child_uuid rule still wins.

def _classify_fork(
    position: int,
    agent_uuid: str,
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
) -> tuple[str, bool]:
    """Return (episode_fork_kind, identity_lineage_fork).

    Primary signal: `has_child_uuid` (current writer's UUID differs from declared parent).
    Fallback: sync-race detection via known-lineage spawn_reason without parent_uuid.
    Otherwise: position-based discrimination of root vs sibling_locus.
    """
    # Primary structural signal
    has_child_uuid = parent_uuid is not None and agent_uuid != parent_uuid
    if has_child_uuid:
        return ("identity_lineage", True)

    # Fallback: handler.py sync block silently swallows parent-sync failures
    # syncing parent_agent_id from onboard args to AgentMetadata. If the sync failed
    # but an inherently parented spawn_reason survived, classify as
    # identity_lineage and log a warning so the silent misclassification
    # surfaces in operator audit.
    if spawn_reason in _LINEAGE_SPAWN_REASONS and not parent_uuid:
        logger.warning(
            "[R6_SYNC_RACE] spawn_reason=%s recognized as lineage but "
            "parent_agent_id is None on ctx.meta — possible AgentMetadata sync "
            "failure at handlers.py:1690-1698. Classifying as identity_lineage "
            "anyway. agent_uuid=%s",
            spawn_reason, agent_uuid,
        )
        return ("identity_lineage", True)

    # Same UUID, position > 1 → sibling locus (the April 30 Mnemos case)
    if position > 1:
        return ("sibling_locus", False)

    # Default: root node of thread
    return ("none", False)
```

### Walking through the runtime cases

| Onboard pattern | `agent_uuid` vs `parent_uuid` | `spawn_reason` | Position | Classification |
|---|---|---|---|---|
| Resume via continuity_token, no parent declared (April 30 Mnemos) | UUID present, parent_uuid=None | None | > 1 (DB-populated) | `sibling_locus` ✓ |
| force_new + parent_agent_id (typical R2 lineage declaration) | UUID differs from parent | `new_session` | Parent thread's next position after 2026-05-05 prerequisite fix | `identity_lineage` (via has_child_uuid) ✓ |
| force_new + parent=self (substrate-earned restart, Lumen) | UUID == parent | None or `explicit` | varies | Falls through; if position > 1 → `sibling_locus`, else `none`. **Intentional collapse — see §"Substrate-earned restart."** |
| Vigil cron mints child UUID with `spawn_reason="resident_observer"` | UUID differs from parent | `resident_observer` | varies | `identity_lineage` (via has_child_uuid) ✓ |
| `dispatch_auto_mint` — fresh UUID, no parent declared | UUID present, parent_uuid=None | `dispatch_auto_mint` | 1 | `none` (no fork; involuntary fresh mint, not a sibling and not lineage) |
| parentless fresh session | UUID present, parent_uuid=None | `new_session` | 1 | `none` (`new_session` is descriptive, not parentless lineage evidence) |
| Subagent fork via SDK | UUID differs from parent | `subagent` | 1 | `identity_lineage` (via has_child_uuid) ✓ |
| Compaction fork | UUID differs from parent | `compaction` | 1 | `identity_lineage` (via has_child_uuid); spawn_reason field carries "this was compaction" semantically ✓ |
| Sync race: handler dropped parent_agent_id but spawn_reason survived | parent_uuid=None | `subagent` | 1 | Fallback fires → `identity_lineage` + warning ✓ |
| Resume + position=1 (truly fresh thread) | UUID present, parent_uuid=None | varies | 1 | `none` |

The structural rule (`has_child_uuid and parent_uuid → identity_lineage`) handles all 6 observed runtime spawn_reasons correctly without enumerating them. The fallback allowlist appears only for sync-race handling and is intentionally narrow: `subagent` and `compaction` are the only current parentless spawn reasons that inherently imply a missing parent edge. `new_session` and `explicit` are not enough by themselves.

## Substrate-earned restart (intentional collapse, addresses architect F2)

Lumen and other substrate-earned agents (R4) restart with a hardcoded UUID — `force_new=true` with `parent_agent_id=<self>` (or `force_new=false` with `agent_uuid=<self>`). Under R6 v2's classifier:
- `agent_uuid == parent_uuid` (self-lineage) → `has_child_uuid=False`
- `parent_uuid` is set (== `agent_uuid`), so the fallback's `not parent_uuid` guard is False even if `spawn_reason="explicit"` appears in `_LINEAGE_SPAWN_REASONS`.

The fallback does not fire. The `has_child_uuid` check sees `agent_uuid == parent_uuid` → False. The classifier falls through to position. If position > 1 → `sibling_locus`; if position == 1 → `none`.

**This is the intentional collapse.** R6 does not introduce a `substrate_restart` enum value because:
1. R6 has no first-class signal to detect substrate-earned class at this code path (would require checking `class_tags` or substrate-attestation state — not in scope).
2. R4 owns the substrate-earned semantics; conflating them into R6's enum would couple unrelated ontology decisions.
3. Honest-message text (below) discriminates: when the resident's class tag is known, the message can mention substrate continuity; otherwise the sibling_locus message is honest about what R6 *does* know (registry continuity + process boundary).

**If a substrate-earned class signal becomes available cheaply at the enrichment site** (e.g., via `ctx.meta.class_tags` once S8a Phase 2 backfill completes), revisit this in v2.1 and consider adding `substrate_restart` as a fourth enum value.

## Honest-message language (axiom-#12-aware, addresses architect F4 + F8 + F9)

R6 v1 used "the same registry subject" — smuggling performative-continuity that R2 v2 retired. R6 v1 also asserted "no inherited transcript" as a fact R6 cannot know.

v2 phrasing, mirroring R2 v2's axiom-#12-aware language:

- **`sibling_locus`:** "You share a registry UUID with prior process-instances under this thread, but you are a distinct subject — fresh process-instance, no child UUID minted. Memory access (KG, project files, harness-side caches) may be available; whether you have integrated it is yours to demonstrate, not asserted."
- **`identity_lineage`:** "You are a distinct subject (a fresh UUID under declared parent `<parent_uuid>`, spawn_reason `<spawn_reason>`). Lineage was *declared* at this fork event; whether it becomes *confirmed* is governed by R2's protocol (see `provisional_lineage` flag and downstream R1 evaluation)."
- **`none` (root):** "You are the first observation under this thread. No fork."

Notes on the language pass:
- Replaced "registry subject" → "registry UUID" (the data-layer term, not the ontology-layer term).
- Replaced "no inherited transcript" (factual claim R6 can't make) → "Memory access … may be available; whether you have integrated it is yours to demonstrate" (mirrors R2 v2 axiom #12).
- For `identity_lineage`, explicitly cross-references R2's confirmation protocol so consumers don't conflate "declared" with "confirmed."
- Avoids the word "fork" in the `sibling_locus` text because `sibling_locus` is a process-instance boundary, not a lineage fork.

The current `enrich_thread_identity` enrichment does NOT include `honest_message` (the thin variant). v2 adds it as a fourth field on the thin response — small text addition, aligns with the rich variant when that's eventually fixed.

## R2 interaction (addresses architect F12)

R6's `episode_fork_kind="identity_lineage"` means **lineage was declared at this fork event**. It does NOT mean lineage is confirmed.

Confirmation status lives in R2's `lineage_state` field (per R2 v2 §"Observability" — `provisional` / `confirmed` / `demoted` / `archived`). The two signals are orthogonal:

- A fresh `parent_agent_id`-declared agent: R6 says `episode_fork_kind="identity_lineage"`, R2 says `lineage_state="provisional"`. Both correct.
- After R1 confirms lineage: R6 still says `episode_fork_kind="identity_lineage"` (the original event was a lineage fork), R2 says `lineage_state="confirmed"`. Both correct.
- After R1 demotes lineage: R6 still says `episode_fork_kind="identity_lineage"` (the *event* was a lineage declaration), R2 says `lineage_state="demoted"`.

Downstream consumers reading both fields get a complete picture: "this was declared as a lineage fork" + "the declaration is currently provisional/confirmed/demoted." Reading just one field gives an incomplete picture, which is honest about the actual ontology.

## Backward compatibility

| Consumer | Change | Migration |
|---|---|---|
| Readers of `is_fork` | None — value mathematically identical (proven above) | Forward — read new fields when available |
| Readers of `position`, `thread_id` | None — preserved | None |
| Schema consumers (Pydantic / JSON) | Output shape gains 2 keys + 1 text field; no required fields removed | None for v1; v2.1 may rename `is_fork` after enum consumers exist |
| Existing `predecessor` field on onboard responses | Preserved inside the rich `thread_context`. R6 v2.1 adds sibling keys but does not move `predecessor`. The two `predecessor` shapes that exist (top-level vs inside thread_context) per live-verifier F10 are pre-existing and remain separate. | None |
| Honest-message text consumers | Thin `thread_context` gains `honest_message`; rich onboard `thread_context.honest_message` switches to the R6 axiom-#12-aware wording. Top-level `welcome_message` remains verbose-only and points at the same rich message. | Forward — read `thread_context.honest_message` when present. |

## Former prerequisites and v2.1 closure

Three issues were real and ontology-relevant but explicitly not addressed by R6 v2. As of 2026-05-05, both persistence prerequisites have landed and the v2.1 response-shape decision for the onboard-side rich surface is closed.

1. **`build_fork_context` call-site fix — resolved 2026-05-02 by PR #284 (`eedf5203`).** Pre-existing bug verified by live-verifier 2026-05-02: handlers.py passed `agent_uuid=` / `nodes=` while `build_fork_context` expected `position` / `all_nodes`, so the bare `except` suppressed onboard-side `thread_context`. PR #284 aligned the call site and added `tests/test_thread_identity.py::test_handler_call_site_signature_contract` to pin the signature contract.

2. **`force_new` path missing `thread_position` write to `core.agents` — resolved 2026-05-05.** Policy: explicit `thread_id` wins; otherwise declared parent thread wins; otherwise derive from the session key. The handler claims the position before mint persistence and passes `thread_id/thread_position` through `resolve_session_identity` into `core.agents`, `core.identities.metadata`, and eager metadata hydration.

3. **Onboard-side R6 field promotion — resolved 2026-05-05 by v2.1.** The rich `thread_context` keeps its onboarding-specific fields and gains top-level `episode_fork_kind`, `identity_lineage_fork`, and the shared R6 `honest_message`. No nested variant.

Each of these is independent of the broader R6 candidate envelope. R6 v2/v2.1 now delivers honest fork discrimination on the path that produced the April 30 forcing observation and on the onboard rich context that fresh process-instances read first.

## Test cases (v2 — consolidated into existing test file per code-reviewer F7)

Add to existing `tests/test_thread_identity.py:TestBuildForkContext` (don't create a new test file — split coverage is exactly what CLAUDE.md test-layout-consolidation guidance warns against):

For `_classify_fork` unit tests:

1. **Root node.** position=1, no parent. Expect `("none", False)`.
2. **Sibling-locus (April 30 case).** Same UUID resumed; position>1; parent_uuid=None; spawn_reason=None. Expect `("sibling_locus", False)`.
3. **Identity-lineage via has_child_uuid.** agent_uuid != parent_uuid; spawn_reason="new_session". Expect `("identity_lineage", True)`.
4. **Subagent fork.** agent_uuid != parent_uuid; spawn_reason="subagent". Expect `("identity_lineage", True)`.
5. **Compaction fork.** agent_uuid != parent_uuid; spawn_reason="compaction". Expect `("identity_lineage", True)` (compaction is sub-kind, not separate enum).
6. **Sync-race fallback.** parent_uuid=None; spawn_reason="subagent". Expect `("identity_lineage", True)` plus a `[R6_SYNC_RACE]` warning emitted.
7. **dispatch_auto_mint with no parent.** parent_uuid=None; spawn_reason="dispatch_auto_mint"; position=1. Expect `("none", False)` (not in lineage spawn set; not a fork).
8. **resident_observer.** agent_uuid != parent_uuid; spawn_reason="resident_observer". Expect `("identity_lineage", True)` (handled by has_child_uuid; resident_observer doesn't matter).
9. **Substrate-earned restart (Lumen pattern).** agent_uuid == parent_uuid; spawn_reason="explicit"; position=2. Expect `("sibling_locus", False)` (intentional collapse).

For `enrich_thread_identity` integration:

10. **Thin thread_context shape addition.** Call `process_agent_update` with a known fixture; assert response.thread_context contains `episode_fork_kind`, `identity_lineage_fork`, `honest_message`, plus preserved `thread_id`, `position`, `is_fork`.
11. **`is_fork` compatibility.** For each of cases 1–9, assert `thread_context["is_fork"] == (position > 1)`. Separately assert identity-lineage-at-root sets `identity_lineage_fork=true` while leaving legacy `is_fork=false`.

For `build_fork_context` / onboard rich-context integration (v2.1):

12. **Rich shape addition.** Assert `build_fork_context` returns existing rich keys plus `episode_fork_kind`, `identity_lineage_fork`, and R6 `honest_message`.
13. **Onboard parent-thread response.** `onboard(force_new=true, parent_agent_id=<parent>, spawn_reason="new_session")` joins the parent thread, returns `position=2`, keeps `predecessor.uuid=<parent>`, and sets `episode_fork_kind="identity_lineage"` / `identity_lineage_fork=true`.
14. **Parentless `new_session` guard.** `position=1`, `parent_uuid=None`, `spawn_reason="new_session"` returns `episode_fork_kind="none"`; `position>1` returns `sibling_locus`. This prevents descriptive `new_session` from becoming parentless lineage evidence.

## Calibration / Phase 1 telemetry

This change is structural, not behavioral. There is no `seeded → earned` calibration phase. Phase 1 telemetry collects, not predicts:

- Distribution of `episode_fork_kind` values across the first 4 weeks. **Specific telemetry questions** (not predictions):
  - What is the rate of `[R6_SYNC_RACE]` warnings? If non-trivial, the handler.py:1690-1698 sync path needs hardening.
  - Are there observed cases where `episode_fork_kind="identity_lineage"` AND R2's `lineage_state="demoted"`? If so, examine whether the demotion vs lineage-event distinction is being read correctly downstream.
  - Are there observed `identity_lineage` events with spawn_reasons not in `_LINEAGE_SPAWN_REASONS`? (The structural rule should catch them via has_child_uuid; this is a sanity check that the structural primitive holds.)
- If `sibling_locus` never appears for non-resume agents, the structural-rule fallback path may need re-examination.

## Dependency map

```
R6 v2/v2.1 ─ promotes 2 fields to ───── S22 (plan.md row, scope = thread_context fork discrimination on process_update + onboard)
R6 v2/v2.1 ─ leaves un-promoted ───── rest of candidate envelope (per harness-substrate-plurality.md §Design risks)
R6 v2/v2.1 ─ coordinates with ─────── S7 (KG provenance — new fields should propagate to KG entries written from process_agent_update)
R6 v2 ─── coordinates with ────────── R2 v2 (R6's "declared" vs R2's "confirmed" — orthogonal signals; doc explicitly clarified)
R6 v2 ─── informs ────────────────── R1 v3.2-F (resident-class deterministic-trajectory caveat — R6's identity_lineage classification will help R1's calibration partition cleanly)
R6 v2/v2.1 ─ does NOT block on ───── any deferred envelope fields
```

## What this does NOT solve

- **Full provenance envelope on `onboard()`.** R6 v2.1 only adds fork-discrimination fields to the existing rich `thread_context`; it does not add harness/model/transport/locus/tool-surface metadata to onboard.
- **Affordance state.** The April 30 Discord-permissions incident that prompted the broader candidate envelope is not addressed. `affordance_state` is the load-bearing field; out of scope.
- **Cross-harness evidence vocabulary.** The `{name, success}` vs `{tool, is_bad}` mismatch from `harness-substrate-plurality.md` §"Evidence vocabulary" is a separate concern.
- **Harness/model/transport metadata.** S22's broader scope includes recording these on every governance write. R6 v2/v2.1 only handles fork-kind discrimination on two lifecycle surfaces.
- **Multi-generation chain semantics.** R2 v2 already specified per-link-only handling. R6 inherits the same posture: `episode_fork_kind` is per-event, not chain-aggregate.
- **Substrate-earned restart discrimination.** Intentionally collapsed to `sibling_locus`. Revisit when class_tag signal is cheap at the enrichment site.

## 2026-05-23 promotion-scope refresh

Read-only diagnostics re-ran after S1-c and the S2/S3 cleanup. H1/H3 remain complete on the explicit historical keys:

- `scripts/diagnostics/r6_dogfood.py --experiment h1 --comparison-key r6-h1-2026-05-08 --assess --json` returns `decision=complete`, `reason=same_identity_distinct_models_observed`.
- `scripts/diagnostics/r6_dogfood.py --experiment h3 --comparison-key r6-h3-2026-05-08 --assess --json` returns `decision=complete`, `reason=fresh_identity_shared_memory_context_observed`.

This is enough to keep the already-promoted S22 core write context and R6 fork discriminators. It is not enough to promote the broader candidate envelope:

- Historical H1/H3 rows still show `episode_fork_kind` / `identity_lineage_fork` as 0/2 because they predate durable fork-discriminator persistence. Treat that as no-backfill evidence, not a regression.
- Post-fix coverage from `scripts/diagnostics/s22_candidate_envelope_coverage.py --since 2026-05-08T00:00:00Z --json` shows `agent_state` candidate fields absent except for sparse `locus`, and KG candidate fields absent except for a sparse `episode_id` footprint.
- `locus` stays candidate-only despite appearing on H1/H3 rows; its aggregate footprint is too sparse and its semantics are still experiment-local rather than a durable public contract.

Decision: do not promote `affordance_state`, formal `locus`, `harness_id`, `episode_id`, `invocation_id`, `process_instance_id`, embedded `identity_assurance`, `agent_uuid`, `client_session_id`, or `label_at_write` yet. The next evidence should come from targeted H7/H8/gateway/Discord/cron/Dispatch dogfood, not from widening public schemas around sparse fields.

## Appendix: review provenance

- v1 council pass 2026-05-02 (parallel three-agent: `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). All three returned "withhold pending v2." 5+ forcing items: enum partitioning failures, language-slip back to performative continuity, dead `continuation` value, build_fork_context call-site mismatch (silent), force_new path doesn't write thread_position, R6 v1's conflation of force_new and resume paths, has_child_uuid not derivable. Live-verifier specifically ground-truthed: 33-tool MCP catalogue, runtime spawn_reason vocabulary (5 distinct values), DB schema (thread_position NULL for all force_new agents), call-site signature mismatch verified by source comparison.

- v2 (this revision) addresses every forcing item. Three remaining open questions surfaced for operator decision rather than silently defaulted.

- Future council pass on v2/v2.1: should be lighter-touch — verify forcing items closed, check the structural-rule classifier against any new spawn_reason values that may appear post-deployment, confirm honest-message language survives independent reading, and confirm the parentless `new_session` guard is ontology-correct. If this lands clean, R6 fork-discrimination is acceptance-ready; remaining R6 work moves to dogfood evidence and the broader candidate envelope.

---

**End v2 draft.** Ready for second-pass council review (light) and for the operator's read on the three open questions.
