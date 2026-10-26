# Homebrew release sync

The Homebrew tap is a separate release surface. A UNITARES source tag does not
become a tap update until the release asset, formula transition and installed
Apple Silicon acceptance have each been verified.

## Stable-release path

`.github/workflows/homebrew-release-sync.yml` runs for a published stable
release, or by explicit dispatch for recovery. The `homebrew-release-sync`
environment is the authority boundary and should require operator approval. Its
repository variables and secrets provide the shared `unitares-agents` App
client ID and private key; the minted token is scoped only to
`cirwel/homebrew-unitares`, with contents and pull-request writes.
The job also fails closed unless the repository variable
`UNITARES_HOMEBREW_RELEASE_SYNC_ENABLED` is exactly `true`; enabling it is an
explicit operator rollout decision after the environment protection and App
grant have been checked.

The workflow checks out the exact tag, verifies its commit, creates
`unitares-VERSION.tar.gz` with `git archive` and `gzip -n`, and uploads it only
when the release has no asset by that name. An existing asset must be
byte-identical and is never overwritten. It then runs the tap's offline release
renderer and opens or updates the single `auto/homebrew-release-sync` draft PR.

The renderer accepts only the release-owned
`releases/download/TAG/unitares-VERSION.tar.gz` URL and a full commit plus
SHA-256. It refuses downgrades or missing tracked sources. The v3.5.0 embedded
DATA patch is removed only after the release tree contains the native updater,
service controller, setup/launch entry points and CLI update route. This makes
the removal depend on release contents rather than merely a new tag.

The automation never marks the tap PR ready, applies a merge label, merges it,
or publishes bottles. The draft remains blocked on review and the tap's exact
installed acceptance run.

## Recovery and reruns

Rerunning with the same tag is idempotent: the archive must match and the draft
branch is regenerated from current tap `main`. A different existing asset is a
hard failure and requires investigation. The tap also provides a protected
manual `release-sync.yml` recovery workflow using the same evidence contract.

Apple Silicon bottle build and publication are separate protected workflows in
the tap. The build job has no write credential; publication verifies the exact
successful run and checksums before publishing non-overwriting assets and
opening another draft PR. Billed macOS execution and asset publication remain
explicit operator actions.
