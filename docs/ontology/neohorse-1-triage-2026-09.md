# Triage: NeoHorse-1 against UNITARES (2026-09)

**Prompted by:** operator question ("encroaching?") on the Hugging Face daily-papers
listing for NeoHorse-1 ([arXiv:2609.08183](https://arxiv.org/abs/2609.08183),
TokenRhythm, submitted 2026-09-08, 377 upvotes at read time).
**Follows:** `competitive-analysis-2026-06.md` (the MI9 / Auton triage), same format.
**For:** the v7 §10 spine in `v7-related-work-draft.md`, and the motivation section of
`docs/proposals/self-improvement-loop-evaluation-v0.md`.
**Verdict:** **Not encroaching.** Different plane, different sink, no contested claim on
EISV, coherence, identity, dialectic, or shared memory. It does plant a flag on the word
**harness**, and it is the loudest published instance of "the harness's per-turn records
are the evidence" — which is UNITARES's own premise pointed at a different consumer.
One disambiguation sentence in v7 §10; no defensive framing.

---

## TL;DR

- NeoHorse-1 is a **model-training paper**. A routing layer dispatches each turn across a
  heterogeneous model pool, records what it dispatched and what came back, filters those
  records, and converts them into the next post-training mixture. The output is weights.
- UNITARES is an **inference-time state estimator**. It reads turn shape and outcomes and
  produces a behavioral state estimate, a policy action, and a calibration constant. It
  never touches weights, and the execution-cost policy forbids putting a trained or
  metered model on the required path at all.
- The two **compose rather than compete**: a UNITARES-governed session emits exactly the
  per-turn record with an attached outcome grade that NeoHorse's admission stage wants,
  and NeoHorse's routing harness is the kind of surface a governance plane sits on top of.
  Neither needs the other and neither occupies the other's layer.
- The one real exposure is **vocabulary**, not contribution. Four words collide and mean
  different things (below). If "harness-mediated" becomes theirs the way "runtime
  governance" became MI9's, v7's harness-as-measurement-plane framing reads as downstream
  unless it is differentiated on first use.

## What the paper actually does

Its stated mechanism, in its own terms: recursive self-improvement needs a concrete path
by which a system observes its capabilities and turns that evidence into the next round of
learning. NeoHorse-1 builds one.

1. **Route.** A heterogeneous model pool behind a router. Per user turn it records the
   predicted capability demand, the selected service tier, and the resulting interaction.
2. **Admit.** Those records become training examples that preserve interleaved reasoning,
   tool calls, and harness context. Admission is exact and near-duplicate removal,
   evaluation decontamination, structural validation, a six-dimensional semantic
   evaluation, and subscene-level Scene/Goal/Outcome labeling.
3. **Train.** Routing signals organize supervised fine-tuning into a three-stage
   curriculum, extended to routing-guided on-policy distillation where a teacher supervises
   student-generated responses under the same progression.
4. **Reallocate.** Capability-guided allocation turns evaluation feedback into the next
   training mixture — the "evaluation–selection–update loop."

Reported results: macro-average 58.94 → 64.87 at 4B and 65.60 → 69.04 at 9B, post-trained
from Qwen3.5-9B, Apache-2.0, weights and GGUF released.

## Where it touches UNITARES

| Collision | NeoHorse referent | UNITARES referent | Line to draw |
|---|---|---|---|
| **harness** | the routing/serving layer that dispatches a turn to a model | the agent-side lifecycle that emits a check-in (hooks, adapters, `observe` / `sync_state`) | Both record per-turn context; they sit on opposite sides of the model call. Theirs looks down at a model pool, ours looks across an agent's trajectory. |
| **capability** | what a task demands of a model — a routing input and a training-mixture key | the property that discriminates behavior (`embodied`, `persistent`, `protected`, `cadence.*`) | Theirs is a difficulty estimate. Ours is a dispatch tag, and under fleet neutrality it is the only thing behavior may branch on. |
| **tier** | service tier: model size and cost | identity binding strength in the onboard response (`session_source` / `tier`) | Total referent mismatch. If both papers are ever cited in one paragraph, this word needs a gloss. |
| **six-dimensional semantic evaluation** | scores a candidate training sample's admissibility | EISV scores an agent's behavioral state at a check-in | Shape collision only: both are a small vector derived from a turn. One is a data filter over samples, the other a state estimator over an agent. Neither substitutes for the other. |
| **evaluation–selection–update loop** | outcome feedback → next training mixture (sink: weights) | `record_result` → `outcome_correlation` → policy action and calibration constant (sink: runtime behavior) | Same loop topology, different sink. The weights sink is explicitly out of scope here, and the required-path cost policy rules it out as a dependency. |

## Where it does not touch UNITARES

No behavioral state estimation. No drift as displacement in a state space. No coherence.
No identity or lineage ontology, no class-conditional calibration, no earned-versus-
performative continuity. No dialectic resolution. No cross-agent shared memory. No trust
tiers as write gates. Governance is not the frame at any point — the frame is capability
improvement, and safety appears only as data hygiene.

That is the whole of the answer to "encroaching?": the core contributions are untouched.

## The RSI claim, judged by this repo's own stop rule

`docs/proposals/self-improvement-loop-evaluation-v0.md` names the confound this paper sits
on top of. Its lane taxonomy: lane O is operational closure (a loop exists and runs), lane
F is a frozen non-adaptive automation beating no loop, lane A is outcome-conditioned policy
updating beating the frozen policy. Only lane A can support a self-improvement claim, and
passing O does not imply passing A.

NeoHorse's published comparison is post-trained model versus base model. There is no fixed
non-adaptive arm — no run where the training mixture is frozen at enrollment and the same
data volume is spent. So the evidence supports "this pipeline produced a better model
once," which is lanes O and F. It does not identify recursion, because one round of a
well-built data flywheel and an adaptive loop are indistinguishable under that design.

The paper hedges correctly and should be given credit for it: "initial prototype," "a path
toward," "extending this loop across successive iterations is the next step." The gap is in
the design, not in the honesty of the wording.

The benchmark shape supports the same reading. On the 9B track the gains concentrate where
the training data lives — VitaBench +11.00, PinchBench +7.70, QwenClawBench +4.69 — while
LiveCodeBench v6 and IFBench move +0.00 and IFEval lands slightly below base (89.09 against
89.46). That is the signature of targeted post-training on agentic traces, which is what
they built, not of a system that got better at getting better.

**Use:** cite NeoHorse-1 in the motivation of `self-improvement-loop-evaluation-v0.md` as a
real, well-resourced, externally published pipeline whose evidence still lands in lanes O
and F. It is the strongest available argument that the adaptive-versus-fixed arm is the
load-bearing one and not methodological fussiness.

## What v7 should do

1. **One sentence in §10, on the training plane.** NeoHorse-1 converts harness records into
   the next training mixture; UNITARES converts them into a runtime state estimate. Same
   evidence substrate, different consumer, no contested claim. Cite as a neighbor, the way
   Auton is cited — not as a head-to-head, the way MI9 is.
2. **Disambiguate "harness" on first use**, in the same paragraph that introduces the
   substrate-plurality frame. It is now a term two literatures use for opposite vantages.
3. **Do not import the RSI banner.** UNITARES has a preregistered protocol saying what
   would license that claim and has not run it. Adopting the vocabulary before the
   confirmatory read would be exactly the move the protocol exists to prevent.
4. **No defensive framing.** There is no priority collision on the core to pre-empt.

## Sources and limits

Read through the Hugging Face connector: the paper metadata record (title, authors,
abstract, keywords) and the `TokenRhythm/NeoHorse-1-9B` model card, including its 9B
benchmark table. `arxiv.org` and `huggingface.co` are both blocked by this session's egress
proxy, so **the full technical report was not read**. Everything above about the routing
harness, the six admission dimensions, the three-stage curriculum, and the allocation step
comes from the abstract and the model card's own summary of them, not from the method
sections. Section-level claims are therefore unverified, and the numbers quoted are the
model card's.

Before this triage is lifted into the paper, someone with unblocked network access should
read the report and confirm two things: that the six semantic dimensions score samples
rather than agents (the reading the shape-collision row depends on), and that no
fixed-mixture control arm exists (the reading the stop-rule section depends on). Both are
falsifiable by one pass over the method section, and either one flipping changes a verdict
above.
