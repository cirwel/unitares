# Other pending operator decisions (collected 2026-10-10)

**Status:** collection, 2026-10-10. A companion to
[`open-decisions-packet-v0.md`](open-decisions-packet-v0.md), whose seven
decisions (D1–D7) it does not repeat. It lists the other `active/` documents
whose status says an operator decision is pending or awaited. It records no
decision, recommends nothing, and changes no document's status; each linked
document is authoritative for its own decision.

This was meant as a closing section of the packet. The packet is at its
shrink-only baseline in `scripts/dev/proposals_length_baseline.txt` (2,490
lines), so any added section would fail the length ratchet, whose threshold and
baseline are operator choices. It is therefore a separate document, linked from
the packet's navigation paragraph.

## Recorded facts (not pending)

- **EISV / core boundary.** The direction of
  [`eisv-core-boundary-v0.md`](eisv-core-boundary-v0.md) was adopted by the
  operator on 2026-10-10. It is decided, not pending; its own Authorization
  section still governs what each stage may build.
- **Review as a merge gate.** The operator's decision of 2026-10-08 is settled:
  review is advisory and best-effort and does not gate merging, except on auth
  paths (`CLAUDE.md` / `AGENTS.md`, *Before Committing*;
  `docs/operations/github-workflow-conventions.md`, *Review workflow*).

## Pending, one line per document

The date is that of the reading that states the decision as pending: the
document's own dated status where it has one, otherwise the date given.

| Document | Decision pending | Date |
|---|---|---|
| [`cedar-delegation-authz-v0.md`](cedar-delegation-authz-v0.md) | §8: whether to adopt Cedar at all (deferred-by-default), and if so policy ownership, `effect_grant.py` coupling, high-stakes failure mode and dependency acceptance | 2026-08-21 |
| [`gap-recovery-arming-semantics-v0.md`](gap-recovery-arming-semantics-v0.md) | How long an absence must be before the first check-in after it is distrusted (`GAP_RECOVERY_ARM_SECONDS`; the shipped default reproduces the legacy 150 s) | status as written; file added 2026-09-08 |
| [`dialectic-resolution-receipt-v0.md`](dialectic-resolution-receipt-v0.md) | Whether to enable `drr.v1` receipt minting, once the preconditions in "Why it stays dormant" exist | status as written; file added 2026-09-02 |
| [`relay-substrate-relayering-v0.md`](relay-substrate-relayering-v0.md) | The execute-half question (governed-effect execute plane), deliberately left open; the commissioned [design read](execute-plane-design-read-v0.md) is written and decides nothing | 2026-09-17 |
| [`pcalm-primal-dual-governance-v0.md`](pcalm-primal-dual-governance-v0.md) | Six choices before any outcome-bearing work (first constraint and its owner, dynamics, smallest relevant improvement, outcome class, retention and privacy, closed-loop follow-on), and separate approval of the offline replay | 2026-09-15 |
| [`agent-channel-wake-gate-v0.md`](agent-channel-wake-gate-v0.md) | Signature of the unsigned disconfirmer gate, after piece A is re-derived against the `/v1/msg/*` transport | 2026-09-23 |
| [`interface-contract-release-batching-v0.md`](interface-contract-release-batching-v0.md) | Whether to batch interface-contract releases to one per server release instead of one per PR | 2026-09-30 |
| [`phi-effort-overflag-test-v0.md`](phi-effort-overflag-test-v0.md) | Fix every value in *Operator choices* (complexity measure, strata, outcome window, smallest gap, α and noise floor, minimum bad clusters) before registration; separately, the cold-start prior | 2026-10-03 |
| [`federation-component-boundaries-v0.md`](federation-component-boundaries-v0.md) | Ratify or change the §13 recommended dispositions for the four §10 decisions: guard strictness, route packs, review API before the horizon, lease-plane profile | 2026-10-06 |
| [`mirror-effectiveness-measurement-v0.md`](mirror-effectiveness-measurement-v0.md) | Whether to proceed with operator-gated Phase 2 (feed the verdict back) | 2026-08-16 |
| [`principal-rollup-v0.md`](principal-rollup-v0.md) | Count and mint behaviour changes (Move 3, reframed 2026-06-18 as derive, not store-at-mint) remain operator-gated | 2026-08-16 |
