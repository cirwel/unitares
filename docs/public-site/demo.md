# See the record, without installing anything

Git tracks commits, issue trackers preserve pull-request threads, and handoff
notes summarize a single session. UNITARES is the shared, queryable record
across the agent processes themselves: who made a claim, what evidence
supported or contradicted it, who challenged it, what replaced it, and what
outcome followed—recoverable by the next agent after the process that lived
through it is gone.

This page traces one incident step by step as the record holds it. A writer
process claims a service reload worked, a deterministic smoke test shows it
failed, a reviewer challenges the claim, a correction supersedes it, and a
fresh successor process reconstructs the full chain from the record alone.

## 1. A process makes a claim

Process `{{facts.writer_process_id}}`, acting for `{{facts.writer_principal_id}}`
(`identity-writer`), records a finding (`finding-original`):

> {{facts.original_claim}}

The record binds the finding to the process that wrote it (an
`{{relation.authored}}` link), so the claim stays attributed to its author
across sessions and handoffs.

## 2. The evidence disagrees

A deterministic smoke test runs afterwards and records its result
(`outcome-evidence`):

> {{facts.deterministic_evidence}}

The evidence is linked to the claim it contradicts
(`{{relation.contradicts}}`), and the outcome (`{{facts.outcome}}`) grades the
claim (`{{relation.grades}}`). The original claim and the contradicting test
result sit side by side in the record.

## 3. A reviewer challenges it

Process `{{facts.reviewer_process_id}}` raises a structured objection on the
claim (`review-challenge`):

> {{facts.challenge}}

The challenge is attached directly to the claim (`{{relation.challenged}}`), so
any process reading the claim later sees the objection with it.

## 4. A correction replaces it

The writer records what actually happened (`finding-correction`):

> {{facts.correction}}

The correction `{{relation.supersedes}}` the original claim. The original
finding is not deleted or silently overwritten: it stays in the record, marked
as superseded, and points forward to the correction that replaced it.

## 5. A successor reconstructs the chain

A new process starts with none of this in its context window. Querying the
record, it recovers each piece of the incident from its typed record:

{{questions_table}}

## How this fits with Git, GitHub, and handoff notes

UNITARES works alongside repository history, pull requests, and session handoff
notes rather than replacing them. A finding can cite a commit, PR, test log, or
handoff document while giving every connected runtime—Claude Code, Codex,
Hermes Agent, or a custom MCP client—one shared place to query prior
conclusions, active review objections, and supersession chains before acting.

Every quoted fact, record ID, and link relation on this page is rendered at
build time from [`tests/accountability_journey/incident-v0.json`]({fixture_blob}),
the fixture verified in CI by the [accountability-journey test]({result_blob}).
For measured deployment results and evaluation scope, see
[Evidence and limits]({evidence_blob}).

## Try it yourself

Ready to run your own server? [Install UNITARES](https://github.com/cirwel/unitares#install),
then follow the [captured two-process walkthrough]({walkthrough_blob}) to see
the exact MCP calls and live server responses across two agent sessions.
