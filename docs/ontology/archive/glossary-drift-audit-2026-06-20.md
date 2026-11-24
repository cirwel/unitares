# Glossary Drift Audit — 2026-06-20

**Method:** Swept `docs/ontology/`, `docs/proposals/`, `docs/operations/`,
`CLAUDE.md`, and `AGENTS.md` for a fixed list of load-bearing terms. For each
term, recorded every distinct *sense* (the question it answers) and which file
pins that sense. A term is flagged **DRIFT** when the same word answers a
different question in different places *without* a marker telling the reader
which sense is meant.

**Outcome:** This audit seeded `docs/ontology/glossary.md` (the durable,
question-keyed glossary). This file is the point-in-time evidence and the
recommended-fix list. Re-run and re-date when the vocabulary moves.

**Addendum (2026-06-20b — code-grounded pass).** The first sweep read only docs.
A follow-up read of the *code* (`governance_core/dynamics.py`,
`src/grounding/free_energy.py`, `src/behavioral_assessment.py`,
`docs/EISV_COMPUTATION.md`) found two collisions the docs-only sweep missed and
one wrong claim to retract:

- **NEW `basin` (DRIFT, confirmed in code):** `governance_core/dynamics.py::check_basin`
  (bistable **attractor** basin, research-lens ODE) vs.
  `src/behavioral_assessment.py::_basin_health_gate` / `config.governance_config.BASIN_HIGH`
  (a static **health band** — the verdict-driving gate). Only the second drives
  decisions, and it is a configured box, not an attractor. Previously listed as
  single-sense; **promoted to high-risk homonym.** A runtime glossary
  (`src/governance_glossary.py::explain_basin`, #428) resolves the health-band
  sense — keep it consistent with the doc glossary.
- **NEW `free energy` (DRIFT, code):** ODE `V` ("like Helmholtz free energy", a
  signed integrator) vs. `src/grounding/free_energy.py` target `E=−F` (variational,
  Tier-1 explicitly stubbed).
- **RETRACTED claim:** the Rosetta skeleton's "physics register is aspirational,
  all candidate, zero earned" was **wrong**. Three rows are `◐ research-lens`
  (implemented in the parallel ODE / grounding tiers, honestly provenance-tagged,
  but not wired to the verdict path). The real gap is *wiring built physics onto
  the verdict path*, not building it. Notably `grounding/free_energy.py` already
  enforces this audit's own "name nothing more rigorous than it is" guardrail at
  runtime (tiered estimator, `source=` tags, FEP stubbed).

Lesson for future sweeps: **read the code, not only the proposal docs.** Drift
between a term's doc sense and its code symbol is invisible to a docs-only pass.

**Headline:** Three words carry the most collision risk — `substrate` (3 senses),
`fingerprint` (3 senses), `surface` (2 senses) — because in each case more than
one sense is genuinely load-bearing and at least two senses live close together
(sometimes in the same document). These are not errors to "fix" by renaming;
they are *unmarked* homonyms to make legible.

---

## Collision summary (highest risk first)

| Term | Senses | Co-located? | Verdict | Recommended fix |
|---|---|---|---|---|
| `substrate` | inference / deployment-identity / runtime-scheduler | Across 3 docs | **DRIFT** | Qualify in cross-doc prose: `substrate (inference\|deployment\|runtime)`. Glossary high-risk entry added. |
| `fingerprint` | transport ip:ua / behavioral-lineage / finding-dedup | First two in **same doc** (`identity.md`) | **DRIFT** | Always qualify in `identity.md`; the weak-proof vs strongest-earned-layer split is dangerous unmarked. |
| `surface` | lease target / tool set | Across 2 docs (+ a CLAUDE.md single-writer usage that is the lease sense) | **DRIFT** | Use `surface (lease)` vs `surface (tool)`; note the CLAUDE.md "single-writer surface" is the lease sense pre-plane. |
| `fork` | sibling-locus / identity-lineage | Same doc (`r6-…`) — already disambiguated by enum | **DRIFT (managed)** | Keep the `episode_fork_kind` enum discipline; never ship bare `is_fork`. Already R6's stated position. |
| `harness` | agent-body / lifecycle-wrapper / test-rig | Across docs; registers rarely collide | **SOFT** | Agent-body is canonical; flag the informal senses only when prose mixes them. *Real* issue is an incomplete answer set (see gaps). |
| `continuity` | layered concept / `continuity_token` mechanism | Same doc | **SOFT** | Write the full `continuity_token` for the mechanism; bare "continuity" = the concept. |
| `lineage` | causal ancestry / conversation-thread | Same doc, already distinct | **SOFT** | Keep `thread_id` out of ancestry sentences. |

---

## Per-term sweep

### substrate — DRIFT (3 senses)

- **inference** — "What inference substrate generates behavior?" —
  `harness-substrate-plurality.md`, layer table ("Model / substrate").
- **deployment / identity layer** — "What persistent hardware/disk/DB/config
  survives restart and can earn continuity?" — `identity.md` five-layer table
  ("Substrate | persistent hardware, disk, DB, configuration") and the
  Substrate-Earned Identity appendix (Lumen's Pi).
- **runtime / scheduler** — "What execution model runs the work?" — `CLAUDE.md`
  / `AGENTS.md` Substrate-Tax section ("substrates with per-process scheduling
  and protocol-level connection checkout (e.g., BEAM / db_connection)").

Three orthogonal axes; an agent can vary one while holding the others. This is
the worst offender because all three are load-bearing and none is wrong.

### fingerprint — DRIFT (3 senses)

- **transport (ip:ua)** — weak sticky-resume pin — `identity.md`
  (`recent_onboard:<ip:ua>`).
- **behavioral / lineage** — EISV match against claimed lineage — `identity.md`
  ("behavior diverges from lineage fingerprint"); R1.
- **finding-dedup (sha256)** — Watcher/CI dedup — `CLAUDE.md` Watcher section
  (`--resolve <fingerprint>`); `docs/operations/ci-issue-surfacing.md`.

The first two co-locate in `identity.md` and sit at opposite ends of the
assurance scale (weakest heuristic vs strongest earned layer). Highest in-doc
hazard.

### surface — DRIFT (2 senses + 1 aligned usage)

- **lease** — shared mutation target — `beam-coordination-kernel.md`.
- **tool** — available actions — `harness-substrate-plurality.md`.
- *aligned:* "single-writer surface" in `CLAUDE.md`/`AGENTS.md` is the lease
  sense applied to source control before the plane mediates it — intentional
  continuity, not drift.

### fork — DRIFT, but managed

- **sibling locus** — same registry UUID, fresh process-instance, no child —
  `r6-episode-fork-response-shape.md` (`episode_fork_kind = sibling_locus`).
- **identity lineage** — distinct child UUID + `parent_agent_id`/`spawn_reason`
  — same doc (`identity_lineage_fork`).

R6 already treats a bare `is_fork` as "too compressed" and splits it. The audit
endorses keeping that discipline; the risk is regression to an unqualified
"fork" in new prose/fields.

### harness — SOFT

- **agent body** (canonical) — "What body/interface mediates action?" —
  `harness-substrate-plurality.md`.
- **lifecycle wrapper** (informal) — "Claude Code runs through a plugin-style
  harness" — `CLAUDE.md`.
- **test rig** (informal) — `scripts/dev/calibration_harness/`, `deep-research`
  skill.

Registers rarely collide. The substantive finding is not the homonym but the
**incomplete answer set** for the agent-body sense (no value for a
BEAM-resident agent) — recorded as an open gap in the glossary.

### continuity — SOFT

- **layered concept** — `identity.md` five layers.
- **`continuity_token` mechanism** — advanced same-live-process rebind, largely
  retired for cross-process use — `identity.md`.

Mechanism borrowed the concept's name. Write the full token name for the
mechanism.

### lineage — SOFT

- **causal ancestry** — `parent_agent_id`; "inherits work from, not is identical
  to" — `identity.md`.
- **conversation-thread** — `thread_id` "names logical conversation/history
  lineage" — `harness-substrate-plurality.md`.

Both feed the provenance envelope; already distinct, keep them apart.

### Single-sense (no drift at audit time)

`process-instance`, `registry` (identity layer), `transport`, `lease`,
`handoff`, `basin`, `locus`, `episode`, `affordance_state`, `assurance`,
`governance_mode`, `typed absence`, `proof of life`/`heartbeat`,
`provenance envelope`. Each currently answers one question. `episode` and
`locus` reappear as qualifiers inside the `fork` classification but do not
change sense there. Listed in the glossary so a future split is visible against
this baseline.

---

## Recommendations

1. **Adopt the question-keyed glossary** (`glossary.md`) as the pointer of
   record; stop re-defining these terms inline in new docs — link instead.
2. **Qualify the three high-risk homonyms in cross-doc prose.** Especially
   `fingerprint` inside `identity.md`, where the weak/strong senses are one
   paragraph apart.
3. **Do not rename to resolve drift.** Qualify. Renaming a live term invents
   vocabulary nobody adopts; qualifying makes the existing collision legible —
   the rule `harness-substrate-plurality.md` already half-follows.
4. **Coin a `harness (agent body)` value for BEAM-resident agents** before
   Wave 3 handler-dispatch lands, so the taxonomy doesn't backfill the gap by
   overloading `dispatch` or `lumen`.
5. **Re-run this sweep** when a new proposal cluster matures (next likely
   sources of new terms: Plexus scope, lease-plane Phase B, the principal/
   operator-delegate track). Date the next audit and append it beside this one.
