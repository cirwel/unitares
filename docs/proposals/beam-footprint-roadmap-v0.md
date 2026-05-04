# BEAM Footprint Roadmap

**Created:** May 3, 2026
**Last Updated:** May 3, 2026
**Status:** Draft v0 — Read A roadmap (control plane → BEAM, intelligence plane stays Python) for unitares post-Phase-A

---

## Operator-consent framing (read this first)

The operator stated 2026-04-30 (~13:30 local) and again 2026-05-03 that the goal-level destination is a "full BEAM nervous system" and expressed enthusiasm for "fully migrate." This roadmap **does not give the operator that.** It argues a hybrid architecture (BEAM for the control plane, Python for the intelligence plane) is the right shape, *not* a wholesale Python-to-Elixir rewrite of UNITARES governance MCP.

The operator should explicitly confirm or override this substitution before the roadmap is treated as binding. Drafting a roadmap that quietly translates "fully migrate" into "hybrid that keeps Python permanently" is the substrate-migration enthusiasm bias — exact mirror of the resistance bias in `feedback_substrate-migration-status-quo-bias.md`. Naming it does not absolve it; operator consent does.

**Convergent evidence behind the substitution (2026-05-03):**

- 3-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`) on the prior draft of this roadmap rejected its diplomatic third-position framing and surfaced that Wave 1's central premise (asyncpg/anyio as live bug class) was stale.
- Independent third-party (Perplexity computer task, 2026-05-03): "I would not do a wholesale Python-to-Elixir/Erlang rewrite. The better path is: keep Python for ML, research code, model evaluation, data tooling, and fast iteration; move only the orchestration layer, agent supervision, long-running services, distributed coordination, queues, process lifecycles, telemetry, and fault-boundary logic onto BEAM."
- Live source check: the asyncpg/anyio bug class cited as primary motivation in earlier drafts was closed in production 2026-05-02 by PR #290 (`agents/sentinel/agent.py:413-450`); `phase-a-plan.md:347` confirms ">400 cycles since restart with zero asyncpg/anyio failures."

If the operator wants Read B (full UNITARES rewrite in Elixir) regardless, this roadmap does not block it — but Read B requires a separate edit to `docs/ontology/beam-coordination-kernel.md` to amend the first non-goal ("Do not rewrite UNITARES in Elixir"), and that edit is the operator's call, not this document's.

## What this document is

A sequencing memo for *what BEAM does next, after the lease-plane Phase A complete on 2026-05-03 (PR #305)*. It commits to **Read A as a stable destination, not a way-station to Read B**, organized around the control-plane / intelligence-plane cut.

It is not:

- a Phase B plan for the lease plane — that belongs to `surface-lease-plane-v0.md`;
- an amendment to `docs/ontology/beam-coordination-kernel.md` — the kernel doc's non-goals stand;
- an RFC for any specific port — each named wave below will get its own RFC if and when approved.

## The cut: control plane vs intelligence plane

> **Python thinks. BEAM governs.**

The organizing principle, framed by Perplexity 2026-05-03 (first session) and adopted here:

| Plane | Stays Python (permanently) | Ports to BEAM (eventually, by wave) |
|---|---|---|
| **Intelligence** | `governance_core/` (3,300 LOC; NumPy in `phase_aware.py` + `stability.py`); KG retrieval (`hybrid_rrf` over AGE); Watcher (LLM pattern matcher); Hermes practice body; paper v6/v7 corpus tooling; the dialectic engine's reasoning logic | — |
| **Control** | — | Sentinel (fleet supervision); Vigil (cron janitorial); agent lifecycle/heartbeats; identity/onboarding middleware; fault recovery (currently launchd; OTP supervision is a structural upgrade); event bus / telemetry |
| **Ambiguous** | The 31 `@mcp_tool` handlers are protocol glue + business logic, not "intelligence" — but their placement depends on the Elixir MCP server library question (see §"Read B-shaped risks"). The MCP transport layer itself sits on the control side conceptually but is gated by library maturity. | |

The "stays Python permanently" column is load-bearing. This is not an incremental ratchet to Read B; it is the steady-state architecture. The failure mode of pretending otherwise is that each Python service ported away leaves the residual Python core smaller, more brittle, and harder to defend — the architect council finding (§"Reversibility / exit cost") that the original draft missed.

## Why Read A, not Read B

### Cost (verified 2026-05-03 against `master` at `e4076657`)

- `src/` non-test Python: **83,071 LOC** across 31 `@mcp_tool` handlers in 16 modules.
- `governance_core/`: **3,300 LOC** (stays Python regardless — see §"The cut").
- Test files: **330** in `tests/`.
- Lease plane Elixir (already-shipped, for comparison): **2,798 LOC**.

A Read B port is roughly 30× the lease plane's volume *by raw LOC*, with the caveat that the comparison denominator is imperfect: not all 83K lines are coordination-runtime, and not all 2.8K Elixir lines are pure protocol. Reviewer council finding: "the comparative ratio is real but the denominator is not surgical" — treat 30× as an order-of-magnitude anchor, not a precise multiplier.

### Hidden costs (Read B-shaped risks)

1. **Elixir MCP server library.** Anthropic ships official Python and TypeScript SDKs; an Elixir SDK does not exist as of this writing. Read B requires either hand-rolling JSON-RPC over stdio + HTTP or adopting a community library with unknown maturity. Either path is weeks of work and the largest single risk surface.
2. **AGE / Cypher from Elixir.** Postgrex can call AGE through Postgres (`SELECT * FROM cypher(...)`), but every retrieval path in `src/knowledge_graph.py` and `src/mcp_handlers/knowledge/handlers.py` (verified to use `hybrid_rrf` + `hybrid_rrf_graph`) needs reimplementation.
3. **Identity / onboarding coupling.** CLAUDE.md flags this as a single coupled writer surface across `src/mcp_handlers/identity/`, middleware, schemas, and shared docs. Porting without breaking the live agent fleet is delicate.
4. **REST surface preservation (reviewer council finding the original draft missed).** Watcher, Sentinel, Vigil, the SDK, and external partners all hit governance MCP via REST endpoints (e.g. `post_finding` → `/api/findings`). Read B must replicate that REST surface byte-for-byte at the boundary, or every Python agent silently breaks. This is a contract the doc must preserve regardless of the runtime underneath, and the work is not free.
5. **Test corpus.** 330 test files, much of it pytest-fixture-shaped. ExUnit equivalents must be rebuilt before declaring the port "done." A meaningful fraction of the port effort.
6. **Runway opportunity cost.** Realistic timeline 3–6 months of focused work. Paper v6 / v7 corpus accumulation, Lumen evolution, dispatch refinement, fellowship, fleet maintenance all slow during the window.

### Reversibility and cognitive surface area (architect council findings)

- **Exit cost is not symmetric with entry cost.** Each Python service ported away makes the residual Python core easier to argue against keeping. Read A pretends to be reversible; in practice each wave shifts the operator's mental defaults toward Read B. The roadmap mitigates this by explicit "stays Python permanently" categorization, not by reassurance.
- **Two-substrate cognitive tax.** Running Elixir + Python indefinitely is itself a real cost: two language ecosystems, two CI/test pipelines, two on-call mental models, two flavors of dependency-pinning and security patching. Read A's stable-destination framing accepts this tax explicitly. Read B's "single substrate eventually" framing is comforting but the evidence does not support it (see §"What's *not* a Read B trigger").

## Read A is a stable destination, not a way-station

The original draft of this roadmap framed Read C as "incremental ratchet" to an open-but-deferred Read B. The architect council found this collapses to Read A + governance, and that the "mature systems grow new substrate alongside until the old part becomes vestigial" claim was rhetorical comfort, not principle. Mature systems also fossilize around foreign substrate (C extensions in Python, JNI in JVM stacks, CGI-era PHP under modern Rails); the conditions distinguishing those outcomes are not generally known, and assuming the favorable one is bias.

This draft drops the trichotomy and commits to a binary: **Read A is the destination.** Read B is open only via explicit operator decision and a separate edit to the kernel doc, not via roadmap drift.

### Failure mode named explicitly: integration glue ossifies

The honest failure mode of Read A is that the boundary between BEAM control plane and Python intelligence plane accumulates glue (proto definitions, REST contracts, version-pinned client libraries, error-translation layers) until the integration tax exceeds the original migration cost. The mitigation is:

- **Use Ports / HTTP / gRPC / Redis streams for BEAM↔Python interop.** Treat Python services as supervised external processes.
- **Do NOT use Pythonx NIFs** — embedded CPython runs in the same OS process as BEAM via NIFs and breaks the supervision/isolation guarantees that make BEAM worth the migration in the first place. Per Pythonx's own docs (cited via Perplexity 2026-05-03): for managing multiple Python programs, `System.cmd/3` or Ports is the better isolation model.
- **Keep boundary contracts narrow and versioned.** The narrower the contract, the cheaper the glue. Sentinel-on-BEAM should call governance MCP via the same REST surface every other agent uses, not via Pythonx embed and not via a new Elixir-only wire protocol.

## Wave 0 prerequisite: incident-rate instrumentation

The original draft cited "asyncpg/anyio incident rate trends up/down" as a falsification trigger. **There is no incident-rate measurement infrastructure in the repo.** Reviewer council finding (confirmed): no metrics-series row, no Chronicler schema, no structured event class for coordination-class failures. The triggers in the original draft were performative.

Wave 0 of this roadmap, before any port: emit structured events on coordination-class failures (asyncpg connect errors, anyio task-group cancellations, executor pool exhaustion, MCP handler timeouts) and persist them in a Chronicler-readable form. Without Wave 0, no later wave's "exit criterion" can be honestly evaluated and no Read B trigger can fire on evidence rather than vibes.

Wave 0 is small: a structured-event emitter in the existing Python services + a Chronicler row schema + a dashboard panel. Days of work, not weeks.

### Event envelope (defined upfront, not evolved)

Per Perplexity 2026-05-03 (second session): schemas are cheaper to design before code than after. Wave 0 commits to a stable JSONB envelope with these required fields, not ad-hoc structured logs:

| Field | Type | Required | Purpose |
|---|---|---|---|
| `event_id` | UUID | yes | replay/dedup key |
| `timestamp` | ISO 8601 UTC | yes | ordering and audit |
| `service` | enum (`sentinel`, `governance_mcp`, `lease_plane`, `vigil`, `chronicler`, `watcher`) | yes | originator |
| `event_type` | dotted enum (`coordination_failure.asyncpg_connect_error`, `coordination_failure.anyio_cancellation`, `coordination_failure.executor_pool_exhaustion`, `coordination_failure.mcp_handler_timeout`, …) | yes | category — extensible by namespace, never by ad-hoc string |
| `agent_id` | UNITARES UUID | optional | when the event is agent-attributable |
| `payload` | JSONB | yes | event-type-specific structure (defined per event_type, not free-form) |
| `context` | JSONB | yes | `git_commit`, `service_pid`, `running_since`, `host` — facts about the emitter, not the event |

The envelope persists in a single `audit.coordination_events` table (not per-service tables — single replay surface). Wave 0's Chronicler row schema is this envelope's projection into `metrics.series`.

Stability discipline: `event_type` extends by adding new dotted namespaces, never by reusing or renaming existing ones. `payload` shape per `event_type` is documented at the time the event_type lands. This is the contract Wave 1+ will rely on; getting it ad-hoc and refactoring later is the avoidable mistake.

## Wave 1 — Sentinel (re-justified on substrate-fit grounds)

### Why first — the honest motivation

The earlier draft pitched Sentinel-on-BEAM as the cure for the asyncpg/anyio bug class. **That motivation is dead.** PR #290 closed that CONCERN 2026-05-02; `agents/sentinel/agent.py:413-450` runs `asyncio.run(asyncio.wait_for(poll_forced_release_alarms(...), 30s))` inside a `loop.run_in_executor` call; `phase-a-plan.md:347` records ">400 cycles since restart with zero asyncpg/anyio failures."

Sentinel-on-BEAM's real motivation, with that pitch retired:

1. **Substrate fit, not bug fix.** A continuous fleet monitor with rule-based anomaly detection over event streams *is* the GenServer-per-rule-under-DynamicSupervisor shape. The Python implementation works (post-PR-#290), but the structural fit argues OTP supervision will hold under classes of failure the current mitigation pattern cannot cover (executor thread-pool exhaustion at sustained DB outage; cascading rule failure; alarm-handler crash without restart policy).
2. **Launchd → OTP supervision is a real upgrade.** Launchd restarts a crashed process; OTP can isolate failures within a process, restart subtrees, and apply explicit restart strategies. For a fleet monitor whose individual rules can fail independently, this is structurally better fault containment than what launchd offers.
3. **Second proof of the Postgrex pattern.** The lease plane proved Elixir/OTP can talk to the same Postgres database the Python services use without coordination pathology. Sentinel-on-BEAM tests whether that pattern generalizes to a service that *consumes* fleet events rather than *originates* coordination events.
4. **Smallest control-plane Python service.** Lower port cost than Vigil (cron infrastructure to redo) or governance MCP middleware (deeply coupled). Reasonable first wave.

### Out of scope for this roadmap

The Wave 1 RFC is a separate document. Roadmap-level: the alarm rules, the asyncpg-using probes, telemetry to UNITARES, launchd-managed lifecycle. Reuses lease-plane patterns (Postgrex, bearer-token auth from `~/.config/cirwel/secrets.env`).

### Exit criterion (gated by Wave 0)

Sentinel-on-BEAM has been the production fleet monitor for **≥ 14 days continuous** with:

- zero coordination-class incidents in the Wave-0 instrumentation feed (not "trends down" — zero, attributable);
- alarm rule parity with the Python implementation (every alarm fires the same way, verified by the existing `tests/test_sentinel_*` suite re-pointed at the BEAM endpoint);
- supervision tree absorbs at least one induced fault (kill a worker, supervisor restarts, no manual intervention);
- the operator does not declare success on enthusiasm — the 14-day window and the Wave 0 incident-feed must both hold before Wave 1 closes.

The last bullet is the architect council's stop-sign #1, promoted from a footnote into the exit criterion.

## Wave 2 — deferred pending Wave 1 evidence

Wave 2 is intentionally not pre-committed. Candidates:

- **Vigil** — substrate uniformity for the cron-driven janitorial agent. Easy port if Wave 1 has solidified the Postgrex + telemetry pattern.
- **Chronicler** — substrate uniformity for the daily metrics agent. Lowest priority.
- **A new BEAM service that fills a gap discovered during Wave 1** — only if real.
- **Pause and let Phase A + Wave 1 settle.** Solo-founder runway is finite; pausing is a real option.

The decision belongs to the operator at Wave 1 exit, with Wave 0's incident-rate data and Wave 1's parity/fault-induction evidence in hand.

## Out of scope for this roadmap

Stays Python (and not because Read A might "eventually" port them — the categorization is structural):

- **`governance_core/`** — intelligence plane. NumPy is used in `governance_core/phase_aware.py` and `governance_core/stability.py`; the dynamics path itself is stdlib. Either way, this is math, not coordination.
- **Watcher** — single-shot LLM pattern matcher invoked per code edit. No coordination shape; OTP supervision adds nothing. (Note: even staying Python, Watcher hits governance MCP REST endpoints — see §"Hidden costs" #4.)
- **Hermes practice body** — research/practice surface per `feedback_violist-poker-asymmetry` and `Mnemos_07d0f9c7`. Solo-process. Stays Python.
- **Pi anima-broker** — measured falsification 2026-05-01 (S1 idle RSS 123.7 MB falsified the §8.2 prediction; S6 distribution-win 50–75% on the 70% gate). See `docs/proposals/anima-broker-beam-port-v0.md`. Re-open requires operator-authorized "Lumen as appliance OS" reframe or a second Pi joining the fleet, not enthusiasm.
- **Discord dispatch bot** — TypeScript, not Python. Coordination-shaped (per-thread sessions, shared `.dispatch-sessions.json`) but not suffering Python's coordination class. Different cost calculus; port only if it starts hurting.
- **Data plane (Postgres + AGE schema, including `lease_plane`)** — the migration creates schema `lease_plane`, not `coordination`. Postgrex talks to it. The KG retrieval rebuild (`UNITARES_KNOWLEDGE_BACKEND=age`, `hybrid_rrf` / `hybrid_rrf_graph`) does not change.

## What's *not* a Read B trigger

Substrate-migration enthusiasm is the *prompt* to write this roadmap, not *evidence* that justifies escalation. Per `feedback_substrate-migration-status-quo-bias`, the bias cuts both ways: resistance and enthusiasm are symmetrical errors. Operator stating "I'm all about BEAM" is data about operator state, not about whether Read B's costs are warranted.

What *would* be a Read B trigger (kept here so the question is honestly open, not buried):

1. Wave 0 incident-rate instrumentation runs for ≥ 60 days post-Sentinel-port and shows the coordination-class bug rate is *not* decreasing in remaining Python services. (The mitigation pattern caps off rather than scales.)
2. A community Elixir MCP server library reaches production maturity, OR a hand-roll spike proves the work is bounded.
3. A second runtime consumer materializes that wants OTP-native APIs (a non-Python harness, a partner integration).
4. Operator runway permits 3–6 months of focused port without sacrificing paper / fellowship / Lumen / dispatch.

Two or more triggers → operator decides whether to amend the kernel doc non-goal. Single trigger → re-evaluate the roadmap, not the kernel doc.

## What this roadmap deliberately does not adopt from Perplexity (second session, 2026-05-03)

The second Perplexity output proposed elements that look reasonable in generic-architecture terms but are wrong for UNITARES specifically. Documenting the rejections so future agents reading this doc don't reintroduce them:

1. **Coherence-as-runtime-control-signal — REJECTED.** Perplexity #2 framed coherence as a generic governance metric ("entropy of tool-call distribution, drift from declared task objective, repeated failed retries…") with BEAM-side reactive control ("decide whether to continue, slow down, replan, isolate, or terminate"). UNITARES coherence is C(V, Theta), a thermodynamic state-vector property defined in `governance_core/` and the v6 paper — descriptive, not gating. Treating it as a reactive runtime threshold is the buzzword reading the v6.x corpus pushed back against. See `project_unitares-vocabulary-mismatch.md`.

2. **BEAM-owned `AgentRegistry` / `PolicyEngine` / `CoherenceMonitor` / `AuditLogWriter` — REJECTED.** Despite Perplexity #2's "not a rewrite" framing, moving authority over agent-registry, policy decisions, coherence monitoring, and audit writing to BEAM *is* Read B in disguise — those are the governance MCP's current responsibilities. Adopting this structure would amend the kernel doc's first non-goal ("Do not rewrite UNITARES in Elixir"), which requires a separate operator-authorized edit to that doc, not roadmap drift.

3. **Phase-five distributed BEAM nodes (Mac + Pi) — REJECTED.** Re-proposes the **retired** anima-broker BEAM port. S1 measured 123.7 MB idle RSS against a falsifier; S6 distribution-win was 50–75% on a 70% gate; retired 2026-05-01 (PR #279). Re-open requires operator-authorized "Lumen as appliance OS" reframe or a second Pi joining the fleet, per `anima-broker-beam-port-v0.md` §"Re-open conditions." Not enthusiasm.

4. **SQLite-or-Postgres audit start — REJECTED.** UNITARES has one Postgres database (governance), one location, by standing rule (CLAUDE.md "Database" section, `feedback`-anchored). The Wave 0 envelope persists in `audit.coordination_events` in the existing governance Postgres. SQLite reintroduces the second-instance anti-pattern Kenny has explicitly forbidden.

These rejections are documented at envelope-table granularity so the boundary between "Perplexity #2 worth keeping" (slogan, event envelope, lifecycle state machine) and "Perplexity #2 generic-architecture overreach" is auditable, not lost in the diff.

## Stop signs

Pause and request review if a roadmap revision proposes any of these:

- silently amending the kernel doc's non-goals without an explicit operator decision and a separate edit to that doc;
- collapsing Read A back into "incremental ratchet to Read B" language — the trichotomy is dead, do not resurrect it;
- declaring Wave 1 successful without **both** the 14-day window and Wave 0 incident-rate evidence;
- treating operator enthusiasm as substitute for §"What *would* be a Read B trigger" evidence;
- pre-committing Wave 2 before Wave 1 has shipped and its window closed;
- a Read B spike has been "proposed but not scheduled" for > 90 days (drift by deferral; named gate, not ambient sentiment);
- including `governance_core/` math, Watcher, Pi-side anima-broker, Hermes practice body, Discord dispatch, or the data plane in any wave without separate operator approval;
- Pythonx / NIF embed proposed as the BEAM↔Python boundary instead of Ports / HTTP / gRPC / Redis streams.

## Re-evaluation cadence

Revised:

- after each wave ships and its window closes;
- if Wave 0 incident-rate data shifts materially in either direction;
- if the Elixir MCP server library landscape changes (community library lands, Anthropic adds an SDK, or a credible hand-roll spike completes);
- if a Read B trigger fires.

Revisions land as `beam-footprint-roadmap-v0.1.md`, `v0.2.md`, etc.; full v1 when Wave 1 closes and the question shape itself updates.

## Relationship to other docs

| Doc | What it owns | Relationship to this roadmap |
|---|---|---|
| `docs/ontology/beam-coordination-kernel.md` | Integration framing, non-goals (incl. "Do not rewrite UNITARES in Elixir"), OTP process shape, lease-plane Phase 0–4 sequence | Roadmap respects non-goals; expansion past lease plane sits *outside* its scope |
| `docs/proposals/surface-lease-plane-v0.md` | Lease-plane contract spec, Phase A → Phase B gates | Roadmap's Wave 1 (Sentinel) is downstream of Phase A complete; not a Phase B item |
| `docs/proposals/surface-lease-plane-phase-a-plan.md` | PR-by-PR Phase A breakdown with status; the Sentinel asyncpg CONCERN closed at line 347 | Source of truth for Wave 1's "asyncpg fixed" claim |
| `docs/proposals/plexus-scope.md` | Plexus product/boundary name; what Plexus v1 owns and does not own | Roadmap is about runtime substrate, not lease semantics; orthogonal |
| `docs/proposals/anima-broker-beam-port-v0.md` (retired) | Pi-side BEAM port; retired with measured falsifications 2026-05-01 | Roadmap explicitly excludes Pi from scope |

## Sources of the substitution argument

- Council pass on draft v0 (2026-05-03), three agents in parallel:
  - `dialectic-knowledge-architect`: surfaced the operator-consent issue, the trichotomy collapse, the falsification asymmetry, the missing reversibility/cognitive-surface category, and the rhetorical-comfort claim.
  - `feature-dev:code-reviewer`: surfaced the dead Wave 1 motivation (Sentinel asyncpg fixed), the missing measurement infrastructure, the imperfect 30× denominator, the missing Watcher REST coupling.
  - `live-verifier`: factual corrections (test count 330 not 329, PR #305 merged 2026-05-03 not -05-02, schema `lease_plane` not `coordination`, "60 MiB target" absent from anima-broker doc, scipy unverified, NumPy verified in `phase_aware.py` + `stability.py`).
- Independent third-party (Perplexity computer task, 2026-05-03): control-plane / intelligence-plane cut; Ports-not-NIFs interop discipline; "hybrid architecture first, not full rewrite."
- Direct source verification (this drafter, 2026-05-03): `agents/sentinel/agent.py:413-450` confirms the asyncpg/anyio mitigation; `phase-a-plan.md:347` confirms ">400 cycles, zero failures."

The substitution survives if the operator confirms it explicitly. Otherwise the doc reverts to a question, not a plan.
