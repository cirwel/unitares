# Re-layering on NeMo Relay — exporter and gate shipped, substrate decision packet open (v0)

**Status:** DRAFT decision packet, 2026-09-16. The integration it describes is
built and tested (`unitares_sdk.integrations.nemo_relay`, optional extra); no
runtime posture, flag, threshold, or roadmap commitment changes with this
document. The three parking questions below are operator decisions, and none
is recorded as taken.
**Tracking:** companion to the roadmap's *Now* item (independent evidence,
#1607) and to the BEAM roadmap's V0.4 resolution and the signed Wave 3
go-decision, which this packet does not reopen.

## Why now

NVIDIA now ships two Apache-2.0 runtimes that own what this project built its
own plumbing for. [NeMo Relay](https://github.com/NVIDIA/NeMo-Relay) is a
multi-language agent runtime with execution scopes carrying parent-child
lineage, standardized lifecycle events, and middleware that can block,
transform, or replace tool and LLM calls, including pre-tool blocking for
Claude Code and Codex. [NeMo Agent Toolkit](https://github.com/NVIDIA/NeMo-Agent-Toolkit)
wraps LangChain, LlamaIndex, CrewAI, Semantic Kernel, Google ADK and custom
agents with profiling, tracing exporters and an offline evaluation suite, and
publishes a third-party plugin API.

Read on 2026-09-16, neither repository documents agent identity across
processes, a durable audit record, outcome tracking, human or structured
review, persistence, or cross-process continuity. That list is the product
definition of this kernel. The overlap is the transport and the enforcement
point; the non-overlap is the record.

The consequence is a re-layering, not a retirement: emit into their transport,
enforce through their middleware, keep the operator-owned record and the
evidence discipline here. `docs/proposals/README.md` carries no prior Relay or
NeMo design; this is the first.

## What shipped with this packet

`unitares_sdk.integrations.nemo_relay` (extra `nemo-relay`, Relay >=0.8.4):

| Piece | Behaviour | Contract it honours |
|---|---|---|
| Exporter | One fresh identity per root Relay scope (`force_new=true`); nested agent scopes share it unless `subagent_identities` mints lineage-linked children with `spawn_reason="subagent"` | Strict Identity, Simple Contract (AGENTS.md / CLAUDE.md) |
| Check-ins | Tool and LLM counts filed every N tool calls and at run end as `epistemic_class="substrate_interpretation"`, never with a confidence | the plugin `post-stop` semantics: a substrate reading, not an agent-authored report |
| Outcomes | Tool errors → `task_failed`; foreign guardrail rejections → `tool_rejected`; the server's default in-process `verification_source` is kept, never `external_signal` | `SCOPE_AND_THREAT_MODEL.md`: an agent-controlled outcome record is not independent evidence |
| Gate | Relay conditional-execution guardrail refuses tool calls for a run whose latest policy action is `pause` or `reject`; the refusal names verdict, guidance and the review path | pause as a contestable hypothesis, not an opaque verdict |
| Loop guard | Rejections by the gate itself are counted in the substrate summary and never filed as outcomes | the governor must not grade its own enforcement |
| Failure posture | Fail-open by default with logging; `fail_closed` refuses unbound runs | operator's choice, stated as a choice |

Enforcement latency is bounded by the check-in cadence, because the gate reads
policy state kept by a worker thread and never calls the server inline.
Sixteen tests drive the exporter with Relay 0.8.4's exact event shapes; one
end-to-end test activates the real runtime and skips without the extra.

## Decision packet — three surfaces that duplicate Relay's transport or enforcement

None of these is decided here. Each row states the surface, the overlap, the
options, and the evidence that would decide it. Parking means flag-off with
docs retained, never deletion; the measurement-authority rule applies (a count
may retire an instrument, never a capability).

| Surface | What Relay now covers | Options | Evidence that decides |
|---|---|---|---|
| Host hook chain in the governance plugin (Claude Code / Codex `session-start`, `post-edit`, `post-stop`) | Relay documents pre-tool blocking and lifecycle events for both harnesses | (a) keep both; (b) route new harness integrations through the Relay plugin and keep the hook chain for harnesses Relay does not support; (c) retire the hook chain once Relay parity is shown | A side-by-side run of one harness under both paths with the same server: identical identity binding, check-in count within cadence tolerance, and pause delivery on both. No such run exists yet. |
| Governed-effect execute plane (BEAM, port 8788, execute half not cleared) | Tool middleware that blocks or replaces execution at the call boundary, in-process, with no host-execution surface of its own | (a) keep the plane as designed; (b) park the execute half and express "agents propose, governance commits" as a Relay tool-execution intercept plus the existing governance veto; (c) keep the record-only shadow, retire the execute half | Whether an intercept-based commit path can carry the S7 strong-tier re-certification and the per-effect veto without a second credential boundary. This is a design read, not a usage count. |
| Surface lease plane / Plexus | Not covered: Relay has no leases, no cross-process exclusion | Out of scope for this packet; deferred to the Plexus thread | — |

**Constraint stated up front.** The BEAM roadmap committed Wave 3 on a
coordination/ownership argument and retired latency as a gate (V0.4, 2026-06-25);
the Wave 3 go-decision was signed GO-WITH-REDUCED-SCOPE on 2026-08-22. This
packet does not relitigate either. The question it adds is narrower: whether
the *enforcement transport* BEAM was going to own can be delegated to Relay
middleware while the coordination and ownership case stays where the operator
put it. That is a scope question for the operator, raised once, here.

## The drafting agent's recommendation, labelled as such

1. Keep the record kernel: identity, claims and evidence, review, outcomes,
   reconstruction. Nothing in either NVIDIA repository replaces it.
2. Make Relay the default transport for new host integrations and hold the
   existing hook chain until the side-by-side run above exists.
3. Park the execute half of the governed-effect plane in favour of a Relay
   intercept plus the governance veto, subject to the design read above.
4. Publish the integration upstream as a NeMo Agent Toolkit plugin package
   once the Relay path has one external operator behind it.

## Ninety-day evidence gate

The re-layering is judged on evidence that will exist by mid-December 2026,
not on this week's reading:

- one operator other than the maintainer running the Relay path under the
  independent-operator cohort protocol (`independent-operator-cohort-preregistration-v0.md`);
- the side-by-side harness run named above, reported per deployment and never
  pooled;
- the upstream plugin submission's review state.

A fair zero on any of these banks the datum and moves to the next lever; it
does not close the track.

## Risks

- Relay is pre-1.0 (0.8.4 on PyPI at drafting) with no stability guarantees;
  the plugin API can move. The integration pins `<1.0.0` and keeps its Relay
  imports lazy so the SDK never depends on it.
- A vendor can absorb this layer once it is proven. The operator-owned record
  is the part a vendor cannot honestly offer; the transport was never the
  moat.
- Single-maintainer bandwidth: this packet adds one integration and asks
  three questions; it does not add a workstream.

## What this document does not claim

It does not claim the integration improves outcomes, prevents anything, or
predicts anything; the claim ledger's rows on prevention (untested) and
predictive lift (non-detection, inconclusive) are unchanged. It does not
register a protocol. It does not park, retire, or enable any surface.
