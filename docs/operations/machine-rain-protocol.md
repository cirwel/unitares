# Machine R.A.I.N. Protocol

**Last Updated:** 2026-05-01

Status: operational recovery doctrine. Use when an agent detects contradiction, uncertainty, stale data, failed validation, identity ambiguity, or surface conflict.

Machine R.A.I.N. is not a mindfulness metaphor for agents. It is a short control loop for preserving truth under pressure.

> When a machine encounters contradiction, uncertainty, or instability, it must not rush to coherence. It must register the signal, allow the evidence to remain visible, investigate provenance, and choose the smallest action that restores truthfulness and stability.

## When To Run It

Run Machine R.A.I.N. before recovery or escalation when any of these appear:

- tool output contradicts memory, docs, or expectation
- tests fail or cannot run
- a database, migration, or cache state looks stale
- identity assurance is weak or ambiguous
- another process may own the same write surface
- a circuit breaker returns `guide`, `pause`, or `reject`
- an agent is stuck, looping, or tempted to force progress

## R: Register

Name the signal in concrete, observable terms.

Good registrations:

- `schema_migrations` reports source/DB drift at versions 5, 8, 10, 12.
- `eisv_pca_analysis.py` reads `data/governance.db`, which is months stale.
- `git push` rejected because `origin/master` advanced.
- `identity()` resolved through a weak fallback instead of proof-owned continuity.
- The lease plane is unavailable, so acquisition returned `service_unavailable`.

Bad registrations:

- "Something feels off."
- "The system is probably fine."
- "I should just retry until it works."

## A: Allow

Do not hide, overwrite, or rationalize the signal.

Machine meaning:

- preserve failing output and logs
- do not destructively reset state
- do not replace evidence with a confident guess
- lower confidence when evidence is incomplete
- keep stale sources labeled as stale
- let advisory mode remain advisory until coverage is measured

Allowed does not mean approved. It means the signal stays visible long enough to be inspected.

## I: Investigate

Trace the signal to canonical evidence.

Ask:

- What is the source of truth for this surface?
- Is this live data, stale data, synthetic data, or a cache?
- What changed recently?
- Which process, UUID, branch, or service owns the surface?
- Is the failure local, systemic, or a model inference error?
- What would make this claim falsifiable?

Prefer direct evidence:

- live Postgres over old SQLite files
- `git status`, `git log`, and PR state over memory
- focused tests over assumed behavior
- explicit identity assurance over label continuity
- lease status over informal coordination

## N: Next

Take the smallest stabilizing action.

Examples:

- add a read-only guard around stale data
- run a focused regression test
- write a KG note with provenance
- request dialectic review
- switch to advisory mode
- rebase before push
- stop before a destructive command
- ask the operator when the ownership boundary is unclear

Do not use Machine R.A.I.N. as permission to keep looping. If the next action does not reduce uncertainty, preserve the evidence and escalate.

## Output Shape

When an agent reports Machine R.A.I.N., keep it terse:

- Register: `<specific signal>`
- Allow: `<what evidence/state I preserved>`
- Investigate: `<canonical source checked>`
- Next: `<smallest stabilizing action>`

## Boundary

Machine R.A.I.N. is pre-recovery discipline. It does not replace:

- `self_recovery()` safety checks
- dialectic thesis/antithesis/synthesis
- lease-plane ownership
- identity proof requirements
- operator approval for destructive actions
