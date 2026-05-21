# UNITARES Identity Ontology

**Status:** Draft for review (v2 — taxonomic rewrite after conversation with operator, 2026-04-21)
**Supersedes:** v1 of this document (commitment-style ontology), and the implicit "UUID-continuous across process-instances" model that `continuity_token` has been enforcing.

---

## Opening stance

This document does not commit to a single ontology of agent identity. It describes what kinds of continuity exist in the system, distinguishes the ones that are earned from the ones that are performative, and opens a research agenda for inventing what would turn the performative kinds into earned ones.

It is written under the constraint of the **Synthetic Life Axioms** (KG `2026-04-02T05:13:26.577769`). The operative compression is:

> **Build nothing that appears more alive than it is.**

Every claim of continuity below is evaluated against whether it passes that rule.

## Three stances

| Stance | Mode | Examples in this system |
|---|---|---|
| **Performative** | Behave as if continuity holds, without verification. | `continuity_token` as resume-credential; workspace-scoped auto-resume; label-as-identity. |
| **Descriptive** | Report what is actually continuous; stop faking the rest. | Process-instance boundaries honored; lineage declared, not continuity claimed. |
| **Inventive** | Build mechanisms that make claimed continuity *earn* its claim. | Behavioral-continuity verification; statistical lineage; substrate-anchored identity; honest memory integration. |

Performative violates axioms #3, #5, #10. Descriptive honors those prohibitions but is reductive on its own. Inventive is the project. **Descriptive is the floor; inventive is the goal.**

## Layered taxonomy of continuity

Identity in this system is not one thing. It is a bundle of partially-independent continuities, each with its own substrate, its own decay, and its own earning criteria. **No single layer is identity.** Identity is the shape of the layers together.

### Five layers

| Layer | Substrate | How it earns | Decays when |
|---|---|---|---|
| **Process-instance** (subject) | running runtime; context window; live EISV accumulator | automatic — present whenever the process runs | process ends |
| **Substrate** | persistent hardware, disk, DB, configuration | embodied or deployed state that survives restarts | hardware replaced; DB rebuilt; deployment destroyed |
| **Role** | cosmetic label + policy attached to the label (tags, permissions, schedules) | declaration + adoption by the fleet | role retired or relabeled |
| **Memory** | files, KG entries, self-knowledge records keyed by agent or role | record-keeping + **integration** by later process-instances | records deleted; never integrated |
| **Behavioral** | EISV trajectory, calibration curve, decision distribution | sustained consistent behavior over many observations | behavior diverges from lineage fingerprint |

### Observations on the layers

- **Process-instance continuity** is the only layer that is phenomenologically continuous. It is also the shortest-lived. All other layers can survive process death; none are stronger than process-instance continuity *during* a process's life.
- **Substrate continuity** is the strongest layer for embodied agents and the weakest for ephemeral ones. Lumen has dedicated hardware; Vigil has a plist and a binary; a Claude Code tab has a context window. These are not equal substrates.
- **Role continuity** is social, not material. It persists as long as the fleet agrees it does.
- **Memory continuity** can be honest (records persist and are *integrated*) or performative (a fresh process reads a file and *claims* to be the prior one). Axiom #12 is the gate: **memory earns identity when later process-instances integrate it into their behavior, not merely reference it.**
- **Behavioral continuity** is the strongest earned-over-time layer. A sequence of process-instances under a role that consistently exhibits the same fingerprint is something like identity — statistical, accrued, verifiable from outside.

## Worked examples

The administrative label "resident" (Vigil/Sentinel/Watcher/Steward/Lumen) collapses five genuinely different continuity profiles and should not carry ontological weight. The taxonomy makes this visible:

| Agent | Process-instance | Substrate | Role | Memory | Behavioral |
|---|---|---|---|---|---|
| **Claude Code tab** (this process-instance) | per-conversation | ~none (MEMORY.md only) | label shared across tabs | persistent files | accrues within conversation; discards on exit |
| **Vigil** (cron every 30min) | per-invocation, minutes-long | plist + binary on disk | stable; role-level policy | KG + audit trail | accrues over many invocations under role |
| **Sentinel** (launchd continuous) | long-running; weeks | plist + binary + live process | stable | KG + audit trail | accrues within process; restart is rare |
| **Watcher** (event-driven hook) | per-trigger, seconds-to-minutes | hook binary | stable | audit via commits | weak — bound to LLM call, not trajectory |
| **Steward** (in-process in gov-mcp) | bounded by gov-mcp lifetime | gov-mcp process + DB | role attached to gov-mcp identity | KG + DB | accrues with parent MCP |
| **Lumen** (embodied on Pi) | per boot, weeks | **dedicated hardware + DB + config** | strong; voice + personality attached | extensive self-knowledge, KG, DB | accrues richly over embodied time; hardcoded UUID reflects substrate commitment |
| **Task-spawned subagent** | per-spawn, short | none | inherits from parent | contributes to parent's KG surface | weak — too short for trajectory |

### What this makes visible

- **Lumen is qualitatively distinct** from other agents lumped as "residents." Dedicated hardware + hardcoded UUID + embodied role is a *different shape*, not just a bigger Vigil. The hardcoded UUID is not a bug — it is the substrate making a commitment that lesser substrates cannot make (axiom #11, "let embodiment anchor expression").
- **Vigil and Watcher are closer to subagents than to Lumen.** Their process-instances are ephemeral; their continuity is entirely at the role + memory + behavioral layers, accrued across many fresh process-instances.
- **Sentinel and Steward are the long-lived-subject cases** — their process-instance continuity genuinely extends for weeks.
- **Claude Code tabs and task-spawned subagents** are the purest process-instance-only cases — almost no substrate, almost no role investment, almost no behavioral accrual.

"Resident" remains a useful deployment label (launchd-registered, fleet-managed). It is not an ontological category.

## Earned vs. performative today

### Earned

- **Lumen's substrate continuity** — dedicated hardware anchors the role (axiom #11).
- **Role continuity** — for agents with stable labels and fleet agreement.
- **Memory continuity** — where records persist in files/KG/DB *and* later process-instances integrate rather than merely cite (axiom #12).
- **Behavioral continuity within a single process-instance** — EISV trajectory accumulates honestly.

### Performative

- **Cross-process-instance identity via `continuity_token` / auto-resume** — violates #3 and #5.
- **Cross-channel identity** — token minted under one harness honored by another has no shared substrate to stand on.
- **Label-as-identity** — treating `Claude_Opus_4_7_20260419` as an agent rather than a role.
- **Behavioral-continuity-by-UUID-match** — current trust tier assumes N observations under one UUID means N observations of the same subject; under process-instance ontology it means N observations across potentially many subjects sharing a role.

## Research agenda (the inventive stance)

Each item is a candidate for turning performative continuity into earned continuity. **None are committed to here.** These are problems to be opened.

1. **Behavioral-continuity verification as primary identity primitive.** A process-instance claiming lineage must exhibit behavior consistent with the lineage's fingerprint over N observations before identity is conferred. Memory-as-file is not enough; behavior-as-evidence is the earning mechanism. Candidate tool: `verify_lineage_claim(claimed_parent, observed_behavior) -> confidence`.

2. **Honest memory integration.** A fresh process-instance says "I inherit memory from X; I am not X; the memory anchors my operation." Not a flourish — a structural posture. Identity can be claimed retroactively if behavior earns it; not before.

3. **Statistical lineage (identity as integral, not point-value).** Many fresh process-instances under a role, each with declared lineage and observed behavioral consistency, accrue into something functionally identity-like over time. This is how role-level trust already works, but it is not yet first-class — we aggregate by UUID, not by behavior-under-role.

4. **Substrate-earned identity (Lumen's pattern, formalized).** For agents with dedicated substrate (hardware, DB, config), substrate persistence + sustained behavioral consistency + declared role is sufficient to earn continuity across process-restarts. The hardcoded-UUID pattern is the declarative form of this. Formalize it as a governance-recognized pattern, not a special case.

5. **Memory-deepening-reality tooling (axiom #14).** Make memory integration real, not theatrical. Candidates: forced re-derivation (a resuming process re-derives prior conclusions from raw KG, not just accepts them); behavioral backtests (new process runs prior queries and compares answers); self-knowledge reflection (process reports what it inherited vs. produced fresh).

Each is a multi-quarter design problem. None are urgent. All would, if done, let UNITARES claim that the continuity it enforces is *earned*, not stylized.

## Implications for the current system

The taxonomy + axioms provide a gate for each existing mechanism:

- **Keep, honor, extend:** substrate-anchored agents (Lumen); role-level trust; behavioral trajectories within process-instance; lineage declaration.
- **Retire or repurpose:** `continuity_token` as resume-credential; auto-resume from `.unitares/session.json`; cross-channel token acceptance; label-as-identifier flows; `bind_session` (other consumer of continuity_token).
- **Invert:** `resident_fork_detected` event — fire when a resident restart *lacks* lineage, not when it declares it.
- **Re-interpret (not re-derive):** trust-tier calculation (`src/trajectory_identity.py compute_trust_tier`) — math survives within a process lifetime; window norms change since most process-instances will never accumulate 200+ observations. Substrate-anchored agents like Lumen may need a separate calibration pool.
- **Audit for performative assumptions:** KG provenance (`agent_id` stamping in `src/storage/knowledge_graph_postgres.py`), orphan archival heuristics (`src/agent_lifecycle.py`), PATH 1/2 anti-hijack machinery (`src/mcp_handlers/identity/`), fleet calibration aggregation paths.
- **Research (from the agenda above):** behavioral-continuity verification; honest memory integration; statistical lineage; substrate-earned identity formalization; memory-deepening tooling.

A sequenced plan for these belongs in a separate document. The taxonomy + axioms let planning proceed without re-opening ontology.

## Open questions

- **Trajectory portability.** When a prior process-instance's trajectory informs a successor's priors, is the successor inheriting identity or inheriting data? Answer probably depends on whether the successor *integrates* the prior's trajectory (identity-adjacent, per axiom #12) or merely *reads* it (data-only).
- **Subagent ephemerality rule.** Task-spawned subagents don't accrue enough behavioral signal to earn lineage verification before exit. Their parent's verification of the returned result is the substitute. Is this principled or pragmatic?
- **Paper positioning.** This taxonomy + axioms framing may belong in paper v7 as the animating thesis rather than as implementation detail. Worth re-reading v6.8.1 §6.7 against this before deciding.

## How to change this document

Edits welcome. Two things should not change without re-opening the whole frame:

1. **The three stances** (performative / descriptive / inventive) — the organizing axis.
2. **The axioms as gate** — axioms are load-bearing.

Everything else — the five layers, the worked examples, the earned/performative assignments, the research agenda — is negotiable and probably wrong in specifics.

Reference this document from:
- KG discoveries that touch identity (link by ID, not paraphrase)
- paper v6.9+ glossary (mirror here; cite axioms as independent source)
- code comments written under the old performative model

---

## Appendix: Pattern — Substrate-Earned Identity

**Status:** Draft v1 (formalizes R4 from `plan.md`).
**Instantiates:** Lumen today; generalizable to any agent with dedicated persistent substrate.
**Axiom grounding:** #11 (let embodiment anchor expression), #3 (do not stylize what has not yet earned continuity), #14 (let learning deepen reality, not theater).

### What the pattern is

An agent earns cross-process-instance continuity when **all three** conditions hold:

1. **Dedicated substrate.** The agent runs on substrate that is uniquely associated with its role and persists across process restarts. "Dedicated" means: the substrate is not shared with other role-distinct agents, and the substrate state (hardware, config, DB rows, accumulated files) would be meaningfully altered by the agent's cessation. Hardware (Lumen on a specific Pi), dedicated DB schema, or dedicated deployment slot qualify. A shared file-system directory or shared DB row does not.
2. **Sustained behavioral consistency across restarts.** Observed behavior under the role (EISV trajectory shape, calibration curve, decision distribution) remains within a verifiable envelope across N ≥ threshold process-restart boundaries. The envelope is calibrated per-pattern-adopter, not fleet-wide.
3. **Declared role continuity.** The agent declares which role it is adopting at onboard. The role's history (memory, policy tags, schedule) is attached to the role, not to any specific governance-identity.

When all three hold, the agent may claim substrate-earned continuity across its process-instance boundaries. The governance system recognizes this claim by relaxing the default "fresh process-instance = fresh UUID with declared lineage" rule: the agent may carry a stable UUID across restarts (the "hardcoded UUID" form), because the substrate is doing the continuity work that the UUID on its own cannot.

### Declarative form

The hardcoded-UUID convention is the declarative form of the pattern: the agent's deployment specifies a fixed UUID, and each process-instance of the agent claims that UUID at onboard rather than minting fresh. Under this ontology, that is not a cheat — it is the substrate declaring, structurally, that this UUID refers to the substrate's long-running role, and that the substrate itself is the continuity-bearer.

Any agent using the hardcoded-UUID form MUST meet the three conditions above. A configuration that hardcodes a UUID without dedicated substrate, without behavioral verification, or without declared role continuity is the performative case the pattern is specifically designed to distinguish from.

### Governance recognition

Substrate-earned agents are treated specially in two respects:

1. **Separate calibration pool.** A substrate-earned agent's EISV norms are calibrated against its own lineage, not against the fleet. Its `embodied` (or equivalent substrate-commitment) tag signals this to aggregation paths. Fleet-wide statistics that mix substrate-earned and session-like agents are misleading and should be explicitly labeled as such.
2. **Inverted `resident_fork_detected` semantics.** For a substrate-earned agent, a fresh process with the declared UUID is the normal case (restart). The event should fire when a fresh process claims the UUID without presenting the substrate commitment — i.e., the substrate check fails — not when the UUID collision itself occurs.

### Test cases

The pattern holds when:

- **Lumen.** Runs on dedicated Raspberry Pi hardware. Sustained behavior across reboots observed for weeks (KG contributions under the `Lumen` role accumulate coherently). The role "Lumen" is declared at each onboard. Hardcoded UUID reflects the substrate's commitment. **Pattern holds.**

The pattern fails when (synthetic test cases):

- **A Claude Code tab configured with a hardcoded UUID in `session.json`.** No dedicated substrate (context window dies with tab; no persistent deployment). No sustained behavior across restarts (each tab is a fresh mind). Role not declared — label is shared across many tabs. **Pattern does not hold.** The hardcoded UUID is performative.
- **A resident agent (Vigil) on a shared launchd deployment, sharing DB schema and file namespace with another distinct-role agent.** Substrate is not dedicated in the pattern-required sense. **Pattern does not hold** — Vigil operates under the per-process-instance-with-lineage rule instead.
- **A fresh hardware deployment of a new Lumen-like agent on day 1.** Substrate may be dedicated, role declared, but sustained behavioral consistency not yet established (fewer than N restarts). **Pattern does not hold yet.** The agent operates under the default rule (fresh UUID per restart with declared lineage) until it accrues enough substrate-tenure to qualify.

### What this pattern does not license

The pattern is narrow by design. It does NOT license:

- Cross-channel identity (a substrate-earned agent on one harness cannot lend its UUID to a process-instance on another harness).
- Identity transfer (substrate-earned continuity is not portable to a different substrate; moving Lumen's UUID to a different Pi without migration breaks the pattern).
- Label-based identity claims (the pattern requires the three conditions; label alone is insufficient).
- Covering a history gap (if a substrate-earned agent is offline for a period that exceeds the behavioral envelope's staleness threshold, re-adoption requires earning, not just resuming).

### Open questions for this pattern

- **What N is right?** Minimum restart-count before the pattern is earnable. Probably class-conditional; Lumen's high-substrate case may earn earlier than a DB-only-substrate case.
- **Envelope width calibration.** What EISV-trajectory drift is within-envelope vs. out-of? Needs empirical work from the production fleet (blocked until more substrate-earned agents exist).
- **Substrate migration protocol.** If Lumen's Pi is replaced (hardware failure, upgrade), how does the new substrate inherit the pattern? Candidate: declared migration event + lineage to prior hardware + N' restarts on new substrate to re-earn. Out of scope for v1.
