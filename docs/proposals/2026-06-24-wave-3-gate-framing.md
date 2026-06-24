# 2026-06-24 Wave-3 Gate — Framing Note

**Created:** 2026-06-22
**Status:** **RESOLVED 2026-06-24.** Both gate decisions are made (see RESOLUTION below). Body preserved as the framing that led here.
**Scope:** Frames the single decision that resolves the largest cluster of open proposals at once. Does not relitigate destination A′ (operator-committed 2026-05-05); it asks whether the *Wave-3 forward build* still has a basis.

---

## RESOLUTION (2026-06-24, operator-decided)

**Decision B — Wave-3 BEAM handler-dispatch port: NO-GO. Falsifier disconfirmed it; do not build it; do not re-litigate without a NEW latency premise.**

The (A.2) falsifier (PR #1017, advisory-lock backend replacing the `fcntl`
spin-wait) was deployed 2026-06-23 and answered the gate's one question: the p99
coordination tail was the **file lock**, not the substrate.

- **Evidence.** 14-day pre-fix baseline: p50 154ms / p99 4729ms / **max 15118ms**.
  Post-deploy: the 15s ceiling is gone (nothing >3.9s in 30h, and the daytime
  churn-free hours sit at p99 ~270ms — well under the 2.0s gate). The 15118ms max
  was structurally the `fcntl` `timeout=5.0 × max_retries=3` retry-exhaustion;
  the advisory path has no such loop (`pg_try_advisory_lock` grabs or short-polls,
  releases on holder exit; live `lock_acquire` tick = 2ms). The mechanism is
  **removed, not merely unobserved** — so this is decided on the mechanism + early
  read, not pending a multi-day burn-in.
- **Consequence.** The only live argument for the handler-dispatch port was the
  p99 tail, and it's closed in Python for ~one file. A ~9-week BEAM migration buys
  nothing on latency. Freeze the handler-dispatch fork.
- **Scope guard.** This kills ONLY the Wave-3 handler-dispatch port. It does NOT
  touch the rest of the BEAM surface, which stands on its own rationales: the
  lease plane (live), the **agent-orchestrator** (Decision A), governed-effects.

**Decision A — agent-orchestrator: GO, and DONE.** Named first consumer = the
orchestrated independent dialectic reviewer; de-inerted and serving live reviews
2026-06-23 (#1020/#1021/#1024/#1025/#1029; live-verified own-identity reviewer
submitting `agrees=false`). See [[project_agent-orchestrator-beam]].

Everything below is the original framing note, preserved.

---

## The honest question

Strip the ceremony and the gate read is one yes/no:

> **Is there a real, named consumer for the BEAM agent-orchestrator — yes or no?**

Almost everything stacked on the Wave-3 / BEAM-forward thread resolves from that answer, because the original justification for the thread has changed under it.

## What changed: the *floor* latency case closed; the *tail* case is still live

> **Correction (2026-06-22, measured against the live governance DB).** An earlier
> draft of this note said "the latency case is conceded dead." That over-generalized
> from the p50/floor fixes and is **wrong**. Live data below; the distinction matters
> for the gate.

Two latency claims must be kept separate:

1. **The p50 / user-visible floor — genuinely closed.** PR #533 wrapped the enrichment scan (`run_in_executor`), p50 dropped ~104×; live `process_agent_update` p50 is now **152ms** (14d, n=7382). The §5.2 dialectic-helper reclassifications really do "save milliseconds per month" on a path that runs ~1 session/30d. That part of the dead-latency framing holds.

2. **The p99 coordination tail — still alive, and it is the real Wave-3 surface.** Live 14d: `process_agent_update` **p99 = 4740ms, max = 15118ms**, while the ODE numpy math is **p99 ≈ 63ms** — i.e. the math is **~1.3% of the handler p99**. The tail is *coordination*-bound (lock + identity-middleware + event-loop), exactly what Wave-3 dispatch ports. This is the **same signature** as the 2026-06-03 profiling (p99 5181ms, math 0.8%) — the June Python fixes moved the floor, not the tail.

So the honest gate question on the latency axis is **not** "is there any argument left" (there is) but: **does a persistent, math-independent ~4.7s p99 / 15s-max tail justify the port at this absolute volume** (~927 `core.agent_state` writes/day, ~1 dialectic session/30d)? That is a real operator judgment — tail-severity vs. low traffic — not a dead question and not a slam-dunk either way.

What *is* clearly unsupported is **new build atop the inert orchestrator** (next section): that rationale is "architectural coherence / fleet capability we lack," explicitly not demand-pulled, and is independent of the latency tail.

## The keystone and what hangs off it

`agent-orchestrator-beam-v0.md` is built, tested, and **unwired** — by its own words "explicitly NOT a fix for a measured failure," and `:8789` is not listening, nothing spawns through it. It is the keystone: a cluster of proposals can only earn their value once it has a live consumer.

Proposals that gate on the orchestrator de-inerting (or are otherwise demand-empty until it does):

| Proposal | Dependence |
|---|---|
| `orchestrator-vouched-identity-v0.md` | Serves a population that is "**currently empty**" — the orchestrator. Inert PoC shipped; cutover deferred to this gate. |
| `monitor-delegated-liveness-v0.md` | Build-trigger is "the agent-orchestrator de-inerting to become the live spawn path." Zero live consumers today. |
| `behavioral-running-hot-detector-v0.md` | "The orchestrator is the consumer the behavioral arm exists to serve." Also blocked on the behavioral-EISV arm emitting signal. |
| `governed-effect-plane-v0.md` / dossier runtime phases | Reframed to ride the **inert** orchestrator app; `execute`-mode needs a "cheap-but-contended" surface the council says must be *manufactured* to test. |
| `beam-wave-3-handler-dispatch.md` | The dispatch port itself. **Does *not* belong in this orchestrator-dependent cluster** — it has the live p99-tail argument above; judge it on its own tail-vs-volume merits, separately from the orchestrator question. |
| `beam-wave-3a-read-only-handlers.md` | Inbound-HTTP infra-first wave; self-states it "adds no evidence" for migration pressure. (Not gated on this read — operator-review only — but same demand question.) |

This is the "inventory ahead of demand" pattern at fleet scale: each doc is cheap to write, several can ship inert, and they accrete faster than demand arrives. (`beam-event-adapter-design-v0.md` and `monitor-delegated-liveness-v0.md` are the *healthy* counter-examples — both explicitly parked behind demand-triggers, both cite the #819 "feasible ≠ needed" lesson.)

## Decision structure

There are **two independent decisions** here; the earlier draft wrongly merged them.

### Decision A — the orchestrator cluster (latency-independent)
Question: *is there a real, named workload that needs orchestrated headless children (not "would be nice," a workload)?*

**If YES:** de-inert the orchestrator behind that consumer (plist + first caller); then `orchestrator-vouched-identity`, `monitor-delegated-liveness`, and `behavioral-running-hot` have a non-empty population and proceed on their own gates.

**If NO (current state):** **freeze the cluster** — park orchestrator + vouched-identity + monitor-liveness + behavioral-hot behind one explicit demand-trigger ("a real workload needs orchestrated children"), the way `beam-event-adapter` is already parked. Close or freeze `beam-wave-3a-read-only-handlers` (infra-first build, self-admitted no evidence). Keep `governed-effects` Phase 1 (shipped, #846) + protocol spec as design-of-record; no `elixir/` runtime work.

### Decision B — Wave-3 handler-dispatch (latency-driven, separate)
Question: *does the live, coordination-bound p99 tail (~4.7s, 15s max, math-independent) justify porting handler dispatch — at ~927 writes/day and ~1 dialectic session/30d?*

This is a genuine tail-severity-vs-volume call, **not** resolved by the orchestrator answer and **not** dead. If deferred, defer it on the **volume** argument (low absolute traffic), not on "latency is gone" — because the tail is measured and present.

The gate read should **state both axes accurately**: the p50 floor is closed, the p99 coordination tail persists, and the orchestrator demand is empty.

## Author recommendation (labelled)

- **Decision A: freeze** absent a named consumer surfacing before the read. The keystone has been inert since 2026-06-12; the rationale is "architectural coherence," not demand; #819 (inert build = inventory ahead of demand) is the operator's own axis. Freezing is reversible — a real workload un-freezes it in one decision.
- **Decision B: genuinely open — operator's call on tail-vs-volume.** I won't push it either way; my substrate-migration biases cut in both directions and the evidence is mixed (real tail, low volume). My only ask is that it be decided on the *measured* tradeoff, not on the dead-floor framing my first draft mistakenly supplied.

Proposals **outside** this cluster are healthy and proceed independently of the read (Track A strict-identity flip, dashboard-hero rollup, the eisv-probe KILL already recorded, the reversible identity micro-steps). This note is only about the BEAM-forward cluster.
