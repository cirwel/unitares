# R2 — Honest memory integration

**Status:** Design doc v2; Phase 1 runtime implementation shipped 2026-05-05 in #357.
**Scope:** Plan row R2 (`docs/ontology/plan.md`). Defines the structural posture: what a fresh process reads when declaring inheritance, what counts as "integration" (vs. mere reference), what behavior change is required before identity-under-the-lineage-chain becomes confirmed.
**Author:** agent `eee3bea8-7353-48a9-bea6-a6e912992f6c` (claude_code), 2026-05-02.
**Builds on (design dependency only — see §"Implementation status"):** R1 v3.2 (`r1-verify-lineage-claim.md`) provides the trajectory-similarity gate; R2 specifies the protocol that wraps it.

**Revision history:**
- v1 (2026-05-02 morning) — single-channel, R1-as-integration-test, recommendation A (retroactive trust-tier crediting). Reviewed by parallel three-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). All three returned "withhold pending v2." Convergent forcing items: (1) asymmetry-claim violated by recommendation A's interpretive rewrite of parent's history; (2) axiom #12 conflation between "trajectory similarity" and "memory operative on behavior" (R1 explicitly disclaims being an integration test, R2 v1 conscripted it as one); (3) multi-generation chain semantics unspecified; (4) R1 is entirely unimplemented in master so all "R1 provides X" claims are forward references, not callable code; (5) `provisional_lineage` column does not exist in any schema (R2 v1 said "reuses, does not re-introduce" — false premise); (6) check-in-triggered evaluation as described would deadlock under the anyio-asyncio conflict (CLAUDE.md). Plus substantive findings on cross-role lineage silently demoting, demote-vs-fork interpretive-rewrite symmetry, FSM gaps, audit-write-path gaps, identity-vs-onboard-response gaps, subject-identity language slips.
- **v2 (2026-05-02 afternoon).** Forcing items addressed via the strategic calls listed at end of v1 council writeup. Substantive items addressed in-line. Remaining items surfaced as open questions for Kenny.
- **Phase 1 implementation (2026-05-05, #357) — current runtime.** Storage helpers, migration 036, FSM/audit events, cross-role pre-check, `onboard()` / `identity()` response fields, 30-minute sweeper, and `process_agent_update` trigger shipped. Phase 2 downstream consumers remain deferred until telemetry matures.

---

## Implementation status

R2 is design-dependent on R1 v3.2 and on the substrate-attestation work shipped under S19 (2026-04-26). The table below reflects the live repo after #357, superseding the 2026-05-02 pre-implementation dependency check.

| Surface R2 references | Status today (2026-05-05) | Implication for R2 |
|---|---|---|
| `score_trajectory_continuity` (R1 callable) | Shipped in `src/identity/trajectory_continuity.py`. | R2 FSM calls it from `src/identity/lineage_lifecycle.py`, outside the anyio handler context. |
| R1 EISV-series reconstruction | Internal to the R1 scorer; not exposed as an MCP tool. | R2 consumes only the scorer boundary, not the reconstruction helper directly. |
| `provisional_lineage` / R1 lineage columns | Shipped by R1 PR #306 (migration 031): `provisional_lineage`, `provisional_score_id`, `provisional_recorded_at`, `confirmed_at`. | R2 reuses these; `confirmed_at` satisfies the design's earlier `lineage_promoted_at` name. |
| R2 lifecycle columns | Shipped by #357 (migration 036): `lineage_declared_at`, `lineage_demoted_at`, `lineage_archived_at`, `lineage_last_eval_at`, `chain_obs_count`. | Phase 1 storage source of truth is now live on `core.identities`. |
| `class_promotion_sweeper_task` (cadence reference) | Exists in `src/background_tasks.py`; R2 adds sibling `lineage_eval_sweeper_task`. | R2 inherits the same background-task pattern and audit-only-on-transition convention. |
| `lineage_*` audit event types | Shipped by #357: `lineage_declared`, `lineage_cross_role_rejected`, `lineage_promoted`, `lineage_demoted`, `lineage_grace_expired`. | Audit writes record storage-confirmed transitions; empty sweeper cycles remain quiet. |
| `parent_agent_id` storage | Present on both `core.identities` and `core.agents`; R2 treats `core.identities` as canonical. | Cross-role rejection clears the declared edge before it becomes a lineage claim. |

Phase 1 is intentionally not the downstream consumer flip. Trust-tier S6, KG provenance S7, R3 baselines, and dashboard interpretation remain deferred to Phase 2 after the shadow-mode telemetry gate in §"Shadow-mode calibration path" is met.

---

## Purpose

A fresh process-instance can declare `parent_agent_id=<uuid>` today. The server records the field. Downstream consumers (trust-tier, KG provenance, dashboard) currently have no honest interpretation of that declaration — declaration is treated as fact. Under axiom #3 ("build nothing that appears more alive than it is") this is performative.

R2 specifies the protocol that turns a declaration into a verified continuity claim:

1. **Reading** parent's memory at onboard is unconditional. Memory inheritance is data-access, not identity-transfer.
2. **Promotion gate** is operationalized as R1 plausibility ≥ threshold *after* the successor accumulates ≥ `min_observations`. R1 measures one facet of behavioral overlap (trajectory similarity); axiom #12's full "memory operative on behavior" check is *not yet implementable* — it waits on R5 (replay-discrimination tooling).
3. **Lineage chain interpretation** by downstream consumers shifts at promotion: from "successor declares parent" (advisory) to "successor's behavior is consistent with parent's under their shared role" (confirmed). The successor remains a distinct subject; the chain itself gains an interpretive credential.

The structural posture is: **a successor inherits memory access from t=0; the lineage chain it claims gains weight only when behavior earns it.** Memory access is asymmetric (successor reads parent; parent is unaffected). The successor never *becomes* the parent. The successor's lineage *claim* is what becomes confirmed.

This is a **necessary-but-not-sufficient** gate toward axiom #12. R2 v1 honestly stops short of axiom #12's full demand: behavior-similarity (which R1 measures) is necessary for "memory operative on behavior," but not sufficient — a successor that inhabits the same operating envelope as parent (same role, same tasks, same time-of-day) will produce a similar trajectory whether or not it actually integrated parent's memory. R5's replay-discrimination tooling is the load-bearing fix; R2 is the protocol R5 plugs into.

## Non-goals (explicit)

- **Not authentication.** Auth remains bearer-token + (under S19) substrate-attestation.
- **Not memory-transfer or UUID-merge.** Successor is a distinct subject. Promotion does not relabel, does not rewrite history, does not retire the successor's UUID.
- **Not retroactive rewriting of parent's records.** Parent's `agent_states` rows, `audit.events`, KG entries are immutable to anything successor does. Asymmetry is structural and operative — see §"What does NOT change at promotion" below for the explicit interpretive-asymmetry boundary.
- **Not the memory-deepening mechanism (R5).** R2 measures whether the v1 *necessary* condition is met; R5 measures whether memory was actually operative (sufficient condition). R2 v1 ships under the explicit caveat that the necessary-but-not-sufficient gap exists.
- **Not a substitute for R4.** Substrate-earned agents bypass R2 via `verify_substrate_earned` + S19 attestation.
- **Not a security primitive.** R1's adversarial-forgery limitation propagates: an adversary with KG read access can synthesize a passing trajectory. R2's promotion does NOT credit successor with parent's accrued tier (forward-only crediting; see §"Trust-tier crediting policy"), which preserves the honesty-vs-security boundary that v1 violated under recommendation A.
- **Not a multi-signal integration test.** R2 v1 sits on R1's single-channel score. Additional channels (KG cite-and-extend, calibration-envelope match, decision-distribution overlap) are deferred with named prerequisites — see §"Deferred."

## The structural posture (R2's heart)

| Aspect | Rule |
|---|---|
| **Memory access** | Successor reads any/all of parent's memory artifacts unconditionally at onboard. Reading is not gated. |
| **Lineage claim state at t=0** | `provisional_lineage=true` on successor's `core.identities` row. Claim exists; chain weight is conditional. |
| **Subject identity at t=0** | Successor is a distinct subject. `parent_agent_id` records inheritance, not identity. |
| **Cross-role declaration** | Pre-check at onboard: if successor's class tag differs from parent's class tag (S8a), lineage declaration is **rejected** with audit event `lineage_cross_role_rejected`. Successor proceeds without lineage edge. (See §"Cross-role pre-check.") |
| **Promotion measurement** | R1's `score_trajectory_continuity(parent, successor)`, evaluated as successor accumulates check-ins. |
| **Promotion criterion** | `verdict == "plausible"` AND `successor.observations >= min_observations`. |
| **Demotion criterion** | `verdict == "unsupported"` AND `successor.observations >= min_observations`. |
| **Grace expiration** | After `grace_window` (default 30 days) without promotion or demotion, lineage edge is archived. Successor continues without lineage claim. |
| **Subject identity post-promotion** | Successor remains a distinct subject. The lineage *chain* (parent → successor) is now treated as one continuous behavioral pattern by downstream consumers, but observation-count credit is **forward-only** from promotion (no retroactive crediting of parent's accrued history; see §"Trust-tier crediting policy"). |
| **Effect of demotion on confirmed chains** | Per open question #3 (kept open): demote-in-v1 vs. fork-in-v1 trades interpretive-honesty for protocol simplicity. Default in this draft: demote, with explicit clawback semantics specified below. |
| **Multi-generation chain handling** | R1 evaluates only against immediate declared parent. No transitive trajectory windowing. Forward-only trust-tier crediting is per-link, not transitive across the chain. (See §"Multi-generation chains.") |

The asymmetry is the load-bearing structural claim: **successor's behavior cannot rewrite parent's records and — under forward-only crediting — cannot rewrite the *interpretation* of parent's pre-promotion records either.** Forward-only crediting is what makes the asymmetry honest at the interpretive layer; v1's recommendation A retroactively reinterpreted parent's prior records as "chain-continuous" based on successor's behavior, which the council flagged as an interpretive-rewrite that violated the asymmetry claim.

### What does NOT change at promotion

The interpretive boundary v2 commits to:

| Surface | Pre-promotion interpretation | Post-promotion interpretation |
|---|---|---|
| Parent's `agent_states` rows | "evidence about parent (a distinct subject)" | **unchanged** — still about parent |
| Parent's audit events | "events from parent's process-instances" | **unchanged** |
| Parent's KG contributions | "discoveries by parent under role X" | **unchanged** |
| Parent's accrued trust-tier observation count | "parent has N observations" | **unchanged** — the count belongs to parent's UUID |
| Lineage edge state | "successor *declares* parent (provisional)" | "successor's behavior under role X is *plausibly* continuous with parent's (confirmed)" |
| Successor's accruing observation count post-promotion | (was accruing under successor as a fresh agent) | now **also** accrues under the chain identifier (chain has its own counter, separate from parent's pre-promotion count) |

The chain gains a *new* observation counter at promotion that begins at 0 and accumulates from successor's post-promotion check-ins. Parent's pre-promotion observation count is *not* added to this counter. This is the forward-only commitment.

This means: a successor inheriting from a tier-3 parent does NOT become tier-3 at promotion. The successor remains at whatever tier its own observation count has earned. The chain has its own tier accrual, starting from 0. Over time, if the chain accumulates ≥ tier-3 observations under sustained behavioral similarity, the chain itself reaches tier-3 — but that takes ≥ N actual successor check-ins post-promotion, not "inherits parent's 500 in one moment."

Trade-off acknowledged: this loses the operational ergonomics of "successor jumps to parent's tier on a passing R1 score." It buys the asymmetry-honesty that the v1 council pass said was load-bearing. If R5 ships and provides replay-discrimination strong enough to close the security exposure of recommendation A, the trade can be revisited. Until then, B is the honest default.

## Cross-role pre-check

R1 measures per-dimension EISV-trajectory similarity assuming a shared operating envelope. A successor whose class tag differs from parent's class tag (e.g., a `session_like` successor declaring an `embodied` parent, or a `engaged_ephemeral` successor declaring a `resident_persistent` parent) will have a different operating envelope and R1 will return `unsupported` — not because the lineage claim is false, but because the role envelope mismatches.

If R2 silently allows cross-role declarations through to the demotion path, the audit log will fill with `lineage_demoted` events whose actual cause is "wrong role" — not "failed integration." This is a category error that pollutes the audit trail and makes telemetry uninterpretable.

**Pre-check rule.** At onboard, when `parent_agent_id` is provided:
1. Look up parent's class tags (`core.identities.metadata.class_tags` per S8a).
2. Compare to successor's class tags (which are stamped at onboard per S8a Phase 2).
3. If the primary class tag differs, **reject the lineage declaration** with audit event `lineage_cross_role_rejected`, payload `{successor_class, parent_class, reason: "role_envelope_mismatch"}`.
4. Successor proceeds with no `parent_agent_id` recorded. The `parent_agent_id` value the agent supplied is logged in the audit event but not written to `core.identities`.

**Caveat.** S8a's class-tag taxonomy is itself evolving. The pre-check defers to S8a's taxonomy of-the-day — if S8a adds or splits classes, R2 inherits the change without code modification. If the successor or parent has no class tag (orphan-record path before S8a backfill completes), the pre-check is skipped and the declaration proceeds as same-role (charitable default).

**Open question #1 (was Q5 in v1):** should cross-role lineage be *flagged-but-allowed* rather than *rejected*? Allow-with-flag preserves operator visibility into "X tried to claim Y's lineage despite role mismatch," which can surface attempted fork-reclassifications. Reject-at-declaration is cleaner but loses that signal. v2 picks reject as default for clean audit semantics; revisit if operator wants the flagged-allow visibility.

## What "memory" inventories (the inheritance surfaces)

Available to a successor declaring `parent_agent_id`:

| Surface | Source | Unconditional read? |
|---|---|---|
| KG discoveries written by parent | `knowledge` tool, KG storage | Yes |
| Audit events for parent | `audit.events` filtered by `agent_id` | Yes (operator-readable subset) |
| Parent's EISV trajectory | R1's planned `reconstruct_eisv_series` (internal helper, no agent surface) | Yes — used by R1 measurement |
| Parent's identity metadata (role, class tags, status) | `core.identities` row | Yes |
| Files keyed by parent agent_id | `data/`, log files, harness-side caches | Out-of-band; not gated by governance |

**R2 does not gate any of these reads.** What R2 gates is what the *system* concludes about identity-under-the-chain once the successor has acted on the read material.

## What "promotion" measures mechanically (v1: single-channel)

R2 v1 promotes when:

```
score_trajectory_continuity(parent, successor).verdict == "plausible"
AND successor.observations_in_window >= min_observations
```

R1 measures per-dimension DTW similarity over EISV trajectories. This is a **necessary** condition for behavioral continuity (an integrated successor's trajectory shape will resemble parent's under the shared role) but **not sufficient** (a non-integrated successor inhabiting the same operating envelope will produce a similar trajectory by environmental confound, not by integration).

R2 v1 ships with this gap explicit. The promotion event signals "behaviorally plausible chain" not "memory was operative." The forward-only trust-tier crediting policy (above) is what keeps the honesty-vs-security boundary defensible despite this gap — promotion confers interpretive credibility on the chain, not privilege.

R5 (memory-deepening tooling) is the load-bearing follow-on that adds replay-discrimination. When R5 ships, R2's promotion criterion gains a conjunct: `R1.plausible AND R5.integrated`. R2 v1's protocol structure does not change; the criterion gets sharper.

### Deferred — additional integration channels

Same as v1, with explicit acknowledgement these are deferred *not* because they're hard but to maintain single-channel discipline matching R1's pattern:

| Channel | What it would measure | Prerequisite |
|---|---|---|
| **KG cite-and-extend** | Successor's KG writes within window cite or build on ≥ N of parent's entries | Selected as R5 v0.1's first shadow channel in `docs/ontology/r5-memory-deepening-reality.md`; runtime R2 gating still deferred until telemetry matures. |
| **Calibration-envelope match** | Successor's per-agent calibration curve falls within parent's confidence envelope | Per-agent calibration storage (R1 deferred C3). |
| **Decision-distribution overlap** | Successor's decision distribution overlaps parent's | Persistent per-agent decision log (R1 deferred C4). |
| **Forced re-derivation** (R5's domain) | Successor independently reproduces N of parent's KG conclusions from raw inputs | R5 — out of scope for R2; this is the integration-vs-replay discriminator. |

**Open question #2:** should KG cite-and-extend ship as a v1.1 supplemental channel? It has no blocking prerequisite, and adding *any* channel orthogonal to trajectory similarity tightens the necessary-but-not-sufficient gap. Argument against v1: maintain R1's discipline of "single channel until prerequisite-blocked." Argument for v1.1: cite-and-extend is genuinely orthogonal (R1 cannot see KG writes), and it directly attacks the council's axiom-#12-conflation finding. Recommendation: **defer to v1.1**, after Phase 1 telemetry establishes a baseline. Surface the question now so it doesn't get lost.

## Promotion / demotion / archival protocol

### Lineage edge lifecycle (FSM, v2 — completed)

```
[declared at onboard]
     │
     ├─ class mismatch ──▶ [rejected]   (audit: lineage_cross_role_rejected;
     │                                   no edge written)
     ▼
[provisional]  ── R1.plausible + obs ≥ min ──▶ [confirmed]
     │
     ├─ R1.unsupported + obs ≥ min ──▶ [demoted]   (edge removed; audit: lineage_demoted)
     │
     └─ grace expired (default 30d) ──▶ [archived] (edge retired; audit: lineage_grace_expired;
                                                    successor continues fresh)

[confirmed]    ── R1.unsupported + obs ≥ min (re-eval) ──▶ [demoted]
                                                            (edge removed; chain counter cleared;
                                                             audit: lineage_demoted, reason="post_promotion_divergence")
                                                            (Open question #3 — fork vs demote)
```

(v1 omitted the cross-role rejection branch and the confirmed→demoted path. v2 completes both.)

### Evaluation triggers (v2 — anyio-safe)

R1 is invoked against an active provisional or confirmed pair when **any** of these fires:

1. **Sweeper task** (every 30min, mirroring `class_promotion_sweeper_task`). Runs as a background task under `_supervised_create_task`, outside the anyio handler context; safe to `await` asyncpg directly. Selects pairs where `(now - last_eval_ts) >= sweep_cadence` (default = 6h) and re-evaluates. Sweeper handles slow agents and grace-window expiration.
2. **Successor's `process_agent_update` lands** AND `(now - last_eval_ts) >= eval_cadence` (default = 1h). The update handler is in MCP/anyio context, so it does **not** call R1 inline. Instead, when the cadence guard passes, the handler calls `create_tracked_task(_evaluate_lineage_for(successor_id))` — fire-and-forget background dispatch. The dispatched task runs outside the anyio context and is safe to `await` R1's DB-touching scorer. (See §"Implementation pattern: anyio-safe check-in trigger.")
3. **Explicit re-evaluation request** from operator tooling (via `agent` MCP action or DB script).

The check-in trigger handles fast-moving sessions (active agents promote within hours of crossing `min_observations`). The sweeper handles slow agents and enforces the grace-window expiration. Both write to `last_eval_ts` to keep the cost bounded.

### Implementation pattern: anyio-safe check-in trigger

CLAUDE.md ("anyio-asyncio Conflict") requires that DB-touching work fired from MCP handlers use one of three patterns: read cached data, `run_in_executor` with sync client, or `asyncio.wait_for` with timeout-degrade. The check-in trigger uses **none** of these directly — it sidesteps the conflict by deferring the work entirely:

```python
# Inside process_agent_update handler (anyio context):
if successor.has_provisional_or_confirmed_lineage and \
   (now - successor.last_eval_ts) >= eval_cadence:
    create_tracked_task(
        _evaluate_lineage_for(successor.uuid),
        name=f"r2_lineage_eval_{successor.uuid[:8]}",
    )
    # Handler returns immediately; no await on R1 or DB.

# In _evaluate_lineage_for (asyncio task, NOT anyio context):
async def _evaluate_lineage_for(successor_uuid: str) -> None:
    parent_uuid = await _get_parent_uuid(successor_uuid)  # safe await, not in anyio
    score = await score_trajectory_continuity(parent_uuid, successor_uuid)
    await _apply_lineage_transition(successor_uuid, score)  # writes audit + state
```

This pattern matches existing background tasks (`class_promotion_sweeper_task`, `periodic_matview_refresh`, etc.) which all `await` asyncpg directly without the anyio conflict. The handler-context call is a fire-and-forget `create_tracked_task`, which does not block the anyio loop.

### Audit write path (v2 — handler-context-safe)

Audit events fire from two contexts:

1. **`lineage_cross_role_rejected`** and **`lineage_declared`** fire from the onboard handler (anyio context). Per the same rule above, these use the existing `append_audit_event_async` pattern that wraps writes in fire-and-forget background dispatch (see `agent_silent` audit at `src/background_tasks.py:1023-1032` for the precedent). Onboard handler does NOT block on the audit write.
2. **`lineage_promoted`**, **`lineage_demoted`**, **`lineage_grace_expired`** fire from `_evaluate_lineage_for` or from the sweeper — both outside anyio context — and can use the standard direct-await audit path.

### What changes at each transition (v2 — with clawback semantics)

| Transition | Storage change | Audit event | Downstream effect |
|---|---|---|---|
| **declared → rejected (cross-role)** | No edge written. Successor's `parent_agent_id` field cleared. | `lineage_cross_role_rejected` (new) | None. Successor proceeds as fresh. |
| **declared → provisional** | Write `provisional_lineage=true`, `lineage_declared_at=now`, `lineage_last_eval_at=NULL`. | `lineage_declared` (new) | Trust-tier ignores; KG provenance flags; dashboard shows "provisional" badge. |
| **provisional → confirmed** | Flip `provisional_lineage=false`; stamp `lineage_promoted_at=now`. Initialize chain observation counter (`chain_obs_count=0`). | `lineage_promoted` (new) | Trust-tier begins crediting forward-only to chain counter; KG provenance treats edge as confirmed; dashboard removes badge. **No retroactive credit.** |
| **provisional → demoted** | Remove edge (clear `parent_agent_id`). Successor's lineage history retained in audit only. | `lineage_demoted`, payload `reason="r1_unsupported"` | Trust-tier was already ignoring; no further effect. KG provenance loses edge. |
| **provisional → archived** | Mark edge `archived=true`; `lineage_archived_at=now`. Successor's `parent_agent_id` retained but inert. | `lineage_grace_expired` (new) | Same as demoted, with reason = "insufficient observations within grace window." |
| **confirmed → demoted (post-promotion divergence)** | Remove edge; **clawback chain counter** (set to 0; do not credit successor with chain counter accrual). | `lineage_demoted`, payload `reason="post_promotion_divergence"` | Trust-tier *for the chain* claws back; trust-tier *for successor as fresh agent* unaffected (forward-only credit was always per-link, not transitive). KG provenance must invalidate any chain-attributed aggregations. *(See open question #3.)* |

**Clawback honesty.** Confirmed→demoted does erase the chain-counter accrual that happened during the confirmed period. This is the operational consequence of the council's interpretive-rewrite finding: if we want to be honest that "post-promotion divergence means the chain was not what we thought it was," we have to claw back the credit the chain accumulated. The alternative (keep the chain counter, just stop adding) is dishonest because it preserves a trust signal whose underlying premise was retracted.

The forward-only trust-tier policy means clawback is bounded: only the *chain's* counter resets. Successor's per-UUID counter (which would have accrued naturally from check-ins) is unaffected.

## Storage (v2 — no longer deferred)

R2 uses the following columns on `core.identities` (canonical lineage table). R1 shipped the provisional/promotion subset; R2 Phase 1 shipped the lifecycle subset in migration 036:

**Reconciliation (2026-05-05):** R1 PR #306 (migration 031) already shipped `provisional_lineage`, `provisional_score_id`, `provisional_recorded_at`, and `confirmed_at` on `core.identities`. R2 reuses these. `lineage_promoted_at` in the table below is satisfied by R1's existing `confirmed_at` — R2 does not introduce a duplicate column. R2's Phase 1 implementation (#357, migration 036) added the genuinely new columns: `lineage_declared_at`, `lineage_demoted_at`, `lineage_archived_at`, `lineage_last_eval_at`, `chain_obs_count`. See `docs/handoffs/2026-05-04-r2-implementation-plan.md`.

| Column | Type | Default | Purpose | Status |
|---|---|---|---|---|
| `provisional_lineage` | BOOLEAN NOT NULL | `false` | True from declaration until promotion or until edge is removed. | Shipped by R1 #306 (migration 031) |
| `lineage_declared_at` | TIMESTAMPTZ | `NULL` | Timestamp of `parent_agent_id` first set. | Shipped by #357 (migration 036) |
| ~~`lineage_promoted_at`~~ → `confirmed_at` | TIMESTAMPTZ | `NULL` | Set on `provisional → confirmed`. | Shipped by R1 #306 as `confirmed_at` |
| `lineage_demoted_at` | TIMESTAMPTZ | `NULL` | Set on `* → demoted`; cleared on re-declaration. | Shipped by #357 (migration 036) |
| `lineage_archived_at` | TIMESTAMPTZ | `NULL` | Set on grace expiration. | Shipped by #357 (migration 036) |
| `lineage_last_eval_at` | TIMESTAMPTZ | `NULL` | Updated by sweeper or check-in trigger to enforce cadence guards. | Shipped by #357 (migration 036) |
| `chain_obs_count` | INTEGER NOT NULL | `0` | Forward-only counter for chain trust-tier accrual. Incremented on each post-promotion check-in. Reset to 0 on confirmed→demoted clawback. | Shipped by #357 (migration 036) |

**Schema decision rationale:** column-on-`core.identities` is simpler than a separate `lineage_edges` table. The `parent_agent_id` field already lives on `core.identities`; co-locating the lineage state machine there avoids JOINs on every read. A separate table would only be justified if multi-parent or multi-edge-per-pair semantics arise; v1 has neither. Revisit if R3's lineage-chain queries (S7) need a distinct edge table for graph-aware indexing.

**No `r1_score_audit` table introduced by R2.** R1 v3.2-A specified that table as part of R1's implementation row. R2 reads from it (for shadow-mode telemetry inspection) but does not introduce it.

## Multi-generation chains (v2 — addresses council finding 5a)

A chain may have arbitrary depth: `A → B → C → D` where each successor declared its immediate predecessor. R2 handles depth as follows:

1. **R1 evaluation** is always against the *immediate declared parent only*. C declares B; R1 scores C against B's trajectory. R1 does not re-score C against A. (Per R1's `score_trajectory_continuity` signature: `(claimed_parent_id, successor_id)` — it takes one parent.)
2. **Trust-tier crediting** is per-link, not transitive. When C is promoted on the C-B link, the C-B chain gains an interpretive credential and its own forward-only counter starts. C does NOT inherit B's chain counter from the B-A link. If the operator wants chain-of-chains aggregation (e.g., "all activity under the lineage tree rooted at A"), that's a query-level concern handled by S7's lineage-chain provenance schema, not by transitive trust-tier inheritance at R2's layer.
3. **Demotion at any link** affects only that link. Demoting the C-B link does not affect the B-A link. C loses its chain credential w.r.t. B; B's relationship to A is untouched.
4. **Multi-generation evaluation triggers**. If the sweeper finds a confirmed B-A link and a provisional C-B link, both get evaluated independently. There is no transitive evaluation.
5. **Cross-role across the chain** is rejected per-link at declaration. If A is class X, B declared A successfully (same class), C declares B as class Y — the C-B declaration is rejected as cross-role even though the C class is internally consistent with itself.

Open question for chain-of-chains aggregation policy is deferred to S7's KG provenance work.

## Caller policy

R2 inherits R1's `marks` policy at the onboard call-site (per R1 v3.1 §"Caller policy"): a fresh declaration always proceeds (subject to the cross-role pre-check), lineage edge is written `provisional`, R1 evaluates as observations accumulate. There is no "pre-flight rejection" in R2 *for same-role lineage*.

The orphan-archival call-site (S8) — when a long-idle agent re-emerges with declared lineage — runs R1 with `blocks` posture. **Asymmetry vs. onboard path acknowledged:** because the re-emergent successor by definition has few recent observations, R1 will return `inconclusive`, which the orphan call-site treats as orphan. The successor does NOT get the grace window R2 grants the onboard path. This is intentional (orphan = high suspicion, default-deny is correct under axiom #5 "do not stylize what has not yet earned continuity"); R2 v2 documents the asymmetry rather than papering over it.

## Test fixture (synthetic)

**Honest dependency note.** R1's test fixture (`tests/helpers/trajectory_fixtures.py` per R1 v3.1) does not exist in master either. R2's test fixture builds on R1's; R1 implementation row creates `synthetic_trajectory_pair`, R2 implementation row creates `synthetic_lineage_pair` on top.

`tests/helpers/lineage_integration_fixtures.py` (new) exports `synthetic_lineage_pair(seed, kind, observations)` returning `(parent_rows, successor_rows, expected_terminal_state)` where `expected_terminal_state ∈ {"confirmed", "demoted", "archived", "rejected_cross_role"}`.

Test cases in `tests/test_lineage_integration.py` (new):

1. **Genuine, fast-promotion.** Parent 30 rows; successor 10 rows from same generator. After successor's 5th check-in (`min_observations`), R1 returns `plausible`; lineage flips to `confirmed`. Assert `lineage_promoted` audit event fires once.
2. **Divergent, fast-demotion.** Parent 30 rows; successor 10 rows from independent generator. After 5th check-in, R1 returns `unsupported`; lineage edge removed. Assert `lineage_demoted` audit event with `reason="r1_unsupported"`.
3. **Inconclusive-then-grace-expired.** Parent 30 rows; successor accumulates 3 rows then idles. After grace window expires (test injects `now()` via `monkeypatch` on the time source used by both the sweeper's eval logic and the FSM transition function — the sweeper's wall-clock `asyncio.sleep` loop is NOT exercised; `_evaluate_lineage_for` is invoked directly with mocked time), sweeper archives. Assert `lineage_grace_expired` audit event.
4. **Inconclusive-then-promotion.** Successor's first 5 check-ins fall in plausibility band 0.55–0.70 (`inconclusive`); next 5 push average above 0.70. Assert promotion at observation 10, not 5.
5. **Promotion-then-stable.** After promotion, simulate 50 more successor check-ins matching parent. Assert no further state changes; sweeper does not re-flip. `chain_obs_count` increments by 50.
6. **Promotion-then-divergent.** Successor matches parent for 10 check-ins (promoted; `chain_obs_count=10`), then drifts hard for 20 check-ins. Assert: confirmed → demoted transition fires after R1 re-evaluates and returns `unsupported`; `chain_obs_count` reset to 0; `lineage_demoted` audit with `reason="post_promotion_divergence"`. **(This test asserts the recommended "demote" answer to open question #3; if the question resolves to "fork," this test is rewritten.)**
7. **Cross-role rejection at onboard.** Parent class = `embodied`; successor class = `session_like`. Onboard with `parent_agent_id=parent.uuid`. Assert `parent_agent_id` is NOT written to `core.identities`; `lineage_cross_role_rejected` audit event fires with both class tags in payload.
8. **Multi-generation per-link.** Chain `A → B → C`: A and B confirmed (B-A link in `confirmed` state). C declares B; R1 scores C only against B (not A); when C is promoted, the C-B chain counter starts at 0 independently of B's chain counter under B-A.
9. **Triggers cadence.** Verify check-in-triggered eval respects `eval_cadence`; verify it dispatches `create_tracked_task` and does NOT inline-await R1; verify sweeper-triggered eval respects `sweep_cadence`; verify operator override bypasses both.
10. **Conftest stub registration regression.** This is a meta-test — it verifies that `tests/conftest.py:_isolate_db_backend` registers the new mock methods (`reconstruct_eisv_series`, `score_trajectory_continuity`, `get_provisional_lineage_pairs`, `update_lineage_state`). Per R1 v3.2-E, missing stubs produce auto-generated `AsyncMock` children returning coroutines instead of lists; this test catches that class of failure at CI time. Lists the new method names that must be registered.

## Shadow-mode calibration path

R2 ships in two phases:

1. **Phase 1 (storage + audit, no enforcement).** Land the new columns, audit event types, evaluation infrastructure, sweeper task. Edges are written `provisional`; promotion/demotion/rejection logic runs and writes audit events; **downstream consumers are not yet wired**. Operator dashboards show the audit stream so calibration of `min_observations`, `eval_cadence`, `sweep_cadence`, `grace_window` can happen against real fleet data.
2. **Phase 2 (downstream wiring).** Trust-tier (S6), KG provenance (S7), R3 baselines, dashboard each consume the `provisional_lineage` flag and chain counter. Promotion becomes operationally meaningful. Phase 2 ships only after Phase 1 audit data shows promotion/demotion/rejection rates and timing distributions are sensible.

R1's calibration-status framing (v3.2-C: `seeded` → `earned`) applies recursively to R2: R2 ships at `seeded`; flip to `earned` is an explicit operator action after Phase 1 telemetry passes a cutoff (proposed: ≥4 weeks Phase 1 + ≥50 promoted pairs + ≥10 demoted pairs + ≥1 rejected-cross-role event observed).

## Observability

Following the `class_promotion_sweeper_task` precedent:

- **State transitions emit audit events** (`lineage_declared`, `lineage_promoted`, `lineage_demoted`, `lineage_grace_expired`, `lineage_cross_role_rejected`).
- **Sweeper cycles do NOT emit audit events** when no transition occurs. Per-cycle visibility lives in `logger.debug` lines and a stat counter exposed via the metrics endpoint (e.g., `r2_sweeper_cycles_total`, `r2_evaluations_total`, `r2_no_transition_total`). This matches the existing `class_promotion_sweeper_task` convention (zero audit events emitted when no class promoted).
- **Identity response addition.** `provisional_lineage` is added to the `identity()` response (as a top-level boolean field). Phase 1 callers are expected to ignore this field; Phase 2 wires consumers.
- **Onboard response addition.** `provisional_lineage` and `lineage_state` (literal `"provisional" | "rejected_cross_role" | "no_lineage_declared"`) are added to the `onboard()` response so a freshly-onboarded agent knows immediately whether its lineage declaration succeeded, was rejected, or was recorded as provisional.

## Dependency map (v2)

```
R1 ─── provides trajectory-similarity gate to ─── R2
R2 ─── provides Q1's operational definition ──── (inheritance is data-access; chain-credential is provisional until promoted)
R2 ─── provides Q2's operational definition ──── (subagents structurally fall to "archived" after grace expiration; principled by the same protocol that handles all agents)
R2 ─── unblocks ────────────────────────────── R5 (R5 plugs into R2's promotion criterion as an additional conjunct)
R2 ─── coordinates with ────────────────────── S6 (chain trust-tier crediting via forward-only chain counter)
R2 ─── coordinates with ────────────────────── S7 (lineage-chain provenance, multi-generation aggregation)
R2 ─── coordinates with ────────────────────── S8a (cross-role pre-check consumes class tags)
R1 implementation row ─── blocks ──────────── R2 implementation row
```

R2 design proceeds in parallel with R1 implementation. R2 implementation sequences after R1 implementation lands.

(v1 mistakenly framed Q2 as "informs but does not unblock." Per the council finding, R2 *does* unblock Q2's operational definition even if it doesn't force code action. Corrected.)

## Open questions for Kenny

(v1 had 5 open questions. v2 has resolved 2 of them via the council pass: retroactive vs. forward-only crediting → forward-only [decided]; whether to add `provisional_lineage` to the response → yes, both `identity()` and `onboard()` [decided]. Three new questions surfaced from v2's structural changes.)

1. **Cross-role lineage: reject vs. flag-and-allow?** v2 picks reject for clean audit semantics. Flag-and-allow preserves operator visibility into "X tried to claim Y's lineage despite role mismatch" (potentially surfacing attempted reclassification). Recommendation: **reject in v1**; revisit if Phase 1 telemetry shows operator wants the attempted-cross-role signal preserved.

2. **KG cite-and-extend as v1.1 supplemental channel?** **Partially resolved 2026-05-05:** `docs/ontology/r5-memory-deepening-reality.md` selects KG cite-and-extend as R5 v0.1's first shadow channel. Runtime R2 gating remains deferred until Phase 1 trajectory-only telemetry establishes a baseline.

3. **Confirmed → demoted: clawback (current rec) vs. fork?** v2 picks demote-with-clawback. Demote is operationally simpler but does erase the chain counter that accrued during the validly-confirmed period (the council's interpretive-rewrite concern, accepted as the price of the simpler protocol). Fork preserves the pre-divergence segment as still-valid-for-its-time and starts a new fork-point edge for post-divergence behavior. Fork is more honest under axiom #3 but adds data-model complexity (multi-edge per pair, fork-point timestamps, chain-counter splits). Recommendation: **demote-with-clawback in v1**, escalate to fork in v1.1 if demotion oscillation pathology surfaces in Phase 1 telemetry.

## Appendix: what this does NOT solve (v2 — expanded)

- **Replay attacks.** A successor that mechanically replays parent's trajectory passes R1, passes R2 promotion, but has not integrated. R5 is the discriminator. Captured limitation; R2 v1 ships with the gap. **Forward-only trust-tier crediting (v2's strategic call) bounds the operational damage** — promotion does not confer privilege, only interpretive credibility; replay-attack succeeds at the credibility layer but not at the security layer until R5 lands and either (a) closes the replay gap or (b) explicitly authorizes recommendation A.
- **Trajectory portability across roles.** Closed by the cross-role pre-check (v2). Cross-role declarations are rejected at onboard rather than silently demoting downstream.
- **Subagent ephemerality.** R2 v2 makes Q2 a measurement question rather than a definitional one: subagents fall to `archived` after grace expiration via the same protocol that handles all agents. Whether subagents *should* be exempt or *should* fall through naturally is now an operator-configurable knob (`grace_window` per class), not an ontology question.
- **Multi-channel integration measurement.** Single-channel v1 by design; cite-and-extend kept open as v1.1 candidate.
- **Adversarial substrate-claim.** S19's domain. R2 layers on top of an honest substrate-attestation; if S19's attestation is bypassed, R2's promotion can be triggered by a forged successor. R2 inherits S19's threat model.
- **Cross-process-instance memory caching.** R2 specifies what a fresh process *reads at onboard*; whether that read is cached in process-instance memory is a harness concern. R2 v2 unchanged on this.
- **Multi-generation transitive trust-tier inheritance.** Per-link only; chain-of-chains aggregation deferred to S7.

## Appendix: review provenance

- v1 council pass 2026-05-02 (parallel three-agent: `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). Convergent verdict: withhold pending v2. Forcing items: asymmetry violation by recommendation A; axiom #12 conflation; multi-generation chains unspecified; R1 entirely unimplemented; `provisional_lineage` column nonexistent; check-in eval anyio-deadlock risk. Substantive items: trust-tier-vs-security boundary under R1 forgery; demote-vs-fork interpretive symmetry; cross-role silent demotion; orphan-re-emergence asymmetry; FSM gaps; audit-write path; identity-vs-onboard response asymmetry; subject-identity language slips. Live-verifier ground-truthed all runtime claims via `mcp__unitares-governance__list_tools`, `identity()`, `agent(get)`, `describe_tool(onboard)`, and direct DB queries on `core.identities`, `core.agents`, `audit.events` (38 distinct event types enumerated, no `lineage_*` collisions), `core.schema_migrations` (latest = v29 lease_plane).

- v2 (this revision) addresses every forcing item, all substantive items, and the relevant nits. Three remaining open questions (cross-role reject vs. flag-allow; v1.1 cite-and-extend; demote vs. fork) are surfaced for operator decision rather than silently defaulted.

- Future council pass on v2: should be lighter-touch — verify forcing items closed, check no new ones introduced, confirm the strategic calls (forward-only crediting, single-channel discipline maintained, anyio sidestep pattern) are sound. If v2 lands clean, R2 design is acceptance-ready pending Kenny's three open questions.

---

**End v2 draft.** Ready for second-pass council review (light) and for Kenny's read on the three open questions.
