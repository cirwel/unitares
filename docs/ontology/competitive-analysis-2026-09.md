# Claims Falsification Audit and Competitive Map (2026-09)

**Status:** Research note (external market audit). Supersedes
[`competitive-analysis-2026-06.md`](competitive-analysis-2026-06.md) as the
project's market map; the June verdicts on MI9 and Auton as paper-positioning
neighbors stand and are re-verified below.
**Date:** 2026-09-16.
**Prompted by:** a Codex threat scan on 2026-09-16 that listed substitutes the
June map omits (cloud agent platforms, identity standards, policy engines,
runtime systems, audit products) and asked for a falsification audit before
more implementation. The scan's conclusion is adopted; its table is not,
because it mixed verified products with names this audit could not find and
graded capability claims as "refuted" when a substitute merely exists.
**Companion:** `competitive-survival-audit-2026-09.md` (opened the same day
as PR #2257) asks the product question this audit does not: whether to
keep, merge, or stop investing. This audit asks only which public claims
survive contact with the substitutes. Read the two together.
**Method:** every public claim was enumerated from six surfaces (the root
README, `PRODUCT_DEFINITION.md`, the public site, the trust contract, the
preprint's title, and the Mercor Expression of Interest draft). For each, the
strongest existing substitute was identified and its primary source opened
from this session where the network policy allowed. Each claim is then typed
(capability, uniqueness, or benefit) and classed (unique, differentiated,
commodity, unsupported). A substitute existing refutes a uniqueness claim; it
does not refute a capability claim, and it says nothing about benefit.
**Scope caveat:** this session's egress policy blocked `docs.aws.amazon.com`,
`learn.microsoft.com`, `arxiv.org`, `zenodo.org`, `spiffe.io`,
`datatracker.ietf.org`, `huggingface.co` (web) and `driftgate.ai` (DNS).
Where a source could not be opened, the row says so and the entry is graded
on what was opened instead (a GitHub-hosted README, a conformant
implementation, or a search snippet). Snippet-only entries are not
byte-verified and must be re-opened before they are cited anywhere else.

---

## TL;DR

1. **The repository makes almost no uniqueness claims, and the two it implies
   should go.** "Federation kernel" and "federation" read as a claim on
   cross-runtime interoperability, which the A2A protocol (v1.0.0, signed
   Agent Cards, task lifecycle, multi-vendor collaboration) now owns. The word
   survives only in the narrow sense `PRODUCT_DEFINITION.md` already gives it,
   and it needs the A2A disambiguation stated once.
2. **Every capability claim is commodity or differentiated; none is unique.**
   Identity, policy enforcement, observability, audit, durable execution and
   lineage each have a cloud-platform or open-source substitute that was
   opened and read. That is not a refutation of any README bullet; each
   bullet describes a capability the system has. It is a refutation of any
   roadmap line that treats those capabilities as moats.
3. **One combination is differentiated and unproven:** claims, disagreement
   with conditions, and outcomes bound to a process identity in a record the
   operator owns. No source opened covers all of it. The nearest open
   substitute is AgentLens plus OpenTelemetry GenAI plus an IETF audit-trail
   export, which together give an operator-owned flight recorder with no
   adjudication and no claim-to-outcome binding. Whether anyone pays for
   adjudication over a flight recorder is unknown; no audit answers it.
4. **The map that was missing is now here:** AWS AgentCore, Microsoft Entra
   Agent ID, Google's SPIFFE-based agent identity and Agent Gateway, A2A,
   NeMo Relay and Agent Toolkit, DriftGate, AgentLens, the IETF Agent Audit
   Trail draft, and Temporal. Two names from the scan, Watchlight and
   BridgetOS, could not be found and are excluded.
5. **Two gaps in the record itself surfaced:** the audit log is not
   hash-chained or exportable in the IETF format, and the record emits into
   no standard trace or audit pipeline except the Relay exporter added on
   2026-09-16. Both are cheap and raise legibility more than any new feature.
6. **The preprint's title overclaims relative to the repository's own
   posture.** "Information-Theoretic Governance" names a grounding the
   2026-04-23 F-hat spike demoted to a neighbor; the v7 title or abstract must
   say so.

---

## Classification used

| Class | Meaning | What it licenses |
|---|---|---|
| **Unique** | No substitute found among sources opened | A positioning claim, provisionally; re-audit on every new market read |
| **Differentiated** | Substitutes exist; a specific, named difference survives comparison | A "differs by" sentence, never a "first" or "only" |
| **Commodity** | A substitute covers it at least as well; the difference is packaging | Keep the capability; make no positioning claim on it |
| **Unsupported** | The claim outruns the repository's own evidence ledger | Rewrite or retire the sentence |

Claim types: **capability** (the system does X), **uniqueness** (only or first
to do X), **benefit** (X improves an outcome). Evidence classes are those of
[`../EVIDENCE_AND_LIMITS.md`](../EVIDENCE_AND_LIMITS.md).

---

## Substitute map

Source status: **opened** = primary page fetched in this session; **impl** =
verified through a conformant implementation's README; **snippet** = search
result only, not byte-verified; **blocked** = the primary domain refused.

| Substitute | Covers (as read) | Lacks (as read) | Source |
|---|---|---|---|
| **NeMo Relay** (NVIDIA, Apache-2.0, 0.8.4) | Multi-language runtime: execution scopes with parent-child lineage, ATOF lifecycle events, middleware that blocks or transforms tool and LLM calls, pre-tool blocking for Claude Code and Codex, OpenTelemetry export | Cross-process identity, durable audit record, outcomes, review, persistence, continuity | opened (README and package source) |
| **NeMo Agent Toolkit** (NVIDIA, Apache-2.0) | Wraps LangChain, LlamaIndex, CrewAI, Semantic Kernel, Google ADK, custom agents; profiling, tracing exporters (LangSmith, Phoenix, Weave, Langfuse, OTel), offline evaluation, MCP and A2A support, third-party plugin API | Identity, lineage across processes, audit record, human review, outcome persistence, runtime policy, pausing, continuity | opened (README) |
| **A2A protocol 1.0.0** (Linux Foundation project) | Discovery via Agent Cards, JWS-signed with RFC 8785 canonicalization; auth delegated to web security schemes; task lifecycle (`SUBMITTED` … `INPUT_REQUIRED`, `AUTH_REQUIRED`); streaming and push; cross-framework delegation over JSON-RPC, gRPC, REST | Durable audit of claims versus outcomes, outcome tracking, adjudication, identity across restarts, behavioral state, pause verdicts (all explicitly out of scope) | opened (specification) |
| **Amazon Bedrock AgentCore** | Runtime, Gateway (APIs to MCP tools), Identity (access management), Memory, Tools, Observability (OpenTelemetry), Evaluations (built-in and custom, on-demand and online), Policy (Cedar) | Self-hosting, cross-cloud portability, outcome tracking tied to agent predictions, adjudication records, behavioral state; whether Policy evaluates every tool invocation and any "Dogwood" component could not be verified | opened (samples README); policy docs blocked |
| **Microsoft Entra Agent ID / Agent 365** | Agent identities with adaptive access, risk detection, lifecycle management; Agent Identity Blueprints as reusable permission templates; human sponsors accountable for lifecycle; governance announced 2026-07 | Nothing verifiable here about behavioral state, claims-to-outcome binding, adjudication, or portability outside Microsoft's cloud | snippet; learn.microsoft.com blocked |
| **Google Cloud agent security** (2026-05-06 post) | SPIFFE-based, cryptographically attested agent identity (GA for Agent Runtime); Agent Gateway enforcing agent-to-agent and agent-to-tool policy; Model Armor runtime protection; organization policies; Agent Security dashboard | Behavioral state, outcome tracking tied to claims, adjudication, self-hosted or cross-cloud use, portable records | opened (blog post) |
| **SPIFFE** (CNCF graduated) | Workload identity: SPIFFE IDs and SVIDs for service-to-service authentication | Agents, behavioral state, audit, review (not its problem) | opened (README); spiffe.io blocked |
| **OpenTelemetry GenAI semantic conventions** | Spans and signals for inference, agents, tool execution, evaluation and MCP; `gen_ai.*` and `mcp.*` attribute registries | Identity across processes, claims versus outcomes, review, policy verdicts, audit semantics beyond traces | opened (repository index only) |
| **Langfuse** (MIT, representative of LangSmith, Phoenix, Weave, Braintrust) | Tracing of LLM calls and agent actions, LLM-as-judge and manual evaluation, prompt management, self-hosting | Cross-process identity, claims versus outcomes, adjudication, pausing | opened (README) |
| **AgentLens** (MIT, 23 stars) | SHA-256 hash-chained append-only audit of LLM calls, tool invocations, approvals and errors; OTel GenAI ingestion without an SDK; MCP server; self-hosted on SQLite or Postgres; EU AI Act Article 12 framing; guardrails with dry-run; multi-tenancy | Identity continuity across restarts, outcome feedback loops, review workflows, policy enforcement or pausing, claim-to-evidence linking | opened (README) |
| **IETF `draft-sharif-agent-audit-trail`** (-03, 2026-09) | JSON audit records with agent identity, action classification, outcome and trust level; SHA-256 hash chain over JCS; optional ECDSA; EU AI Act, SOC 2, ISO 42001 mappings | Linking a prior claim or prediction to a later outcome, adjudication records, parent-child lineage (per a conformant implementation's own "lossy mapping" note) | impl (omega-evidence README); datatracker blocked |
| **IETF `draft-kuehlewind-audit-architecture`** | An architecture for auditing agent delegation and interactions | Not opened; listed for follow-up | snippet; datatracker blocked |
| **Temporal** (durable execution) | Workflows that survive process failure with automatic retry; event history | Agents, behavioral monitoring, claims versus outcomes, adjudication (not its problem) | opened (README) |
| **DriftGate** | Runtime governance control plane: identity, policy, routing, risk and evidence contracts; policy checks on tool calls before execution; SaaS, hybrid, private cloud, on-prem | Unknown; the site did not resolve from here | snippet only |
| **Watchlight, BridgetOS** | Named in the scan | Not found in search; excluded until a source exists | not found |
| **MI9** (Wang et al., Barclays, arXiv:2508.03858) | Runtime safety framework: agency-risk index, agent-semantic telemetry, goal-aware authorization monitoring, finite-state conformance, goal-conditioned drift detection, graded containment; evaluated on 1,000 synthetic scenarios; open-sourced prompts and scripts | Identity ontology, class-conditional calibration, shared memory, adjudication record (June verdict stands) | opened (abstract via Hugging Face papers) |
| **Auton** (Snap, arXiv:2602.23720) | Declarative Cognitive Blueprint separated from a Runtime Engine; formal auditability of the agent artifact; POMDP execution model; constraint manifold for safety | Behavioral state at runtime (June verdict stands: different layer) | opened (abstract via Hugging Face papers) |
| **Multiagent debate** (Du et al., MIT and Google Brain, 2023) | Multiple model instances propose, critique and revise over rounds to converge on an answer | Any durable record of the debate, adjudication with conditions, binding to later outcomes | opened (abstract via Hugging Face papers) |

---

## Claim-by-claim audit

| # | Public claim (surface) | Type | Strongest substitute | Class | Evidence today | Action |
|---|---|---|---|---|---|---|
| 1 | "Accountability infrastructure for long-running AI agents" (README tagline, public site) | capability | AgentLens; AgentCore Observability; DriftGate | Commodity | Operational observation | Keep; no positioning weight |
| 2 | "Its federation kernel connects independent runtimes to one operator-controlled server" (README, public site) | capability with implied uniqueness | A2A 1.0.0 owns cross-runtime interoperability | **Unsupported as read** | — | Rewrite: the runtimes interoperate over A2A or their own transports; UNITARES is the shared record behind them. Define "federation" once, in the narrow sense, with the A2A disambiguation |
| 3 | "Identity and lineage — know which process acted and where inherited work came from" (README) | capability | Entra Agent ID, Google attested identity, AgentCore Identity (credential identity); Relay scopes (execution lineage) | Differentiated | Exercised path | Keep; add "for attribution, not authentication", which `SCOPE_AND_THREAT_MODEL.md` already states |
| 4 | "Claims and evidence — retain important findings, corrections, and their provenance outside any one context window" (README) | capability | Memory services (AgentCore Memory), evaluation datasets (Langfuse), audit logs (AgentLens, IETF AAT record actions, not claims) | Differentiated | Exercised path | Keep |
| 5 | "Governed review — preserve disagreement, conditions, and resolution as part of the work record" (README) | capability | Multiagent debate (a method, no record); approval and input-required states (AgentLens approvals, A2A `INPUT_REQUIRED`); IETF human-in-the-loop drafts (not opened) | **Differentiated, strongest** | Exercised path (81 non-canary verdicts; reviewer-label study) | Keep; this and row 6 are the sentence to lead with |
| 6 | "Outcome grounding — connect predictions and check-ins to what later happened" (README) | capability | IETF AAT `outcome` (action outcome, no prediction binding); evaluation platforms score outputs without binding to prior self-reports | Differentiated | Exercised path; predictive lift: non-detection, inconclusive | Keep the capability wording; never let "grounding" read as validated prediction |
| 7 | "Runtime policy — return an action, reason, and next step at meaningful checkpoints" (README) | capability | Relay middleware, AgentCore Policy (Cedar), Google Agent Gateway and Model Armor, DriftGate | Commodity as enforcement; differentiated only in the contestability fields (reason, next step, review path) | Pause actuation: event reconciled, protection untested | Keep; state that enforcement is delegated to runtimes (the 2026-09-16 Relay gate is the pattern) |
| 8 | "Reconstruction — give a successor the records needed to understand and continue earlier work" (README) | capability | Temporal event history (execution state); Relay ATIF trajectories; AgentCore Memory | Commodity for execution state; differentiated for records of claims, review and outcomes | Untested versus git plus handoff (already stated) | Keep with the existing caveat |
| 9 | "an operator-owned accountability layer across coding agents, research agents, residents, and custom runtimes" (README) | positioning | AgentLens (self-hosted, MIT) plus OTel GenAI plus an AAT export | Differentiated | — | Sharpen to "self-hosted and vendor-neutral"; name the open substitute honestly |
| 10 | "self-hosted federation kernel for agent identity, claims and evidence, review, outcomes, and reconstruction" (`PRODUCT_DEFINITION.md`) | combination | None opened covers all five | Differentiated combination | Benefit untested | Keep the five nouns; fix "federation" per row 2 |
| 11 | "The deployed policy path uses auditable behavioral state estimation" (public site) | capability | MI9 (conformance, risk index, drift); DriftGate drift | Differentiated technically (continuous estimator versus discrete conformance) | Predictive validity: research claim, not guarantee (trust contract §4) | Keep with the caveat that already follows it |
| 12 | "Many agents, one record" (public site tagline) | positioning | Any multi-agent observability product | Commodity | — | Keep; it is a description, not a claim |
| 13 | "runs beside evals, guardrails, and sandboxes and replaces none of them" (public site) | scope | — | honest | — | Keep |
| 14 | "Every value UNITARES emits carries provenance" (trust contract §1) | system guarantee | Not a competitive claim | — | Enforced by lint | Keep |
| 15 | "UNITARES: Information-Theoretic Governance of Heterogeneous Agent Fleets" (preprint title, CITATION.cff) | theory framing | The repository's own `paper-positioning.md`: FEP grounding demoted 2026-04-23, EISV reframed as an engineering instance of interoceptive inference | **Unsupported as titled** | Zenodo blocked; the abstract was not opened here | The v7 title or abstract must state that the information-theoretic formulation is the research target, not the deployed decision path |
| 16 | Mercor EoI: "public claim ledger with evidence classes", "preregistered protocols with stop rules", "named, reproduced blind spots" | process claims about the project | Few open projects publish preregistrations and negative results; none opened here do | Differentiated | Documented | Keep; these describe method, not product |
| 17 | Codex table item "tamper-evident agent audit" | not a UNITARES claim | AgentLens, IETF AAT, hash-chained ledgers | Commodity, and a gap: `audit.events` is not hash-chained | HMAC attestation is not non-repudiation (stated) | Do not claim it; add an AAT-conformant export (see gaps) |
| 18 | Codex table item "durable long-running agents" | not a UNITARES claim as stated | Temporal, AgentCore Runtime | Commodity | — | Keep the README's narrower wording: records survive restarts; execution durability is the runtime's job |

---

## Gaps in the record that the map exposes

1. **No tamper-evident export.** The audit log is queryable and replayable but
   not hash-chained, and it cannot be handed to an auditor in the IETF Agent
   Audit Trail format. An export that maps identity, action, outcome and
   trust level onto that record shape, with the claim and review fields
   carried in `action_detail` as one conformant implementation already does,
   is a small change that makes the record legible to every tool built for
   that draft.
2. **No standard emission.** Until the Relay exporter (2026-09-16) the record
   received events only through its own tools and hooks. An OpenTelemetry
   GenAI exporter would let any Langfuse, Phoenix, or AgentLens user see
   UNITARES identity, verdicts and outcomes beside their traces without
   adopting the server. The Relay plugin already emits into a runtime; this is
   the same move for traces.
3. **Identity vocabulary collides with credential identity.** Every platform
   opened uses "agent identity" to mean a credential (Entra, SPIFFE, AgentCore
   Identity). UNITARES's identity is a process-instance record for attribution.
   The README should say that in the bullet, not only in the threat model.

---

## Recommended rewrites

| Surface | Current | Proposed |
|---|---|---|
| README, opening paragraph | "Its federation kernel connects independent runtimes to one operator-controlled server over MCP or HTTP, where they share a durable record" | "Independent runtimes connect to one operator-controlled server over MCP or HTTP and share a durable record. They interoperate with each other over their own transports or A2A; UNITARES is the record behind them, not the transport between them." |
| README, identity bullet | "know which process acted and where inherited work came from" | "know which process acted and where inherited work came from. A process identity here is a record for attribution, not a credential; platform identities such as Entra Agent ID or SPIFFE-based identity are the credential layer." |
| README, closing sentence | "an operator-owned accountability layer" | "a self-hosted, vendor-neutral accountability layer. Open flight recorders exist (AgentLens); what this adds is adjudication: disagreement, conditions and outcomes bound to the process that made the claim." |
| Public site, "What operators get" | four rows | Add one line under the table naming the delegated pieces: enforcement through runtime middleware (NeMo Relay), traces through OpenTelemetry, interoperability through A2A |
| `PRODUCT_DEFINITION.md` | "self-hosted federation kernel" | Keep, and add the A2A sentence from the README rewrite to the paragraph that defines "federation" |
| Paper v7 title | "Information-Theoretic Governance of Heterogeneous Agent Fleets" | Drop "Information-Theoretic" or qualify it in the abstract's first sentence, consistent with `paper-positioning.md` |
| Roadmap, "Now" | unchanged | Add the two exports above under "make the evidence path independently interpretable"; they are legibility, not features |

None of these rewrites is applied by this document. Each is a one-line PR
against a reader-facing surface guarded by the contested-claims lint, and
each should land with the canonical wording registered in
[`../dev/CANONICAL_SOURCES.md`](../dev/CANONICAL_SOURCES.md).

---

## What this audit does not establish

- It does not establish value. Every "differentiated" row is a difference in
  design, not a measured benefit; the claim ledger's rows on prevention
  (untested) and predictive lift (non-detection) are unchanged.
- "Unique" was never awarded, and would be weak if it were: it means "no
  substitute among the sources opened in one session under a network policy
  that blocked six primary domains".
- Snippet-only entries (DriftGate, Entra Agent ID, the IETF drafts' own
  text) are placeholders until opened. Two names from the prompting scan
  (Watchlight, BridgetOS) were not found and are not in the map.
- The audit read product documentation, not products. Whether AgentCore
  Policy evaluates every tool invocation, or how DriftGate's evidence
  contracts are shaped, is unverified here.
- It is one session's reading. The prior-art audit precedent in this folder
  used three independent verification passes; this one used one, and should
  be re-read adversarially before any README line changes on its authority.

## Open questions carried forward

1. Is there a buyer for adjudication over a flight recorder? The only answer
   is an operator other than the maintainer running the record for a reason
   of their own, which is the roadmap's standing gate.
2. Should the record adopt the IETF Agent Audit Trail shape as its export
   format, and what is lost in the "lossy mapping" the draft forces on claims
   and review?
3. Does A2A's `INPUT_REQUIRED` task state give a standard seam for governed
   review, so that a UNITARES pause could surface as an A2A task state rather
   than a bespoke verdict?
