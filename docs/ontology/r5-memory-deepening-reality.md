# R5 - Memory-Deepening Reality Tooling

**Status:** Shadow scorer implemented. No R2 runtime gate.
**Last Updated:** 2026-05-06
**Scope:** Plan row R5 (`docs/ontology/plan.md`). Specifies the first concrete channel for distinguishing "successor read inherited memory" from "successor's behavior actually used inherited memory."
**Builds on:** R2 Phase 1 (#357), R1 trajectory-continuity scoring, KG `response_to` links.

---

## Purpose

R2 Phase 1 makes lineage honest at the behavior layer: a successor's lineage claim starts as provisional, then R1 can promote or demote it based on trajectory similarity.

That is necessary, not sufficient. A successor can behave in the same envelope as its parent without integrating the parent's memory. R5 supplies the next evidence channel: did the successor cite, extend, correct, or otherwise operate on concrete memory artifacts inherited from the parent?

## First Channel: KG Cite-And-Extend

R5 v0.1 chooses **KG cite-and-extend** as the first supplemental channel.

Reasons:

- It has no new storage prerequisite. KG discoveries already have `agent_id`, `response_to`, `response_type`, provenance, timestamps, and lineage context.
- It is orthogonal to R1. R1 sees EISV trajectory shape; KG cite-and-extend sees knowledge-use behavior.
- It keeps R2 Phase 1 clean. The scorer can run in shadow/advisory mode until R2 Phase 1 telemetry matures.

Deferred channels remain valuable, but they are heavier:

| Channel | Why deferred |
|---|---|
| Forced re-derivation | Requires task fixtures, raw-input reconstruction, and grading. Stronger signal, larger surface. |
| Behavioral backtests | Requires persistent prior-query sets and expected-answer policy. |
| Self-knowledge reflection | Useful for operator-facing honesty, but too easy to fabricate as the first discriminator. |

## Definitions

**Parent memory artifact:** A KG discovery authored by the immediate declared parent. v0.1 does not traverse multi-generation chains; S7 owns chain-of-chains provenance.

**Successor response:** A KG discovery authored by the successor with `response_to.discovery_id` pointing at a parent memory artifact.

**Constructive response types:** Existing KG store paths accept `extend`, `support`, `answer`, `follow_up`, `correction`, `elaboration`, and `supersedes`. For v0.1:

- Strong constructive: `extend`, `elaboration`, `correction`, `supersedes`, `answer`.
- Weak constructive: `support`, `follow_up`.
- Non-integrating by default: `question`, `disagree`.

This classification is not moral judgment. A `disagree` response can be valuable, but by itself it does not show the successor integrated the parent's working memory into a new operational conclusion.

## Implemented Primitive

Runtime module: `src/identity/memory_integration.py`.

Public signature: `async def score_memory_integration(parent_id, successor_id, *, channel="kg_cite_extend", window_days=30, min_parent_discoveries=3, min_strong_extensions=2, min_distinct_parent_targets=2, graph=None, now=None, max_discoveries=500) -> MemoryIntegrationScore`.

The `graph`, `now`, and `max_discoveries` parameters are operator/test controls; the normal path uses the configured KG backend and current UTC time.

Batch shadow sampling is also available via `score_memory_integration_batch(...)` and `scripts/diagnostics/score_r5_memory_integration.py --lineage-state provisional|confirmed|all`.

Return shape:

| Field | Meaning |
|---|---|
| `score_id` | Unique score identifier. |
| `parent_id` / `successor_id` | Immediate lineage pair being scored. |
| `channel` | Literal `kg_cite_extend` in v0.1. |
| `verdict` | One of `integrated_candidate`, `weak_signal`, `absent`, `insufficient_parent_memory`, `inconclusive`. |
| `confidence` | Bounded heuristic confidence, not a calibrated probability in v0.1. |
| `parent_discoveries_seen` | Eligible parent KG artifacts in the scoring corpus. |
| `cited_parent_discoveries` | Distinct parent artifacts cited by successor responses. |
| `strong_extensions` / `weak_extensions` | Counts by response-type class. |
| `successor_discoveries_seen` | Eligible successor KG writes in the scoring window. |
| `cited_discovery_ids` | Parent discovery IDs cited by successor. |
| `generated_discovery_ids` | Successor discovery IDs that cite parent memory. |
| `reasons` | Human-readable explanation list. |
| `calibration_status` | `seeded`, `calibrating`, or `calibrated`. |

## Verdict Rules

v0.1 thresholds are seeded defaults. They are not load-bearing until calibrated.

| Verdict | Rule |
|---|---|
| `insufficient_parent_memory` | Parent has fewer than `min_parent_discoveries` KG discoveries in the eligible window/corpus. Absence of citations is not evidence when there was too little parent memory to use. |
| `integrated_candidate` | Successor has at least `min_strong_extensions` strong constructive responses to at least `min_distinct_parent_targets` distinct parent discoveries. |
| `weak_signal` | Successor has at least one parent citation, but does not meet the strong-extension threshold. |
| `absent` | Parent corpus is sufficient and successor has no eligible response links within the window. |
| `inconclusive` | KG backend/read failure, ambiguous authorship, or conflicting status/provenance makes the result unsafe to interpret. |

Confidence is a bounded heuristic over count strength and target diversity. v0.1 should expose the raw counts prominently so operators do not over-read a scalar.

## Integration With R2

R2 Phase 1 remains unchanged: `R2 Phase 1 promotion = R1 plausible + min observations`.

R5 v0.1 does **not** alter that rule. It produces advisory telemetry only.

Future R2 v1.1 may add a stricter conjunct: `R2 v1.1 promotion = R1 plausible + R5 integrated_candidate + min observations`.

Do not enable that conjunct until there is enough shadow data to evaluate false negatives. Sparse KG writers should not be demoted merely because they did not write citations.

## Implementation Sequence

1. **PR 0: this spec + plan update.** Done 2026-05-05. No runtime behavior.
2. **PR 1: read-only scorer.** Done 2026-05-06 in `src/identity/memory_integration.py`. Queries existing KG rows only; no schema migration.
3. **PR 2: tests.** Done 2026-05-06 in `tests/test_memory_integration.py`. Synthetic coverage: strong, weak, absent, insufficient-parent-memory, inconclusive, and archived-parent exclusion.
4. **PR 3: operator surface.** Done 2026-05-06 via `scripts/diagnostics/score_r5_memory_integration.py`. Supports single-pair scoring and batch sampling over provisional/confirmed lineage pairs. Read-only shadow scoring; not called from the R2 hot path.
5. **PR 4: optional audit table.** Only if shadow operation needs durable score history. Candidate table name: `audit.r5_memory_integration_audit`.
6. **PR 5: R2 v1.1 decision.** After telemetry, decide whether to add R5 as a conjunct to R2 promotion or keep it advisory.

## Shadow Sample 2026-05-06

First live read-only sample used:

- `scripts/diagnostics/score_r5_memory_integration.py --lineage-state provisional --limit 10 --max-discoveries 200`
- `scripts/diagnostics/score_r5_memory_integration.py --lineage-state confirmed --limit 10 --max-discoveries 200`

Results:

| Lineage state | Pairs scored | Verdicts |
|---|---:|---|
| `provisional` | 4 | 3 `insufficient_parent_memory`, 1 `absent` |
| `confirmed` | 3 | 3 `insufficient_parent_memory` |

Interpretation: the first corpus is too sparse to justify a durable R5 audit table or any R2 promotion conjunct. Most parent agents do not yet have enough eligible KG memory artifacts under the seeded `min_parent_discoveries=3` rule. The lone `absent` result is useful as a proof of the query path, not as policy evidence.

## Non-Goals

- No automatic R2 promotion/demotion changes in v0.1.
- No multi-generation chain traversal. Immediate parent only.
- No semantic proof that a citation is meaningful. That can be added later with semantic comparison or reviewer sampling.
- No agent-facing requirement to write KG citations on every inherited-memory use. That would turn a signal into performative paperwork.
- No security claim. An adversary with KG read/write access can fabricate citations. R5 is an honesty primitive, not authentication.

## Risks

**Goodharting.** If agents learn that citations are rewarded, they may write superficial `response_to` links. Mitigation: start shadow-only, expose raw counts, and later sample for semantic quality.

**Sparse-memory false negatives.** Some parent agents may not write enough KG discoveries. Mitigation: `insufficient_parent_memory` is separate from `absent`.

**Harness variance.** Some harnesses make KG writing easier than others. Mitigation: classify by harness once S22 provenance exists; do not compare raw citation rates across harnesses prematurely.

**Parent memory quality.** Extending bad memory can be integration but still be wrong. R5 measures memory use, not correctness. Correctness remains calibration/outcome territory.

## Open Questions

1. Should `disagree` ever count as strong integration when it includes a correction or supersession? v0.1 says no unless the response type is `correction` or `supersedes`.
2. Should parent corpus include discoveries by confirmed ancestors once S7 lineage-chain provenance lands? v0.1 says no.
3. Should the operator surface be a migration script, an MCP diagnostic action, or both? Prefer script first to keep the hot path untouched.
