# 2026-06-24 Wave-3 Gate — Framing Note

**Created:** 2026-06-22
**Status:** Draft framing for the operator's 2026-06-24 read. Not a decision; a decision structure. Author recommendation included and labelled as such.
**Scope:** Frames the single decision that resolves the largest cluster of open proposals at once. Does not relitigate destination A′ (operator-committed 2026-05-05); it asks whether the *Wave-3 forward build* still has a basis.

---

## The honest question

Strip the ceremony and the gate read is one yes/no:

> **Is there a real, named consumer for the BEAM agent-orchestrator — yes or no?**

Almost everything stacked on the Wave-3 / BEAM-forward thread resolves from that answer, because the original justification for the thread has changed under it.

## What changed: the latency case is conceded dead

This is not an outside critique — it is what the docs themselves now say:

- `beam-footprint-roadmap-v0.md` (V0.3.1b amendment): PR #533 fixed 104× of the user-visible overhead **Python-side** (`run_in_executor`); this "materially weakens 'Wave 3 as urgent latency rescue.'"
- `wave-3-section-5-2-boundary-audit-summary.md`: every reclassification "saves milliseconds **per month**… architectural verdicts, not performance work. The latency case for Wave 3 … is not here." Live mix = **1 dialectic session in 30 days**.
- `beam-governed-effects-dossier-2026-06-18.md`: the dogfood run is "**not** a blanket argument to migrate UNITARES to BEAM," only "a strong argument for a narrower runtime boundary"; the identity-bleed evidence was a proof-origin bug fixed Python-side (#839), "not a BEAM failure."

So the operator's A′ commit rested on an **architectural-ceiling** argument, not a measured failure — and the surviving rationale for *new forward build* is "architectural coherence / fleet capability we lack," which is explicitly **not demand-pulled**.

## The keystone and what hangs off it

`agent-orchestrator-beam-v0.md` is built, tested, and **unwired** — by its own words "explicitly NOT a fix for a measured failure," and `:8789` is not listening, nothing spawns through it. It is the keystone: a cluster of proposals can only earn their value once it has a live consumer.

Proposals that gate on the orchestrator de-inerting (or are otherwise demand-empty until it does):

| Proposal | Dependence |
|---|---|
| `orchestrator-vouched-identity-v0.md` | Serves a population that is "**currently empty**" — the orchestrator. Inert PoC shipped; cutover deferred to this gate. |
| `monitor-delegated-liveness-v0.md` | Build-trigger is "the agent-orchestrator de-inerting to become the live spawn path." Zero live consumers today. |
| `behavioral-running-hot-detector-v0.md` | "The orchestrator is the consumer the behavioral arm exists to serve." Also blocked on the behavioral-EISV arm emitting signal. |
| `governed-effect-plane-v0.md` / dossier runtime phases | Reframed to ride the **inert** orchestrator app; `execute`-mode needs a "cheap-but-contended" surface the council says must be *manufactured* to test. |
| `beam-wave-3-handler-dispatch.md` | The dispatch port itself, deferred here; its latency basis is the one conceded above. |
| `beam-wave-3a-read-only-handlers.md` | Inbound-HTTP infra-first wave; self-states it "adds no evidence" for migration pressure. (Not gated on this read — operator-review only — but same demand question.) |

This is the "inventory ahead of demand" pattern at fleet scale: each doc is cheap to write, several can ship inert, and they accrete faster than demand arrives. (`beam-event-adapter-design-v0.md` and `monitor-delegated-liveness-v0.md` are the *healthy* counter-examples — both explicitly parked behind demand-triggers, both cite the #819 "feasible ≠ needed" lesson.)

## Decision structure

**If YES** — there is a real, named workload that needs orchestrated headless children (not "would be nice," a workload):
- De-inert the orchestrator behind that consumer (plist + the first caller).
- Then `orchestrator-vouched-identity` and `monitor-delegated-liveness` have a non-empty population and can proceed on their own gates.
- Wave-3 handler-dispatch stays **deferred** regardless — its latency basis is gone; do not revive it on coherence alone.

**If NO** — no consumer today:
- **Freeze the sub-tree**, don't keep building atop an empty foundation. Concretely: park orchestrator + vouched-identity + monitor-liveness + behavioral-hot behind a single explicit demand-trigger ("a real workload needs orchestrated children"), the way `beam-event-adapter` is already parked.
- Close or freeze `beam-wave-3a-read-only-handlers` (infra-first build with self-admitted no evidence).
- Keep `governed-effects` Phase 1 (shipped, #846) and the protocol spec as design-of-record; do not start `elixir/` runtime work.

Either way the gate read should **state the dead latency case out loud** so a future session doesn't re-pull these on a rationale that no longer holds.

## Author recommendation (labelled)

Recommend **NO / freeze** absent a named consumer surfacing between now and the read. The evidence in-repo is uniformly "no demand, architectural coherence only," the keystone has been inert since 2026-06-12, and the #819 lesson (inert build = inventory ahead of demand) is the operator's own stated axis. Freezing is reversible — a real workload un-freezes the whole tree in one decision. Continuing to build is the harder-to-reverse direction (more inert surface to maintain and re-verify).

Proposals **outside** this cluster are healthy and proceed independently of the read (Track A strict-identity flip, dashboard-hero rollup, the eisv-probe KILL already recorded, the reversible identity micro-steps). This note is only about the BEAM-forward cluster.
