# Proposal layout migration — issue #2347

**Status:** Proposal-directory pilot; post-merge reader feedback incorporated without further directory moves.
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
rewrite plan, compatibility decisions, and an audit of seven companion repository
revisions plus a published Zenodo deposit. It is a dated record, not a second
live proposal-status registry.

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

The root directory has sixteen files: the guide, thirteen compatibility locators,
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
preregistration. The explicitly verbatim individuality result transcript retains
its original path, which resolves through that locator. The inventory records
the preserved files and their hashes.
Links pinned to a historical Git commit retain that commit and path. Only
floating links to this repository's `master` or `main` move.

The companion-repository scan inspected committed trees of the governance
plugin, public site, four paper repositories, and the reproduction repository.
Their exact revisions are in the JSON. The digital-proprioception paper names
the outcome-grounding stop rule; its old locator is retained. The two preexisting
Wave 1 and Wave 3a compatibility locators also remain. Council review added the
accountable-testbeds paper and verified its public
[Zenodo deposit](https://zenodo.org/records/21930162): the deposited manuscript's
reference [29] cites the original v0 metrics-preregistration path, and the
deposit metadata cites v1. Both original paths now retain compatibility locators.
The JSON records the inspected repository revision and public deposit evidence,
including the PDF digest. This bounded scan does
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
index rows, broken links, and incorrect totals. Both document-health filesystem
walks include the canonical proposal archive; unrelated historical archive
directories retain their existing exclusions.

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
The requested Claude route was unavailable because of its weekly limit.

The subsequent independent council used three fresh Codex contexts for
provenance, tooling, and navigation; the author did not serve as a reviewer.
Before reading the migration report or diff, the navigation reviewer followed
the repository links from `README.md` through all five categories and into
representative bodies. It reached the current core-boundary proposal, registered
outcome-grounding protocol, and historical individuality result with their
status and authority distinctions intact. This is one technical reviewer's
actual traversal, not measured human-reader improvement. Findings and re-review
are recorded on [PR #2350](https://github.com/cirwel/unitares/pull/2350).

The `operations/`, `ontology/`, `evaluation/`, `evaluations/`, and loose docs-root
layout stay outside this pilot. Further moves depend on reader feedback from
this proposal migration, as #2347 specifies.

## Post-merge reader feedback — 2026-09-23

Two fresh Codex contexts read the merged tree at
`49143add79ee630a60549f9ba79f1ca9b62b3423`. One took an integrator's perspective
(R1); the other took a research evaluator's perspective (R2). Neither opened
this migration report, the issue, prior feedback, or the migration diff before
reporting. Both read the required `AGENTS.md` bootstrap, which already points
to some relevant architecture, proposal, and evidence documents. This was
therefore **not a fully unprimed or blind exercise**.

Starting at the root `README.md`, both readers followed explicit links into
representative bodies. They did not use a repository-wide filename search to
find the destinations; scoped text searches within already selected documents
helped locate passages. These are their observed routes, with the shared
`README.md` starting point omitted after the first row:

| Requested category | Observed route |
|---|---|
| Product | Both: `README.md` → `docs/PRODUCT_DEFINITION.md` |
| Architecture | Both reached `docs/UNIFIED_ARCHITECTURE.md`; R1's traversal ended there, while R2 continued to `docs/dev/CANONICAL_SOURCES.md` |
| Current research | Both: `docs/README.md` → `proposals/active/README.md`; R1 continued to `eisv-core-boundary-v0.md`, R2 to `pcalm-primal-dual-governance-v0.md` |
| Frozen protocols | Both: `docs/README.md` → `proposals/registered/README.md` → `eisv-outcome-grounding-stop-rule-v0.md` |
| Historical results | R1: `docs/README.md` → `proposals/README.md` → `archive/README.md` → `eisv-distributional-signal-probe-v0.md`; R2: `docs/README.md` → `EVALUATION_INDEX.md` → that archived probe |

Both reached all five categories without a category-route dead end. They
distinguished current product mechanisms from demonstrated benefit, canonical
architecture summaries from runtime authority, and proposal placement from
permission to execute. R1 identified the core-boundary proposal's later
sequencing restriction; R2 identified PCALM's blocked real-constraint replay.
Both recognized the fixed outcome-read gate and the archived probe's withdrawn
stronger conclusion. Reaching these distinctions sometimes required reading
past a misleading entry summary.

Additional traversals covered the manual and operator runbook, the reviewer
guide, the frozen ablation and corrected power audit, and the review-correction
trace sample. The readers treated dated evidence as a preserved record and
did not infer causal benefit or a population rate from the selected review
sample. R2 used the link already seen in the active index to find the cohort
enrollment ledger because the protocol supplied only its filename.

### Findings and changes

| Reader finding | Follow-up |
|---|---|
| R1: the manual's Docker fast path pointed to a removed `#quickstart` anchor | Link to the root README's current `#install` heading. |
| R1: the core-boundary index emphasized an implementation ceiling before the stricter later sequencing decision | Lead its active-index row with operative §12.1: documentation and the preservation harness may proceed; Stage 1 and Stage 2 runtime work wait, with no waiver. Link to the existing decision and authorization sections; leave the proposal body intact. |
| R1: the archived distributional probe retained a live-sounding June 20 run instruction beneath its June 22 result | Add a dated reading note identifying the old plan and linking the standing stop rule. Preserve the original implementation blockquote and result text. |
| R1: the reviewer guide instructed readers to quote a power figure that the corrected audit had withdrawn | State the unresolved read-specific power limitation; the corrected synthetic procedure does not supply a valid historical power number for that frozen slice. |
| R2: the operations index called the frozen ablation result negative | Describe the selection-adjusted non-detection and unresolved read-specific power, matching the preserved evidence. |
| R2: the docs index told most readers to skip `operations/`, although it holds essential evaluation evidence | Describe the directory's mixed role and point evaluators to the evidence and evaluation guides. |
| R2: original DRAFT/proposed headers made registration state unclear at entry | Keep those historical headers and dated index tags; add operative-section links and explain the cohort protocol's registration-at-merge provenance in the registered index. Its introducing [PR #1785](https://github.com/cirwel/unitares/pull/1785) merged as `4907e926` on 2026-08-21. |
| R2: the cohort protocol supplied no direct enrollment link; R2 relied on the earlier active-index route | Turn that filename into a relative link to the separate active enrollment ledger and record the editorial correction in the required amendment log; change no protocol clause or enrollment record. |

### Decision and limits

Keep the physical layout for now. These routes support the smaller navigation
and inference corrections above; they supply no concrete reason for another
directory move. Issue [#2347](https://github.com/cirwel/unitares/issues/2347)
remains the coordination anchor for later feedback and any inventoried follow-up.
The other documentation directories remain in place.

This is bounded feedback from two technical agent contexts, not a human
usability study, a before/after improvement measurement, a complete link audit,
or live-system validation. Representative reads do not establish that every
document is clear. No scientific analysis was rerun, no frozen read was
refreshed, and no registration, stop rule, enrollment, or runtime authorization
was changed. The cohort protocol edit repairs navigation only and adds a dated
editorial amendment entry; its original header, registration clause, prior
amendment entry, and substantive text remain.
