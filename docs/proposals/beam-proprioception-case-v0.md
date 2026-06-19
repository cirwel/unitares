# The Proprioception Case for the BEAM Footprint

**Created:** June 19, 2026
**Status:** Draft v0 — conceptual companion to the committed A′ destination. **This is not a relitigation.** It argues no change to scope, sequencing, or the decision boundary already set by [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) (v0.3, A′ committed) and [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md). It supplies a *name and a frame* for the boundary those documents already drew, so the commitment is easier to hold and harder to re-open from cold.
**Relationship to existing docs:** read the footprint roadmap V0.3 RESOLUTION and the dossier's Decision Boundary first. This doc sits behind both; it adds no new surface and gates nothing.

---

## Why this doc exists

Every prior round of this debate has been fought on the **latency axis**, and the substrate keeps losing that fight honestly: PRs #350 / #354 / #360 / #361 / #533 each collapsed a measured floor Python-side (#533 alone was 104× on user-visible overhead with one `run_in_executor`). The roadmap's own V0.3.1 amendment names this and flags the documented `feedback_substrate-migration-status-quo-bias.md` pole. The latency argument for migration is genuinely weak, and saying so is the honest posture.

This doc makes a claim on a **different axis entirely**, one the latency rebuttals do not touch:

> A *governance* substrate's core job is self-sensing — an agent (and the fleet) perceiving its own state honestly. BEAM is built to perceive its own running processes; UNITARES is built to integrate an agent's deviation trajectory over time. These are two distinct senses of proprioception, and the committed migration boundary is exactly the seam between them. Each kind of self-sensing belongs on the substrate shaped for it.

If that framing holds, the A′ boundary is not an empirical compromise pending the next benchmark — it is principled, and that is what stops the relitigation loop.

## Two proprioceptions, one boundary

The ecosystem already runs **two** distinct self-sensing systems. They are usually discussed separately; naming them as a pair is the contribution here.

**1. Interoceptive / autobiographical self-knowledge — stays Python (UNITARES).**
This is the sense formalized in the digital-proprioception paper (Wang 2026): the Anima Void Integral $V_{\text{anima}}(t) = \int_0^t \lVert \mathbf{a}(\tau) - \boldsymbol{\mu_a} \rVert\, d\tau$, the time-integrated deviation of an agent from its operating point — allostatic load on a four-dimensional EISV manifold. It is *durable, semantic, and historical*: identity continuity, trajectory, calibration, KG sediment, dialectic record. It answers "how far have I drifted, over my whole life, from who I am?" That is meaning-legibility, and it lives in Postgres/AGE and the analysis lane by design.

**2. Runtime / process proprioception — moves to BEAM (A′ surfaces).**
This is the sense the substrate is built to give natively: which process holds which lease, what crashed, what is queued, who is the single winner of a contested surface, what was revoked. It is *live, structural, and immediate*. It answers "what is true about my running body right now?" That is runtime-legibility, and OTP supplies it as ambient physics — PIDs, `:DOWN` monitors, supervision trees, Registry, bounded mailboxes — rather than as bookkeeping simulated in a `Map`.

**The dossier's Decision Boundary is this split, item for item.** Its "Move to BEAM" column — runtime custody, supervision (failures visible/isolated/restartable), leases/conflicts, revocation, bounded queues, typed telemetry — is precisely runtime proprioception. Its "Keep in Python" column — durable identity, EISV/calibration, KG/dialectic, ablation — is precisely interoceptive self-knowledge. The boundary was drawn empirically, surface by surface, under adversarial council review. This doc observes that it converged on a clean conceptual seam, which is evidence the boundary is real and not an artifact of where the benchmarks happened to land.

## Why this argument is immune to the latency rebuttal

The Python-side fixes optimize *how fast the system computes*. They do not give the system *legible knowledge of its own running state* — because that was never their target. You cannot `run_in_executor` your way to "this process honestly knows it is no longer the lease holder after a restart." That property is structural to the actor model, not a hot path you can profile and unblock.

Concretely, the lease-plane's own documented failure mode — *holder UUID does not survive restart → false `held_by_other`* — is a **runtime-proprioception defect**: the system held a false belief about its own process state. The roadmap's "architectural ceiling" point (per-agent GenServer dissolves the shared lock) is the same observation from the performance side. Naming it as proprioception unifies the lease-plane bug, the shared-lock ceiling, and the dossier's custody/revocation invariants under one property the substrate provides and Python emulates. None of those three is a latency claim; all three survive every Python floor-fix in the project's history.

## Existence proof: dispatch_beam

`~/projects/dispatch_beam` is a governed Discord orchestrator **already running on BEAM**, and it is a working instance of exactly this split:

- It draws the same boundary in miniature — its `PLAN.md` "Proprioception" section and "ontology payoff" keep the agent's governance identity out of the harness (the load-bearing identity-honesty caveat) while letting OTP own holder lifecycle, exactly as the dossier's invariant 1 requires of the effect plane ("BEAM never asserts a caller identity from transport inference alone").
- Its recent robustness audit (#18: resume-failure recovery, active-run reap distinction, write-amplification collapse) was **tractable because the runtime state was legible** — `Process.info`, `Port.info`, the Registry, ETS gave honest answers about what was actually running. That is runtime proprioception paying off as operability, not as throughput.

dispatch_beam is the cheap, low-blast-radius demonstration that the runtime-proprioception layer behaves as claimed before more of UNITARES's governed effects ride on it. It is a citation, not a dependency.

## What this doc explicitly does NOT argue

- **It does not move the boundary.** Stateless computation (numpy ODE, embeddings, LLM SDK, EISV) stays Python. MCP transport stays Python until the SDK gate is evaluated. No surface is added, promoted, or resequenced. Phases 3–5 of the dossier stay gated behind the dual-mode contract and the 2026-06-24 Wave-3 gate.
- **It does not claim a latency win.** The opposite: it concedes the latency axis to the Python-fix posture and argues the case lives elsewhere.
- **It does not relitigate A′.** A′ is committed by operator decision. This doc strengthens the rationale future-sessions read so the destination is not re-derived from cold each time.

## Bias accountability (both poles)

`feedback_substrate-migration-status-quo-bias.md` warns that this author reliably *resists* substrate migrations. That pole is real and must be priced in. But the symmetric failure is also real: a *pro-migration* proprioception narrative is rhetorically attractive precisely because it is clean, and clean frames invite motivated reasoning. Two guards:

1. This doc is **falsifiable in the same shape as the roadmap's stop-signs.** If Wave-1 Sentinel-on-BEAM or the governed-effect plane produces runtime-state defects (lost custody, false lease beliefs, supervision that fails to isolate) that the Python path did *not* have, the proprioception claim is *wrong* — BEAM would be giving worse self-knowledge, not better — and that is a v0.4 trigger, not a footnote.
2. It earns its place only by being **orthogonal**: it must not be cited to override a latency stop-sign or to accelerate a gated phase. If it is ever used to justify moving faster than the dossier's gates allow, it has been misused.

## Suggested disposition

A conceptual companion, not an RFC. If accepted, the only mechanical change is a one-line entry under "BEAM footprint (substrate migration waves)" in [`docs/proposals/README.md`](README.md) pointing here, and (optionally) a single back-reference from the roadmap's "Why migrate anyway, eyes-open" §1 noting that the architectural-ceiling argument has a proprioception framing recorded here. A council pass on *the frame itself* (does naming the two proprioceptions clarify or merely decorate the committed boundary?) would be appropriate before either cross-link lands.

## References

- [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) — v0.3, A′ committed; this doc sits behind its RESOLUTION.
- [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md) — the Decision Boundary this doc names.
- Wang, K. (2026). *Digital Proprioception and Allostatic Load* (`~/projects/digital-proprioception-paper/paper.md`) — the interoceptive sense ($V_{\text{anima}}$).
- `~/projects/dispatch_beam` `PLAN.md` — the running existence proof; "Proprioception (the prophetic case)" + "The ontology payoff".
