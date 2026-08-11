# Docs consolidation v0 — one owner per claim

**Status:** RESOLVED — Phase 0 and Phase 1 shipped by 2026-08-11, after the
reader-facing documentation freeze ended.

## Problem

The 2026-07-02 README coherence audit found that every substantive defect was
the same failure shape: an architecture fact was corrected (e.g. the PR #1235
verdict-driver inversion, the Redis-posture correction), some prose copies were
updated, and other copies kept asserting the stale claim. Nothing was broken at
the file level — links resolved, files existed, dates were fresh — so
`check_doc_health.py` stayed green while the *sentences* disagreed.

Root cause is duplication: the warmup-verdict story existed in at least six
places (README, SCOPE_AND_THREAT_MODEL, EISV_COMPUTATION, the manual ×3, the
ontology contract, the governance skill); the Redis posture in four. Every fact
stated in N places must be corrected N times, and the consistency mechanism was
the operator's memory.

## Audience model (settles "who is the manual for")

| Audience | Front door | Owns |
|---|---|---|
| Evaluators (humans, first contact) | `README.md` → `docs/REVIEWER_GUIDE.md` → `docs/PRODUCTION_SNAPSHOT.md` | the pitch, the verify-yourself path, frozen metrics |
| Operators & integrators (humans, task-ordered) | `docs/manual/` — **the** human user's guide | install → run → integrate → read → operate → troubleshoot walkthrough |
| Agents (machine consumers) | `AGENTS.md` / `CLAUDE.md` + `skills/` | terse contracts and lifecycle rules |
| Internal research | `docs/proposals/`, `docs/ontology/` | design history, plans, provenance |

The manual is **for humans** and stays that way. Agents get the same facts
through `AGENTS.md` and the skills — and the way both stay truthful is not
parallel maintenance but the claims registry: human docs and agent docs quote
the canonical wording instead of paraphrasing it independently.

## Principle

**One owner per claim.** Every load-bearing fact has exactly one canonical doc
(mapped in `docs/dev/CANONICAL_SOURCES.md`); every other surface states it in
at most one sentence and links to the owner. A surface that asserts less is
harder to make wrong.

## Phase 0 — shipped before Phase 1

1. **Contested-claims registry** — new section in `docs/dev/CANONICAL_SOURCES.md`:
   corrected facts, their canonical wording, their owner docs.
2. **`check_contested_claims`** in `scripts/diagnostics/check_doc_health.py`:
   deny-patterns for the stale wordings observed in the wild, scoped to
   reader-facing surfaces (proposals/ exempt as provenance). Runs in the same
   pre-push doc-health pass as the existing checks.
3. **Copy fixes** — remaining stale copies of the warmup-verdict and
   Redis-posture claims corrected in the manual (README, 01, 02, 05), the
   ontology proprioception contract, and the governance-fundamentals skill;
   plus one stale README anchor in the manual.
4. **Correction ritual** — one line added to
   `docs/operations/github-workflow-conventions.md`: a PR that corrects an
   architecture fact greps reader-facing docs for the old claim and updates the
   registry in the same PR.

## Phase 1 — shipped 2026-08-11

1. **README dedup.** The front door was cut by roughly half while retaining the
   governing loop, quickstart, fit, evidence, limits, verification path, and
   citation. Detailed mechanics now point to their canonical owners.
2. **Collapsed duplicate layers.** `guides/START_HERE.md` is a compatibility
   redirect; `guides/TROUBLESHOOTING.md` owns symptom-and-recovery guidance and
   `manual/07` routes to it; `install/PLAYBOOK.md` and
   `integration/MCP_CLIENTS.md` remain canonical while `manual/02` and
   `manual/04` provide thin workflows.
3. **Audience-split docs index.** `docs/README.md` now separates reader-facing,
   operator/contributor, research/provenance, and optional essay material.
4. **Separated optional interpretation.** Tonality and philosophy-of-mind
   essays moved under `docs/essays/` and are explicitly non-normative.
5. **CI enforcement.** Strict doc-health, contested-claim, tool-count, and
   Python-version checks now run in the documentation validation workflow.

## Non-goals

- No doc-generation tooling beyond the lint. (Catalog generation was DEFERRED
  and doc quarantine REJECTED in the doc-generator hardening thread; this
  proposal respects both decisions — the registry + lint is the accepted
  machine-facts pattern, not a new pipeline.)
- No attempt to rewrite research history. This record moved to `resolved/`;
  unrelated proposal and ontology content remains provenance and the
  demotion-candidates check continues to triage it advisorily.
