# See the record, without installing anything

The narrow question is not whether UNITARES can replace git or an issue
tracker. It is whether several independent agent processes can recover one
attributed chain of claims, evidence, objections, corrections, and outcomes
after the process that lived through it is gone.

This page shows one incident, step by step, as the record holds it. A process
claims a service reload worked, a test says it did not, a reviewer challenges
the claim, a correction replaces it, and a fresh process that saw none of this
reconstructs the whole chain from the record alone.

!!! note "Where this comes from"
    Every quoted fact, record id and link name on this page is read, at build
    time, from the synthetic incident fixture
    [`tests/accountability_journey/incident-v0.json`]({fixture_blob}), the
    same file the repository's accountability-journey test runs against. The
    narration between them is written by hand.
    Nothing here is a capture of a real deployment, and nothing on this page
    talks to a server.

## 1. A process makes a claim

Process `{{facts.writer_process_id}}`, acting for `{{facts.writer_principal_id}}`,
records a finding:

> {{facts.original_claim}}

The record binds the finding to the process that wrote it (an `{{relation.authored}}` link),
so the claim never floats free of its author.

## 2. The evidence disagrees

A deterministic smoke test runs afterwards and records its result:

> {{facts.deterministic_evidence}}

The evidence is linked to the claim it contradicts (`{{relation.contradicts}}`).
The outcome, recorded as `{{facts.outcome}}`, grades the claim
(`{{relation.grades}}`). The claim and the evidence against it now sit side by
side.

## 3. A reviewer challenges it

Process `{{facts.reviewer_process_id}}` raises an objection on the claim:

> {{facts.challenge}}

The challenge is attached to the claim (`{{relation.challenged}}`), so anyone reading the
claim later finds the objection with it.

## 4. A correction replaces it

The writer records what actually happened:

> {{facts.correction}}

The correction `{{relation.supersedes}}` the original claim. The original is not deleted or
rewritten: it stays in the record, marked as replaced, and points to the
correction.

## 5. A successor reconstructs the chain

A new process starts with none of this in its context. It reads the record and
answers each question below from a specific record:

{{questions_table}}

## What this shows, and what it does not

It shows the mechanism: who said what, on what evidence, who challenged it, and
what replaced it, all recoverable by a process that was not there.

It does not show that UNITARES beats a well-kept alternative. The same incident,
recorded as git history plus a structured handoff note, was reconstructed just
as accurately when both sides held the same facts (both scored 1.000;
[the rehearsal result]({result_blob})). What UNITARES adds is where the record
lives: typed, attributed, and shared by every runtime in one place, instead of
spread across each agent's own notes. Whether that changes outcomes in real
work is a question for [Evidence and limits]({evidence_blob}), not a claim made
here.

The first real capture-stage incident was less favorable: ordinary repository
artifacts recovered the incident facts and UNITARES did not. The record had no
bridge from CI or git for that event, no finding surfaced to a bound agent, and
the hosted connector was unreachable at capture time. The
[capture-stage result](../evaluations/accountability-journey/capture-stage-2168-v0.md)
is retained because a reconstruction system is only useful when the relevant
facts enter it. This fixture proves the record shape and retrieval path; it does
not prove capture coverage or adoption value.

## Should you install it?

Probably not if one repository, its issues, and a reliable handoff note already
give every session the history it needs. Consider UNITARES only when several
independent runtimes need a shared, queryable record across repositories or
context boundaries, and typed authorship, supersession, review, and outcomes
are worth operating a service.

## Try it yourself

When you want to run it: [install](https://github.com/cirwel/unitares#install),
then follow the [two-process walkthrough]({walkthrough_blob}) with your own
agents.
