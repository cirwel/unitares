# Release Process

This checklist separates source delivery, runtime deployment, and package
publication. A Git tag does not by itself prove that a service restarted or a
package reached a registry. `VERSION` identifies the source tree;
`PUBLISHED_VERSION` identifies the most recent server release whose tag,
release page, and container were verified. Public install pins follow
`PUBLISHED_VERSION`, so a source-version bump cannot advertise missing artifacts.
The root README carries no pin: its install command reads `PUBLISHED_VERSION`
from master at install time, so merging the publication PR updates it.
Merging a change under `docs/public-site/` does deploy GitHub Pages; it does
not publish a server tag or container.

Per-release notes and errata live in `docs/releases/`; the
v2.22.1 maintenance release, cut from `release/2.22` rather than master, is
recorded in [`../releases/2.22.1.md`](../releases/2.22.1.md).

## Cadence

Cut a server release roughly every **50 merged PRs or 1–2 weeks** of active
work, whichever comes first, or immediately after a breaking change, security
fix, or user-blocking defect lands. Waiting for 100+ PRs turns changelog
citation triage and skill-attestation pruning into a multi-hour audit.

## Server release checklist

The releasing agent performs every step in this section and in *SDK release*:
the version choice, changelog, review, merge, tag, release page, Promote
Release dispatch, the pin pull request and the `sdk-v*` tag (operator decision,
2026-10-17, KG `2026-10-18T05:54:48.538440+00:00`). The operator's one act per
release is approving the `release-promotion` environment, which GitHub enforces
with a required reviewer who must be a user or team. The agents' GitHub App
holds `actions: write` for the dispatch (operator decision, 2026-10-18, KG
`2026-10-18T10:37:46.085398+00:00`).

1. **[Releasing agent] Start from current `master` on a clean branch.**
   Confirm no open PR is already preparing the release
   (`unitares-github --read-only -- gh pr list --search "release vX.Y.Z" --state open`).
2. **[Releasing agent] Run `make release-prep PART=patch|minor|major`.**
   This runs `python3 scripts/ops/prepare_release.py --part <part>`, which:
   - folds pending [`docs/changelog.d/`](../changelog.d/README.md) fragments into
     `docs/CHANGELOG.md` via `scripts/dev/changelog_assemble.py` and deletes the
     consumed fragment files,
   - bumps `VERSION` and propagates it across all source-version targets via
     `scripts/ops/version_manager.py --bump <part>` and `--update` (leaving
     `PUBLISHED_VERSION` untouched until promotion),
   - stamps `## [X.Y.Z] - YYYY-MM-DD` in [`docs/CHANGELOG.md`](../CHANGELOG.md)
     and `date-released` in [`CITATION.cff`](../../CITATION.cff),
   - runs `scripts/client/check-skill-freshness.sh --prune 3` and
     `scripts/client/check-skill-freshness.sh` with `UNITARES_PROJECTS_ROOT`
     resolved across worktrees, failing closed if any skill reports
     `cited source(s) absent from this checkout, not covered` (when the prune
     would push the release diff past 300 files, land the prune first in its own
     `chore(release)` PR so GitHub's 300-file merge-queue fingerprint API does
     not refuse the PR),
   - runs `scripts/ci/changelog_coverage.py`,
     `scripts/ci/release_series_drift.py`, and
     `scripts/ops/version_manager.py --check`,
   - checks the release range for container build input changes with
     `git log --oneline vLAST..origin/master -- Dockerfile 'requirements*.txt' constraints.txt docker-compose.yml db/postgres elixir/lease_plane elixir/unitares_sdk scripts/demo/quick_demo.py scripts/demo/coordination_demo.py scripts/ci/check_mcp_tool_surface.py scripts/ci/plugin_file_lease_proof.py scripts/ci/kg_graph_proof.py docker-compose.full.yml .github/workflows/docker-quickstart.yml`,
   - writes draft release notes (`release-notes-X.Y.Z.md`), PR body
     (`pr-body-X.Y.Z.md`), and any uncited PR candidates (`uncited-prs-X.Y.Z.txt`)
     under `.git/release-prep/` so untracked `.md` files never trip
     `check_doc_sprawl.py`.
3. **[Releasing agent] Resolve remaining release-seam items and compatibility prose.**
   - For every uncited PR listed by `changelog_coverage.py`, either fold a bullet
     into `## [X.Y.Z]` or add a reviewed one-line exemption comment
     (`<!-- changelog-coverage-exempt: #NNNN <reason> -->`).
   - Add the `- vX.Y.Z` prose bullet to [`docs/COMPATIBILITY.md`](../COMPATIBILITY.md)
     (`tests/test_compat_component_rows.py` requires it).
   - If `release_series_drift.py` reports `skills/` changes, mirror them with
     `scripts/dev/sync-plugin-skills.sh`, cut the plugin release, and record
     `<!-- plugin-bundle-recut: vX.Y.Z -->` in `## [X.Y.Z]`.
4. **[Releasing agent] Run validation and open the release PR.**
   Run `./scripts/dev/test-cache.sh` and `make validate`. If step 2 reported
   `NEEDS-QUICKSTART`, ensure the Docker quickstart runs on a Docker-capable host
   (or passes in the required CI workflow) and record the result in the PR body.
   Open the release PR, obtain independent cross-family review on the release
   notes, version choice, and compatibility claims, and merge via the merge queue.
5. **[Releasing agent] Tag the merged release commit immediately.**
   Run `python3 scripts/ops/prepare_release.py --after-merge --sha <merged-sha>`
   to print the exact post-merge commands with the SHA and version filled in.
   Create and push the annotated tag on the merged release commit when signing is
   available:
   `git tag -s vX.Y.Z <merged-sha> -m "UNITARES vX.Y.Z" && unitares-github -- git push origin vX.Y.Z`.
   Do not replace an existing public tag merely to add a signature. A tag an agent
   creates names that agent as its tagger, not the Git identity configured on the
   host.
6. **[Releasing agent] Publish the GitHub release page (triggers `Publish Container` and Zenodo).**
   Run `unitares-github -- gh release create vX.Y.Z --title "vX.Y.Z" --notes-file .git/release-prep/release-notes-X.Y.Z.md`.
   Publishing the release page automatically triggers `Publish Container`
   (`publish-container.yml`, `on: release: types: [published]`), which builds
   and attests `linux/amd64` and `linux/arm64` images for `ghcr.io/cirwel/unitares`,
   `ghcr.io/cirwel/unitares-lease-plane`, and `ghcr.io/cirwel/unitares-postgres`,
   and archives the tagged tree on Zenodo.
   - If `Publish Container` must be dispatched manually instead, select the same
     tag as both workflow ref and input:
     `gh workflow run publish-container.yml --ref vX.Y.Z -f ref=vX.Y.Z`.
     This keeps the attestation certificate bound to that tag and source commit.
     A historical tag that predates the workflow cannot satisfy that binding;
     leave it version-tag-only and cut a patch release if it must become
     `latest`, rather than weakening promotion verification.
7. **[Releasing agent] Verify anonymous GHCR visibility and dispatch `Promote Release`.**
   Once `Publish Container` succeeds, run:
   ```bash
   python3 scripts/ops/prepare_release.py --check-ghcr --version X.Y.Z
   ```
   A newly created GHCR package defaults to private, which causes `promote-release.yml`
   to fail at `verify` when pulling anonymously. If `--check-ghcr` reports `private`,
   the operator flips visibility to public at the printed package settings URL.
   Once all three packages report `public`, dispatch promotion:
   ```bash
   unitares-github -- gh workflow run promote-release.yml -f tag=vX.Y.Z
   ```
8. **[Operator] Approve the `release-promotion` environment.**
   In the GitHub Actions run for `Promote Release`, after `verify` and
   `native-bootstrap` pass, approve the `release-promotion` environment. The
   `promote` job re-verifies freshness and points `ghcr.io/cirwel/unitares:latest`
   at the verified multi-arch index digest without rebuilding, and the `pin` job
   pushes branch `publish/vX.Y.Z` updating `PUBLISHED_VERSION`, install pins, and
   `docs/COMPATIBILITY.md` rows (`compat_published_row.py` and
   `compat_component_rows.py`).
9. **[Releasing agent] Open and merge the `publish/vX.Y.Z` pin PR.**
   Open the PR from `publish/vX.Y.Z` (using the command in the `Promote Release`
   run summary or `--after-merge` output), run independent review, and merge it
   through the merge queue. Merging `publish/vX.Y.Z` updates `PUBLISHED_VERSION`
   on `master`, which automatically triggers `publish-mcp-registry.yml` to
   publish `io.github.cirwel/unitares` to the MCP Registry
   (`registry.modelcontextprotocol.io`). Finish with `git closeout --strict`.

## Why the guards exist

- **Two-version split (`VERSION` vs `PUBLISHED_VERSION`):** `VERSION` moves on
  the release PR so the tagged tree and its `docker-compose.yml` name the new
  `vX.Y.Z` images. `PUBLISHED_VERSION` moves only in step 8–9 after `vX.Y.Z`
  images and provenance are verified in GHCR, so public install commands never
  point at an unbuilt tag. The lease-plane and database images are pinned by exact
  version tag (`vX.Y.Z`) and never tagged `latest`.
- **Native Bootstrap Acceptance:** The required `test (3.12)` check and
  `Promote Release` both run `native-bootstrap.yml`, which extracts the candidate
  source archive, runs bare-metal bootstrap against PostgreSQL with AGE/pgvector,
  verifies migration checksums from 062 onward, and tests interrupted-migration
  recovery via `apply_migrations.py`. Homebrew packaging requires its own
  installed-artifact acceptance in the tap using `scripts/ci/bootstrap_acceptance.py`.
- **Release range anchoring (`changelog_coverage.py` and `release_series_drift.py`):**
  Both **Release Seams** gates stand down when `VERSION` is already tagged. While
  `VERSION` is untagged, they anchor their commit range at the release commit that
  updated `VERSION` / `docs/CHANGELOG.md` (failing closed to `HEAD` on dirty trees)
  and resolve the baseline tag via `git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*'`.
  This prevents an ordinary PR that merges to `master` before the operator pushes
  `vX.Y.Z` from reddening `master`, and `.github/workflows/release-seams.yml`
  also runs on `release/**` maintenance branches.
- **Changelog fragments and coverage (`docs/changelog.d/`):** Ordinary PRs add a
  fragment under `docs/changelog.d/<section>-<slug>.md` and are blocked from
  editing `## [Unreleased]` directly (`scripts/ci/changelog_direct_edit.py`).
  `changelog_coverage.py` checks the first-parent walk since the previous tag,
  excluding only `chore(release)` commits and dependabot bumps whose diff proves
  inert (manifest/lockfile/digest/workflow-pin only, same-major for runtime
  manifests). Every other PR must be cited or carry a one-line exemption from
  the closed set (`release-chore`, `superseded`, `no-user-effect`,
  `covered-elsewhere`):
  ```markdown
  <!-- changelog-coverage-exempt: #1788 release-chore -->
  ```
- **Independent series drift (`release_series_drift.py` and `published_claims.py`):**
  `release_series_drift.py` blocks a server release when `agents/sdk/` or `skills/`
  changed since its last tag without a forward version bump or
  `<!-- plugin-bundle-recut: vX.Y.Z -->` declaration. `published_claims.py` runs
  on every PR to verify that any SDK version advertised on a public surface has
  a matching `sdk-v*` tag.
- **Scheduled sync monitors:** Mondays at 09:07 UTC (`sdk-release-sync.yml`) and
  09:27 UTC (`server-release-sync.yml`), scheduled workflows compare in-tree
  versions, tags, release pages, PyPI/GHCR artifacts, and `PUBLISHED_VERSION`,
  opening a single deduplicated `ci-finding` issue with the exact next command
  when a release was left half-cut.

## Promoting a verified container (manual fallback)

Publication creates the versioned artifact. Promotion moves `latest` to an
already verified artifact without rebuilding it. The Promote Release workflow
performs the steps below behind the `release-promotion` environment; they are
written out so the operation can be audited, or run by hand if the workflow is
unavailable (for example, on a repository's very first release before `latest`
exists).

One-time setup: create the `release-promotion` environment with at least one
required reviewer (user or team). Docker documents this single-index operation
as a [carbon copy](https://docs.docker.com/reference/cli/docker/buildx/imagetools/create/).

Record the release tag, source commit, multi-architecture **index digest**, both
platforms, SBOM and provenance verification in the release evidence. Confirm
this is the intended current release, not an older release being backfilled.
With registry write access, substitute that recorded digest below:

```bash
docker buildx imagetools create --prefer-index=false --tag ghcr.io/cirwel/unitares:latest ghcr.io/cirwel/unitares@sha256:<verified-index-digest>
docker buildx imagetools inspect ghcr.io/cirwel/unitares:latest
```

Require the resulting `latest` digest to equal the recorded index digest before
advancing installation pins. Do not rebuild to promote, and do not run the manual
fallback while a `Promote Release` run is pending or in progress.

## Correcting a published release

Treat a published tag as immutable; a release body is not. When a published
release is found to be wrong or incomplete, correct the prose and leave the tag
alone.

1. Record the correction in `docs/releases/<version>-errata.md`: what was
   omitted, what was miscited, what was overstated, and what evidence has since
   been recorded. See [`2.18.0-errata.md`](../releases/2.18.0-errata.md).
2. Update the release body in place with `gh release edit vX.Y.Z --notes-file`.
   Append the correction under an `## Errata (recorded YYYY-MM-DD)` heading and
   leave the original text above it unchanged.
3. Never move, delete, or re-cut a published tag to absorb a correction. If code
   must change, cut a patch release.

## SDK release

The SDK has its own version series in `agents/sdk/pyproject.toml`.

1. **[Releasing agent] Open and merge the SDK version-bump PR.**
   Bump `version` in `agents/sdk/pyproject.toml`, confirm the intended server
   compatibility range in [COMPATIBILITY.md](../COMPATIBILITY.md), run
   `pytest agents/sdk/tests -q`, and merge the PR to `master` before advertising
   the new SDK version on any public install surface (`published_claims.py` blocks
   advertising an untagged SDK version).
2. **[Releasing agent] Tag `sdk-vX.Y.Z` at the merged `master` commit.**
   Confirm the `pypi` GitHub environment and PyPI trusted publisher are
   registered for `cirwel/unitares` / `publish-sdk.yml`. Create and push
   `sdk-vX.Y.Z` at the merged commit (`publish-sdk.yml` refuses a tag whose
   version differs from `agents/sdk/pyproject.toml`).
3. **[Releasing agent] Publish the GitHub release for `sdk-vX.Y.Z` (triggers PyPI publish).**
   Run `unitares-github -- gh release create sdk-vX.Y.Z --title "unitares-sdk vX.Y.Z" --notes "..."`
   (or dispatch `publish-sdk.yml`).
4. **[Operator] Approve the `pypi` environment if gated, then verify PyPI.**
   Verify the published distribution and test `pip install unitares-sdk==X.Y.Z`
   in a clean environment before updating public install pins. PyPI names and
   released versions are permanent public state; never publish from an unmerged
   PR or reuse a released version number.

## Runtime deployment

Production deployment is a separate operator action. Follow
[`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md), record the deployed commit, restart
only the affected services, and verify the live health/version endpoints. Do not
describe a merged PR or release tag as deployed without that observation.

## Cross-surface delivery check

Generate the [operator surface report](deployment-inventory.md#one-report-across-publication-and-running-installations)
after publishing. Check the affected entries in
[distribution-surfaces.toml](distribution-surfaces.toml), including the Homebrew
tap and cirwel.org installation copy. An available website is not evidence that
its content reflects the release; run its named content check. Keep unresolved
or unreachable surfaces explicitly unverified in the release handoff. Package
acceptance and a running machine's health are separate observations.
