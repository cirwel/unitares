# Interface-contract release batching v0 — one release per server release, not per PR

Status: Draft v0, design-only, awaiting operator decision
Date: 2026-09-27

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
- Twenty-one commits have moved the constant since 2026-08-26, up to three a
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

1. **No client reads the minor number.** The one negotiating client in the
   workspace, `unitares-resident` (`src/unitares_resident/contract.py`), checks
   the schema family, a minimum of 1.1.0 and a maximum major of 1. The host
   adapter and the SDK do not read the version. Nothing branches on 1.14.0
   vs 1.19.0.
2. **Hash-pinning clients re-pin on the digests, not on the number.** The
   per-capability `input_schema_sha256` values and `surface_sha256` are
   computed from the catalog, in `build_interface_contract()`, and they move
   whenever the surface moves, whatever the version says.

A deployment sees the version change only when it upgrades the server, so the
unit a client can observe is the server release. Nineteen numbers between two
server releases tell a client nothing that one number and the digests do not.

## Proposal

**The interface release advances at most once per server release.** Pull
requests describe their surface change in a fragment. The release cut folds
the fragments in and sets the number. This mirrors the changelog.

1. **Fragments.** A PR that moves the surface adds one file,
   `docs/interface-contract.d/<kind>-<slug>.md`, where `<kind>` is `added`
   (a compatible addition: new capability, parameter or accepted value),
   `changed` (a description or a validation tightening that moves digests) or
   `breaking` (needs a new schema family or a deprecation window, per the
   existing rule). The body is the clause as it would appear in the release
   list: what moved, which input digests and the surface digest move, and
   what does not change for an existing caller. A unique file name cannot
   conflict.
2. **The artifact stays exact.** `docs/interface-contract.v1.json` is still
   regenerated in the PR, so `test_checked_in_contract_matches_runtime` keeps
   proving the checked-in contract matches the catalog and the digests stay
   honest. Only its `version` stays put. The digests are recomputed from
   the catalog, so a later PR that regenerates on a moved base does not
   conflict on the version line and gets the correct digests by running the
   generator.
3. **Unreleased marker.** While fragments are pending, the negotiated
   contract says so without inventing a number:
   `version` stays at the last released number, and a new field,
   `unreleased_changes: <count>`, reports how many surface changes sit on top
   of it. A client that only reads `version` is unaffected. An operator
   running `master` can see that the surface has moved since the release.
   This is an additive field, which is itself a compatible change, and it is
   the last one made the old way.
4. **Release cut.** Next to `changelog_assemble.py`, a
   `contract_assemble.py` does three things. It chooses the increment: a
   minor increment if any fragment is `added` or `changed`, and a stop if any
   is `breaking`, because that needs an operator decision. It appends the
   fragments to the release list and the history comment under the new
   number, and it deletes them. The release PR regenerates the artifact once.
5. **Guard.** A check in the Release Seams workflow, modeled on
   `scripts/ci/changelog_direct_edit.py`, fails an ordinary PR that changes
   `INTERFACE_CONTRACT_VERSION` or the release paragraph, and fails a PR
   whose regenerated artifact moves `surface_sha256` without adding a
   fragment. Release trees, where `VERSION` is untagged, are exempt, the same
   test the changelog guard uses.

## What does not change

- The schema family, `unitares.interface-contract.v1`, and its rule that a
  shape change needs a new family.
- The compatibility rule: renaming, removing or changing the meaning of a
  capability still needs a new family or a deprecation window.
- Per-capability and surface digests, their computation and the parity tests.
- The history already written: 1.2.0 to 1.20.0 stay as recorded.

## Migration

- Pick the release that closes the per-PR era. Master currently negotiates
  1.20.0 while the published server says 1.1.0, so the next server release
  ships 1.20.0 (or 1.21.0 if #2490 lands first under the old rule) as its
  negotiated interface. The fragment rule applies to PRs opened after this
  proposal merges.
- An open PR that already claims a number, #2490 today, is renumbered once by
  its owner under the old rule, or converted to a fragment. That is the
  owner's call, per the branch-ownership rule
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
- **Only generate the artifact at release.** This would remove the conflict
  but also the PR-time proof that the checked-in contract matches the
  catalog, which is the check that caught F12.
- **Number by date (`1.20260927.0`).** Two PRs on one day still collide, and
  it changes the meaning of minor for every client.

## Decision needed

1. Adopt one interface release per server release, via fragments. The
   recommendation is yes.
2. Add the `unreleased_changes` field, or leave `master` silent until
   release. The recommendation is to add it: an operator on `master` should
   not see 1.20.0 describe a surface that has moved since.
3. Whether #2490 lands under the old rule first. That is the owner's call;
   the proposal works either way.

No implementation starts until the operator decides. The implementation
would be one PR with the assembler, the guard, the field, the README for the
fragment directory and their tests.
