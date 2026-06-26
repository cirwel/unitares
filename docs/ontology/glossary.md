# UNITARES Glossary — Terms Keyed by the Question They Answer

**Created:** June 20, 2026
**Status:** Living document. Expect churn; the field is inventing terms and so are we.
**Companion to:** `docs/ontology/identity.md`, `docs/ontology/harness-substrate-plurality.md`, `docs/ontology/beam-coordination-kernel.md`
**Audit trail:** `docs/ontology/glossary-drift-audit-2026-06-20.md` (point-in-time sweep that seeded this file)

---

## How to read this glossary

A term here is defined by **the question it answers**, not by a list of examples.
This is deliberate. A term defined by its discriminating question survives
redefinition; a term defined by examples rots the moment a new example straddles
the boundary. (The canonical example: "harness" defined as "Hermes / Claude Code
/ Codex" breaks the instant BEAM starts hosting an agent loop — see the open gap
below. The question — *"what body mediates this agent's action?"* — does not
break; it just gets a new answer.)

The layer table in `harness-substrate-plurality.md` already works this way
("Harness | what body/interface mediates action?"). This file generalizes that
discipline across the whole vocabulary.

## How to add or change a term

1. **State the question it answers**, in one line, before any examples.
2. **When a term splits, do not rename — disambiguate.** If the same word starts
   answering two different questions, give each sense a parenthetical qualifier
   (`substrate (inference)` vs `substrate (deployment)`) and let both coexist
   explicitly. Renaming invents a word nobody adopts; qualifying makes the
   existing collision legible. This is the rule `harness-substrate-plurality.md`
   half-follows already.
3. **Point at the canonical doc**, don't re-define inline. Inline re-definitions
   are how drift starts.
4. **A homonym is not a bug to fix.** Three legitimate senses of "substrate"
   are fine. The bug is an *unmarked* homonym — a reader binding the wrong sense
   because nothing told them which question was being asked.

---

## High-risk homonyms (same word, different questions)

These are the words where a reader can silently bind the wrong sense. Each is
load-bearing in more than one sense; none of the senses is wrong.

### substrate — **three** distinct questions

| Sense | Question it answers | Canonical source |
|---|---|---|
| `substrate (inference)` | What inference engine generates this behavior? (GPT-5.5, Opus, Qwen, Ollama) | `harness-substrate-plurality.md` — "Model / substrate \| What inference substrate generates behavior?" |
| `substrate (deployment / identity layer)` | What persistent hardware/disk/DB/config survives a process restart and can *earn* continuity? (Lumen's Pi) | `identity.md` five-layer table — "Substrate \| persistent hardware, disk, DB, configuration"; Appendix "Substrate-Earned Identity" |
| `substrate (runtime / scheduler)` | What execution model runs the work — per-process scheduling + protocol-level checkout vs. a shared asyncio loop? | `CLAUDE.md` / `AGENTS.md` "Substrate Tax" — "does not exist on substrates with per-process scheduling and protocol-level connection checkout (e.g., BEAM / db_connection)" |

These three are orthogonal axes. An agent can switch `substrate (inference)`
(swap model) while holding `substrate (deployment)` constant (same Pi), running
on a `substrate (runtime)` that has no anyio coupling (BEAM). Always qualify
which axis you mean in cross-doc prose.

### fingerprint — **three** distinct questions

| Sense | Question it answers | Canonical source |
|---|---|---|
| `fingerprint (transport)` | Which interactive driver is this, by ip:ua, for the weak sticky-resume pin? | `identity.md` — "a short-TTL Redis pin keyed on the transport fingerprint (`recent_onboard:<ip:ua>`)" |
| `fingerprint (behavioral / lineage)` | Does this process-instance's EISV behavior match the lineage it claims? | `identity.md` — "behavior diverges from lineage fingerprint"; R1 verify-lineage-claim |
| `fingerprint (finding-dedup)` | Is this CI/Watcher finding the same issue we already saw? (sha256 hash) | `CLAUDE.md` Watcher section — `--resolve <fingerprint>`; `docs/operations/ci-issue-surfacing.md` |

The first two are identity machinery and live in the same document; the third is
operational tooling. The transport vs. behavioral split is the dangerous one
because both appear in `identity.md`: one is a *weak* heuristic proof, the other
is the *strongest earned* layer. Never let "fingerprint" stand unqualified there.

### surface — two distinct questions

| Sense | Question it answers | Canonical source |
|---|---|---|
| `surface (lease)` | What shared mutation target is being claimed under single-writer coordination? (`repo_path`, `td_network`, a branch, a Discord locus) | `beam-coordination-kernel.md` — "A surface is any shared mutation target" |
| `surface (tool)` | What actions are available to the agent right now? | `harness-substrate-plurality.md` — "Tool surface \| What actions are available?" |

Note the **near-collision with a third usage**: "single-writer surface" in
`CLAUDE.md`/`AGENTS.md` ("Before Starting Work on a Single-Writer Surface") is
`surface (lease)` applied to source control — the same sense, just not yet
mediated by the lease plane. That continuity is intentional; it's why the lease
plane's first surface class is `repo_path`.

### harness — one question, an under-enumerated answer set

| Sense | Question it answers | Canonical source |
|---|---|---|
| `harness (agent body)` | What body/interface mediates this agent's action? | `harness-substrate-plurality.md` — "Harness \| What body/interface mediates action?" |
| `harness (lifecycle wrapper)` — *informal* | What plugin/hook chain wraps the Claude/Codex session? | `CLAUDE.md` — "Claude Code runs through a plugin-style harness" |
| `harness (test rig)` — *informal* | What scaffold drives a bounded eval? | `scripts/dev/calibration_harness/`, the `deep-research` skill |

The agent-body sense is the load-bearing ontology term. The other two are
colloquial and rarely collide in practice, but flag them when prose mixes
registers. **The real issue is not a homonym — it's that the answer set for
`harness (agent body)` is incomplete.** See the open gap below.

**Preferred term:** `body` is the precise synonym for the agent-body sense and is
already in use (`harness-substrate-plurality.md`: "powerful fixed bodies",
"variable body"). The field's "harness" descends from *test harness* — a scaffold
that drives code from outside — so it natively leans toward the scaffold/wrapper
sense (rows 2–3), which pulls against the *inhabited body* sense (row 1). Prefer
`body` when you mean what mediates an agent's action; reserve `harness` for the
scaffold/lifecycle-wrapper sense. This assigns the two words we already use to
the two questions they each answer best, rather than coining anything new.

### fork — two questions the bare word conflates

| Sense | Question it answers | Canonical source |
|---|---|---|
| `fork (sibling locus)` | Is this a fresh process-instance under the *same* registry UUID, no child minted? | `r6-episode-fork-response-shape.md` — `episode_fork_kind = sibling_locus` |
| `fork (identity lineage)` | Is this a *distinct child UUID* with declared `parent_agent_id` + `spawn_reason`? | `r6-episode-fork-response-shape.md` — `identity_lineage_fork` boolean |

R6 already disambiguates via the `episode_fork_kind` enum and the
`identity_lineage_fork` boolean precisely because a bare `is_fork` is "too
compressed." Never ship a field or sentence with unqualified "fork."

### continuity — concept vs. mechanism

| Sense | Question it answers | Canonical source |
|---|---|---|
| `continuity (layered concept)` | At which layers does this identity claim actually hold? | `identity.md` — the five-layer taxonomy |
| `continuity_token (mechanism)` | *Advanced:* can this same live process re-bind by signed proof? | `identity.md` — "an advanced same-live-process rebind proof"; largely retired for cross-process use |

The token is a deprecated implementation that borrowed the concept's name. When
you write "continuity," mean the layered concept unless you write the full
`continuity_token`.

### lineage — ancestry vs. conversation thread (same doc, both senses)

| Sense | Question it answers | Canonical source |
|---|---|---|
| `lineage (causal ancestry)` | Which finished predecessor's work does this process inherit? (`parent_agent_id`) | `identity.md` — "inherits work from, not is identical to" |
| `thread lineage (conversation history)` | Which logical conversation/history thread is this? (`thread_id`) | `harness-substrate-plurality.md` — "`thread_id` names logical conversation/history lineage" |

Both appear in the provenance envelope; keep `thread_id` out of any sentence
about causal ancestry.

### basin — attractor vs. health band (**confirmed at code level, 2026-06-20**)

| Sense | Question it answers | Canonical source |
|---|---|---|
| `basin (attractor)` | Which equilibrium will the system's own EISV *dynamics* flow to? | `governance_core/dynamics.py::check_basin` — bistable `I>0.5` high/low/boundary; the parallel ODE (research lens) |
| `basin (health band)` | Is each EISV axis inside its *configured* healthy box, so self-relative deviation should count as risk? | `src/behavioral_assessment.py::_basin_health_gate` (#689), `config.governance_config.BASIN_HIGH` — the **verdict-driving** gate |

This is the dangerous kind: only the second sense drives verdicts, and it is a
*static configured box* (`BASIN_E_HEALTHY=0.60`, …), **not** an attractor derived
from dynamics — even though it borrows the attractor word. The real attractor
basin exists in the ODE but does not gate verdicts. A runtime glossary
(`src/governance_glossary.py::explain_basin`, #428) resolves the health-band sense
for users; keep it consistent with this entry.

### free energy — ODE accumulator vs. variational −F (code level)

| Sense | Question it answers | Canonical source |
|---|---|---|
| `free energy (ODE V)` | What is the running E−I imbalance accumulator? | `governance_core/dynamics.py` — `V` is "like Helmholtz free energy", a signed integrator |
| `free energy (variational −F)` | What is `E` as negative variational free energy under a generative model? | `src/grounding/free_energy.py` — Tier-1 FEP, **explicitly stubbed** (`NotImplementedError`, "Phase 2") |

The first ships and drives nothing on its own; the second is the *target* `E`
semantics and is honestly not-yet-implemented. Don't let "free energy" stand
unqualified across the two.

---

### proof of life — self-attested vs. externally-observed

| Sense | Question it answers | Canonical source |
|---|---|---|
| `proof of life (self-attested)` | How does a holder prove *itself* still alive? (it renews a heartbeat/check-in; liveness = `expires_at > now()`) | `beam-coordination-kernel.md` — lease heartbeat |
| `proof of life (externally-observed)` | How does a *supervising* process attest the subject died, without the subject's cooperation? (the owner watches the process and reports its end) | `beam-coordination-kernel.md` — OTP monitors/supervision; live today in `dispatch_beam` holder leases (`Process.monitor` → `:DOWN` release) |

The first is a *claim the subject makes*; the second is a *fact something watching it reports* — different questions, so binding the wrong one is the bug. The recurring false-archival incidents are self-attested-only liveness read as death (absence of a heartbeat is not observed death), and it is weakest exactly at silent-hang, where a heartbeat can outlive the work. Where an owning monitor exists (a BEAM orchestrator holding the agent's OS process), the observed sense is authoritative and the self-attested one is the fallback for agents with no supervisor. Sourcing the *archival gate* from the observed sense is proposed, not yet canonical (KG `2026-06-21T16:20:42`, "liveness should be monitor-delegated, not self-reported").

---

## Single-sense load-bearing terms

These currently answer one question each. Listed so a future split is visible
against a baseline.

| Term | Question it answers | Canonical source |
|---|---|---|
| `process-instance` | Which live subject is speaking right now? | `identity.md`, `harness-substrate-plurality.md` |
| `EISV` | What proprioceptive state vector says how this agent is running right now? | `eisv-proprioception-contract.md`; runtime `primary_eisv` / `behavioral_eisv` / `ode_eisv` fields |
| `outcome label` | What external evidence/rubric classified a result as task-negative, contract/process violation, authority/harm, synthetic fixture, or unknown? | `eisv-proprioception-contract.md`; `audit.outcome_events.is_bad` is the compact storage label, not a moral verdict |
| `registry` (identity layer) | Which governance record is this claim bound to? (the UUID) | `harness-substrate-plurality.md`, `identity.md` |
| `transport` | Through what channel does the process act? (CLI, MCP-http, Discord, cron) | `harness-substrate-plurality.md` |
| `lease` | What time-bounded claim to a surface is live, with what proof obligation and expiry? | `beam-coordination-kernel.md` |
| `handoff` | How is custody of a lease (or of work) transferred without TTL expiry or ghost claims? | `beam-coordination-kernel.md`, `identity.md` |
| `locus` | What situated coordinate is this? (guild_id, channel_id, tab_id, profile) | `harness-substrate-plurality.md` |
| `episode` | What bounded local interaction span is this? (one thread, one CLI conversation) | `harness-substrate-plurality.md` |
| `affordance_state` | What reach/permissions/capability does the agent actually have at event time? | `harness-substrate-plurality.md` |
| `assurance` (`identity_assurance`) | How strongly is this identity claim grounded? (tier + source) | `harness-substrate-plurality.md`, `identity.md` |
| `governance_mode` | Under what authority context was this write made? (explicit / ambient / gated / lifecycle / posthoc) | `harness-substrate-plurality.md` |
| `typed absence` | *What kind* of absence is this? (`not_found` / `pending` / `expired` / `stale` / …) — never a bare null | `beam-coordination-kernel.md` |
| `provenance envelope` | What situated facts surrounded this single governance write? | `harness-substrate-plurality.md` (s22 write_context) |

---

## Open gaps (terms we're already pointing at but haven't coined)

These are places where the vocabulary lags the thing. Tracked here so the gap is
explicit, not silently papered over by reusing a near-term.

- **BEAM-resident agent has no `harness (agent body)` value.** The lease enum
  is `hermes / claude_code / codex / dispatch / lumen` — there is no value for
  an agent whose body *is* a BEAM/OTP process (Sentinel Wave 1, Wave 3 handler
  dispatch). The BEAM coordination kernel itself is **not** a harness — it is a
  coordination substrate, a peer of UNITARES governance (it explicitly
  disclaims the role: "Do not replace Hermes as the agent harness"). But an
  agent *resident on* BEAM needs a harness value the taxonomy hasn't minted.
  This is the distinction that prompted this glossary. See
  `harness-substrate-plurality.md` layer table and `beam-coordination-kernel.md`
  non-goals.
  - *Sub-distinction (orchestrated agents).* When a BEAM process *spawns* a
    non-BEAM agent (`dispatch_beam` → a `claude`/`codex` OS process) there are
    **two** bodies: the **holder body** — the supervised BEAM process that owns
    the agent's OS process and observes its `:DOWN` — and the **agent body** —
    the spawned CLI the agent reasons in. The `harness (agent body)` value names
    only the latter; the holder body has no term. They must not be conflated:
    the load-bearing rule is *the PID models the holder, not the agent's
    governance self* — the orchestrator gives the holder honest lifecycle but
    must not forge the agent's identity (`dispatch_beam` PLAN, "ontology
    payoff" + identity-honesty caveat).
- **`affordance_state` shape is uncoined.** Boolean reach? Capability list?
  Permission diff? Named as the most new-territory field in the provenance
  envelope; the *term* exists but the *type* does not.
- **`episode` nesting is undecided.** Clean for Discord threads (one thread =
  one episode); ambiguous for a long shell session with many invocations. The
  question is answered; the granularity is not.

---

## Registers (the second axis)

The vocabulary feels unbounded because it spans several **registers** — parallel
ways of describing the *same* underlying referents, each existing for a different
reason. The term count is not really growing without bound; a roughly fixed set
of referents is being named in four registers. Tagging a term's register is the
orthogonal second axis to "question answered."

| Register | What it is *for* | Source of truth |
|---|---|---|
| **philosophical** (grounding) | What counts as real / earned vs. performative | `identity.md` — three stances, layered taxonomy |
| **fep** (modeling) | Why the math is justified — borrowed from the Free Energy Principle / active inference | external (Friston); adopt only when *earned* |
| **ops** (mechanism) | How it's implemented | handlers, lease plane, EISV code |
| **standard** (public) | How an outside AI/ML engineer would name the referent — the register for public copy, dashboards, pitch, onboarding intros | external observability / MLOps conventions (drift, telemetry, health signals, guardrails). Pulled from `ops`, **never** from `fep` |
| **manifesto** (norm) | What must *not* be built | Synthetic Life Axioms (`identity.md` cites them) |

**Public-copy rule.** Any public-facing surface (pitch, dashboard labels,
README first paragraph, onboarding intros) names a referent from the `standard`
column. It must not borrow from `fep` — a physics term on a public surface
claims rigor the deployed system does not drive behavior with (see the
code-grounded correction below). `ops` symbols (`EISV`, `basin`) stay the
internal and paper vocabulary; `standard` is their translation, not a rename.

**Guardrail — "name nothing more rigorous than it is."** This is the sibling of
the manifesto axiom *"build nothing that appears more alive than it is."* A `fep`
term (`free energy`, `Markov blanket`, `active inference`) is seductive because
it *sounds* earned — it makes the system sound like it is doing variational math
it may not be doing. A physics term enters the `ops` or `philosophical` registers
only when the mapping is **earned** (the math actually holds), not as decoration.
This is the same earned-vs-performative gate `identity.md` applies to identity,
applied here to borrowed rigor.

## Cross-register map (Rosetta — skeleton)

One row per **referent**; columns are its name in each register. This bounds the
"unbounded nomenclature" into a finite table that grows by *rows* (new referents),
not by uncontrolled vocabulary. Status marks the `fep` column specifically:

- **✓ earned** — mapping holds and *drives behavior* (verdict path) today.
- **◐ research-lens** — implemented as real math in the **parallel ODE / grounding tiers**, but **not wired to the verdict path**; honest, not decorative, but not yet load-bearing. (Added 2026-06-20 after reading the code; see correction below.)
- **~ candidate** — plausible conceptual fit, *not yet* implemented anywhere; decorative if shipped as fact.
- **✗ false friend** — looks like a synonym across registers but is a different referent; do not conflate.

| Referent | philosophical | ops | standard (AI/ML) | fep | manifesto |
|---|---|---|---|---|---|
| Agent identity | layered continuity bundle | `uuid` + `client_session_id` | session / agent ID, trace ID | — | must be earned, not performative |
| Internal state | behavioral trajectory | `EISV` state vector | **agent health signals** / state telemetry | ◐ ODE state (`governance_core/dynamics.py`); target `E=−F` tiered+stubbed in `grounding/free_energy.py` | — |
| Healthy operating region | "stable self" | `basin` (+ health gating) | **operating regime** (healthy / degraded) / health band | ◐ attractor — **real** in `check_basin` (bistable ODE), but verdict path uses a configured `BASIN_HIGH` box, not the attractor | — |
| Deviation signal | — | `running hot`, basin-edge crossing | **drift** / anomaly signal | ◐ `running hot` (V-sign) ships; `surprise`/prediction-error as `−log P` does not | — |
| Agent↔world boundary | process-instance boundary | `lease surface`, `affordance_state` | resource scope / permissions boundary | ✗ Markov blanket (**false friend** — statistical conditional-independence boundary, *not* a coordination claim) | — |
| Custody transfer | lineage / inheritance | `handoff` | handoff / ownership transfer *(already standard)* | — | — |
| Proof of life | process-instance liveness (self-attested vs. observed) | `heartbeat` (self-attested) · monitor `:DOWN` (externally-observed) | heartbeat / liveness check *(already standard)* | ~ non-equilibrium steady state / self-maintenance | "build nothing more alive than it is" |
| Belief revision | dialectic resolution | `dialectic` | **escalation / peer review** | ~ active inference / belief updating | — |
| Confidence grounding | earned vs. performative | `identity_assurance` tier, calibration | calibration / confidence score *(already standard)* | ~ precision-weighting | — |

**Reading the `standard` column.** Four of the nine referents already *are* the
standard term (`drift`, `heartbeat`, `handoff`, `calibration`) — the
exotic-sounding load concentrates in three: `EISV`, `basin`, and `dialectic`
(plus `valence`/`running hot`). Translating just those three on public surfaces
removes most of the "too much jargon" friction without touching the internal or
paper vocabulary. The `standard` column carries no `fep`-style status markers
(✓/◐/~/✗) — those grade the physics mapping's rigor; the `standard` column is a
naming convenience, not a rigor claim.

**Correction (code-grounded, 2026-06-20).** The skeleton's first claim — "physics
register is aspirational, all `~`, zero earned" — **was wrong, and reading the
code corrected it.** Three rows are `◐ research-lens`, not `~`:

- `governance_core/dynamics.py` is a **real thermodynamic ODE** (full `dE/dt…dV/dt`,
  RK4, coherence feedback, soft barriers, `compute_equilibrium`, contraction-theory
  convergence, and `check_basin` — a genuine bistable basin of attraction).
- `src/grounding/free_energy.py` already implements the **"name nothing more
  rigorous than it is" guardrail in code**: a 3-tier estimator where Tier-1 FEP is
  explicitly `raise NotImplementedError("…Phase 2 scope")`, Tier-2 resource ships,
  and every value carries a `source=` provenance tag so a heuristic is never
  laundered as a measurement. The guardrail predates this glossary and runs at
  runtime.
- `docs/EISV_COMPUTATION.md` already states the deployed-vs-target split this table
  was re-deriving: deployed EISV = "auditable heuristic blends, EMA-smoothed";
  target = `E`as`−F`, `I`as mutual information, `S`as entropy. **The ODE "runs in
  parallel and does NOT drive verdicts."**

So the accurate finding is sharper than "aspirational": **the physics is built but
not wired.** Your system is two loops — a heuristic verdict path (EMA + z-score +
`BASIN_HIGH` health gate) that drives decisions, and a parallel thermodynamic ODE
that's a research lens. Promoting a `◐` to `✓` does not mean *building* the math;
it means **wiring the existing ODE/grounding forms onto the verdict path** — a real
control-loop change to trajectory/telemetry, gated tier-by-tier with the provenance
honesty already in place. That is the single most important thing this whole
glossary exercise surfaced.

The register model still holds for these nine rows (four grading registers plus
the `standard` public-naming column); if a referent won't fit a
column, that absence is a finding (e.g. most rows have no genuine `manifesto` name
— the norm register governs a few load-bearing referents, not all of them), not a
gap to backfill.

### FEP promotion conditions (roadmap)

Each `~ candidate` from the table above, with the **falsifiable condition** it
must meet before its `fep` cell is promoted to `✓ earned`. The discipline: if you
can't state a test that would *fail*, the mapping is decoration, not rigor. Listed
most-reachable first.

| Candidate mapping | Earned when (falsifiable test) | Reachability |
|---|---|---|
| assurance/calibration → **precision-weighting** | An observation's effect on state/calibration is scaled by its assurance as an explicit inverse-variance **precision** term — low-assurance writes are down-weighted *proportionally*, not by category. Test: the update weight is a continuous function of assurance, derivable as precision, not an `if tier == weak` branch. (`identity.md` already gestures at this: "a gated pre-check should not weight calibration the same way an explicit check-in does.") | **High** — closest to earnable; the weighting intent already exists, it just needs to be Bayesian rather than categorical. |
| basin → **attractor / characteristic-state set** | *Already built (`◐`)* — the attractor exists in `check_basin` (bistable ODE). Earned (`✓`) when the **verdict path's** basin gate is the ODE attractor, not the static `BASIN_HIGH` box in `behavioral_assessment.py`. Test: the gate edge moves with the dynamics; replacing the config box with the ODE separatrix changes no test that depends on a *real* edge. | **High** — not "build the math" but "wire ODE→verdict path"; the math is done. |
| EISV → **generative-model belief state** | *Partly built (`◐`)* — the ODE evolves EISV; what's missing is **uncertainty**: EISV is a point estimate. Earned when EISV carries explicit precision (a distribution) and updates approximate free-energy minimization, not EMA smoothing. Test: the EISV update equals/approximates FE-minimization under a stated generative model. | **Medium** — the prerequisite that unlocks three rows; needs EISV distributional (mean + precision). |
| running-hot / basin-edge → **surprise / prediction error** | The hot signal is computed as (an approximation of) `−log P(behavior \| model)` under an explicit generative model — how *unexpected* current behavior is, not variance-from-norm. Test: high surprise provably drives belief updates (couples to the EISV and dialectic rows). | **Medium** — depends on EISV-as-belief-state landing first. |
| dialectic → **active inference / policy selection** | Resolution is formalized as selecting the position/action that minimizes **expected** free energy (future surprise), with an explicit EFE objective. Test: a dialectic verdict is reconstructable from an EFE computation, not an LLM judgment or scoring heuristic. | **Medium-low** — the largest formalization lift. |
| heartbeat → **non-equilibrium steady state / self-maintenance** | Proof-of-life is contingent on the agent *actively maintaining its own boundary/characteristic states* (resisting dissipation), not emitting a TTL ping. Test: liveness fails when self-maintenance work stops, even if the timer fires. | **Decorative — recommend demote.** A TTL heartbeat is an ops timer; "NESS" is almost certainly borrowed rigor here. Drop the `fep` name unless heartbeat is re-grounded in real self-maintenance. |

**On the false friend.** `Markov blanket` stays `✗` for the lease-surface
referent — they are not the same boundary. *If* the system ever wants a genuine
Markov-blanket referent, it is the conditional-independence boundary separating an
agent's internal states from external ones (sensory + active states mediating) —
closer to `affordance_state` than to a coordination claim — and it is earned only
when that statistical independence structure is actually modeled, not asserted.

**Roadmap reading (code-corrected):** the work is mostly *wiring, not building*.
`basin-as-attractor` is a wire-up (ODE exists); `precision-weighting` is the
closest greenfield-ish step (the basin-health gate is already a 0→1 ramp, which is
precision-shaped); the EISV-distributional change is the prerequisite that unlocks
surprise + active-inference; `heartbeat→NESS` should be dropped. The single
highest-leverage move is **making EISV distributional** (mean + precision), because
it both unlocks three FEP rows *and* is the precondition for letting the ODE drive
verdicts honestly. That ordering is the actual deliverable — it says which physics
claims to earn
next and which to stop borrowing.

---

## Maintenance

When a sweep finds a new collision, add it to the high-risk table here and log
the sweep as a dated audit alongside `glossary-drift-audit-2026-06-20.md`. Keep
this file keyed by *question answered*; if you find yourself writing a definition
that leads with examples, you are seeding the next drift.
