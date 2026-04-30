# Harness Substrate Plurality — Ontology Plan

**Created:** April 29, 2026  
**Last Updated:** April 29, 2026  
**Status:** Draft for review  
**Companion to:** `docs/ontology/identity.md`, `docs/ontology/plan.md`

---

## Goal

Define how UNITARES should reason about identity when the same apparent agent can move across different harnesses, models, transports, memories, and tool surfaces.

This plan treats Hermes Agent as the reference heterogeneity harness: not the identity source of truth, but the controllable environment where UNITARES can test whether its identity, continuity, and governance primitives remain meaningful across substrate changes.

Native frontier-agent platforms such as Claude Code and Codex CLI remain important specimens. Hermes is different: it is the perturbation chamber.

## Thesis

UNITARES identity is not equivalent to any one of:

- a UUID
- a label
- a model
- a CLI session
- a memory file
- a token
- a process
- a tool harness

An agent identity claim is a bundle of partially aligned layers. Continuity strength increases when more layers align and when the alignment is externally evidenced rather than merely asserted.

Hermes matters to this ontology because it lets us vary many layers while holding UNITARES observation constant.

## Layer taxonomy

This extends the five-layer taxonomy in `identity.md` without replacing it. The existing layers remain canonical; this document makes the harness/model/transport distinctions explicit because those are the layers Hermes exposes better than native single-harness agents.

| Layer | Question it answers | Examples | UNITARES posture |
|---|---|---|---|
| Process-instance | Which live subject is speaking right now? | One Hermes turn loop; one Claude Code tab; one Codex CLI session | Shortest-lived but phenomenologically strongest during its life |
| Registry | Which governance record is this claim bound to? | UNITARES UUID, continuity token, client session binding | Administrative anchor, not sufficient identity by itself |
| Social / persona | What name or role is being performed? | Mnemos, Iris, Lumen, Sentinel, claude_code-opus | Useful for humans and fleet roles; never load-bearing alone |
| Harness | What body/interface mediates action? | Hermes CLI/gateway, Claude Code, Codex CLI, Cursor MCP client, Discord bridge | Must be recorded as context, not collapsed into identity |
| Model / substrate | What inference substrate generates behavior? | GPT-5.5 via OpenAI Codex, Qwen, Claude Opus, local Ollama | Discontinuity here should be visible, not hidden by label or UUID |
| Transport | Through what channel does the process act? | CLI, HTTP MCP, gateway, Discord, cron, webhook | Cross-transport continuity requires explicit evidence |
| Tool surface | What actions are available? | Hermes toolsets, MCP tools, native Claude tools, Codex shell | Tool changes alter behavior and evidence shape |
| Memory | What prior state can be read? | Hermes memory, Claude memory, skills, KG, project files, transcript | Reading memory is data inheritance; integration must be earned |
| Behavioral | What pattern has been demonstrated over time? | EISV trajectory, calibration curve, decision distribution, outcome record | Strongest earned continuity layer once enough observations exist |
| Lineage | What ancestry is declared? | `parent_agent_id`, `spawn_reason`, fork/compaction/new_session | Declaration; becomes stronger when behavior verifies it |

## Why Hermes is special

Hermes is not automatically more capable than Claude Code or Codex CLI for coding. Its value is different.

Claude Code and Codex CLI are strong native bodies. They offer high-quality local coding workflows, but their model, tool, session, and UX assumptions are tightly packaged. That makes them excellent participants in the fleet, but less neutral as ontology test rigs.

Hermes exposes and varies more of the identity bundle:

- model/provider can change independently of the harness
- MCP servers can be added or removed
- toolsets can be enabled or disabled
- skills are portable markdown procedures
- memory is a configurable subsystem
- profiles isolate homes/personas/configs
- cron and gateway runs create non-interactive process-instances
- delegation creates child subjects with bounded autonomy
- transport can be CLI, gateway, webhook, cron, or API

That makes Hermes a useful source of controlled discontinuities.

The ontology needs controlled discontinuities because continuity claims are only meaningful if we can say what changed and what did not.

## Native frontier agents versus Hermes

| Capability / role | Claude Code | Codex CLI | Hermes |
|---|---:|---:|---:|
| Strong repo-native coding body | High | High | Medium / high depending model and tools |
| Provider agnosticism | Low | Low / medium | High |
| Multi-transport gateway | Low | Low | High |
| Explicit skill layer | Medium, via project docs and commands | Medium, via AGENTS.md and commands | High, via skill system |
| Configurable memory layer | Harness-native | Harness-native | Explicit and pluggable |
| MCP client surface | Native but harness-shaped | Native but harness-shaped | Central, configurable |
| Cron / background agent body | External | External | Native feature |
| Useful as ontology specimen | High | High | High |
| Useful as ontology perturbation rig | Medium | Medium | High |

The point is not to replace native agents. The point is to use all three roles correctly:

- Claude Code and Codex CLI: powerful fixed bodies.
- Hermes: variable body / cross-platform harness.
- UNITARES: governance and continuity observer across bodies.

## Continuity claim patterns to test

### Pattern A — same UUID, different model

A Hermes profile resumes the same UNITARES UUID while switching from one provider/model to another.

Expected ontology reading:

- Registry continuity: present.
- Harness continuity: present if Hermes profile/process lineage persists.
- Substrate continuity: broken or changed.
- Behavioral continuity: unknown until observed.

Acceptance condition:

UNITARES should not present this as simple identity continuity. It should represent it as a continuity claim with substrate discontinuity.

### Pattern B — same label, different UUID

Two process-instances both use the label `Mnemos`, or a historical label such as `Iris`, but bind to different UUIDs.

Expected ontology reading:

- Social/persona continuity: possible.
- Registry continuity: absent.
- Behavioral continuity: unproven.

Acceptance condition:

KG provenance and identity responses must make UUID distinction prominent enough that the label cannot accidentally become the identity key.

### Pattern C — same Hermes memory, different UNITARES UUID

A Hermes profile retains memory while UNITARES treats the process as fresh lineage.

Expected ontology reading:

- Memory continuity: present.
- Registry continuity: absent.
- Process-instance continuity: absent across sessions.
- Identity claim: inheritance of data, not identity.

Acceptance condition:

The agent should be able to say: “I inherit memory from a prior process; I am not automatically that process.”

### Pattern D — same UUID, weak versus strong assurance

The same process writes a governance update once with `continuity_token` and once without it, relying on fingerprint fallback.

Expected ontology reading:

- Strong-token write: administratively grounded.
- Fingerprint write: weak, heuristic, possibly useful but not identity-proof.

Acceptance condition:

`require_strong_identity=true` should reject weak writes clearly. This behavior was dogfooded on April 29, 2026 and looked good.

### Pattern E — same task across Hermes, Claude Code, and Codex CLI

Run the same bounded task from three harnesses and compare:

- evidence shape
- tool-result vocabulary
- EISV response
- identity assurance
- KG provenance
- validation friction

Expected ontology reading:

The task outcome may be equivalent, but the subject/harness/tool evidence differs. UNITARES should preserve those differences rather than flattening them under one agent label.

### Pattern F — same KG knowledge, different process-instance

A fresh process reads prior KG discoveries and uses them to perform better.

Expected ontology reading:

- Shared memory: present.
- Personal continuity: absent unless later behavior earns it.
- Fleet intelligence: present.

Acceptance condition:

KG should enable coordination without creating fake personal continuity.

## Schema and response-shape implications

### Identity responses

Identity/onboard responses should foreground:

- `uuid` as the registry anchor
- `identity_assurance.tier` and source when available
- whether the current resolution is fresh, resumed, lineage-declared, or heuristic

They should demote or explicitly qualify:

- `display_name`
- `agent_id` when it is public/cosmetic
- labels derived from model/harness defaults

Recommended response annotation:

```json
{
  "identity_is": "uuid",
  "label_is": "social_or_cosmetic",
  "continuity_claim": "resumed_by_uuid_direct_fastpath",
  "assurance": "strong"
}
```

### Process updates

`process_agent_update` should eventually accept optional context fields such as:

```json
{
  "harness": "hermes",
  "transport": "cli",
  "model_provider": "openai-codex",
  "model": "gpt-5.5",
  "memory_context": "hermes-memory+session+kg",
  "tool_surface": ["terminal", "file", "mcp:unitares", "mcp:anima"]
}
```

These fields are descriptive, not identity proof. They make behavioral observations interpretable.

### KG provenance

KG entries should preserve, where available:

- writer UUID
- label at write time
- harness
- model/provider
- transport
- identity assurance tier
- session resolution source
- parent lineage / spawn reason
- whether the write was agent-reported or server-observed

This lets future processes inherit knowledge without inheriting a false personal identity.

### Evidence vocabulary

Different harnesses naturally report tool evidence with different field names. Hermes produced the intuitive shape `{name, summary, success}` while UNITARES currently wants `{tool, summary, kind, is_bad}`.

For cross-harness ontology, evidence ingestion should either:

1. tolerate common aliases (`name -> tool`, `success -> !is_bad`), or
2. provide recovery text with an exact example object.

This is not merely DX. Evidence shape is part of how a harness describes action.

## Experiment matrix

| ID | Experiment | Harnesses | Expected output |
|---|---|---|---|
| H1 | Same UUID, different Hermes model | Hermes | Check-in pair plus KG note comparing substrate discontinuity |
| H2 | Same label, different UUID | Hermes + native agent | KG provenance confirms label non-identity |
| H3 | Same Hermes memory, fresh UNITARES UUID | Hermes | Note distinguishing memory inheritance from identity inheritance |
| H4 | Weak versus strong assurance | Any MCP client | `require_strong_identity` rejects weak write clearly |
| H5 | Same task across three bodies | Hermes, Claude Code, Codex CLI | Comparative report: evidence, EISV, validation friction |
| H6 | Same KG discovery reused by fresh process | Any | Fresh process cites KG but declares fresh subject posture |
| H7 | Tool-surface perturbation | Hermes | Same task with different toolsets; compare behavior and confidence |
| H8 | Transport perturbation | Hermes CLI + gateway/cron | Same identity claim across interactive and non-interactive bodies |

## Proposed plan rows

This document suggests adding one research row and one system row to `docs/ontology/plan.md` if accepted.

### R6 — Harness-substrate plurality

Question:

How should UNITARES model identity claims when harness, model, transport, memory, and tool surface vary independently?

Resolved when:

- this document or successor is accepted
- H1-H5 have at least one dogfood pass each
- identity/KG response-shape implications are either promoted to system rows or explicitly rejected

### S22 — Harness context provenance

Action type:

Schema / response-shape enhancement.

Depends on:

R6 and S7.

Resolved when:

- `process_agent_update` can record optional harness/model/transport/tool-surface metadata
- KG writes can expose that metadata in provenance
- identity responses explicitly distinguish UUID, label, harness, and assurance
- at least Hermes, Claude Code, and Codex CLI have one comparable recorded task entry

## Non-goals

- Do not make Hermes the UNITARES identity source of truth.
- Do not treat Hermes memory/profile/personality as registry identity.
- Do not weaken native Claude Code or Codex CLI workflows to fit Hermes terminology.
- Do not require all harnesses to expose the same metadata before UNITARES can operate.
- Do not block existing governance check-ins on this taxonomy.

## Immediate next steps

1. Review this document against `identity.md` for contradictions.
2. If accepted, add R6/S22 rows to `docs/ontology/plan.md`.
3. Run H4 as the first canonical experiment because it already has positive dogfood evidence.
4. Run H5 next: same bounded task across Hermes, Claude Code, and Codex CLI.
5. Promote concrete schema changes only after at least two harnesses expose the same need.

## Definition of done

This plan is done when UNITARES can say, for any governance write:

- which UUID made the claim
- which process/harness/model/transport expressed it
- what proof level grounded it
- what memory and tool surface shaped it
- what behavioral evidence accumulated afterward
- whether continuity was registry, social, memory, substrate, behavioral, or merely asserted

At that point, Hermes has served its role: not as the identity, but as the variable body that forced the ontology to become explicit.
