# Proposal layout migration — issue #2347

**Status:** Proposal-directory pilot; broader documentation moves wait for reader feedback.
**Source:** `41dbf4f2` (2026-09-22), after the #2342 semantic migration closed.

The proposal directory now has three reading paths: current work and operative
contracts in `active/`, frozen protocols in `registered/`, and historical or
parked records in `archive/`. The folder is an audience cue. Every document
retains its status, scientific conclusions, decisions, and authorization limits.

## Inventory before relocation

The [machine-readable inventory and rewrite plan](proposals-layout-2347.json)
was generated before any file moved. It records all 119 original files, their
SHA-256 hashes, owning indexes, declared status, index disposition, Git history,
inbound reference candidates, proposed audiences, destinations and placement
reasons. Its 95 tagged entries have exactly one index row; another 19 historical
records and a JSON trace are indexed without inventing new dispositions.

`last_meaningful_update` uses a disclosed mechanical proxy: the latest Git
content diff after ignoring whitespace and following renames. It is not a claim
that Git can judge semantic significance. The separate `last_path_change` field
retains the literal last commit. Reference candidates are conservative filename
mentions, including ambiguous stub/archive basenames; they are not automatically
safe rewrite instructions. Copied Git subjects normalize absolute operator-home
prefixes to `~`; commit hashes identify the authoritative originals.

The inventory generator can inspect any committed revision:

```bash
python3 scripts/dev/proposals_inventory.py --ref 41dbf4f2 --output inventory.json
```

The committed migration artifact adds the reviewed placement map, reference
rewrite plan, compatibility decisions, and an audit of six companion repository
revisions. It is a dated record, not a second live proposal-status registry.

## Placement decisions

| Exception to a direct tag-to-folder mapping | Reason |
|---|---|
| Built lease, identity, review, calibration and measurement contracts remain in `active/` | Their implementation phase shipped, but they still define current behavior, safety gates, or open work. The inventory names each reason. |
| The lease-plane Phase A plan and Wave 3 boundary audit stay current | One is a live sequencing ledger; the other is an executable CI prerequisite. Their consumers move with them. |
| The worktree/lease counter-note and effect-binding design remain current despite their Parked tags | The dated lifecycle review retains the counter-note; the implemented effect-grant primitive cites the binding design. Their status and build gates remain unchanged. |
| Three Active-tagged protocols go in `registered/` | Accountable coordination, independent-operator cohort, and self-improvement protocols state registration at their introducing merge. Their DRAFT headers and original tags are preserved. This move does not enroll or run them. |
| Completed preregistrations and negative results go in `archive/` | Their consumed stop rules remain binding. Archiving does not permit another read. |
| Parked proposals go in `archive/` | This preserves their questions and resume conditions without presenting them as a current implementation queue. |
| The incremental-value and orientation protocols retain their original paths and bytes | The pilot manifest and cohort enrollment bind their SHA-256 hashes. Rewriting even their links breaks those bindings. The current-work and registered indexes link to the preserved originals; neither manifests nor instruments are rehashed. |

The root directory has fourteen files: the guide, eleven compatibility locators,
and the two protocols whose digests are bound by manifests or enrollment.
Each audience directory has a short explanation and the original index rows,
still grouped by subject. No proposal was deleted or merged into another.

## Links and provenance

Repository-relative Markdown links, source comments, executable document paths,
shared agent contracts, and CI/documentation guards follow the new locations.
Historical SQL migrations remain byte-identical, including diagnostic strings;
their six embedded proposal paths have compatibility locators. Five are at the
proposal root, and one is the sole file under the legacy `resolved/` directory.
Evaluation artifacts also remain byte-identical: recorded MCP responses, cohort
enrollments and results, and the accountability capture's historical quotations.
The individuality and orientation runners retain their bytes because published
results or enrollments cite their hashes. Three more locators preserve those
records' references to the maths roadmap, open-decisions packet, and individuality
preregistration. The inventory records the preserved files and their hashes.
Links pinned to a historical Git commit retain that commit and path. Only
floating links to this repository's `master` or `main` move.

The companion-repository scan inspected committed trees of the governance
plugin, public site, three paper repositories, and the reproduction repository.
Their exact revisions are in the JSON. The digital-proprioception paper names
the outcome-grounding stop rule; its old locator is retained. The two preexisting
Wave 1 and Wave 3a compatibility locators also remain. This bounded scan does
not establish absence of links elsewhere on the web or in untracked files.

GitHub does not redirect a moved `blob/master` URL. Other old floating deep links
will break; the inventory's source-commit permalink preserves the original
document and anchors. A compatibility locator supplies a clickable target, not
an HTTP redirect, and an old section fragment must be selected on that target.
The original stop-rule and Redis-retirement source-commit paths were verified
through GitHub before relocation. Branch-path and compatibility checks accompany
the PR validation.

Three archived dated records lacked a parseable header status before the move.
The recursive guard records those exact exceptions rather than editing history;
it rejects stale exceptions, new missing status fields, missing or duplicate
index rows, broken links, and incorrect totals.

The scope guard scans entire relocated files, so it encounters previously
published review records again. Its allowlist names those exact preserved
records and this provenance inventory; the JSON records the exceptions. No
folder-wide exception or historical prose rewrite is introduced.

## Reader check and remaining scope

A fresh Claude CLI reader was requested, but returned its weekly-limit message
without reading the files. The fallback was a local `gemma4:latest` consultation
given the two guide texts, with no filesystem or browser access. Its first
route-finding response sent current research through the history path. That
finding led to an explicit current-research row in `docs/README.md` and the
product → architecture → interface → evidence path for first-time visitors.

On the revised guides, consultation `0ccb3c58-f2ff-4934-89f9-ffd37157f912`
identified all five requested destinations:

| Category | Destination identified (resolved relative to the supplied guide) |
|---|---|
| Product | `docs/PRODUCT_DEFINITION.md` |
| Architecture | `docs/UNIFIED_ARCHITECTURE.md` |
| Current research | `docs/proposals/active/README.md` |
| Frozen protocols | `docs/proposals/registered/README.md` |
| Historical / negative results | `docs/proposals/archive/README.md` |

The model returned relative paths and expressed uncertainty about their roots.
Link resolution is checked separately by the deterministic guards. This is
advisory evidence from supplied text, **not** a traversed-link usability test,
governed review, independent PR approval, or measured human-reader improvement.
The full independent PR review remains a separate gate; the requested Claude
route was unavailable because of its weekly limit.

The `operations/`, `ontology/`, `evaluation/`, `evaluations/`, and loose docs-root
layout stay outside this pilot. Further moves depend on reader feedback from
this proposal migration, as #2347 specifies.
