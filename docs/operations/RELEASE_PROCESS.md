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

Prevention is cheaper than errata. Before tagging, check that the changelog
entry actually covers the merge range:

```bash
git log --no-merges --format='%s' vLAST..origin/master \
  | grep -oE '\(#[0-9]+\)$' | tr -d '(#)' | sort -u > /tmp/merged
sed -n '/## \[NEW\]/,/## \[LAST\]/p' docs/CHANGELOG.md \
  | grep -oE '#[0-9]+' | tr -d '#' | sort -u > /tmp/cited
comm -23 /tmp/merged /tmp/cited
```

The omissions that matter most are the ones that qualify a claim the entry
already makes. An entry that cites a new capability but not the change that
bounds it reads as a stronger claim than the code supports.

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
