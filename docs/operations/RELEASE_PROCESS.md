# Release Process

This checklist separates source delivery, runtime deployment, and package
publication. A Git tag does not by itself prove that a service restarted or a
package reached a registry.

## Server release

1. Start from current `master` with a clean named branch and no active surface
   claim. Run `git prepr --scope "release vX.Y.Z"` before the release PR.
2. Run `make version-bump PART=patch|minor|major`, update
   [`docs/CHANGELOG.md`](../CHANGELOG.md) and the `date-released` field in
   [`CITATION.cff`](../../CITATION.cff), then review every generated version
   change. `VERSION` remains the authority.
3. Run `./scripts/dev/test-cache.sh` and `make validate`. When container build
   inputs changed **anywhere in the release range** (`vLAST..master`), not merely
   in the release PR's own diff, also run the documented Docker quickstart on a
   Docker-capable host and record the command/result in the release PR. Check
   the range with
   `git log --oneline vLAST..origin/master -- Dockerfile 'requirements*.txt' docker-compose.yml db/postgres scripts/demo/quick_demo.py`. Delegating this check
   to CI counts only when the required CI workflow actually contains and passes
   that quickstart; a green Python/package test job is not equivalent evidence.
4. Merge the release PR only after required CI is green and every applicable
   release-specific check from step 3 has recorded evidence.
5. From the merged release commit, create a signed annotated tag when signing is
   available: `git tag -s vX.Y.Z -m "UNITARES vX.Y.Z"`. Do not replace an
   existing public tag merely to add a signature.
6. Push the tag and create the GitHub release with user impact, compatibility,
   migrations, evidence changes, known limits, and rollback notes.
7. The `Publish Container` workflow publishes `linux/amd64` and `linux/arm64`
   images to GHCR with an SBOM and build-provenance attestation. For a release
   created before that workflow existed, dispatch it manually with the existing
   release tag. Leave `publish_latest` off when backfilling an older release.
8. Verify the release page, container digest, attestation, and clean closeout.

## Correcting a published release

A tag is immutable; a release body is not. When a published release is found to
be wrong or incomplete, correct the prose and leave the tag alone.

1. Record the correction in `docs/releases/<version>-errata.md`: what was
   omitted, what was miscited, what was overstated, and what evidence has since
   been recorded. See [`2.18.0-errata.md`](../releases/2.18.0-errata.md).
2. Update the release body in place with `gh release edit vX.Y.Z --notes-file`.
   Append the correction under an `## Errata (recorded YYYY-MM-DD)` heading and
   leave the original text above it unchanged — the value of an errata is the
   difference between what was claimed and what is true, and overwriting the
   body destroys that.
3. Never move, delete, or re-cut a published tag to absorb a correction. If code
   must change, cut a patch release.

Prevention is cheaper than errata, and both preventive checks now run in CI as
the **Release Seams** workflow. Run them locally before opening the release PR:

```bash
python scripts/ci/changelog_coverage.py     # entry vs the merge range
python scripts/ci/release_series_drift.py   # SDK and skills vs their own tags
```

Both stand down unless the tree is a release — `VERSION` names a version with no
tag yet — so they cost ordinary PRs nothing.

`changelog_coverage.py` lists every merged pull request the entry does not name.
It reads the first-parent walk and accepts both a squash subject's trailing
`(#N)` and a merge commit's `Merge pull request #N`, and it **fails** when any
first-parent change carries neither rather than measuring the smaller set it can
parse. It prints provenance rather than a ratio, because the previous version's
ratio was copied verbatim onto an immutable release page and was wrong.

Fold each listed change into the entry. If one genuinely does not belong, declare
it inside the entry, one per line, with a reason from a closed set
(`release-chore`, `superseded`, `no-user-effect`, `covered-elsewhere`):

```markdown
<!-- changelog-coverage-exempt: #1788 release-chore -->
```

Exemptions live in the entry the release ships, so they are reviewable in the
diff and expire with it. The script does not print a paste-ready declaration:
that made declaring one feel like satisfying the tool rather than making a claim.

The omissions that matter most are the ones that qualify a claim the entry
already makes. An entry that cites a new capability but not the change that
bounds it reads as a stronger claim than the code supports.

`release_series_drift.py` covers the series `VERSION` does not reach: the SDK
under `agents/sdk/` with its own `sdk-v*` tags, and `skills/`, which is mirrored
into the separately tagged plugin. It blocks unless the declared version is
demonstrably **newer** than the published one — equal, older, malformed, and
unreadable all leave a consumer unable to reach the change. Because it cannot
see the plugin's tags, the skills series clears on a declaration in the entry:

```markdown
<!-- plugin-bundle-recut: v0.4.14 -->
```

`published_claims.py` runs on every pull request, not only a release. It checks
that a version a public page advertises has a tag that publishes it. On
2026-08-21 `COMPATIBILITY.md` advertised `pip install unitares-sdk==0.2.1` under
a column reading "Published Python client" while PyPI served 0.2.0, held there by
a required test that compared the page to `agents/sdk/pyproject.toml`. A guard
must resolve the same referent the reader will resolve; repo-internal artifacts
are a model of it, never the thing itself.

## SDK release

The SDK has its own version series in `agents/sdk/pyproject.toml`.

1. Confirm the intended server range in [COMPATIBILITY.md](../../COMPATIBILITY.md).
2. Run `pytest agents/sdk/tests -q` and build both wheel and source distribution.
3. Confirm the `pypi` GitHub environment and PyPI trusted publisher are
   registered for repository `cirwel/unitares`, workflow `publish-sdk.yml`, and
   environment `pypi`.
4. Create `sdk-vX.Y.Z` at the merged commit. The workflow refuses a tag whose
   version differs from the SDK `pyproject.toml`.
5. Verify the PyPI files and test `pip install unitares-sdk==X.Y.Z` in a clean
   environment before changing the README from the version-pinned Git install.

PyPI names and released versions are permanent public state. Never publish from
an unmerged PR or reuse a released version number.

## Runtime deployment

Production deployment is a separate operator action. Follow
[`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md), record the deployed commit, restart
only the affected services, and verify the live health/version endpoints. Do not
describe a merged PR or release tag as deployed without that observation.
