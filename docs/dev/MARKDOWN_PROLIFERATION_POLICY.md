# Markdown Proliferation Policy

Status: specialized policy reference. Use this when deciding whether to create, consolidate, classify, or archive markdown files in the active docs set.

**Last Updated:** 2026-04-04

## Why This Policy Exists

This repository has accumulated multiple generations of documentation: active operational guides, architecture summaries, specialized reference material, historical archives, and one-off planning artifacts. That is normal for a long-running codebase, but it creates a real failure mode for both humans and coding agents: too many docs that appear current, too many partially overlapping explanations, and too many stale links that continue to look authoritative.

The problem is not just “too many markdown files.” The real problem is too many active-looking files competing to explain the same part of the system. Once that happens, agents can form a plausible but outdated mental model after reading only one or two docs. Humans do the same thing under time pressure. This policy exists to keep the active documentation surface small enough that readers can build the right model quickly.

## Core Principle

Prefer a small set of durable, maintained docs over many medium-quality docs with overlapping scope.

That means:

- broad user-facing explanations should be consolidated into existing live docs
- narrow workflows may have their own docs, but must be marked as specialized
- compatibility entrypoints should stay thin
- historical material should be deleted or noted inline as stale

## Current Active Doc Model

The active documentation set is intentionally structured:

- `README.md`: public overview and quick-start framing
- `docs/UNIFIED_ARCHITECTURE.md`: canonical prose summary of the runtime
- `docs/dev/CANONICAL_SOURCES.md`: authority ordering and source-of-truth map
- `docs/guides/START_HERE.md`: thin compatibility entrypoint
- `docs/guides/TROUBLESHOOTING.md`: live troubleshooting guide
- `docs/operations/OPERATOR_RUNBOOK.md`: live operator procedures
- thin infrastructure registries such as `docs/operations/database_architecture.md` and `docs/operations/DEFINITIVE_PORTS.md`
- specialized references for narrow topics such as CIRS, tunnel deployment, tool registration, and contract drift

If a proposed new file does not fit one of those roles clearly, it is probably a sign that the content belongs in an existing doc or in the archive/knowledge layer instead.

## Decision Rules Before Creating A New Markdown File

Ask these questions in order:

1. Does an existing active doc already cover the same audience and topic?
2. Is the content broad enough that it should live in `README.md` or `docs/UNIFIED_ARCHITECTURE.md`?
3. Is the content only useful for a niche workflow, and if so can it be marked as specialized?
4. Is the content historical, session-specific, or reflective? If yes, archive it or store it as knowledge instead of adding it to the active docs surface.
5. Will readers treat this as current operational truth? If yes, the file needs explicit classification and must not contradict canonical runtime sources.

If you cannot answer those questions cleanly, do not create the file yet.

## When New Markdown Is Appropriate

A new markdown file is justified when all of the following are true:

- it has a clearly distinct audience or job
- it does not substantially duplicate an existing active doc
- it has enough substance to deserve standalone maintenance
- it is classified with a `Status:` line near the top
- it either becomes part of the active doc model or is clearly specialized/historical

Small one-off docs are the highest-risk category. They tend to carry just enough truth to be believable, but not enough maintenance attention to stay aligned as the code changes.

## Thin Docs Versus Full Docs

Not every doc needs to be long. Thin docs are acceptable when they act as registries or entrypoints rather than second manuals.

Examples of acceptable thin docs:

- a compatibility entrypoint that only gives the default workflow and points outward
- a port registry that only records assignments and verification commands
- a storage/backend reference that only states where data lives and where to look next

What thin docs must not do:

- silently become full architecture explanations
- restate runtime semantics better handled elsewhere
- compete with the canonical docs for conceptual authority

## Status Markers

Active docs should declare what they are. Use a `Status:` line near the top.

Recommended labels:

- `live overview`
- `canonical prose summary`
- `live operator guide`
- `live troubleshooting guide`
- `thin entrypoint`
- `thin infrastructure reference`
- `thin operational registry`
- `specialized protocol reference`
- `specialized developer reference`
- `specialized policy reference`

This is not cosmetic. It tells agents whether a doc is meant to explain the whole system or only a narrow slice.

## Stale Material

If a document is mainly a completed plan, one-time incident writeup, migration narrative, session artifact, or obsolete architecture explanation — delete it from the public tree or mark it inline as historical. Do not maintain a parallel *public* archive tree.

The repository's `.gitignore` already encodes the operator's split between public docs and local-only working notes:

- local-only archive directory under `docs/` — retired/superseded proposals kept locally for the operator's records, not surfaced publicly. The frontmatter banner on the retired doc is sufficient public signal; the body of a rejected proposal serves no public purpose and noises the active proposals/ directory.
- `docs/handoffs/` — date-stamped session-to-session handoffs. Pinned to their moment by definition; aged-out handoffs only confuse external readers. Specific handoffs may be force-added to the public tree when they crystallize into permanent contracts (e.g., `2026-05-03-r1-implementation-handoff.md`).
- local-only internal configuration file under `docs/` — operator-specific configuration not meant for forks.

Retiring a proposal: move the file to the gitignored local archive under `docs/`, and `git rm` the public copy. The file stays on the operator's disk; the public repo loses a 400-line RFC that proposes work nobody should implement.

## Relationship To Knowledge Storage

Not every insight belongs in markdown. If the content is primarily:

- a discovery
- a note
- a short-lived insight
- a pattern observed during work
- a bug finding or question

then it may belong in knowledge storage rather than the active markdown tree. Markdown should bias toward durable reference material, not every useful thought produced during development.

## Enforcement

The repository uses lightweight checks to catch the highest-risk cases:

- new markdown files that are too small to justify standalone maintenance
- stale phrases in active docs
- missing `Status:` markers on classified active docs
- thin compatibility docs that silently grow back into parallel manuals

These checks are intentionally narrow. They are not meant to replace judgment. They exist to catch the classes of drift that have repeatedly confused both humans and agents.

## Practical Rule Of Thumb

If you are tempted to create a new markdown file, first try one of these:

- add a section to an existing live doc
- add a short note to a specialized reference
- move the content to archive if it is historical
- store it as knowledge if it is an insight rather than a durable reference

Create a new file only when the content genuinely has a distinct job and is likely to remain worth maintaining.
