# 2026-06-24 Wave-3 Gate — Framing Note

> **Design record.** A planning / RFC document kept as design provenance; it captures intent at a point in time and may lag the running code. For current behavior see [`UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) and the runtime sources it points to.

**Created:** 2026-06-22
**Status:** **RESOLVED 2026-06-24.** Both gate decisions are made (see RESOLUTION below). Body preserved as the framing that led here.
**Scope:** Frames the single decision that resolves the largest cluster of open proposals at once. Does not relitigate destination A′ (operator-committed 2026-05-05); it asks whether the *Wave-3 forward build* still has a basis.

---

## RESOLUTION (2026-06-24, operator-decided)

> **Correction (2026-06-24, supersedes this section's first draft).** The first
> draft recorded Decision B as "NO-GO — latency disconfirmed." That was the wrong
> axis. The operator's stated basis: **latency is negligible and was never the
> point**; the driver is *coordination + "aliveness" + better signal for all
> stakeholders, and not continuing to paper over / work around the substrate* — a
> direction already proven on the BEAM surfaces in production (the BEAM Discord
> bridge, the lease plane, the orchestrator). Latency is struck from the ledger
> entirely.

**Decision B — Wave-3 handler-dispatch: the BEAM destination STANDS; the only open
question is SEQUENCING by blast radius, not whether.**

- **Latency was the wrong axis — negligible, irrelevant.** `process_agent_update`
  is a background accounting call running alongside the agent's real work (LLM
  calls in seconds-to-minutes); p50 ~150ms, and even the old rare multi-second
  tail at ~1,000 calls/day touched nothing an agent or operator feels. The (A.2)
  falsifier (#1017) measured a quantity that was never going to decide this. It
  still earns its keep tactically — it killed the 15s `fcntl` hangs — but it is
  *itself* a Python workaround, i.e. more of the papering the operator wants to
  stop, not the direction.
- **The real axis = stop papering; move stateful-coordinating surfaces to BEAM for
  coordination / "aliveness" / stakeholder signal.** This is the A′ destination
  (operator-committed 2026-05-05), validated by lived experience — NOT a per-port
  demand-trigger. Demanding a fresh measured justification for each port is
  substrate-status-quo bias (see [[feedback_substrate-migration-status-quo-bias]],
  [[feedback_sqlite-migration-bias]]); the destination is not re-litigated.
- **Therefore: not NO-GO.** The only honest open question is *sequencing by blast
  radius.* The identity-bearing handler dispatch is the **worst first port**
  (hottest surface, `proof_origin` gate, immediately after the 2026-06-22 strict
  flip — a BEAM divergence there could silently pass or block writes). So it is
  **not first** — but the direction holds.
- **Honest next BEAM increments** (coordination/aliveness-flavored, acceptable
  blast radius, no new Python workarounds): governed-effects (#862/#863, in
  flight); dialectic sessions as BEAM GenServers (RFC §5 — presence + liveness of
  the now-working orchestrated dialectic; convergence can stay Python); presence /
  liveness as a first-class fleet signal on the lease plane's `agent:` scheme.

**Decision A — agent-orchestrator: GO, and DONE.** Named first consumer = the
orchestrated independent dialectic reviewer; de-inerted and serving live reviews
2026-06-23 (#1020/#1021/#1024/#1025/#1029; live-verified own-identity reviewer
submitting `agrees=false`). This is the coordination/aliveness direction already
landing. See [[project_agent-orchestrator-beam]].

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

## Council review (2026-06-22) — the binary collapses; the decision is cheaper

A 3-lens council (architect + code-reviewer + live-verifier, adversarial) materially reframed Call B. **Supersede the "tail-vs-volume" framing above with this.**

**Corrected numbers (live-verifier):** volume is **~1,009 `core.agent_state` writes/day** (not 927) and **2 dialectic sessions/last-30d** (not ~1). Direction works against porting (slightly higher), doesn't flip anything. p99 4738ms / math 1.3% confirmed (one 3973ms math outlier aside). Orchestrator demand verified **empty** across audit.events, KG, git, port checks, dispatch_beam source → **Call A "freeze" confirmed.** (Follow-up 2026-06-23: the orchestrator is **proven-working, not broken** — 68/68 tests, and `live_smoke.exs` ran a full live acquire→run→release round-trip against the lease plane, real os_pid + lease + exit 0. So freezing it is *pure demand-discipline* — zero technical risk, zero resumption cost; de-inerting is one start.sh/plist + a caller. It's frozen for lack of demand, not capability.) Track A flip healthy: post-flip `identity_required` failures are tokenless callers rejected at the gate (agent_id=NULL, latency 0) — expected, residents not dark.

**The killer finding (code-reviewer, code-verified):** the p99/15s-max tail is the **`fcntl` file-lock spin-wait** in `src/state_locking.py::acquire_agent_lock` (`LOCK_EX|LOCK_NB` polled with `time.sleep(0.1)`, `timeout=5.0 × max_retries=3` = ~15s ceiling, a dead-on match for the observed 15s max). It is **not** PG coordination and **not** math. There is a **cheap one-file Python fix**: replace the file lock with `pg_try_advisory_lock(hashtext(agent_uuid))` (asyncpg already ExecutorPool-wrapped; crash-safe; deletes the stale-lock-cleanup code). This is the RFC's own **(A.2) disconfirmer**: **if it drops p99 < 2.0s, the Wave-3 port question is moot** — the tail was the file lock, not the substrate.

**The structural finding (architect, DB-verified):** the full port's *own* go/no-go evidence does not exist. Disconfirmer (B) needs `measurement.beam_python_boundary.request` boundary-cost rows; the live count is **zero** (that emitter is Wave-3 impl, paused under (α)). The lease-plane *baseline* has its 14 days (p50 4ms/p99 15ms, n=17,121) but the comparison side is empty by construction. So **"port the full handler-dispatch on measured boundary cost" was never an available option for 06-24.** The dominating middle options are the roadmap's own **(γ) ETS / (β) dialectic-only scoped ports** — the only way to *generate* the (B) evidence at minimal blast radius — which the binary wrongly parked with the frozen cluster. Also: identity-middleware (in Call B's port) and vouched-identity (in Call A's freeze) are the **same single-writer surface** — don't split them across the two calls.

**Blast-radius caveat:** the full identity-middleware port is the hottest surface in the codebase and Track A strict just went live today; a BEAM divergence in the `server_inferred`/`sticky_cache` proof_origin gate would silently pass or block writes. Argues against a big port now regardless.

### Reframed decision (council-synthesized)
Call B is **not** "port vs don't-port." The honest, cheap sequence:
1. **Ship the one-file `fcntl`→`pg_advisory_lock` fix** (the A.2 falsifier). Reversible, one deploy, no BEAM.
2. **Measure p99 ~7 days.** If **< 2.0s** → tail solved Python-side, **don't port**, freeze the whole BEAM-forward tree. If **still high** → the architectural-ceiling case strengthens *with evidence*, and the next step is a scoped **(γ)/(β)** probe (to populate disconfirmer B), **not** the full handler-dispatch port.
3. **Call A: freeze** the orchestrator cluster regardless (empty demand).

So the operator's actual 06-24 call shrinks to: **approve the one-file falsifier + freeze Call A.** The 9-week (mid-August) full port is not the live next step.
