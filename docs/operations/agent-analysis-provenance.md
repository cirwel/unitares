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
`embedder_available` and `semantic_search_reachable` both true. One
`health_check` call settled what four independent adversarial passes had agreed
on.

The reviewers' own closing verdict named the failure without noticing they had
just repeated it: *"the diagnosis trusted documentation and naming over code at
every step."* The generalisation is one step further out — **the tree is not the
deployment**, and a comment, a template, a default, or a static constant is not
a reading of the running system.

Three other findings that day had the same shape:

| Claimed from the tree | What the system said |
|---|---|
| A plist comment said the process is a jetsam target | 9h 3m uptime across every drop; nothing restarted |
| The SDK's `Allow: GET, POST, DELETE` header | A literal inside the 405 path; `stateless=True` means DELETE always 405s |
| `launchctl` showed the tunnel `runs = 1` | Process-level; the edge connections it holds are a different layer |

## What this does not license

Reading the running system is **not** a substitute for reading the code, and a
system reading is not self-interpreting. `tool_count: 44` from a live server and
`len(TOOL_HANDLERS) == 42` in the checkout are both correct; the gap is a loaded
entry-point plugin, and only the tree explains that. Cite both when both bear.

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
