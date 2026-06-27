# Identity Ontology — Reader's Guide

This folder is the system's **versioned identity ontology** — the conceptual model of what "an agent" is in UNITARES, plus the working RFCs that evolved it. It is referenced from `src/`, `tests/`, `AGENTS.md`, `CLAUDE.md`, and the paper. The folder name is load-bearing; renaming it is a refactor, not a cleanup.

## Start here (in order)

1. **[`identity.md`](identity.md)** — the canonical ontology doc, v2. Three stances (performative / descriptive / inventive), the layered taxonomy of continuity, and the Synthetic Life Axioms it's evaluated against. ~30 min read.
2. **[`plan.md`](plan.md)** — the **resolution ledger**: every open question, research item, and system implication that flows from `identity.md`, with state and dependencies. Read the tables, skim the appendix. Not a starting point — a state board.
3. **[`paper-positioning.md`](paper-positioning.md)** — how the ontology relates to the v6 paper and the planned v7. Read after `identity.md`.

The remaining files are working RFCs and specs that resolve specific rows in `plan.md`.

## File naming convention

Prefixes correspond to row IDs in [`plan.md`](plan.md):

| Prefix | Stance | Meaning |
|---|---|---|
| `Q` | Open question | Things we don't know yet; resolved by re-reading + decision |
| `R` | Research agenda (**inventive**) | New primitives we're building so claimed continuity *earns* its claim |
| `S` | System implications (**descriptive**) | Cleanups so the system stops claiming what it can't verify |
| sub-letter (`s8a`, `s21b`) | Phase or sub-item of the parent row | |
| `v7-*` | Paper v7 alignment specs | |

So `r1-verify-lineage-claim.md` resolves row R1 in `plan.md`; `s1-continuity-token-retirement.md` resolves row S1; `s8a-phase2-prep.md` is phase 2 of S8a.

## Index by category

**Conceptual entries** (read these for the model):
- [`eisv-proprioception-contract.md`](eisv-proprioception-contract.md) — EISV as proprioceptive telemetry, not an outcome oracle; separates measurement, diagnosis, policy, enforcement, and external labels
- [`identity.md`](identity.md) — v2 ontology
- [`paper-positioning.md`](paper-positioning.md) — Q3, ontology ↔ paper relationship
- [`harness-substrate-plurality.md`](harness-substrate-plurality.md) — R6, identity across variable harness/model/transport
- [`beam-coordination-kernel.md`](beam-coordination-kernel.md) — R7, lease-plane coordination primitive
- [`v7-fhat-spec.md`](v7-fhat-spec.md) — paper v7 generative-model spike

**Research RFCs** (R-rows, *inventive* primitives):
- [`r1-verify-lineage-claim.md`](r1-verify-lineage-claim.md) — behavioral-continuity verification (shipped)
- [`r2-honest-memory-integration.md`](r2-honest-memory-integration.md) — honest memory integration (Phase 1 shipped)
- [`r6-episode-fork-response-shape.md`](r6-episode-fork-response-shape.md) — R6 sub-item, episode-fork API shape

**System RFCs** (S-rows, *descriptive* cleanups):
- [`s1-continuity-token-retirement.md`](s1-continuity-token-retirement.md) — token deprecation plan
- [`s8a-tag-discipline-audit.md`](s8a-tag-discipline-audit.md) · [`s8a-phase2-prep.md`](s8a-phase2-prep.md) — tag discipline + phase 2
- [`s10-fleet-aggregation-plan.md`](s10-fleet-aggregation-plan.md) — fleet aggregation
- [`s15-server-side-skills.md`](s15-server-side-skills.md) — server-side skills surface
- [`s21-session-resolution-bypass-incident.md`](s21-session-resolution-bypass-incident.md) — S21 incident record
- [`v7-fhat-spec-v5-amendment.md`](v7-fhat-spec-v5-amendment.md) — v5 amendment to v7 spec

**Dated records**:
- [`ledger-triage-2026-06-11.md`](ledger-triage-2026-06-11.md) — read-only triage of the deferred/blocked `plan.md` rows against their unblock triggers

## Operator-archived docs

Not every doc cited by a `plan.md` row is in this folder. On 2026-05-21 the internal-dialogue artifacts — council review notes, dated dogfood reports, implementation handoffs — were moved to the operator's private archive (commits `27642d1` / `4ad624a`); the load-bearing specs above were restored, the rest stayed archived. Archived from this folder: the R5 memory-deepening spec, the R6 dogfood/envelope evaluations, the S7 KG-provenance schema, S11-a skill-text drift, S20 cache-scope narrowing, and the S21 council-review series (`s21-fix-council-review`, `s21b-*`). `plan.md` still cites them by filename — that's intentional; the ledger is the historical record. `docs/handoffs/` is gitignored for the same reason (operator-local handoffs, cited from public docs as provenance).

## How rows resolve

A row in `plan.md` typically progresses: open → spec doc lands here as `{id}-*.md` → council review → operator decision recorded inline → implementation shipped (referenced from `src/` and tests) → row marked **Resolved** in `plan.md` with a date. Older resolved rows stay in `plan.md` as the historical record.

## What this folder is not

- Not the system's runtime API — see [`docs/UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) and [`docs/integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md).
- Not deployment/operator docs — see [`docs/operations/`](../operations/) and [`docs/install/`](../install/).
- Not the published paper — see the [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) repo (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)).
