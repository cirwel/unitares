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

---

## Single-sense load-bearing terms

These currently answer one question each. Listed so a future split is visible
against a baseline.

| Term | Question it answers | Canonical source |
|---|---|---|
| `process-instance` | Which live subject is speaking right now? | `identity.md`, `harness-substrate-plurality.md` |
| `registry` (identity layer) | Which governance record is this claim bound to? (the UUID) | `harness-substrate-plurality.md` |
| `transport` | Through what channel does the process act? (CLI, MCP-http, Discord, cron) | `harness-substrate-plurality.md` |
| `lease` | What time-bounded claim to a surface is live, with what proof obligation and expiry? | `beam-coordination-kernel.md` |
| `handoff` | How is custody of a lease (or of work) transferred without TTL expiry or ghost claims? | `beam-coordination-kernel.md`, `identity.md` |
| `basin` | Is the agent's EISV inside its healthy operating region? | `eisv-basin-health-gating-v0.md` |
| `locus` | What situated coordinate is this? (guild_id, channel_id, tab_id, profile) | `harness-substrate-plurality.md` |
| `episode` | What bounded local interaction span is this? (one thread, one CLI conversation) | `harness-substrate-plurality.md` |
| `affordance_state` | What reach/permissions/capability does the agent actually have at event time? | `harness-substrate-plurality.md` |
| `assurance` (`identity_assurance`) | How strongly is this identity claim grounded? (tier + source) | `harness-substrate-plurality.md`, `identity.md` |
| `governance_mode` | Under what authority context was this write made? (explicit / ambient / gated / lifecycle / posthoc) | `harness-substrate-plurality.md` |
| `typed absence` | *What kind* of absence is this? (`not_found` / `pending` / `expired` / `stale` / …) — never a bare null | `beam-coordination-kernel.md` |
| `proof of life` / `heartbeat` | How does a remote holder prove it is still alive on its lease? | `beam-coordination-kernel.md` |
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
| **manifesto** (norm) | What must *not* be built | Synthetic Life Axioms (`identity.md` cites them) |

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

- **✓ earned** — mapping holds in code/math today.
- **~ candidate** — plausible conceptual fit, *not yet* earned; decorative if shipped as fact.
- **✗ false friend** — looks like a synonym across registers but is a different referent; do not conflate.

| Referent | philosophical | ops | fep | manifesto |
|---|---|---|---|---|
| Agent identity | layered continuity bundle | `uuid` + `client_session_id` | — | must be earned, not performative |
| Internal state | behavioral trajectory | `EISV` state vector | ~ belief / hidden states of a generative model | — |
| Healthy operating region | "stable self" | `basin` (+ health gating) | ~ attractor / characteristic-state set (**strongest candidate**) | — |
| Deviation signal | — | `running hot`, basin-edge crossing | ~ surprise / prediction error / precision spike | — |
| Agent↔world boundary | process-instance boundary | `lease surface`, `affordance_state` | ✗ Markov blanket (**false friend** — statistical conditional-independence boundary, *not* a coordination claim) | — |
| Custody transfer | lineage / inheritance | `handoff` | — | — |
| Proof of life | process-instance liveness | `heartbeat` | ~ non-equilibrium steady state / self-maintenance | "build nothing more alive than it is" |
| Belief revision | dialectic resolution | `dialectic` | ~ active inference / belief updating | — |
| Confidence grounding | earned vs. performative | `identity_assurance` tier, calibration | ~ precision-weighting | — |

**What the skeleton already reveals:** the `fep` column is almost entirely `~`
(candidate) — one false friend, zero `✓` earned. So as of this writing the
**physics register is aspirational, not load-bearing**: it's the register most at
risk of decorative borrowing, which is exactly what the "name nothing more
rigorous than it is" guardrail exists to police. Promote a `fep` cell to `✓` only
when there is real variational machinery behind it, not when the metaphor merely
reads well. The four-register model holds for these nine rows; if a referent
won't fit a column, that absence is a finding (e.g. most rows have no genuine
`manifesto` name — the norm register governs a few load-bearing referents, not
all of them), not a gap to backfill.

---

## Maintenance

When a sweep finds a new collision, add it to the high-risk table here and log
the sweep as a dated audit alongside `glossary-drift-audit-2026-06-20.md`. Keep
this file keyed by *question answered*; if you find yourself writing a definition
that leads with examples, you are seeding the next drift.
