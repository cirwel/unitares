---
status: v0 draft (pre-review; not an implementation gate)
authored: 2026-08-01
review_note: |
  Every "verified" claim below was checked against the live system on
  2026-08-01. Re-verify at review time rather than trusting this header.
related:
  - docs/proposals/eisv-maths-roadmap-v0.md (Appendix A: external_signal scarcity)
  - unitares-discord-bridge #36 (bridge.ack from reactions, merged 2026-07-31)
  - src/http_api.py adjudication endpoints (#1214 exogenous-anchor channel)
---

# Bridge→Dispatch: operator on-the-loop, not in-the-loop (v0)

## 1. Problem

The operator is currently the transport layer of the fleet's response loop:
a signal renders in Discord, the operator reads it, copies it into a Claude
session, and re-establishes context by hand before any investigation starts.
Separately, the adjudication channel asks the operator to certify findings
he cannot verify from the widget (2026-08-01 case study: five `forced
release` findings, confirmed by habit; a manual data pass later showed all
five were correct — but the click carried no evidence either way).

Two distortions follow:

- **Labels record certification where only attestation happened.** A confirm
  click is stored as "Sentinel's judgment externally verified" regardless of
  whether anything was checked. Measured precision is then pinned at 1.0 and
  cannot distinguish "Sentinel is right" from "operator never looked."
- **Response latency is operator latency.** Signal→investigation is gated on
  a human copy-paste even when the investigation itself is mechanical.

The design goal ratified in the north-star ontology applies directly:
groundedness means signal anchored to something exogenous to the loop. An
operator click echoing the system's own report is not exogenous. Outcomes
(did the fix hold, did the incident recur, did work output resume) are.

## 2. Principle

Move the operator from serial gate to exception handler:

- **Attestation ≠ certification.** A human click means "consistent with what
  I know," never "verified correct." Record it as such (§8).
- **Mechanical truth gets mechanical verification.** Findings whose truth is
  a database fact (forced releases, lease orphans, partition gaps) are
  verified deterministically, with the evidence attached to whatever a human
  later sees. No LLM in this path (execution-cost policy: the core stays
  free).
- **Judgment stays human, moved to after the evidence.** The operator
  reviews diagnoses, diffs, and outcomes — not raw signals.

## 3. What already exists (verified live 2026-08-01)

| Piece | State |
|---|---|
| Bridge renders findings/events in Discord | live |
| `bridge.ack` from Discord reactions, matched on `discord_message_id` | merged 2026-07-31 (#36) |
| Adjudication queue + verdict endpoints, `adjudicated_via` provenance field | live (`/v1/sentinel/adjudication-queue`, `/v1/sentinel/adjudicate`) |
| Verdict semantics: only `dismissed reason=fp` produces `is_bad=true` | `agents/common/resolution_outcome.py` |
| Deterministic forced-release verifier (attribution match, span/TTL orphan discriminator, completeness sweep vs `audit.events`) | performed manually this session; pure SQL, mechanizable as-is |
| Agent orchestrator (BEAM, :8789), dispatch_beam harness | live |
| Governed effects: `file_write`, `agent_spawn` | live (T3 residual) |
| Delivery convention: draft PR, human merge gate (`ship.sh`) | repo contract |
| Orchestrator-vouched identity | flag-off (#807) — dependency for §5, not §4 |

The wire from signal to dispatched investigation is the only missing part.

## 4. Step 1 — evidence at the point of verdict (deterministic, free)

Smallest change that fixes the label channel; no new services.

When the adjudication queue is built, enrich each machine-checkable finding
with its evidence, server-side:

- `forced_release` findings: the matching `lease_plane.surface_leases` row —
  surface, `release_reason`, held-for as a multiple of TTL, `holder_pid`
  null-ness, and whether the lease matches the finding's claim. One query
  per finding at queue-build time.
- Findings with no matching row, a mismatched surface, or a duplicate
  fingerprint are flagged `evidence: contradicts` — the dashboard renders
  that in red. This is the affordance that makes a wrong finding *visibly*
  wrong, converting a habit-click into a cheap real verification.

Same evidence block goes into the bridge's Discord embed for the finding,
so the operator's first sight of the signal already carries the check.

Exit criterion: every `forced_release` finding in the queue renders with
evidence attached; an operator confirm is made with the check visible.

## 5. Step 2 — reaction→dispatch (the wire)

Extend the reaction plumbing that #36 built for acks: a designated reaction
(e.g. 🔍) on a bridge finding message emits `bridge.dispatch_request`
(same event contract family as `bridge.ack`, matched on
`discord_message_id`). A dispatch consumer maps the signal family to an
investigator profile and spawns it via the orchestrator / dispatch_beam.

- **Context pack, assembled mechanically:** the finding + its §4 evidence +
  `scan-actors.sh <topic>` output + pointers to the relevant memory topics.
  The investigator starts where a human-driven session ends up after twenty
  minutes of re-establishing context.
- **Authorization:** dispatch-triggering reactions honored only from the
  operator's Discord identity. Reaction spoofing is a real surface (§9 L2).
- **Allowlist per signal family**, starting with exactly one: Sentinel
  lease/starvation findings. Families are added by editing the allowlist,
  not by generalizing the mechanism.
- **Cost policy:** the dispatch plumbing is free-path. Investigator model
  backends follow the repo rule — local/deterministic by default, metered
  only where the operator has opted in.

## 6. Step 3 — investigator output contract

An investigator produces, in order of preference:

1. A **diagnosis with evidence** posted back to the same Discord thread the
   signal originated in (closing the loop where the operator already looks).
2. Where a fix is mechanical and in scope: a **draft PR** via the normal
   `ship.sh` route. Never ready-marked, never merged, never deployed.
3. An `outcome_event` recording what was verified, with machine provenance
   (§8), attributed via its own governance identity (`spawn_reason=
   "subagent"` until #807 lands orchestrator-vouched identity).

The operator's "proceed" moves from before the work to after the evidence.

## 7. Trust boundaries (unchanged by this proposal)

Never automated, regardless of investigator confidence: C4 actions, real
pauses, database migrations, PR merges, deploys, anything on the
never-automate list of the doctor layer. Investigator concurrency is
lease-guarded like any other actor. A dispatched investigator that wants to
touch a single-writer surface follows the same open-PR check every session
follows.

## 8. Label provenance (rides behind, separate change)

Outcome events grow a provenance grade on the verdict itself:

- `machine_verified` — deterministic check against exogenous data (§4)
- `operator_attested` — human click, evidence visible
- `operator_certified` — human click after stated verification
- (existing `verification_source` tiers continue to apply underneath)

The falsifier can then weight labels by what they actually were, instead of
reading every click at certification strength. Whether machine-verified
labels count toward the falsifier's "independent adjudication days" metric
is an operator call (§9 L3) — it changes what the SBIR-facing number means.

## 9. Open questions

- **L1 (verify before build):** where §4 enrichment lives (queue-builder vs
  bridge consumer — the queue already joins `audit.events`, so server-side
  looks right); exact `bridge.dispatch_request` schema against the bridge
  event contract.
- **L2 (red-team):** investigator-verifies-actor circularity (mitigation:
  deterministic checks wherever truth is mechanical; outcome-anchoring for
  the rest; investigators never adjudicate their own findings). Reaction
  authorization. Dispatch rate-limiting / runaway-loop guard (a finding
  storm must not become an agent storm — cap concurrent dispatches, dedup
  on fingerprint). Prompt-injection via signal content into the context
  pack.
- **L3 (operator):** falsifier semantics of machine-verified labels (§8);
  which signal family is second after lease findings; whether dispatch
  results should auto-attach to the adjudication queue item as §4 evidence
  (probably yes — it makes the human click informed by the machine pass
  without replacing it).
