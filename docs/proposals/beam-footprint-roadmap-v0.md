# BEAM Footprint Roadmap

**Created:** May 3, 2026
**Last Updated:** May 5, 2026 (v0.2 — PR #350 verdict landed; destination reopens per v0.1 conditionality)
**Status:** v0.2 — destination is OPEN. v0.1's A′ destination commitment was provisional and conditional on PR #350's post-fix data; the data landed and resolved as Python-fixable (architect council C3 was right). v0.1's body is preserved below as historical record alongside v0; neither v0's Read A nor v0.1's A′ is the current destination. Read the V0.2 RESOLUTION block first.
**Council pass v0.1 (2026-05-04):** dialectic-knowledge-architect (2B/4C/3D/4N), feature-dev:code-reviewer (2B/3C/2D/2N), live-verifier (7 VERIFIED, 6 DRIFT, 0 REFUTED, 1 SOURCE_ONLY) — all findings folded inline. Architect C3 + reviewer C3 both flagged "v0.1 destination committed pre-experiment"; the v0.1 conditionality block was the fold for that finding, and v0.2 is the realization of it.

---

## V0.2 RESOLUTION 2026-05-05 — verdict landed; destination reopens

**Read this before any other section.**

PR #350 (merged 2026-05-05T03:28Z) dropped `force=True` from 6 observe sub-handlers, removing the per-call 3221-await loop on the request path. Per v0.1 §"Conditionality on PR #350's post-fix verdict," the experiment was: does the in-handler floor close (Python-fixable in-place) or persist (substrate-coupling)?

**Today's data (2026-05-05, ~05:00 UTC, post-restart probe):**

| Handler | Pre-fix | Cold-start (first call after restart) | Steady-state (subsequent calls) |
|---|---|---|---|
| observe(action=aggregate) | ~2,864ms (council live-verifier) / 15,000ms+ timeout under load | 17,062ms (one coord_failure event recorded) | 167–182ms (5 runs) |
| observe(action=anomalies) | (timeout under load) | not measured | 92–95ms (3 runs) |

**Verdict: Python-fixable.** Steady-state observe handlers are now sub-200ms. The 60× amplification floor was the 3221-await loop, not anyio/asyncio coupling at the substrate layer. v0.1's strongest single piece of falsifying evidence resolves as a Python-side anti-pattern that PR #350 closed for these specific handlers.

**Per v0.1's own conditionality, the destination commitment reverts to a question.** v0.2 is that revert.

**What v0.2 IS:**

- The destination is OPEN. Neither Read A (v0) nor A′ (v0.1) is currently committed-to. The Wave structure (Sentinel → force=True audit + lease-integration → handler dispatch + identity + dialectic) is preserved because that work is right regardless of destination — it eliminates the substrate-tax surface where it currently bites without pre-deciding the larger question.
- A formal record that v0.1's enthusiasm-pole-bias check (which architect C3 + reviewer C3 both flagged) was correct. v0.1 was committed pre-experiment; the experiment ran; the prediction the council warned against materialized; v0.2 is the discipline closing the loop.
- A live recommendation: continue accumulating Wave 0 channel data on the OTHER ~24 force=True sites (Wave 2 scope) before any further destination commitment. Those sites still bypass the cache (force=True is the bypass) and may still produce steady-state amplification. Today's verdict tells us about observe specifically; it does NOT generalize across the other force-reload-bearing surfaces.

**What v0.2 ISN'T:**

- A return to v0's Read A. v0's "bug class closed" premise (PR #290 fixed Sentinel-loop call site) is still narrow. Today's data doesn't restore Read A's load-bearing claim; it just means the case for moving past Read A is not as strong as v0.1 said it was. Both v0 and v0.1 had load-bearing premises that didn't survive contact with new evidence; v0.2 commits to neither and waits for more data.
- A retraction of the substrate-tax framing in CLAUDE.md / AGENTS.md. The four documented mitigation patterns (cached snapshot, run_in_executor, tight wait_for, force=True N-await) are still real. The asymptote argument (workarounds keep accreting) still applies if more patterns emerge. CLAUDE.md's "do not treat pattern-accumulation as progress" stance survives.
- A retraction of Wave 0 itself. Wave 0 just did the job it was designed for: surfaced a measurement, the measurement drove a destination commitment, the commitment was conditional on follow-up data, the follow-up data ran, the commitment resolved. That's the discipline working.

**What v0.2 keeps from v0.1:**

- The §"v0.1 cut" framing (stateful-coordinating vs stateless-computing) as the right *test* per surface, even though v0.1 used it to pre-decide a destination prematurely. The test stays useful for evaluating individual port decisions (Sentinel → BEAM still passes the test; observe handlers staying Python passes the test post-fix).
- The §"MCP SDK gate" — still the right binary if/when destination questions reopen. Three named conditions, NOT-closure list, named owner.
- Wave 1 (Sentinel-on-BEAM) — substrate-fit argument stands; today's verdict doesn't move it.
- Wave 2 (force=True audit + lease-integration + Wave 0 schema extension) — the work is right regardless of destination. The ~24 remaining force=True sites are still substrate-tax surfaces that need site-by-site treatment; PR #350 established the playbook.
- Wave 3 (handler dispatch + identity + dialectic) — deferred indefinitely until Wave 2 produces its data. The doc no longer leans on "Wave 3 is where governance MCP coordination ports" as a destination claim; Wave 3 is one possible future, not the committed path.
- The conditionality discipline. v0.2 itself is conditional on more Wave 0 data: if the other ~24 force=True sites produce steady-state amplification under load (post-Wave-2 cleanup), v0.3 may re-open A′; if they also resolve as Python-fixable, the destination genuinely is "stay Python with periodic substrate-tax cleanup," and v0.3 closes the question in that direction.

**Source of the v0.2 resolution.**

- **2026-05-05 verdict probe** (this session). Restarted governance MCP at 04:55 UTC after operator authorization (process-restart blast radius across active sessions). Pulled local master from 5615bc22 → 60fe16bb (PR #350's merge commit; local master had been stale 1.5h post-merge, requiring `git pull` before restart for the fix to actually be live in the running process). Probed observe(action=aggregate) and observe(action=anomalies) via curl against `localhost:8767/mcp/`; timed each via `python3 -c 'import time;print(time.time())'` deltas. Cold-start first call: 17,062ms (audit.events coord_failure recorded). Subsequent 5 aggregate + 3 anomalies probes: 92–182ms. No further coord_failure events.
- **v0.1's own conditionality block** (folded from architect council C3 + reviewer council C3). The conditionality WAS the fold; v0.2 is the conditionality firing on real data. This is the discipline doing what it was designed to do.
- **Memory anchors:** `feedback_substrate-migration-status-quo-bias.md` (cuts both ways), `feedback_verify-construction-lifecycle.md` (lazy vs eager — relevant to "cold-start tax stays even with force=True dropped"), `feedback_running-process-vs-master-commit.md` (the "long-lived resident may have stale code" pattern fired today: process restart didn't deploy the fix because local master was stale).

**What's needed.**

- v0.2 lands (this commit). No council pass required for v0.2 itself — it's a step-down from v0.1's commitment, which is the conservatively-safer move; the council finding that drove it was already addressed in the v0.1 fold; further adversarial review has diminishing returns when the change is "commit less, not more."
- Wave 2 begins (force=True audit across the ~24 sites). PR #350 established the playbook (drop / replace with single-agent fetch / keep with explicit-comment justification). Doing this site-by-site is the right work and will produce the next round of Wave 0 data.
- Memory project entry updated to reflect the resolution.

---

## V0.1 DESTINATION 2026-05-04 — A′ replaces Read A *(SUPERSEDED by V0.2 RESOLUTION 2026-05-05; preserved as historical record)*

**This block is preserved for historical record. It is NOT the current destination.** v0.1's destination commitment was conditional on PR #350's post-fix data per the Conditionality block below; that data landed 2026-05-05 and resolved as Python-fixable, reverting the destination to a question per the conditionality's own discipline. See V0.2 RESOLUTION above.

After the 2026-05-04 falsifying measurement (see AMENDMENT block below) and a substantive operator/agent dialogue on what the data actually argues for, **the destination of this roadmap is A′, not Read A.** Operator decision: 2026-05-04, this session.

**A′ in one sentence.** Stateful coordination — handler dispatch, identity middleware, dialectic resolution, sentinel/vigil/chronicler, force-reload-bound coordination paths — ports to BEAM. Stateless computation — LLM SDK calls, EISV math, pattern analysis, calibration, ML scoring, the MCP SDK transport layer until/unless an Elixir SDK exists — stays Python and is called from BEAM via Ports / HTTP. The cut is "stateful-coordinating vs stateless-computing," tested per-surface by ecosystem maturity, not "control plane vs intelligence plane" as v0 framed it.

**Conditionality on PR #350's post-fix verdict (folded from council pass C3, both lanes).** A′'s destination commitment is **conditional on PR #350's coordination-failure rate post-fix**. PR #350 (now merged 2026-05-05) drops `force=True` from 6 observe sub-handlers, eliminating the 3221-await loop on the request path. The Wave 0 channel will reveal one of two answers in the days following:

- **If observe-tool `coordination_failure.mcp_handler_timeout.tool_decorator` rate drops to near-zero post-fix** → the in-handler floor was the await loop (Python-fixable in-place). The remaining substrate-coupling evidence is then thinner than v0.1 leans on, and **v0.1 reverts to a question**: maybe the cut shift was right anyway because of the OTHER force-reload sites and the bystander effect, but the falsifying-evidence base is no longer the 60× number on KG calls — it's a smaller observation that requires its own substantive case. v0.2 reopens the destination decision.
- **If observe timeouts persist post-fix, OR if a different surface produces equivalent amplification under load** → the in-handler floor IS substrate-coupling, the falsifying evidence holds, and **v0.1's A′ destination becomes binding** (council ack pass v0.1.1 on this state, then merge as v0.1 final).

Per `feedback_substrate-migration-status-quo-bias.md` — both poles of the bias are wrong; "I want to fully migrate" is data about operator state, not about whether the substrate-tax is real. v0.1 honors that by gating the destination on the experiment that actually distinguishes the two readings of today's data, not on enthusiasm or council consensus alone.

**What changed from v0's Read A.** v0 cut at "Python thinks, BEAM governs," which placed governance MCP — the actual governance/control plane — on the *intelligence* side because it's running in Python today. That was the load-bearing error v0 inherited from the falsified "bug class is closed" premise. The 60× amplification measurement says the substrate-coupling tax is alive on every coordination surface that runs in Python on a shared anyio/asyncio event loop, governance MCP included. A′ moves the cut to where the data actually puts it: the boundary is "does this surface hold state and coordinate, or does it compute?" — not "is this surface decision-making versus reasoning?"

**What does NOT change.** The kernel doc's non-goal "Do not rewrite UNITARES in Elixir" stands — A′ does not rewrite UNITARES; it ports the coordination layer and keeps the compute layer in Python. The Pi-side anima-broker decision (retired 2026-05-01) stands — A′ is Mac-side governance MCP scope, not Pi. Lease plane (Phase A complete 2026-05-03) is a proof of pattern A′ generalizes; nothing about it changes.

**The MCP SDK gate (full Read B remains conditionally open).** A′ has one explicit gate to potentially-future Read B: the Anthropic Python MCP SDK is the primary reason the MCP transport layer stays Python under A′. If a production-mature Elixir MCP SDK lands (Anthropic-shipped, community-built and battle-tested, or a credible hand-roll spike) AND the Wave 0 channel post-A′ shows the BEAM↔Python boundary itself accruing new substrate-tax patterns at the Ports interface, then porting the MCP transport layer becomes the natural Wave-N decision and Read B comes onto the table. The gate is **explicitly external-dependency-bound** so the destination doesn't drift on internal enthusiasm. See §"MCP SDK gate" below for the exact trigger.

**Wave reordering under A′.** v0's "Wave 2 deferred pending Wave 1 evidence" reads under v0.1 as: Wave 1 (Sentinel) ships; Wave 2 is audit pipeline + lease integration (the highest-volume coordination paths after Sentinel); Wave 3 is handler dispatch + identity + dialectic resolution (the largest single port; gets its own RFC and council passes). Each wave still gates on the prior wave's exit criterion via the Wave 0 channel. See §"Wave 2 — under v0.1" and §"Wave 3 — under v0.1" below.

**Stop sign added under v0.1.** Re-cutting at "control plane vs intelligence plane" or any phrasing that places governance MCP coordination on the Python-permanent side is now explicitly out of bounds without operator authorization to revert v0.1. v0's Read A reasoning is preserved below as historical record; do not silently restore it.

**Source of the destination decision.**

- **2026-05-04 operator/agent dialogue** (this session). Operator question "is hybrid best? what do we lose in python if we go full on?" prompted a per-surface ecosystem-maturity audit instead of a generic "Python is for ML" framing. The audit produced the cut shift.
- **AMENDMENT 2026-05-04 evidence** (below). The 60× amplification number on governance-MCP path is the falsifying observation that v0's destination-decision rested on a closed premise.
- **Wave 0 channel** (PRs #342 + #345 + #348 + #350 all merged 2026-05-04/05). Real coordination_failure events confirming the substrate-coupling fingerprint on the governance-MCP request path, captured by the channel v0 prescribed for exactly this question. PR #350's post-fix data is the experiment that gates v0.1's destination commitment per the Conditionality block above.
- **Lease plane Phase A** (PR #305, merged 2026-05-03). Proves the Postgrex / OTP / Ports pattern works at scale; A′ is the same pattern applied to more surfaces.

**What's needed.**

- This v0.1 amendment lands. v0.2+ may revisit the cut with more Wave 0 data; v1 lands when Wave 1 closes and Wave 2 scope locks.
- Wave 0 instrumentation continues to evolve (PR #350 merged 2026-05-05; further force-reload audits across non-observe surfaces are now Wave 2 scope per v0.1 — see Wave 2 below).
- Council pass on this v0.1 amendment, per v0's discipline. Findings folded inline before the v0.1 status is treated as binding.

---

## AMENDMENT 2026-05-04 — falsifying measurement on governance-MCP path

**Read this before any other section.** A measurement on the governance-MCP request path on 2026-05-04 falsifies a load-bearing premise of v0.

**The measurement.** KG calls that complete in 21–71ms standalone run at ~4,464ms in-handler — a ~60× amplification, with the floor sub-100ms and the rest in scheduling / pool-acquisition / event-loop contention. The amplification is, by definition, in the substrate-coupling layer, not in Postgres or Cypher.

**What this falsifies.** v0 cites PR #290 (Sentinel-loop call site, ">400 cycles since restart with zero failures") as evidence the asyncpg/anyio bug class is closed and uses that to declare Wave 1's BEAM motivation "dead" (§"Wave 1 — Why first — the honest motivation") and to re-anchor the roadmap on substrate-fit-not-bug-fix grounds (§"Convergent evidence behind the substitution"). The 2026-05-04 measurement says the bug class is alive on a different surface — same coupling, different call path. PR #290 closed it at *one site*, not at the bug-class level. The conflation drove the Read-A-as-stable-destination conclusion.

**What this does NOT do.**

- It does not by itself argue Read B (full rewrite). The operator's stated destination ("full BEAM nervous system") and the substrate-migration-enthusiasm-bias check from §"Operator-consent framing" both still apply.
- It does not invalidate the lease-plane Phase A or the control-plane / intelligence-plane cut. Those stand on their own evidence.
- It does not retire Wave 0 — Wave 0's measurement infrastructure is exactly what makes amendments like this one possible, and is more clearly load-bearing now, not less.

**What it does change.**

- Sections that depend on "bug class closed" — specifically the bullet on line 17 ("Wave 1's central premise was stale"), the bullet on line 19 ("the asyncpg/anyio bug class … was closed in production"), the §"Wave 1 — Why first" claim "**That motivation is dead**", and the supporting citations on lines 219, 227, 230 — should be re-read as scoped-to-Sentinel-loop, not bug-class-closure.
- The "Read A as stable destination, not a way-station to Read B" framing in §"What this document is" depends on the falsified premise. It is not automatically wrong (substrate fit, supervision discipline, and operator cost are all independent arguments), but it no longer carries the "and the bug class is fixed anyway" wind that the original framing leaned on.
- Wave 1's exit criterion ("zero coordination-class incidents in the Wave-0 instrumentation feed for 14 days") is now also a probe of whether the bug class has substrate-shaped recurrence on a BEAM-resident service, not just a parity check. The same wave will produce the comparison data Read B's case rests on.

**What's needed.**

- Operator decision on whether v0's strategic conclusion holds, weakens, or flips given the new measurement. This amendment does not pre-decide; it re-opens a question v0 closed prematurely.
- A separate amendment or v0.1 that re-states Wave 1's BEAM motivation honestly (substrate-fit AND live bug class on governance-MCP path, not substrate-fit-only-because-bug-class-is-fixed).
- CLAUDE.md §"Substrate Tax: anyio-asyncio Coupling" (updated 2026-05-04) is the operational counterpart to this amendment — it tells in-repo agents the patterns are workarounds, not architecture.

**Source — and why this is on-mission for Wave 0, not adjacent to it.** v0 explicitly frames Wave 0 as the measurement infrastructure that makes later waves' exit criteria evaluable: "Without Wave 0, no later wave's 'exit criterion' can be honestly evaluated and no Read B trigger can fire on evidence rather than vibes" (§"Wave 0 — coordination_events"). The 2026-05-04 measurement is the first round of exactly that evidence:

- **Wave 0 channel proper.** PRs #342 (foundation) + #345 (step 2A: MCP decorator timeout chokepoint emit) + #348 (caller agent_id + session_id context fallback) produced 6 `coordination_failure.mcp_handler_timeout.tool_decorator` events on observe / list_agents in the ~10.75–14.5h window after #345 merged (first event at 21:15 UTC = 10.75h after merge; cluster running through 18:57 MDT next day = 14.5h). 100% concentration on two consolidated tools (`observe`, `list_agents`) at the time v0.1 was first drafted; the dataset has since grown to 13 events with additional event types (process_agent_update, detect_stuck_agents, identity), reducing observe/list_agents share to ~46% — the convergence on observe/list_agents was a *first-window* pattern, not a permanent one. Cascade pairs at 15:15:00.88 / 15:15:00.93 and 18:57:37.93 / 18:57:41.99 (MDT) suggesting in-handler contention; one 22.6s elapsed-past-15s outlier (`elapsed_s=22.615` at 18:18:20 MDT) indicating cancellation propagation friction (an asyncio/anyio coupling tell). These are the substrate-coupling fingerprint, captured by the channel the roadmap said would capture it. The data is truncated at the 15s decorator wall, so the channel sees the symptom but not the magnitude.

  **Schema-routing drift (live-verifier finding):** the events are written to `audit.events` (with `event_type LIKE 'coordination_failure.%'` filter), NOT to the dedicated `audit.coordination_events` table specified in v0's Wave 0 envelope section. The dedicated table exists with the correct schema and check constraints but is empty; `src/coordination_failure_emit.py` (the production wire from `@mcp_tool`'s TimeoutError handler) deliberately routes to `audit.events` via `audit_logger._write_entry` because the council BLOCKED the direct asyncpg-await-from-decorator path. This is consistent — the bug class itself is what blocked the dedicated-table path — but v0's Wave 0 envelope language reads as if `audit.coordination_events` is the canonical surface, which it currently is not. Routing to the dedicated table is a tactical follow-up, not a Wave 2 prerequisite (see Wave 2 below for v0.1 rescoping).
- **Probe alongside.** A parallel Claude session, looking for the unscoped magnitude, measured 21–71ms standalone vs ~4,464ms in-handler on KG calls — the ~60× number cited above. This is the same coupling, measured at a different boundary (per-call latency rather than per-handler timeout).
- **Wave 0 is producing the experiment, too.** PR #348's planned follow-up — drop `force=True` on `observe(action=aggregate|anomalies)` so the 3221-await `load_metadata_async` loop comes off the request path — is a Wave 0–enabled experiment. Post-fix coordination_failure rate is the verdict on whether the in-handler floor was the await loop (Python-fixable in-place) or the substrate-coupling floor (substrate-shaped). The signal will land on the same Wave 0 channel that surfaced the problem.

The amendment is what Wave 0 was for. The signal arrived earlier than the roadmap anticipated because step 2A was a low-risk wire and an unrelated probe converged on the same answer the channel was about to surface. Per `feedback_substrate-migration-status-quo-bias.md` ("ask 'what falsifying evidence would update you?' early"), this is exactly the falsifying-evidence shape the roadmap should fold in rather than route around.

---

## Operator-consent framing (read this first)

The operator stated 2026-04-30 (~13:30 local) and again 2026-05-03 that the goal-level destination is a "full BEAM nervous system" and expressed enthusiasm for "fully migrate." This roadmap **does not give the operator that.** It argues a hybrid architecture (BEAM for the control plane, Python for the intelligence plane) is the right shape, *not* a wholesale Python-to-Elixir rewrite of UNITARES governance MCP.

The operator should explicitly confirm or override this substitution before the roadmap is treated as binding. Drafting a roadmap that quietly translates "fully migrate" into "hybrid that keeps Python permanently" is the substrate-migration enthusiasm bias — exact mirror of the resistance bias in `feedback_substrate-migration-status-quo-bias.md`. Naming it does not absolve it; operator consent does.

**Convergent evidence behind the substitution (2026-05-03 — partially falsified 2026-05-04, see AMENDMENT block above):**

- 3-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`) on the prior draft of this roadmap rejected its diplomatic third-position framing and surfaced that Wave 1's central premise (asyncpg/anyio as live bug class) was stale. **[Falsified 2026-05-04: bug class is alive on governance-MCP path, ~60× amplification. The council's surfacing was correct against the prior draft's *wording*, but the underlying bug class is not stale.]**
- Independent third-party (Perplexity computer task, 2026-05-03): "I would not do a wholesale Python-to-Elixir/Erlang rewrite. The better path is: keep Python for ML, research code, model evaluation, data tooling, and fast iteration; move only the orchestration layer, agent supervision, long-running services, distributed coordination, queues, process lifecycles, telemetry, and fault-boundary logic onto BEAM."
- Live source check: the asyncpg/anyio bug class cited as primary motivation in earlier drafts was closed in production 2026-05-02 by PR #290 (`agents/sentinel/agent.py:413-450`); `phase-a-plan.md:347` confirms ">400 cycles since restart with zero asyncpg/anyio failures." **[Scope correction 2026-05-04: PR #290 closed it for the Sentinel-loop call site only, not at the bug-class level. The same coupling is alive on the governance-MCP request path, measured 2026-05-04.]**

If the operator wants Read B (full UNITARES rewrite in Elixir) regardless, this roadmap does not block it — but Read B requires a separate edit to `docs/ontology/beam-coordination-kernel.md` to amend the first non-goal ("Do not rewrite UNITARES in Elixir"), and that edit is the operator's call, not this document's.

## What this document is

A sequencing memo for *what BEAM does next, after the lease-plane Phase A complete on 2026-05-03 (PR #305)*. **Under v0.1 (2026-05-04), it commits to A′ as the destination** — stateful-coordinating surfaces port to BEAM, stateless-computing surfaces stay Python, the MCP SDK is an explicit external-dependency gate. v0's Read A and the §"The cut" framing below are preserved as historical record but are *not* the current destination; read the V0.1 DESTINATION block first.

It is not:

- a Phase B plan for the lease plane — that belongs to `surface-lease-plane-v0.md`;
- an amendment to `docs/ontology/beam-coordination-kernel.md` — the kernel doc's non-goal "Do not rewrite UNITARES in Elixir" remains satisfied under A′ (the compute layer stays Python; this is not a wholesale rewrite);
- an RFC for any specific port — each named wave below will get its own RFC if and when approved.

## v0.1 cut: stateful-coordinating vs stateless-computing

> **BEAM holds state and coordinates. Python computes.** *(v0.1, 2026-05-04 — supersedes v0's "Python thinks. BEAM governs." slogan. v0's framing is preserved in the historical-record §"The cut" block immediately below.)*

The v0.1 cut is per-surface, tested by ecosystem maturity rather than by static category. The test for a surface: "Does this code hold state, coordinate concurrent work, supervise tasks, or fight anyio/asyncio coupling?" If yes, it ports to BEAM. "Does this code compute over data with library dependencies that are decades-mature in Python (numerical, ML, LLM SDKs, schema validation)?" If yes, it stays Python and is called from BEAM via Ports / HTTP.

| Stays Python under v0.1 (with reason) | Moves to BEAM under v0.1 (with wave) |
|---|---|
| **MCP transport layer** — Anthropic Python SDK is upstream-first-class; no production-mature Elixir SDK exists today; see §"MCP SDK gate" for the explicit trigger that changes this | **Sentinel** (Wave 1) — fleet supervision; OTP-shaped |
| **`governance_core/`** (3,300 LOC; NumPy in `phase_aware.py` + `stability.py`) — numerical maturity | **`force=True` cleanup across remaining ~24 sites + lease-integration boundary hardening** (Wave 2) — reviewer-council-rescoped (see Wave 2 below for the rationale shift away from v0.1's original audit-pipeline framing) |
| **LLM SDK calls** (Anthropic, OpenAI) — tool-use, streaming, prompt cache ergonomics | **Handler dispatch + identity middleware + dialectic resolution coordination** (Wave 3) — the largest single port; gets its own RFC; explicitly NOT a clean cut at the `load_metadata_async` boundary (see "Cross-cutting concern" note below) |
| **Pattern analysis / calibration** — scipy/scikit-learn ecosystem | **Vigil** (post-Wave-3) — cron janitorial; substrate uniformity |
| **Pydantic v2 schemas** — declarative validation; Ecto changesets are a port-not-translate | **Chronicler** (post-Wave-3) — daily metrics; substrate uniformity |
| **KG retrieval (`src/knowledge_graph.py`, `hybrid_rrf` / `hybrid_rrf_graph` over AGE)** — see "KG retrieval placement" note below; this surface is where the 60× amplification was MEASURED, so its placement is load-bearing for v0.1's falsifying-evidence interpretation | — |
| **Watcher** — single-shot LLM pattern matcher per code edit; no coordination shape | — |
| **Hermes practice body** — research surface; solo-process; not coordination-shaped | — |
| **Pi anima-broker** — retired 2026-05-01 with measured falsifications; out of scope | — |
| **Discord dispatch** — TypeScript; different cost calculus | — |

The "stays Python under v0.1" column is **conditional**, not permanent. Specifically, the MCP transport layer is gated on external-SDK availability (§"MCP SDK gate"); the rest are gated on ecosystem-maturity comparisons that may shift over multi-year horizons (Nx maturing toward NumPy parity, Elixir LLM client libraries reaching production-grade ergonomics). v0.1's destination is A′; full Read B is conditionally-open if the gates close.

**KG retrieval placement (folded from architect council B1).** KG retrieval (`hybrid_rrf` over AGE through Postgres) is the surface where the 60× amplification was measured (see AMENDMENT block). Under v0.1 it stays Python because the AGE/Cypher integration in `src/knowledge_graph.py` is database-side (Cypher executes inside Postgres regardless of caller substrate) and the Python wrapper is a thin query layer that doesn't itself coordinate. **However:** the in-handler call sites that DO show the amplification — handler bodies that call `hybrid_rrf` or `hybrid_rrf_graph` — port to BEAM under Wave 3. The amplification on those paths is therefore expected to come down to the BEAM↔Python boundary cost (one Ports round-trip per KG call) rather than the in-handler floor's full 60×. This is a falsifiable prediction; Wave 3's exit criterion already requires "no new substrate-tax pattern at the Python-handler-body boundary" and Wave 2's Wave-0 schema extension (below) is what makes that measurement possible.

**Cross-cutting concern: `load_metadata_async(force=True)` (folded from reviewer council B2).** ~24 force=True call sites are spread across `src/mcp_handlers/dialectic/` (8: handlers.py + resolution.py + auto_resolve.py), `lifecycle/` (8: operations.py + mutation.py + resume.py + stuck.py), `support/condition_parser.py` (1), `admin/handlers.py` (1), `identity/handlers.py` (1), `agent_loop_detection.py` (1, line 601), plus other surfaces. Each is the same anti-pattern (full PG reload + 3221 sequential per-agent cache.set awaits) PR #350 removed from observe handlers, **except** that several of these legitimately need post-write read consistency (after an agent state mutation, reload to confirm). Wave 2 (v0.1-rescoped, see below) audits these site-by-site: drop force=True where the use is read-only-fleet-overview-shaped (matches PR #350's case), keep force=True where the use is post-write-consistency-shaped (matches lifecycle mutation patterns), or replace with single-agent fetch (`load_monitor_state(agent_id)` in executor) where only one agent's state needs freshness. The site-by-site audit is the Wave 2 work. Wave 3's "cleanly separable" claim survives this precisely because Wave 2 lands first — by Wave 3 the remaining force=True calls in dialectic/handlers.py et al. are either dropped or replaced with non-substrate-tax-amplified equivalents, so the Wave 3 port hits a coordination layer that's already been substrate-tax-mitigated.

## v0 historical-record cut: control plane vs intelligence plane (SUPERSEDED 2026-05-04)

> **Python thinks. BEAM governs.** *(v0 slogan, superseded by v0.1.)*

v0's organizing principle, framed by Perplexity 2026-05-03 (first session) and adopted in v0:

| Plane | v0: Stays Python (permanently) | v0: Ports to BEAM (eventually, by wave) |
|---|---|---|
| **Intelligence** | `governance_core/` (3,300 LOC; NumPy in `phase_aware.py` + `stability.py`); KG retrieval (`hybrid_rrf` over AGE); Watcher (LLM pattern matcher); Hermes practice body; paper v6/v7 corpus tooling; the dialectic engine's reasoning logic | — |
| **Control** | — | Sentinel (fleet supervision); Vigil (cron janitorial); agent lifecycle/heartbeats; identity/onboarding middleware; fault recovery (currently launchd; OTP supervision is a structural upgrade); event bus / telemetry |
| **Ambiguous** | The 31 `@mcp_tool` handlers are protocol glue + business logic, not "intelligence" — but their placement depends on the Elixir MCP server library question (see §"Read B-shaped risks"). The MCP transport layer itself sits on the control side conceptually but is gated by library maturity. | |

v0's "stays Python permanently" framing was load-bearing for v0's Read A as stable destination. **Under v0.1 the framing is superseded:** the placement of the 31 `@mcp_tool` handlers is no longer "ambiguous" pending the SDK question — it is "MCP transport stays Python until the gate closes; handler bodies port to BEAM under Wave 3 with the transport layer as a thin Python shim that proxies into BEAM after request unmarshalling." The v0 framing is preserved here so the diff between v0 and v0.1 is auditable, not lost.

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

## MCP SDK gate (v0.1)

A′ has one named external-dependency gate. v0.1 commits to A′ as destination *with* the gate explicitly open; it does not pre-commit to closing the gate.

**What the gate is.** The Anthropic Python MCP SDK is the load-bearing reason the MCP transport layer stays Python under A′. Re-implementing or maintaining a parallel Elixir MCP server is a non-trivial protocol-implementation cost that does not exist today and tracks Anthropic's ongoing protocol evolution.

**What closes the gate.** Any one of:

1. **Anthropic ships an official Elixir MCP SDK.** Tracked at the Anthropic SDK landscape (currently Python and TypeScript only).
2. **A community Elixir MCP server library reaches production-grade maturity.** Indicators: stable API across at least 3 minor protocol revisions; production deployments with public reference; passing the upstream MCP conformance tests (or the equivalent test suite the community has converged on); active maintainership.
3. **A credible Elixir MCP hand-roll spike completes (folded from architect council B2 — tightened to match the NOT-closure list).** A focused implementation effort that lands within bounded time (≤ 2 weeks of focused-engineer time) AND **supports the WHOLE MCP protocol surface** — tool-calls, notifications, prompts, sampling, resources, and tool-use streaming — without protocol-correctness regressions verified against the upstream `modelcontextprotocol/python-sdk` test suite (or community-converged equivalent). A spike that handles the easy 70% (tool-calls only) and stalls on streaming/sampling does NOT close the gate; the NOT-closure list below explicitly catches this case. The spike is the only path that moves the gate without external dependency, and is correspondingly the largest single risk surface; it is not a casual experiment.

**What the gate does NOT close on.**

- Operator enthusiasm. Per `feedback_substrate-migration-status-quo-bias.md`, both poles of the bias are wrong. "I want to fully migrate" is data about operator state, not about whether the SDK gap is bridged.
- A single working-prototype demo of one tool call over hand-rolled JSON-RPC. The MCP protocol surface is wider than tool-calls (notifications, prompts, sampling, resources, tool-use streaming) and the hand-roll either supports the whole surface or it doesn't.
- Apparent stalls in upstream Python SDK evolution. The Python SDK can be slow without that being grounds for replacing it.

**What happens if the gate closes.**

The MCP transport layer becomes the natural Wave-N port and Read B comes onto the table for explicit operator decision via the kernel-doc non-goal amendment process v0 prescribed (it does *not* port silently). Until the gate closes, v0.1's destination remains A′ with the MCP transport layer staying Python.

**What happens if the gate stays open indefinitely.**

A′ is stable destination. The BEAM↔Python boundary at the MCP transport layer accrues some glue (Ports protocol definitions, error translation, version-pinning) but the boundary is **bounded** — one well-understood interface — unlike the unbounded substrate-coupling tax that A′ eliminates from the coordination surfaces. This is the case for A′ as steady state, not transition.

## A′ is the destination (under v0.1; supersedes "Read A is a stable destination")

**Under v0.1, A′ is the destination.** v0's "Read A is a stable destination" is preserved below as historical record but is superseded; the destination is no longer Read A.

The original v0 draft framed Read C as "incremental ratchet" to an open-but-deferred Read B. The architect council on v0 found this collapses to Read A + governance, and that the "mature systems grow new substrate alongside until the old part becomes vestigial" claim was rhetorical comfort, not principle. Mature systems also fossilize around foreign substrate (C extensions in Python, JNI in JVM stacks, CGI-era PHP under modern Rails); the conditions distinguishing those outcomes are not generally known, and assuming the favorable one is bias.

v0 dropped the trichotomy and committed to a binary: Read A is the destination. v0.1 retains the binary commitment-discipline but updates the destination: **A′ is the destination.** Read B is open only via explicit operator decision AND closure of the §"MCP SDK gate" AND a separate edit to the kernel doc — not via roadmap drift, not via single-trigger enthusiasm.

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

The earlier draft pitched Sentinel-on-BEAM as the cure for the asyncpg/anyio bug class. **That motivation is *not* dead — see AMENDMENT 2026-05-04 at top of doc.** PR #290 closed the CONCERN at one call site (`agents/sentinel/agent.py:413-450` runs `asyncio.run(asyncio.wait_for(poll_forced_release_alarms(...), 30s))` inside a `loop.run_in_executor` call; `phase-a-plan.md:347` records ">400 cycles since restart with zero asyncpg/anyio failures"), but the same bug class is alive on the governance-MCP request path with ~60× amplification (measured 2026-05-04). For Wave 1 specifically, the Sentinel-loop call site IS mitigated, so the original "structural fit, not bug fix" reframing below remains the right argument *for Sentinel itself* — but it should not be read as evidence that the bug class is closed in the system.

Sentinel-on-BEAM's real motivation (with the original pitch scoped, not retired):

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

## Wave 2 — under v0.1 (REVISED post-council): force=True cleanup + lease-integration boundary hardening + Wave 0 schema extension

**v0.1 supersedes v0's "deferred pending Wave 1 evidence."** The original v0.1 draft framed Wave 2 as "audit pipeline + lease integration"; the reviewer council BLOCKed this on a factual ground — `audit.events` is already fire-and-forget by design (`src/audit_log.py:519-522` docstring: "Postgres persistence is intentionally fire-and-forget … keeping audit logging off latency-sensitive handler paths"), so the "substrate-coupling on highest-cardinality surface" justification did not actually apply to that path. The dedicated `audit.coordination_events` table is empty and the production wire deliberately routes around it. Audit-pipeline-as-Wave-2 was arguing against a problem that has already been mitigated differently.

**Revised Wave 2 scope (folded from reviewer council B1 + C3, both lanes):** the actually-highest-volume coordination surface still under substrate-tax today is the ~24 `force=True` call sites in dialectic, lifecycle, admin, identity, support, and agent_loop_detection handlers. PR #350 dropped force=True from 6 observe sub-handlers; the rest are still there and produce the same 3221-await pattern under load. **Wave 2 is the systematic site-by-site audit:**

### Wave 2 scope

1. **`force=True` cleanup across all remaining ~24 sites** (`src/mcp_handlers/dialectic/handlers.py` 6 calls, `dialectic/resolution.py` 1, `dialectic/auto_resolve.py` 1, `lifecycle/operations.py` 3, `lifecycle/mutation.py` 3, `lifecycle/resume.py` 1, `lifecycle/stuck.py` 1, `support/condition_parser.py` 1, `admin/handlers.py` 1, `identity/handlers.py` 1, `agent_loop_detection.py:601` 1, plus any surfaced by the audit). Each site receives one of three treatments:
   - **Drop force=True** (PR #350 pattern) where the use is read-only-fleet-overview-shaped — the in-memory cache is fresh enough.
   - **Replace with `load_monitor_state(agent_id)` in executor** where only one agent's state needs freshness post-mutation. Cheaper than a fleet reload.
   - **Keep force=True with explicit comment justification** where the use is post-write-consistency-shaped (mutation handler reloading to confirm the write landed) AND the consistency requirement is real (i.e., the next read MUST see the just-written state). These are the legitimate cache-coherence patterns. The Wave 2 audit makes them explicit.
2. **Lease-integration boundary hardening.** Wave 1's Sentinel-on-BEAM speaks to governance MCP via REST. Wave 2 hardens that boundary — versioned contracts, error translation, supervised health — before Wave 3's larger handler-dispatch port takes the boundary as load-bearing. (This survives the architect council C1 ordering question because Wave 3 will reuse Wave 2's REST-contract work, just on the BEAM side after handler dispatch ports; the contract definition itself is reusable.)
3. **Wave 0 schema extension** (folded from architect council C4): add the `coordination_failure.beam_python_boundary.*` event_type namespace before Wave 3, so Wave 3's exit criterion #3 ("no new substrate-tax pattern at the Python-handler-body boundary") is measurable. Per v0's "Stability discipline" rule, this extends by adding a new dotted namespace, never by reusing existing ones.
4. **Tactical: `audit.coordination_events` routing fix.** The dedicated table exists with the correct schema but is empty (events go to `audit.events` via `coordination_failure_emit.py`). Dual-writing to both tables — without removing the existing audit.events path — restores the dedicated replay surface v0's Wave 0 envelope specified, without breaking the Wave 1 exit criterion's existing query. This is a Wave 2 task only because it's adjacent to the boundary work; could ship sooner if convenient.

### Why this scope

The volume argument from the original draft survives the rescope but lands on the right surface: ~24 sites × per-call cost (~16s blocking) is the actual coordination-tax cardinality today, not the audit writer. The Wave 0 channel will show whether Wave 2 closes the substrate-tax surface or surfaces a residual that's distinct from the force-reload pattern (substrate-coupling at a smaller cardinality). Either outcome informs Wave 3.

### Exit criterion (gated by Wave 1 + Wave 0)

- Wave 1 has closed (its 14-day window held with zero coordination-class incidents).
- All ~24 force=True sites have been audited and treated; the remaining force=True calls in master have explicit-comment justifications matching one of the documented use cases.
- Wave 0 schema extension `coordination_failure.beam_python_boundary.*` is live in `audit.events` / `audit.coordination_events` AND dual-writing is operational (architect C4 prerequisite).
- Wave 0 channel shows the 6 observe + 2 list_agents bystander timeouts that motivated PR #350 do not recur, AND no new force-reload-shaped events emerge from non-observe handlers, AND no new event_type pattern emerges that isn't already in the envelope (per "Stability discipline").
- Lease-integration boundary has absorbed at least one BEAM-side restart and one Python-side restart with no event loss attributable to the boundary (induced fault, observed, no manual intervention).

If the post-Wave-2 channel surfaces the same 60× amplification on a *different* surface that has nothing to do with force-reload — e.g., on knowledge graph reads inside a handler that doesn't touch metadata — that is the strongest possible confirmation of the substrate-coupling thesis (because the Python-fixable in-place hypothesis is then conclusively excluded), and Wave 3's BEAM motivation strengthens further.

## Wave 3 — under v0.1: handler dispatch + identity middleware + dialectic resolution

**v0.1 names Wave 3 explicitly.** This is the largest single port — the governance MCP handler dispatch layer (`src/mcp_handlers/` glue, identity middleware, dialectic resolution coordination). It gets its own RFC and full council passes; this section is roadmap-level scope only.

### Roadmap-level scope (the RFC will detail)

- Handler dispatch (the @mcp_tool decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per §"MCP SDK gate") and proxies to BEAM after request unmarshalling.
- Identity middleware (`src/mcp_handlers/middleware/identity_step.py`, the session-context contextvar chain, agent_id resolution, label resolution) ports to BEAM. This is the largest single coordination surface in governance MCP today and the highest-leverage substrate-tax elimination.
- Dialectic resolution (`src/mcp_handlers/dialectic/`) ports to BEAM. The dialectic engine's *reasoning logic* — what makes a thesis converge, the dialectic-knowledge-architect's substantive work — stays Python (it's compute, not coordination) and is called from BEAM. The coordination layer (session lifecycle, quorum tracking, condition resolution, audit emission) ports.
- Out of scope: `governance_core/`, Watcher, the LLM SDK call paths inside handlers (those stay Python and are called from BEAM via Ports).

### Exit criterion

- Wave 2 has closed (its exit criteria above all hold).
- Handler dispatch on BEAM has served production governance MCP traffic for ≥ 21 days continuous (longer window than prior waves because this is the largest blast-radius port).
- Wave 0 channel shows zero coordination-class incidents attributable to handler dispatch over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
- Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical).

## Post-Wave-3 candidates (under v0.1, deferred)

After Wave 3 closes, what's left in Python is genuinely compute (governance_core math, LLM SDK calls, pattern analysis) plus the MCP transport layer (gated externally). Wave 2-3 will have produced enough Wave 0 channel data to know whether further porting of any kind is warranted. Candidates:

- **Vigil** — substrate uniformity for the cron-driven janitorial agent. Easy port if Wave 2-3 has solidified the patterns.
- **Chronicler** — substrate uniformity for the daily metrics agent.
- **MCP transport layer** — only if §"MCP SDK gate" closes.
- **A new BEAM service that fills a gap discovered during Waves 1-3** — only if real.
- **Pause.** Solo-founder runway is finite; closing A′ at Wave-3-exit and stabilizing is a real option.

The decision belongs to the operator at Wave 3 exit, with Wave 0's accumulated incident-rate data and Wave 2-3's parity evidence in hand.

## Out of scope for this roadmap

Stays Python (and not because Read A might "eventually" port them — the categorization is structural):

- **`governance_core/`** — intelligence plane. NumPy is used in `governance_core/phase_aware.py` and `governance_core/stability.py`; the dynamics path itself is stdlib. Either way, this is math, not coordination.
- **Watcher** — single-shot LLM pattern matcher invoked per code edit. No coordination shape; OTP supervision adds nothing. (Note: even staying Python, Watcher hits governance MCP REST endpoints — see §"Hidden costs" #4.)
- **Hermes practice body** — research/practice surface per `feedback_violist-poker-asymmetry` and `Mnemos_07d0f9c7`. Solo-process. Stays Python.
- **Pi anima-broker** — measured falsification 2026-05-01 (S1 idle RSS 123.7 MB falsified the §8.2 prediction; S6 distribution-win 50–75% on the 70% gate). See `docs/proposals/anima-broker-beam-port-v0.md`. Re-open requires operator-authorized "Lumen as appliance OS" reframe or a second Pi joining the fleet, not enthusiasm.
- **Discord dispatch bot** — TypeScript, not Python. Coordination-shaped (per-thread sessions, shared `.dispatch-sessions.json`) but not suffering Python's coordination class. Different cost calculus; port only if it starts hurting.
- **Data plane (Postgres + AGE schema, including `lease_plane`)** — the migration creates schema `lease_plane`, not `coordination`. Postgrex talks to it. The KG retrieval rebuild (`UNITARES_KNOWLEDGE_BACKEND=age`, `hybrid_rrf` / `hybrid_rrf_graph`) does not change.

## What's *not* a full-Read-B trigger (under v0.1)

**v0.1 update:** the v0 list below was framed as "Read B trigger" when the v0 destination was Read A. Under v0.1's A′ destination, the trigger list is the path *past* A′ to full Read B (porting the MCP transport layer and the remaining stateless-compute surfaces). The list still applies — and applies more sharply, because A′ already moves the surfaces v0's Read A kept Python.

Substrate-migration enthusiasm is the *prompt* to write this roadmap, not *evidence* that justifies escalation. Per `feedback_substrate-migration-status-quo-bias`, the bias cuts both ways: resistance and enthusiasm are symmetrical errors. Operator stating "I'm all about BEAM" is data about operator state, not about whether full-Read-B's costs are warranted.

What *would* be a full-Read-B trigger (kept here so the question is honestly open, not buried):

1. Wave 0 incident-rate instrumentation runs for ≥ 60 days post-A′-completion (i.e., post-Wave-3) and shows the coordination-class bug rate is *not* zero in the remaining Python compute surfaces. (Under A′ the remaining Python surfaces are stateless compute — `governance_core/` math, LLM SDK calls, pattern analysis — so a non-zero rate at the BEAM↔Python boundary itself would be the signal that the boundary tax is unbounded after all.)
2. The §"MCP SDK gate" closes (Anthropic ships an Elixir SDK, OR a community library reaches production maturity, OR a credible hand-roll spike completes).
3. A second runtime consumer materializes that wants OTP-native APIs (a non-Python harness, a partner integration).
4. Operator runway permits the additional port effort without sacrificing paper / fellowship / Lumen / dispatch.

Two or more triggers → operator decides whether to amend the kernel doc non-goal "Do not rewrite UNITARES in Elixir" (which A′ does NOT amend, but full Read B does). Single trigger → re-evaluate the roadmap, not the kernel doc.

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
- **(v0.1 stop sign)** re-cutting at "control plane vs intelligence plane" or any phrasing that places governance MCP coordination on the Python-permanent side — that is v0's superseded framing; v0.1's cut is "stateful-coordinating vs stateless-computing" and reverting requires explicit operator authorization;
- **(v0.1 stop sign, folded from architect council C2)** reclassifying any specific surface from stateful-coordinating to stateless-computing (or vice versa) — e.g., a future revision arguing "dialectic resolution turns out to be mostly LLM calls, so let's leave it Python" — without operator authorization to amend the v0.1 cut table itself. The per-surface table is load-bearing, not just the cut name. Implicit drift via individual-row reclassification has the same end-state as explicit cut reversion and is correspondingly defended.
- **(v0.1 stop sign, folded from reviewer council N1)** treating PR #350 as having closed the `force=True` problem system-wide. PR #350 closed it for 6 observe sub-handlers ONLY. The remaining ~24 sites (dialectic, lifecycle, admin, identity, support, agent_loop_detection) are explicit Wave 2 scope; treating them as already-handled is exactly the scope-creep this stop sign exists to catch.
- **(v0.1 stop sign)** treating §"MCP SDK gate" closure as having occurred without one of the three named conditions actually being met (Anthropic SDK, community library at production maturity, or completed hand-roll spike covering the WHOLE protocol surface per condition #3 as tightened) — partial demos, subset spikes, and stalls in upstream are explicitly not gate-closure;
- collapsing A′ back into "incremental ratchet to Read B" language — under v0.1 A′ is the destination, not a way-station;
- collapsing v0.1 back into v0's Read A destination via "we found the substrate tax is not so bad after all" — the falsifying evidence (60× amplification) is a measurement, not a sentiment;
- declaring any wave successful without **both** the per-wave window AND the Wave 0 incident-rate evidence;
- treating operator enthusiasm as substitute for §"What *would* be a full-Read-B trigger" evidence;
- pre-committing wave N+1 before wave N has shipped and its window closed;
- a full-Read-B spike has been "proposed but not scheduled" for > 90 days (drift by deferral; named gate, not ambient sentiment);
- including `governance_core/` math, Watcher, Pi-side anima-broker, Hermes practice body, Discord dispatch, or the data plane in any wave without separate operator approval;
- Pythonx / NIF embed proposed as the BEAM↔Python boundary instead of Ports / HTTP / gRPC / Redis streams.

## Re-evaluation cadence

Revised:

- after each wave ships and its window closes;
- if Wave 0 incident-rate data shifts materially in either direction;
- **after PR #350's post-fix data lands** (per V0.1 DESTINATION conditionality block) — verdict on whether observe timeouts close (Python-fixable) or persist (substrate-shaped) is what binds or reverts v0.1's destination commitment;
- if the Elixir MCP server library landscape changes (community library lands, Anthropic adds an SDK, or a credible whole-surface hand-roll spike completes);
- if a full-Read-B trigger fires.

**SDK gate monitoring (folded from reviewer council C1).** The MCP SDK gate cannot close without the operator noticing if no one is watching. v0.1 names the operator as the cadence owner for the SDK landscape check (no automated equivalent exists, by design — this is an external-ecosystem question, not a runtime metric). Operator commits to a quarterly check on Hex.pm + GitHub for new Elixir MCP libraries against the gate criteria, and to monitoring `modelcontextprotocol/elixir-sdk` 404 → exists transitions. Currently-tracked: `ex_mcp` v0.9.1 (active 2026-04-28, 14 stars — below production-mature bar), `hermes_mcp` v0.14.1 (dormant since 2025-08, not tracked further unless revived). If a third library appears or one of these crosses the production-maturity bar, the gate closure check fires.

Revisions land as `beam-footprint-roadmap-v0.1.md`, `v0.2.md`, etc.; full v1 when Wave 1 closes and the question shape itself updates.

## Relationship to other docs

| Doc | What it owns | Relationship to this roadmap |
|---|---|---|
| `docs/ontology/beam-coordination-kernel.md` | Integration framing, non-goals (incl. "Do not rewrite UNITARES in Elixir"), OTP process shape, lease-plane Phase 0–4 sequence | Roadmap respects non-goals; expansion past lease plane sits *outside* its scope |
| `docs/proposals/surface-lease-plane-v0.md` | Lease-plane contract spec, Phase A → Phase B gates | Roadmap's Wave 1 (Sentinel) is downstream of Phase A complete; not a Phase B item |
| `docs/proposals/surface-lease-plane-phase-a-plan.md` | PR-by-PR Phase A breakdown with status; the Sentinel asyncpg CONCERN closed at line 347 | Source of truth for Wave 1's "asyncpg fixed" claim |
| `docs/proposals/plexus-scope.md` | Plexus product/boundary name; what Plexus v1 owns and does not own | Roadmap is about runtime substrate, not lease semantics; orthogonal |
| `docs/proposals/anima-broker-beam-port-v0.md` (retired) | Pi-side BEAM port; retired with measured falsifications 2026-05-01 | Roadmap explicitly excludes Pi from scope |

## Sources of the substitution argument (v0)

- Council pass on draft v0 (2026-05-03), three agents in parallel:
  - `dialectic-knowledge-architect`: surfaced the operator-consent issue, the trichotomy collapse, the falsification asymmetry, the missing reversibility/cognitive-surface category, and the rhetorical-comfort claim.
  - `feature-dev:code-reviewer`: surfaced the dead Wave 1 motivation (Sentinel asyncpg fixed), the missing measurement infrastructure, the imperfect 30× denominator, the missing Watcher REST coupling.
  - `live-verifier`: factual corrections (test count 330 not 329, PR #305 merged 2026-05-03 not -05-02, schema `lease_plane` not `coordination`, "60 MiB target" absent from anima-broker doc, scipy unverified, NumPy verified in `phase_aware.py` + `stability.py`).
- Independent third-party (Perplexity computer task, 2026-05-03): control-plane / intelligence-plane cut; Ports-not-NIFs interop discipline; "hybrid architecture first, not full rewrite."
- Direct source verification (v0 drafter, 2026-05-03): `agents/sentinel/agent.py:413-450` confirms the asyncpg/anyio mitigation; `phase-a-plan.md:347` confirms ">400 cycles, zero failures."

## Sources of the v0.1 destination shift (2026-05-04)

- **2026-05-04 falsifying measurement** (parallel Claude session): governance-MCP request path KG calls 21–71ms standalone vs ~4,464ms in-handler (~60× amplification). See AMENDMENT block above. The measurement falsifies v0's load-bearing premise that PR #290 closed the asyncpg/anyio bug class system-wide.
- **2026-05-04 Wave 0 channel evidence** (PRs #342 + #345 + #348 + #350 all merged 2026-05-04/05): 6 `coordination_failure.mcp_handler_timeout.tool_decorator` events on observe / list_agents over the ~10.75–14.5h window after #345 merged, cascade pairs at 15:15:00.88/.93 and 18:57:37.93/41.99 MDT, 22.6s elapsed-past-15s outlier (`elapsed_s=22.615` at 18:18:20 MDT) — substrate-coupling fingerprint, captured by the channel v0 prescribed for exactly this question. Schema-routing drift: events are in `audit.events` filtered by `event_type LIKE 'coordination_failure.%'`, NOT in the dedicated `audit.coordination_events` table (which exists with the correct schema but is empty). Routing fix is Wave 2 scope per v0.1.
- **2026-05-04 operator/agent dialogue** (this session): operator question "is hybrid best? what do we lose in python if we go full on?" prompted a per-surface ecosystem-maturity audit. The audit produced the cut shift from "control vs intelligence" (v0) to "stateful-coordinating vs stateless-computing" (v0.1) by identifying that the v0 cut placed governance MCP coordination on the wrong side of its own test. Operator decision recorded after seeing the per-surface table.
- **Council pass on v0.1 (2026-05-04, three agents in parallel; same precedent as v0):**
  - `dialectic-knowledge-architect`: 2 BLOCK + 4 CONCERN + 3 DRIFT + 4 NIT — addressed inline. B1 (KG retrieval cut placement undefined) folded as new "KG retrieval placement" note in the cut section. B2 (MCP SDK gate condition #3 contradicted NOT-closure list — subset spike vs whole-surface) folded by tightening condition #3 to "whole protocol surface" with explicit cross-reference to the NOT-closure bullet. C1 (Wave 2 ordering — lease-integration redundancy with Wave 3) addressed by Wave 2 rescope (force=True cleanup primary; lease-integration boundary work stays but is reusable in Wave 3, not redone). C2 (drift via per-surface reclassification) folded as new stop sign. C3 (enthusiasm-pole bias on PR #350 outcome) folded as the Conditionality block in V0.1 DESTINATION — destination is conditional on PR #350's post-fix data. C4 (Wave 0 schema gap for BEAM↔Python boundary tax) folded as Wave 2 prerequisite #3. DRIFTs and NITs corrected inline.
  - `feature-dev:code-reviewer`: 2 BLOCK + 3 CONCERN + 2 DRIFT + 2 NIT — addressed inline. B1 (Wave 2 audit-pipeline justification was wrong; `audit.events` is fire-and-forget by design, substrate-coupling argument doesn't apply) folded by Wave 2 rescope (audit pipeline dropped; force=True cleanup substituted as the actual highest-volume coordination surface). B2 (Wave 3 separability undermined by ~24 force=True calls in the coordination layer) folded as new "Cross-cutting concern" note in the cut section, with the explicit Wave 2 ordering (force=True cleanup before Wave 3 ports the coordination layer). C1 (SDK gate monitoring is passive) folded into Re-evaluation cadence as named-owner (operator) + quarterly check + currently-tracked-libraries. C2 (Wave 1 exit criterion ambiguity post-Wave-2) folded into Wave 2's tactical "audit.coordination_events routing fix" item — dual-write preserves the Wave 1 query while restoring the dedicated table. C3 (force=True scope much larger than PR #350) folded as new stop sign + explicit Wave 2 enumeration (~24 sites, file by file). DRIFTs (test count drift, REST surface mechanism deferred to Wave 3 RFC) noted; D1 explicitly stated as architectural-sketch-deferred-to-RFC, D2 stays in v0 historical section per drafter discipline.
  - `live-verifier`: 7 VERIFIED + 6 DRIFT + 0 REFUTED + 1 SOURCE_ONLY — DRIFTs corrected inline (8.5h → 10.75–14.5h window; cascade timestamp .83 → .88; PR #350 status open → merged; phase-a-plan.md citation 347 → 349; sentinel citation 413-450 → helper at 413-454, run_in_executor at 668; force=True scope adds agent_loop_detection.py:601 + condition_parser.py:1; audit.coordination_events table empty / events route to audit.events). The 60× number is verified at order-of-magnitude (different specific call paths give 8–253×; "60×" is plausible for the call the prior session measured but not pinned to a specific handler). The "100% concentration on observe/list_agents" claim was true at first-window; dataset has since grown to 13 events with additional event types (~46% concentration now) — folded as "first-window pattern, not permanent."

All v0.1 council BLOCKs are addressable via folding without architectural revisit; none re-falsify A′ as destination. Wave 2 rescope is the largest single v0.1 change and is structural, not text-tightening.

The v0.1 destination survives if the operator confirms the rescope explicitly AND PR #350's post-fix data confirms the substrate-coupling reading per the Conditionality block above. Otherwise v0.2 reverts the destination to a question, not a plan.
