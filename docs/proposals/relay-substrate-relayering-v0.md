# Re-layering on NeMo Relay — exporter and gate shipped, substrate decision packet open (v0)

**Status:** Decision recorded 2026-09-17 and revised the same day after
adversarial review; drafted 2026-09-16. The integration it describes is built
and tested (`unitares_sdk.integrations.nemo_relay`, optional extra). Two of the
three questions are decided and the execute-half question is deliberately left
open; see *Decision*. No runtime posture, flag, or threshold changes, and
nothing is parked or retired.
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

## Decision (operator, 2026-09-17)

The operator adopted the recommendation on 2026-09-17, by the option letters in
the decision table above. The record below was revised the same day after an
adversarial review through the Codex host adapter, which returned two blocking
findings against the first draft; both are fixed here and named in *What the
review changed*.

| # | Surface | State | Authority | Deciding evidence | Not authorised |
|---|---|---|---|---|---|
| 1 | Record kernel | **Decided: keep.** No change to anything. | Operator, 2026-09-17 | — | — |
| 2 | Host hook chain | **Decided: option (b).** New harness integrations are written against the Relay plugin; the existing `session-start` / `post-edit` / `post-stop` chain keeps running unchanged for every harness it serves. | Operator, 2026-09-17 | Side-by-side run, for option (c) only | Retiring the hook chain. Option (c) is unavailable until the run exists. |
| 3 | Governed-effect execute plane | **No decision.** Option (b) is the *hypothesis the design read tests*; (a) and (c) remain live alternatives. | None taken | The design read (charter below) | Anything. Nothing is parked, flagged off, prioritised, or deleted, and no option holds default status. |
| 4 | Upstream publication | **Decided: publish first**, reversing the packet's ordering. | Operator, 2026-09-17 | — | Any efficacy claim riding on the submission. |
| 5 | Surface lease plane / Plexus | **Unchanged**, out of scope. | — | — | — |

### Row 3 records no decision, and that is deliberate

The first draft of this record adopted option (b) "as direction only, not
executed." The review found that formulation harmful, and it was right: naming
a preferred option before its deciding evidence exists biases the read toward
confirming that option rather than comparing three, and it shifts the burden so
that later evidence must overturn an adopted direction instead of informing an
open choice. That is the failure the shared contract's rule against stating a
deciding standard after the fact exists to prevent.

Nothing is lost by declining to decide. The signed Wave 3 decision
(`wave-3-go-decision-2026-08-16.md`, GO-WITH-REDUCED-SCOPE, 2026-08-22)
authorises *scope, not an implementation start*: "Building begins only after
that gate exists and is met. No implementation PR may cite this signature as
its authority." The execute half is therefore permitted subject to a gate that
does not yet exist, not obliged. Deferring a decision defers nothing that was
scheduled to be built, which also answers the review's question about whether
this relitigates Wave 3 by execution: it does not, because Wave 3 authorised no
execution to relitigate.

**Design-read charter.** The read compares (a), (b) and (c) as live options and
must state disconfirming conditions for each before it begins. Its subject is
whether an intercept-based commit path can carry the S7 strong-tier
re-certification and the per-effect veto without a second credential boundary.
The review named the shape the answer needs: a failure-mode and authority
matrix covering identity binding, intent, decision, veto, actual execution,
replacement, retry, timeout, audit durability and recovery — because an
in-process intercept can block, replace, retry or fail before the record sees
the intended action, and the record can veto on state the runtime cannot
atomically observe. Split-brain accountability is the risk the read exists to
rule in or out. The read carries no implementation authority; a gate document
under Wave 3's own terms still owes ratification before anything is built.

### Row 4 changes an item's governance role, and says so

The packet made upstream publication conditional on an external operator
already running the Relay path. That ordering is circular on its face: it makes
publication contingent on an outcome publication was meant to help produce.
Reversing it is the operator's decision, taken on 2026-09-17.

Two corrections to how the first draft argued it, both from the review:

- **The registry is not established as an acquisition channel.** The first
  draft claimed the NeMo Agent Toolkit's third-party plugin registry is the most
  plausible route by which an unknown operator would find this integration.
  That is an unproven empirical claim, and cheaper channels exist that the draft
  ignored: a release announcement, an ecosystem issue or discussion, a documented
  pilot invitation, direct outreach. The circularity above stands without the
  claim; the claim is withdrawn. Whether the registry is browsed by operators
  seeking plugins, accepts this package, and permits install without
  maintainer-mediated setup is unknown and is part of what submitting will
  establish.
- **The external-operator item's role changed; it was not clarified.** The first
  draft said the item returns to "what it always was." That was wrong. In the
  packet it was an explicit precondition on publishing, and demoting it to
  telemetry and the evidence path for efficacy claims is a substantive change in
  its decision role, not a re-description. Recording it as a clarification would
  have been exactly the retroactive move the measurement-authority rule forbids.

**Consequence that follows, and is owed.** Publication changes who can
encounter, install and self-select into the cohort, which touches recruitment
channel, population, version exposure and support burden. The pre-registered
independent-operator cohort protocol
(`independent-operator-cohort-preregistration-v0.md`) is **not** amended by this
decision, and its stop rule, independence criteria and per-deployment reporting
are untouched. But before any recruitment occurs, that protocol owes a dated
clarification distinguishing registry discovery from cohort enrollment and
stating that discovery through publication is permitted. Publishing before that
clarification exists is not authorised here.

### What replaces the ninety-day evidence gate

The packet named mid-December 2026. Two of its three items are
maintainer-controlled and arrive by being done rather than by being waited for,
so the date was doing no evidence work on them. The first draft removed the date
outright. The review found that a defect, and it was right: the date was the only
mechanism making an un-started action visible as overdue, and without it
provisional choices harden silently while "named work" sits indefinitely.

The date returns as a **process control**, which is a different instrument from
an evidence gate:

| Item | Instrument | On 2026-11-03 |
|---|---|---|
| Side-by-side harness run: one harness under both paths against the same server, comparing identity binding, check-in count within cadence tolerance, and pause delivery. Reported per deployment, never pooled. | Named action; sole condition on row 2's option (c) | Report done or not done. If not done, the operator renews, redirects or stops it explicitly. |
| Design read on the execute plane, per the charter above | Named action; row 3 stays undecided until it concludes | Same. Row 3's "no decision" status is renewed explicitly or the work is stopped. |
| One operator other than the maintainer running the Relay path | Telemetry, and the evidence path for efficacy claims — **changed from a publication precondition on 2026-09-17**, see row 4 | Reported as a count, with no authority to retire anything. A fair zero banks the datum and moves to the next lever. |

The 2026-11-03 checkpoint reports status and forces an explicit renew-or-stop.
It reads no outcome, grades nothing, and authorises no removal. It is unrelated
to the registered 2026-12-01 confirmatory read, which it deliberately precedes
and must not be confused with, and it falls clear of that protocol's
2026-11-15 publication blackout.

### What this decision does not do

- It does not reopen the BEAM V0.4 resolution or the signed Wave 3 decision,
  for the reason given under row 3: that signature authorised scope and
  explicitly not an implementation start.
- It does not amend, weaken or anticipate the pre-registered cohort protocol.
  It does create an obligation against it, stated under row 4.
- It parks nothing, retires nothing, enables no flag, deletes nothing.
- It grades no capability on a usage count, and adds no efficacy claim.

### What the review changed

An adversarial review was requested through the Codex host adapter on
2026-09-17 (advisory model evidence, not a governed review verdict; a governed
verdict would come from `request_review`). It returned seven findings against
the first draft of this record. Two were blocking and both are fixed above:
row 3's "adopted as direction" became "no decision" with a charter, and the
deleted date returned as a dated process control. Three were should-fix and are
also fixed: the registry claim is withdrawn, the external-operator demotion is
recorded as a role change rather than a clarification with its protocol
obligation named, and the Wave 3 question is answered from the signed text
rather than asserted. The review's remaining points — the split-brain failure
mode and the status table — are folded into the charter and the table at the
top of this section. Its own summary named the biggest error as treating the
external-operator condition as though it had always been telemetry; that
correction is the paragraph under row 4.

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
