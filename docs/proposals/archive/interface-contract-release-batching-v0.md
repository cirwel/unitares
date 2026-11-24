# Interface-contract release batching v0 — one release per server release, not per PR

Status: Draft v1, design-only, awaiting operator decision
Date: 2026-09-30 (initial measurement: 2026-09-27)

## Problem

Every pull request that moves the advertised tool surface writes the next
interface release number into three shared files:

- `src/interface_contract.py`: the `INTERFACE_CONTRACT_VERSION` constant and a
  history comment above it;
- `docs/INTERFACE_CONTRACT.md`: the version on line 3 and a new clause
  appended to the single paragraph that lists every release;
- `docs/interface-contract.v1.json`: the generated artifact, whose `version`
  and `surface_sha256` sit at the top.

Two open PRs that both move the surface therefore both claim the same next
number, and whichever merges second is CONFLICTING in all three files. The
loser renumbers, rewrites its prose to say "numbered after 1.N (#M)",
regenerates the artifact, and gets a fresh review, because resolving the
conflict changes the blob of a file in its diff and re-keys
`scripts/dev/review_gate.py`. During that review the next PR merges.

This is the same mechanism `docs/changelog.d/` removed for the changelog
(`scripts/dev/changelog_assemble.py`), on a second surface.

Measured on 2026-09-27 against `origin/master` at `3177b94ef`:

- The published server, v2.22.1 (tagged 2026-09-08), negotiates interface
  **1.1.0**. `master` negotiates **1.20.0**. Nineteen interface releases have
  landed since that tag, and none has shipped in a published server release.
- Twenty first-parent commits have moved the constant since 2026-08-26, up to three a
  day (2026-09-08, 2026-09-25). Three entries already carry a "numbered after"
  note because their PR lost a race: 1.17.0 after #2435, 1.19.0 after #2470
  and 1.20.0 after #2472.
- #2489 merged as 1.20.0 at 00:19 MDT. #2490, drafted as 1.21.0, went
  CONFLICTING at the same moment. `git merge-tree` shows the conflict in all
  three files.

## What the number is for

`docs/INTERFACE_CONTRACT.md` says what the number means: `version` is the
negotiated interface release that a client reads from
`list_tools(lite=true).interface_contract`. Compatible additions advance it.
A moved `input_schema_sha256` is what tells a hash-pinning client to re-pin.

Two facts limit what a per-PR number buys:

1. **No client distinguishes supported minor releases above its floor.** The one negotiating client in the
   workspace, `unitares-resident` (`src/unitares_resident/contract.py`), checks
   the schema family, the full version tuple against a minimum of 1.1.0,
   and a maximum major of 1. It rejects 1.0.9 and accepts 1.1.0. The host
   adapter and the SDK do not read the version. Nothing branches on 1.14.0
   vs 1.19.0.
2. **Hash-pinning clients re-pin on the digests, not on the number.** The
   per-capability `input_schema_sha256` values and `surface_sha256` are
   computed from input schemas in `build_interface_contract()` and move
   when those schemas move, whatever the version says. Top-level tool
   descriptions can change without moving either digest. The current
   negotiation summary returns only the aggregate digest; obtaining current
   per-capability pins requires the full-contract retrieval added below.

A deployment sees the version change only when it upgrades the server, so the
unit a client can observe is the server release. Nineteen numbers between two
server releases tell a client nothing that one number and the digests do not.

## Proposal

**The interface release advances at most once per server release.** Pull
requests describe their surface change in a fragment. The release cut folds
the fragments in and sets the number. This mirrors the changelog.

1. **Fragments.** A PR that moves the surface adds one file,
   `src/interface_contract_fragments/<kind>-<slug>.md`, where `<kind>` is `added`
   (a compatible addition: new capability, parameter or accepted value),
   `changed` (a description or advertised-schema correction that does not
   shrink the runtime-accepted input domain) or
   `breaking` (needs a new schema family or a deprecation window, per the
   existing rule). A tighter maximum or removed accepted enum value is
   breaking even if the schema digest changes. The body is the clause as it would appear in the release
   list: what moved, which input digests and the surface digest move, and
   what does not change for an existing caller. A unique file name cannot
   conflict. The directory is a Python package whose Markdown files are
   included as package data; installed builds read it through
   `importlib.resources`, not a presumed repository checkout. Wheel and sdist
   tests must verify that source and installed builds count the same fragments.
2. **Separate the runtime contract from the released artifact.** Ordinary
   PRs do not regenerate `docs/interface-contract.v1.json`. It describes the
   last interface release. `build_interface_contract()` still computes the
   exact current catalog and digests on every build, including unreleased
   builds. The implementation must add an explicit read-only full-contract
   retrieval option to `list_tools`, including capabilities and their
   `input_schema_sha256` values. The default summary remains compact.
   Hash-pinning clients opt into the full result and re-pin from that live
   catalog, rather than a frozen checked-in artifact. Source, installed and
   remote-transport tests must verify these pins equal the actual schemas.
   This new retrieval option is an additive migration change under the
   existing numbering rule. PR-time tests must compare that result with the actual
   transport catalog, independently recomputing per-capability and surface
   hashes; they must never substitute the frozen artifact for the live catalog.
   The existing exact artifact/runtime parity assertion becomes a release
   check, required whenever no fragments remain and on every release cut.
   Simply dropping that assertion without the transport and digest checks is
   not this proposal. Neither the digest nor a fragment count is written to a
   shared artifact by ordinary PRs, so concurrent surface PRs do not conflict
   on those fields.
3. **Unreleased marker.** While fragments are pending, the negotiated
   contract says so without inventing a number:
   `version` stays at the last released number, and a new field,
   `unreleased_changes: <count>`, reports how many surface changes sit on top
   of it. The count is computed from installed fragment resources each time
   the runtime contract is built, never cached in a tracked generated file.
   Two independently added fragments therefore count as two after merging.
   It is a count of fragment files, not capabilities or deployment activity.
   A client that only reads `version` is unaffected. An operator
   running `master` can see that the surface has moved since the release.
   This is an additive field, which is itself a compatible change, and it is
   the last one made the old way.
4. **Release cut.** Next to `changelog_assemble.py`, a
   `contract_assemble.py` does three things. It chooses the increment: a
   minor increment if any fragment is `added` or `changed`, and a stop if any
   is `breaking`, because that needs an operator decision. It appends the
   fragments to the release list and the history comment under the new
   number, and it deletes them. In the same release candidate it removes
   their associated behavior-change attestations from the active attestation
   directory; Git history retains both as provenance. It also retires active
   no-behavior-change attestations, which are PR-time evidence rather than
   permanent release inputs. Tests must prove a cut consumes linked records
   together, leaves no dangling fragment references, and rejects a partial cut.
   The release PR regenerates the artifact once,
   with `unreleased_changes: 0`, and must pass exact artifact/runtime parity.
   Release cuts claim the release surface so no two assemblers write it at once.
5. **Guard.** A check in the Release Seams workflow, modeled on
   `scripts/ci/changelog_direct_edit.py`, fails an ordinary PR that changes
   `INTERFACE_CONTRACT_VERSION` or the release paragraph, and fails a PR
   that edits the released artifact. It compares the entire base and head negotiated contract, excluding only
   release bookkeeping (`version` and `unreleased_changes`). This includes
   capabilities and their digests, advertisement, transports, lifecycle
   envelopes and limits: `surface_sha256` alone hashes only capabilities.
   The comparison also includes the actual advertised top-level tool
   descriptions and MCP annotations, which the contract builder and its
   digests currently omit. An annotation-only change to readOnlyHint,
   destructiveHint or idempotentHint requires a fragment even when no schema
   or handler changes; negative-control tests must prove that requirement.
   Every semantic contract or advertised-description change requires a
   matching fragment. Catalog comparison is only the structural signal:
   behavior can change while schemas, digests and descriptions stay identical.
   The implementation must maintain an explicit capability-to-handler-path
   manifest and require compatibility tests plus a fragment for changes to
   those behavior-bearing paths. A behavior-only fragment names the affected
   capability, prior and new behavior, and the compatibility tests proving
   preservation or an implemented deprecation transition. Such evidence is
   a corresponding semantic change for the converse check below; an unchanged
   catalog cannot reject a justified behavior-only fragment. The manifest
   includes shared dispatch, validation and authorization paths and its
   coverage is tested against the registered capability catalog.
   The manifest must also cover transitive service, configuration and migration
   dependencies, not only handler entrypoints: for example, changes in
   `src/knowledge_graph.py` can alter `knowledge` behavior without changing its
   handler. Catalog entry coverage alone is insufficient. Until dependency
   coverage can be demonstrated, every changed repository path conservatively
   requires either a
   capability-linked fragment and compatibility evidence or an explicit
   no-behavior-change attestation with compatibility evidence. The fallback
   includes runtime-loaded assets such as `skills/*/SKILL.md`, package resources,
   dependency declarations and deployment/build inputs; source-root enumeration
   is not complete dependency coverage. A path may be excluded only when a
   tested manifest classification establishes that it cannot affect the deployed
   capability behavior or output. Unclassified paths retain the fallback.
   Negative-control
   tests must change a transitive service, a configuration default and a
   migration, a runtime-loaded skill and an unclassified asset while preserving
   the catalog, and prove each fails without the
   required evidence. Mechanical
   edits require an explicit no-behavior-change attestation with compatibility
   tests, rather than silently bypassing this signal. Tests must include a
   handler-only meaning change whose catalog remains byte-identical, both
   with and without the required fragment and compatibility evidence.
   Attestations are unique tracked JSON files at
   `.github/interface-contract-attestations/<slug>.json`, validated against a
   versioned schema. Each records `kind` (`behavior_change` or
   `no_behavior_change`), exact repository-relative `paths`, affected
   `capabilities`, a nonempty `rationale`, `fragment` (required for a behavior
   change, null otherwise), and nonempty `checks` referencing allowlisted
   static-check IDs or pytest node IDs under `tests/`. No arbitrary shell
   command is accepted. Ordinary documentation, test and CI edits can name
   relevant static checks rather than an unrelated runtime compatibility test.
   The guard derives changed paths from the base/head diff, requires coverage
   of every non-metadata path, validates capability names against both catalogs,
   and checks fragment references and consistency with the dependency manifest.
   New or modified attestation JSON is metadata only after schema validation;
   it cannot exempt guard, schema or manifest implementation changes themselves.
   Existing attestations unchanged in this PR do not cover new edits. Wildcards,
   missing paths, unknown checks or capabilities, dangling fragment references,
   and conflicting attestations fail closed. A no-behavior-change assertion
   cannot override an observed catalog/advertisement change or a required
   deprecation gate, and remains subject to the ordinary independent PR review.
   CI executes the selected checks through trusted workflow code and produces
   an evidence artifact containing the actual candidate SHA, attestation content
   hashes, check IDs and results. The guard accepts only successful evidence
   produced in that run for that exact candidate, including the merged candidate;
   committed claims that tests ran, stale artifacts and skipped checks do not
   count. Negative controls cover each refusal and a valid documentation-only
   attestation. This is a proposed implementation requirement, not an existing
   source of validated evidence.
   A breaking fragment cannot authorize a breaking merge:
   the PR-time gate must verify the required new schema family or an
   implemented deprecation transition before that change reaches master.
   An operator authorization is recorded explicitly and cannot silently
   redefine v1 compatibility; deferring the decision to release assembly
   would allow master to reject previously valid inputs under the old family. Conversely,
   a new fragment without a corresponding semantic change fails. Reverting
   an unreleased change must remove or amend its original fragment, not
   retain a stale release claim. Associated attestations must be removed or
   updated in the same revert so their fragment references and changed-path
   claims remain consistent; tests must cover both directions and
   change-then-revert sequences. The check also
   runs on the merged candidate so an automatically merged branch is checked
   against the catalog it will actually ship. The assembler's release PR is
   the explicit exception: it changes the release files, consumes fragments,
   and passes exact artifact/runtime parity. The implementation must define
   and test how that release exception is authorized, rather than treating any
   ordinary version edit as permission to bypass the guard.

## What does not change

- The schema family, `unitares.interface-contract.v1`, and its rule that a
  shape change needs a new family.
- The compatibility rule: renaming, removing or changing the meaning of a
  capability still needs a new family or a deprecation window.
- Per-capability and surface digests and their computation. Their comparison
  with the transport catalog remains a PR-time invariant; released-artifact
  equality becomes the release invariant described above.
- Every interface release already recorded at the migration cut stays recorded.

## Migration

- Pick the implementation release that closes the per-PR era, preserving the
  interface number reached under the existing rule at that cut. The 1.20.0
  master / 1.1.0 published comparison above is the September 27 snapshot,
  not an instruction to restore an older interface number. The fragment rule
  begins only after its implementation lands; merging this design document
  changes no rule, generator, packaging or test.
- An open PR that already claims a number may be renumbered once by its
  owner under the old rule only if it merges before the implementation cut.
  Every such PR still open after the cut must be converted to a fragment by
  its owner; the new guard rejects old-style version, release-paragraph and
  artifact edits. There is no grandfathering exception. Migration tests must
  cover rejection of a stale numbered PR after the cut and acceptance of its
  fragment conversion. The branch edits remain the owner's responsibility,
  per the branch-ownership rule
  (`docs/operations/github-workflow-conventions.md`, *Branch ownership*).
- `RELEASE_PROCESS.md` step 2 gains one command, next to
  `changelog_assemble.py`.

## Alternatives considered

- **Keep per-PR numbers, rebase the loser.** This is the status quo. Each
  collision costs one renumber, one regeneration and one fresh review, and
  the rate is rising.
- **Derive the number from the digest.** Unique by construction, but a
  client can't compare two digests by age, and it breaks `unitares-resident`'s
  semver check.
- **Keep regenerating the artifact in every PR while freezing its version.**
  Rejected: concurrent PRs still collide on `surface_sha256`. If their identical
  `unreleased_changes: 1` edits merge automatically, the resulting artifact
  says one while two fragments exist. This retains the original review race
  and can break parity on master. Draft v0 proposed this; v1 replaces it with
  the separated runtime/release checks above.
- **Only generate the artifact at release without replacement PR checks.**
  Rejected: that would discard the transport/catalog parity that caught F12.
  The proposed release-only artifact requires independent PR-time transport
  and digest checks and installed-resource tests.
- **Number by date (`1.20260927.0`).** Two PRs on one day still collide, and
  it changes the meaning of minor for every client.

## Decision needed

1. Adopt one interface release per server release, via fragments. The
   recommendation is yes.
2. Add the `unreleased_changes` field, or leave `master` silent until
   release. The recommendation is to add it: an operator on `master` should
   not see the last released number describe a moved surface without a marker.
3. Adopt the separated PR-time runtime/transport checks and release-time
   artifact check, including fragment packaging. Freezing only the version
   while retaining per-PR artifact regeneration does not solve the collision.

No implementation starts until the operator decides. The implementation
would be one PR with the assembler, the guard, the field, the README for the
fragment directory and their tests.
