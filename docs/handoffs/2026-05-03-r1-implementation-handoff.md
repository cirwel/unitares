# R1 Implementation Handoff — 2026-05-03

**Branch:** `claude/document-project-handoff-O84Os`
**Spec of record:** `docs/ontology/r1-verify-lineage-claim.md` v3.3 (2026-05-03)
**Plan row:** R1 (`docs/ontology/plan.md`)
**Status:** Shipping in flight — foundation + primitive + first consumer landed; call-site wiring + remaining consumers + KG public emission still open.

> This document is a snapshot of where R1 implementation sits as of 2026-05-03. It pairs with the memory-layer anchor `project_r1-implementation-handoff.md`. Read alongside the v3.3 amendment in the spec.

---

## 1. What R1 is

`score_trajectory_continuity(claimed_parent_id, successor_id, *, min_observations=5, window=30d)` — a single-channel plausibility primitive measuring per-dimension DTW similarity between parent and successor EISV trajectories reconstructed from `core.agent_state`. Output is a dataclass with `verdict ∈ {plausible, inconclusive, unsupported}` plus a strictly-redacted public KG shape.

Single-channel design (v3.1) is final. v3.2 added provisional-lifecycle and calibration-status normative fields; v3.3 tightened public redaction, added `calibration_failed` as a first-class third state, named `core.identities` as the storage target for `provisional_lineage`, and corrected eight doc-text errors.

R1 is not authentication, not an identity issuer, not an integration test, not a substitute for R4. See spec §"Non-goals (explicit)".

---

## 2. Prereqs (all green as of 2026-05-03)

| Prereq | Status |
|---|---|
| S8c — `spawn_reason` write-path repair | ✅ #155 (2026-04-25) |
| S8a Phase 2 — `session_like` class | ✅ #252 (2026-05-01) |
| Light council confirmation pass on v3.2 | ✅ ran 2026-05-03 (verdict WITHHOLD-PENDING-V3.3) |
| v3.3 amendment | ✅ commit `d446c1f` (2026-05-03) |

---

## 3. Shipped against v3.3

| PR | Commit | Spec sections | Scope |
|---|---|---|---|
| PR 1 (#306) | `f30d192` | §A, §D, §E, §F, §I | Migration 031 (4 provisional columns on `core.identities` + `audit.r1_score_audit` RANGE-partitioned 180-day retention table + partition-maintenance wiring); `StateMixin.reconstruct_eisv_series` (epoch-aware, `synthetic=false` filter, window-bounded); epoch column backport into `db/postgres/schema.sql` and `db/postgres/knowledge_schema.sql` |
| PR 2 (#309) | `83e70aa` | §A, §C, §H | `src/identity/trajectory_continuity.py` — `TrajectoryContinuityScore` dataclass + `score_trajectory_continuity` primitive (per-dim DTW, empty-dim skip-not-zero per §H.C4, seeded thresholds 0.55 / 0.70); `AuditMixin.record_r1_score_audit` (fail-loud on write failure per §A join-key durability); `_build_public_payload` redaction shape `{verdict, calibration_status, n_dims_used, score_id}` |
| PR 3 (#314) | `d4740c8` | §C, §D, §G | Migration 032 (`core.r1_calibration_state` singleton, three-state enum); migration 033 (`verdict` + `raw_verdict` columns on `audit.r1_score_audit`); `IdentityMixin.mark_lineage_provisional` + `confirm_lineage` + `read_r1_calibration_state` + `transition_r1_calibration_state` (atomic UPDATE...RETURNING); calibration-status snapshot at scoring time; `calibration_failed` verdict degradation to `inconclusive` for consumer purposes (raw verdict preserved in audit + `reasons`); `class_tag` stamped from parent's `core.identities.metadata.tags` at scoring time |
| PR 4a (#320) | `fecaadb` | §D (consumer 1 of 4) | Trust-tier (S6) provisional gate: `IdentityMixin.is_lineage_provisional`; `_provisional_lineage_tier_dict` returning tier 1 with `source='provisional_lineage_gate'`; `resolve_trust_tier` runs the provisional gate **before** substrate-earned + `compute_trust_tier` so substrate-anchored agents (Vigil/Lumen) with provisional lineage correctly land at tier 1. Strict `is True` identity check defends against unmocked AsyncMock returning truthy. `prefetched_provisional` kwarg lets future callers skip the DB roundtrip |

Test counts at the most recent merge: **8191 passed, 33 skipped, 0 fail**.

---

## 4. Open work — remaining for full v3.3 closure

### 4.1 Call-site wiring — onboard ✅ shipped 2026-05-03 (#321); promotion + orphan still open

`score_trajectory_continuity` is wired into the onboard handler with the `marks` policy as of #321 (`452354b`). Helper `_score_lineage_continuity_bg` in `src/mcp_handlers/identity/handlers.py` fires the score in a tracked background task on both fresh-identity-with-lineage sites; verdict=`inconclusive` calls `mark_lineage_provisional`. The 4-week shadow window per `plan.md` line 25 began on this merge.

Remaining call sites:

| Call site | Policy on `inconclusive` | Where to wire |
|---|---|---|
| ~~`onboard`~~ | ~~`marks`~~ | ~~`src/mcp_handlers/identity/handlers.py`~~ ✅ shipped #321 |
| Trust-tier promotion | `blocks` (refuse promotion) | `src/identity/r1_maintenance.py::sweep_provisional_lineage` + `scripts/migration/r1_lineage_maintenance.py promote-provisional` ✅ shipped 2026-05-05 |
| Orphan archival re-classification | `blocks` | `sweep_provisional_lineage` reports `unsupported` as `orphan_candidate`; destructive lineage-edge removal remains intentionally unwired until a removal primitive is specified |

`confirm_lineage` now has an operator-facing caller. Running `python scripts/migration/r1_lineage_maintenance.py promote-provisional --apply` re-scores provisional lineage claims; plausible scores clear `provisional_lineage` via `confirm_lineage`, inconclusive scores block, and unsupported scores are reported as orphan candidates. Important operational detail: evaluation calls `score_trajectory_continuity`, so it writes the normal R1 audit/KG score records even without `--apply`; `--apply` controls only the confirmation mutation.

First live run, 2026-05-05: 3 provisional rows evaluated, 1 confirmed, 2 blocked as inconclusive, 0 orphan candidates.

### 4.2 Remaining v3.3-D consumers

PR 4a covered consumer 1 of 4. Remaining:

| Consumer | File / module | Action per v3.3-D |
|---|---|---|
| KG provenance (S7) | `src/storage/knowledge_graph_postgres.py` + `src/db/mixins/knowledge_graph.py` (exact site identified during impl) | Aggregations of "lineage-attributed activity" exclude `provisional_lineage = true` by default; explicit query opt-in shows them |
| R3 role baselines | `src/trajectory_identity.py` baseline distribution computation | Exclude provisional pairs from baseline fits |
| Dashboard | `unitares-dashboard/` (specific file TBD) | Show "provisional" badge with `provisional_recorded_at`; may ship as follow-up per v3.3-D scoping note |
| R2 honest memory | (R2 impl row, not R1) | Excluded from forward-only chain crediting; lives in R2's own PR per v3.3-B |

### 4.3 KG public emission — in flight (#324)

PR 2's commit message stated: *"KG public emission (the actual write to AGE) deferred to PR 3 alongside consumer patches; `_build_public_payload` here defines the redaction shape callers will pass."* PR 3 stayed score-side.

#324 closes this — adds `_emit_public_kg_node` inside `score_trajectory_continuity` that publishes the redacted payload to `knowledge.discoveries` via `kg_add_discovery`. Deterministic node id `r1_score:{uuid5(...)}` per (parent, successor) pair; existing ON CONFLICT (id) DO UPDATE gives v3.2-D dedupe-by-pair. v3.3-I corrected the target from AGE to PG-FTS; #324 follows that correction.

What #324 did not pick up, now partially closed:

- 30-day TTL archival of public nodes per v3.2-D second clause — operator-facing sweep shipped 2026-05-05: `python scripts/migration/r1_lineage_maintenance.py archive-public-kg --apply`.
- Re-scoring at promotion/orphan paths — operator-facing re-score sweep shipped 2026-05-05; unsupported lineage is reported as `orphan_candidate`, but destructive edge removal is still pending an explicit primitive/spec.

First public-KG TTL dry-run, 2026-05-05: 0 stale R1 score nodes.

### 4.4 Performance follow-up (non-blocking)

PR 4a flag, surfaced by reviewer, accepted as deferred:

> 3 call sites of `resolve_trust_tier` (`src/trajectory_identity.py:686`, `:693`, `src/agent_metadata_persistence.py:189`) now trigger a DB roundtrip per call because none pass `prefetched_provisional`. Optimization waits on `IdentityRecord` gaining the `provisional_lineage` field; until then, the gate's correctness > the roundtrip cost.

When `IdentityRecord` is extended, thread the prefetched value through these three sites.

---

## 5. Single-writer / collision risks

R1 implementation touches the **identity / onboarding** single-writer surface called out in `CLAUDE.md` ("identity/onboarding — docs AND implementing code are one coupled surface"). Before opening any of the work in §4 above:

```bash
gh pr list -R CIRWEL/unitares --search "in:title,body R1" --state open
gh pr list -R CIRWEL/unitares --search "in:title,body provisional_lineage" --state open
gh pr list -R CIRWEL/unitares-governance-plugin --search "in:title,body R1" --state open
```

If an in-flight PR exists, branch from its head, do not start a parallel attempt. The 2026-04-26 plugin PR #23/#24 collision and the 2026-04-29 migration-drift incident are the canonical examples of why.

The onboard wiring step in §4.1 also crosses the doc/code boundary (handler + operator-runbook update for the new `provisional_lineage` lifecycle in identity flows). Coordinate the doc and code edits in the same PR.

---

## 6. Calibration window — counting from when?

Per the operator decision in `plan.md` §"2026-04-25" (refreshed by v3.3):

> shadow-mode cutoff bumped to **≥100 pairs OR ≥4 weeks, whichever later** (was ≥50/≥2 weeks; corpus-maturity caution from Schmidt n=15 generalizes)

"Pairs" = recorded `audit.r1_score_audit` rows with non-null `parent_id` + `successor_id`. The 4-week clock starts when the **onboard wiring** in §4.1 lands and `score_trajectory_continuity` begins firing in production — not when the migration shipped.

Once both conditions are met, the operator can run the calibration analysis and call `transition_r1_calibration_state('earned' | 'calibration_failed')`. Until then, `calibration_status` stays `seeded` and downstream verdicts are advisory only.

A `seeded_since` ≥90 days without an operator transition surfaces as a flag in the operator's dashboard view per v3.3-C — not a hard cutoff, a visibility primitive.

---

## 7. Known limitations carried forward

From v3.2 §"Captured as known limitation, not a v3.2 fix" + v3.3-G:

- **Resident-class deterministic-script clusters.** Chronicler-style daily-cron pairs score high deterministically (same script behavior, not behavioral lineage). Mitigation lives in `class_tag` on the audit row (v3.3-G, shipped in PR 3): calibration analysis filters by class at analysis time. Until enough `session_like` and script-driven-daily-cron pairs accumulate, R1 will under-discriminate within those classes.
- **Adversary with KG read access can forge a passing trajectory.** R1 detects honest over-claims, not adversarial spoofing. This is by design — the strict redaction in v3.3-A reduces the leak surface but does not close it.
- **Subject-ambiguity at very low observation counts.** Fixtures and the `min_observations=5` floor together prevent the worst pathological cases; the `parent_mature` boolean in the dataclass + audit is the surface for callers that need to be more conservative.

---

## 8. What this handoff is NOT

- Not a re-spec. The spec of record is `docs/ontology/r1-verify-lineage-claim.md` v3.3.
- Not an implementation order claim beyond §4.1's high-priority callout. Whoever picks this up should re-read the spec and the four open consumer rows in v3.3-D before sequencing.
- Not a schedule. The 4-week shadow window starts when wiring lands, not when this doc lands.
- Not a license to delete the seeded thresholds in code. Per v3.3-C the `seeded → earned` transition is an explicit operator action; thresholds may move at that point or stand.

---

## 9. Quick reference

**Spec:** `docs/ontology/r1-verify-lineage-claim.md` (v3.3 amendment at line 305+)
**Plan row:** `docs/ontology/plan.md` row R1 (line 25)
**Primitive:** `src/identity/trajectory_continuity.py:130` `score_trajectory_continuity`
**Backend helpers:**
- `src/db/mixins/state.py:327` `reconstruct_eisv_series`
- `src/db/mixins/identity.py:251` `mark_lineage_provisional`
- `src/db/mixins/identity.py:285` `confirm_lineage`
- `src/db/mixins/identity.py` `read_r1_calibration_state`, `transition_r1_calibration_state`, `is_lineage_provisional`
- `src/db/mixins/audit.py:31` `record_r1_score_audit`

**Trust-tier consumer:** `src/identity/trust_tier_routing.py` (`_provisional_lineage_tier_dict`, `_is_provisional_lineage`, `resolve_trust_tier` gate ordering)

**Migrations:**
- `031` — provisional columns + `audit.r1_score_audit` partitioned table + partition maintenance
- `032` — `core.r1_calibration_state` singleton
- `033` — `verdict` + `raw_verdict` columns on `audit.r1_score_audit`

**PRs:** #306 (PR 1), #309 (PR 2), #314 (PR 3), #320 (PR 4a)
