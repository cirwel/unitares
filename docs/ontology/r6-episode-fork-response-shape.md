# R6 — Episode-fork vs identity-lineage-fork response-shape decision

**Status:** Decision doc, revision pass 2.
**Scope:** A response-shape decision under plan row R6 (`docs/ontology/plan.md`). Promotes two fields to plan row S22 for the **`process_agent_update` enrichment surface only**. Does NOT promote the broader candidate provenance envelope. Does NOT touch the onboard-side `build_fork_context` (gated on a prerequisite bug fix flagged below).
**Author:** agent `eee3bea8-7353-48a9-bea6-a6e912992f6c` (claude_code), 2026-05-02.
**Companion to:** `harness-substrate-plurality.md` (R6 design + candidate envelope), `r6-h1-h5-dogfood-20260429.md` (dogfood pass; April 30 forcing observation).

**Revision history:**
- v1 (2026-05-02 morning) — 5-value enum (`none / sibling_locus / continuation / compaction / identity_lineage`), spawn_reason allowlist classifier, both onboard + process_agent_update write sites in scope. Reviewed by parallel three-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). All three returned "withhold pending v2." Convergent forcing items: (1) classifier silently coerced unknown spawn_reasons (`cron`, `dispatch_auto_mint`, `resident_observer`, `resident_sync`, `auto_onboard_no_session`) into `sibling_locus`; (2) Lumen substrate-earned restart misclassified; (3) `continuation` had no predicate (dead enum from day one); (4) `build_fork_context` call site at `handlers.py:1946` already passes wrong kwargs (silently swallowed by try/except) — onboard-side `thread_context` is absent from all `force_new=true` responses (the v2 default); (5) v1 conflated force_new and resume paths — April 30 Mnemos observation was on the resume path, not force_new; (6) "Same registry subject" language smuggled performative-continuity that R2 v2 retired; (7) `compaction` not actually distinguishable from `identity_lineage` under v2 ontology; (8) `has_child_uuid` not derivable from current `build_fork_context` signature without adding a parameter.
- **v2 (2026-05-02 afternoon) — current.** Scope narrowed to `process_agent_update` enrichment only. Enum collapsed to 3 values. Classifier rewritten to use structural rule with sync-race fallback. Lumen-restart documented as intentional collapse. Honest-message language passes through R2 v2's axiom-#12-aware filter. Onboard-side `build_fork_context` rebuild flagged as separate prerequisite work (see §"Out of R6 scope" below).

---

## Implementation status of dependencies (verified 2026-05-02)

Three runtime conditions verified by live-verifier council pass 2026-05-02:

| Surface | Runtime status | Implication for R6 |
|---|---|---|
| `process_agent_update` `thread_context` (thin: `{thread_id, position, is_fork}`) | **Works.** Verified via direct call against running server. Source: `enrich_thread_identity` at `src/mcp_handlers/updates/enrichments.py:1509-1521`. | R6 v2 targets this surface. |
| `onboard()` `thread_context` (rich: 8 keys per `build_fork_context`) | **Broken.** Absent from all `force_new=true` responses (v2 ontology default). Two compounding bugs: (a) call-site signature mismatch at `handlers.py:1946` passes `agent_uuid=`, `nodes=` while `build_fork_context` expects `position`, `all_nodes` — TypeError silently swallowed by `except Exception`; (b) `force_new` path skips writing `thread_position` to `core.agents`, so the gate `if thread_info and thread_info.get("thread_id")` at line 1942 returns False anyway. | **Out of R6 scope.** See §"Out of R6 scope." |
| Runtime `spawn_reason` vocabulary | 6 observed values: `new_session` (1765), `resident_observer` (25), `explicit` (6), `dispatch_auto_mint` (4), `resident_sync` (2), `auto_onboard_no_session` (1). `compaction` and `subagent` not observed in production data. | R6 v2's classifier must handle all 6 runtime values correctly, *and* the unobserved `subagent`/`compaction` values that the codebase still emits via the SDK and onboard schemas. Achieved by structural-rule design. |

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
| `episode_fork_kind` | string enum: `none` / `sibling_locus` / `identity_lineage` | What kind of fork this event represents. **`none`** = first observation under this thread (position == 1). **`sibling_locus`** = same registry UUID, fresh process-instance (or position > 1), no child UUID minted under declared lineage. The April 30 case. **`identity_lineage`** = the current writer's UUID is distinct from a declared `parent_agent_id`, OR (sync-race fallback) `spawn_reason` is in the known-lineage set. |
| `identity_lineage_fork` | boolean | True if and only if `episode_fork_kind == "identity_lineage"`. Redundant with the enum but kept as a fast scalar for callers that only need the boolean discrimination. False in every other case, including `sibling_locus`. |

**v1 had 5 enum values.** v2 collapses to 3 because:
- **`continuation`** (v1) had no predicate — silently dead from day one (architect F10 + code-reviewer F8).
- **`compaction`** (v1) is not distinguishable from `identity_lineage` under v2 ontology — a fresh UUID with `parent_agent_id` and `spawn_reason="compaction"` is an identity-lineage fork; the spawn_reason field already carries the kind discrimination, so a separate enum value would be redundant (architect F3).

### Deprecated (kept for compat, narrowed documented intent)

`is_fork` (boolean) at `enrichments.py:1518`: currently `position > 1`. Value-equivalent under v2's classifier (proven below). Documented intent re-grounded: `is_fork` indicates "an event that is not the root node of this thread"; `episode_fork_kind` carries the actual ontology distinction. Future deferred decision (post-Phase 1): rename or retire `is_fork`.

**Equivalence proof.** v2's `_classify_fork` returns `"none"` if and only if `position == 1`. So `episode_fork_kind != "none"` ⟺ `position > 1` ⟺ current `is_fork` value. No caller behavior change. The narrowed-intent claim is purely documentary; the field's value is mathematically identical.

## The classifier (v2)

```python
_LINEAGE_SPAWN_REASONS = {"new_session", "subagent", "explicit", "compaction"}
# Note: "resident_observer", "dispatch_auto_mint", "resident_sync",
# "auto_onboard_no_session" are intentionally NOT in this fallback set.
# They are not known-lineage signals by themselves; if parent_uuid exists,
# the structural has_child_uuid rule still wins.

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

    # Fallback: handler.py sync block (lines 1690-1698) silently swallows failures
    # syncing parent_agent_id from onboard args to AgentMetadata. If the sync failed
    # but spawn_reason survived, classify as identity_lineage and log a warning so
    # the silent misclassification surfaces in operator audit.
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
| force_new + parent_agent_id (typical R2 lineage declaration) | UUID differs from parent | `new_session` | 1 (force_new path skips DB) | `identity_lineage` (via has_child_uuid) ✓ |
| force_new + parent=self (substrate-earned restart, Lumen) | UUID == parent | None or `explicit` | varies | Falls through; if position > 1 → `sibling_locus`, else `none`. **Intentional collapse — see §"Substrate-earned restart."** |
| Vigil cron mints child UUID with `spawn_reason="resident_observer"` | UUID differs from parent | `resident_observer` | varies | `identity_lineage` (via has_child_uuid) ✓ |
| `dispatch_auto_mint` — fresh UUID, no parent declared | UUID present, parent_uuid=None | `dispatch_auto_mint` | 1 | `none` (no fork; involuntary fresh mint, not a sibling and not lineage) |
| Subagent fork via SDK | UUID differs from parent | `subagent` | 1 | `identity_lineage` (via has_child_uuid) ✓ |
| Compaction fork | UUID differs from parent | `compaction` | 1 | `identity_lineage` (via has_child_uuid); spawn_reason field carries "this was compaction" semantically ✓ |
| Sync race: handler dropped parent_agent_id but spawn_reason survived | parent_uuid=None | `subagent` | 1 | Fallback fires → `identity_lineage` + warning ✓ |
| Resume + position=1 (truly fresh thread) | UUID present, parent_uuid=None | varies | 1 | `none` |

The structural rule (`has_child_uuid and parent_uuid → identity_lineage`) handles all 6 observed runtime spawn_reasons correctly without enumerating them. The known-lineage allowlist appears only in the sync-race fallback, where it's honest about being a defensive heuristic.

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
| Existing `predecessor` field on onboard responses | Unchanged. R6 v2 does not touch the onboard-side response (out of scope). The two `predecessor` shapes that exist (top-level vs inside thread_context) per live-verifier F10 are pre-existing and out of R6 v2 scope. | None |
| Honest-message text consumers | New `honest_message` key added to thin `thread_context` (was absent). Top-level `welcome_message` field at `identity_payloads.py:233-234` is verbose-only and unaffected by R6. | Forward — read `thread_context.honest_message` when present. |

## Out of R6 scope (flagged as prerequisite work)

Three issues are real and ontology-relevant but explicitly not addressed by R6 v2:

1. **`build_fork_context` call-site fix** (handlers.py:1946 passes wrong kwargs). Pre-existing bug. Verified via live-verifier 2026-05-02. Symptom: `thread_context` is absent from all `force_new=true` onboard responses. Fix scope: align call site with function signature (or vice versa); add `agent_uuid` parameter to `build_fork_context` to enable `_classify_fork`. Should be filed as a separate plan row or fixed as a prerequisite to R6's implementation row.

2. **`force_new` path missing `thread_position` write to `core.agents`.** Verified via DB query: `thread_position=NULL` for all force_new agents. Fix scope: extend the thread-position write in handler.py to fire on force_new as well as `created_fresh_identity`. Decision required: should force_new agents be assigned to a thread at all? (Possibly no, by current ontology — fresh process = fresh agent = fresh thread.)

3. **Onboard-side `thread_context` rebuild after (1) and (2) land.** Once force_new agents have `thread_position` and `build_fork_context` is callable, the onboard surface gains the rich `thread_context` that the doc historically described. R6 v2 does not specify how the new fork-discrimination fields land there because the prerequisite is unfinished. R6 v2.1 (after prerequisites) can add the onboard-side spec.

Each of these is independent of R6's primary value (process_agent_update enrichment). Shipping R6 v2 against the working surface delivers honest fork discrimination on the path that produced the April 30 forcing observation; the broken onboard surface needs upstream repair before R6's spec can extend to it.

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
11. **`is_fork` value equivalence.** For each of cases 1–9, assert `thread_context["is_fork"] == (thread_context["episode_fork_kind"] != "none")`. Catches future regressions in the equivalence claim.

## Calibration / Phase 1 telemetry

This change is structural, not behavioral. There is no `seeded → earned` calibration phase. Phase 1 telemetry collects, not predicts:

- Distribution of `episode_fork_kind` values across the first 4 weeks. **Specific telemetry questions** (not predictions):
  - What is the rate of `[R6_SYNC_RACE]` warnings? If non-trivial, the handler.py:1690-1698 sync path needs hardening.
  - Are there observed cases where `episode_fork_kind="identity_lineage"` AND R2's `lineage_state="demoted"`? If so, examine whether the demotion vs lineage-event distinction is being read correctly downstream.
  - Are there observed `identity_lineage` events with spawn_reasons not in `_LINEAGE_SPAWN_REASONS`? (The structural rule should catch them via has_child_uuid; this is a sanity check that the structural primitive holds.)
- If `sibling_locus` never appears for non-resume agents, the structural-rule fallback path may need re-examination.

## Dependency map

```
R6 v2 ─── promotes 2 fields to ───────── S22 (plan.md row, scope = process_agent_update only)
R6 v2 ─── leaves un-promoted ──────── rest of candidate envelope (per harness-substrate-plurality.md §Design risks)
R6 v2 ─── leaves un-addressed ────── onboard-side thread_context rebuild (gated on §"Out of R6 scope" prerequisites)
R6 v2 ─── coordinates with ────────── S7 (KG provenance — new fields should propagate to KG entries written from process_agent_update)
R6 v2 ─── coordinates with ────────── R2 v2 (R6's "declared" vs R2's "confirmed" — orthogonal signals; doc explicitly clarified)
R6 v2 ─── informs ────────────────── R1 v3.2-F (resident-class deterministic-trajectory caveat — R6's identity_lineage classification will help R1's calibration partition cleanly)
R6 v2 ─── does NOT block on ────── any deferred envelope fields
R6 implementation row ─── blocks on ─── §"Out of R6 scope" prerequisite (1) — `build_fork_context` call-site fix
                                       (or scope is narrowed at impl time to just `enrich_thread_identity`)
```

## Open questions for Kenny

(v1 had 3 open questions. v2 has resolved 2 of them via the council pass: continuation drop [decided], `episode_id` separate field [decided — not in R6 scope]. Two new questions surfaced.)

1. **Should the `build_fork_context` call-site fix (out-of-scope #1) ship as part of R6's implementation row, or as a separate row first?** Argument for same row: R6 implementation can't fully deliver onboard-side fields without it; tight coupling. Argument for separate row: it's a pre-existing bug that affects more than R6 (anyone reading the rich `thread_context` from onboard responses), and bundling it inflates R6 scope. **Recommendation: separate row, opened as a precondition. R6 implementation row narrows to enrichment-only until prereq lands.**

2. **Should sync-race detection (`[R6_SYNC_RACE]` warning) escalate to an audit event for operator visibility?** Currently the proposal is just a warning log. Audit events provide queryable history; logs only surface in real-time inspection. Telemetry question depends on whether sync race is rare (log fine) or chronic (audit needed). **Recommendation: warning log in v1; promote to audit event in v1.1 if Phase 1 shows non-trivial frequency.**

3. **`substrate_restart` as a fourth enum value if class_tags become available cheaply.** v2 documents the intentional collapse to `sibling_locus`; if S8a Phase 2 backfill makes `class_tags` available at the enrichment site without a DB read, R6 v2.1 could add `substrate_restart`. Or operator may decide collapse is correct ontology and not worth a fourth value. **Recommendation: defer; not in v1 scope.**

## What this does NOT solve

- **Onboard-side `thread_context`.** Out of R6 v2 scope per §"Out of R6 scope." Prerequisite fix needed first.
- **Affordance state.** The April 30 Discord-permissions incident that prompted the broader candidate envelope is not addressed. `affordance_state` is the load-bearing field; out of scope.
- **Cross-harness evidence vocabulary.** The `{name, success}` vs `{tool, is_bad}` mismatch from `harness-substrate-plurality.md` §"Evidence vocabulary" is a separate concern.
- **Harness/model/transport metadata.** S22's broader scope includes recording these on every governance write. R6 v2 only handles fork-kind discrimination on one surface.
- **Multi-generation chain semantics.** R2 v2 already specified per-link-only handling. R6 inherits the same posture: `episode_fork_kind` is per-event, not chain-aggregate.
- **Substrate-earned restart discrimination.** Intentionally collapsed to `sibling_locus`. Revisit when class_tag signal is cheap at the enrichment site.

## Appendix: review provenance

- v1 council pass 2026-05-02 (parallel three-agent: `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). All three returned "withhold pending v2." 5+ forcing items: enum partitioning failures, language-slip back to performative continuity, dead `continuation` value, build_fork_context call-site mismatch (silent), force_new path doesn't write thread_position, R6 v1's conflation of force_new and resume paths, has_child_uuid not derivable. Live-verifier specifically ground-truthed: 33-tool MCP catalogue, runtime spawn_reason vocabulary (5 distinct values), DB schema (thread_position NULL for all force_new agents), call-site signature mismatch verified by source comparison.

- v2 (this revision) addresses every forcing item. Three remaining open questions surfaced for operator decision rather than silently defaulted.

- Future council pass on v2: should be lighter-touch — verify forcing items closed, check the structural-rule classifier against any new spawn_reason values that may appear post-deployment, confirm honest-message language survives independent reading. If v2 lands clean, R6 v2 design is acceptance-ready pending Kenny's three open questions.

---

**End v2 draft.** Ready for second-pass council review (light) and for Kenny's read on the three open questions.
