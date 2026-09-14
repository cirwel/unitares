# Agent analysis: say where each claim came from

**Applies to** any agent-authored analysis of this system — council reviews, the
dispatched-reviewer pattern recorded in
`docs/proposals/open-decisions-packet-v0.md` (§ *Council review*), a `consult`
critique, or a single session's own diagnosis.

**The rule.** Mark every load-bearing claim with its source:

- **tree** — read from a checkout: source, config templates, docs, git history.
- **system** — read from the running deployment: a live tool call, a log, a
  process listing, a database query.
- **inferred** — neither; derived from the two above.

A claim about what the deployment *does* is only as good as a **system** source.
A **tree** source establishes what the code *can* do, which is a different
claim and must not be reported as the first one.

## Why this exists

**Provenance of the examples below.** This document was itself codified from
reading a session transcript, not from any live system check made while
writing it — a **tree**-only artifact by its own rule. The historical readings
cited here (uptimes, counts, process states) are reported as the transcript
recorded them on 2026-09-13; none has a durable, linkable receipt in this repo
(no `outcome_event`, no knowledge-graph entry, no committed log excerpt), so
none is re-verified here and each is marked **UNVERIFIED** below rather than
presented as re-confirmed fact. That does not weaken the lesson — the
classification error the examples illustrate is visible from the transcript
alone — but a reader must not treat the specific historical numbers as
independently checked by this document, and this document must not, and does
not, invent a current health check as if it were evidence for a past reading.

On 2026-09-13 a four-reviewer council was dispatched to attack a diagnosis of
intermittent MCP disconnects. It did its job: it refuted both the diagnosis and
the proposed fix, with file:line citations, and caught a shipped commit whose
stated premise was false.

Then all four reviewers made the same mistake, and it was the mistake that had
produced the diagnosis they were attacking. Each read
`UNITARES_KNOWLEDGE_BACKEND=postgres` out of
`scripts/ops/com.unitares.governance-mcp.plist`, observed that
`KnowledgeGraphPostgres` has no `semantic_search`, and concluded the embedding
model has no reachable caller on this deployment. That plist is a **template**.
The running server reports `knowledge_graph.backend = "age"` with
`embedder_available` and `semantic_search_reachable` both true (UNVERIFIED
here: a 2026-09-13 transcript-reported system reading, not re-run for this
document). One `health_check` call settled what four independent adversarial
passes had agreed on.

The reviewers' own closing verdict named the failure without noticing they had
just repeated it: *"the diagnosis trusted documentation and naming over code at
every step."* The generalisation is one step further out — **the tree is not the
deployment**, and a comment, a template, a default, or a static constant is not
a reading of the running system.

Three other findings that day had the same shape — tagged explicitly below,
because getting this tagging right is exactly the discipline the rule asks
for, and the first draft of this table got one entry wrong:

- **Tree, refuted by system (UNVERIFIED here).** A plist comment said the
  process is a jetsam target. The system reading: 9h 3m uptime across every
  drop, nothing restarted.
- **Tree, refuted by a closer tree reading — no system probe involved.** The
  SDK's `Allow: GET, POST, DELETE` header looked like confirmation that
  `DELETE` was implemented on `/mcp`. It is not: the header is a literal
  inside the SDK's own 405-rejection function, and `stateless=True` means
  `mcp_session_id` is always `None`, so `DELETE` 405s unconditionally. Nothing
  here required a live probe; a closer reading of the same source was enough.
  This row does not fit a tree-vs-system frame at all — both readings are
  tree — which is itself worth naming: not every correction needs a system
  reading, and forcing one into this table would have been the same
  misclassification in the other direction.
- **System, qualified by inference, not by a second system reading
  (UNVERIFIED here).** `launchctl` showed the tunnel `runs = 1` — that count
  is itself a system reading, not a tree claim. But reading it as settling
  whether edge-connection churn occurred is an **inference** layered on top
  of that measurement, not a second measurement; `runs = 1` is process-level
  and says nothing about what happened on the connections the process held.
  The first draft of this document placed both halves of this row backwards
  — the tunnel count under "tree" and the inference under "what the system
  said" — which is the exact error this document exists to prevent,
  reproduced in its own worked example.

## What this does not license

Reading the running system is **not** a substitute for reading the code, and a
system reading is not self-interpreting. `tool_count: 44` from a live server and
`len(TOOL_HANDLERS) == 42` in the checkout are both correct (UNVERIFIED here:
a 2026-09-13 transcript-reported system reading, not re-run for this document).
A loaded entry-point plugin is a plausible **tree**-side explanation for that
kind of gap — the checkout does permit one — but that a plugin caused this
particular gap was not independently confirmed at the time (no plugin
inventory was diffed against the registry), so treat the specific causal claim
as **inferred** and unverified, not settled. Cite tree, system, and the
confidence level of any inference between them when more than one bears.

Nor does this weaken the existing instruction to attack rather than ratify, or
the prohibition on proposing that a pre-registered stop rule be weakened,
re-run, or reinterpreted.

## When the system is unreachable

Say so, in those words, and mark every claim that would have needed it
**UNVERIFIED** — not REFUTED and not VERIFIED. The 2026-09-12 council recorded
its own instrument outage exactly this way and that record is the model. An
analysis written without system access is an analysis of the repository; label
it as one.

## Relation to the measurement-authority rules

`CLAUDE.md` § *Measurement authority* requires naming which of the four states a
zero rules out, and states a deciding standard must be declared as a choice
before it is applied. This rule is the same discipline one level down: the
provenance of a reading is part of what the reading establishes, and a tree
source silently presented as a system source imports a conclusion that was never
measured.
