# Homebrew release sync

The Homebrew tap is a separate release surface. A UNITARES source tag does not
become a tap update until the release asset, formula transition and installed
Apple Silicon acceptance have each been verified.

## Stable-release path

`.github/workflows/homebrew-release-sync.yml` runs for a published stable
release, or by explicit dispatch for recovery. The `homebrew-release-sync`
environment should require operator approval for each privileged job. Its
environment-scoped variables and secrets provide the shared `unitares-agents` App
client ID and private key; the minted token is scoped only to
`cirwel/homebrew-unitares`, with contents and pull-request writes.
The workflow also fails closed unless the repository variable
`UNITARES_HOMEBREW_RELEASE_SYNC_ENABLED` is exactly `true`; enabling it is an
explicit operator rollout decision after the environment protection, its
environment-scoped `UNITARES_HOMEBREW_RELEASE_SYNC_ARMED=true` secret, and the
App grant have been checked. Before arming either privileged stage:

- Store `UNITARES_AGENT_APP_PRIVATE_KEY` only in the protected environment;
  remove any copy from repository or organization secret scope. Otherwise the
  renderer refuses after source publication, leaving no tap PR.
- Configure the environment's deployment rules to permit the release tags
  (`v*` tag rule), with required reviewers. A branch-only policy blocks the
  mandatory tag-context runs; restrict deployment refs rather than relying on
  approval alone.

The workflow checks out the exact tag, verifies that the workflow event's ref
and SHA match that tag and commit, and creates `unitares-VERSION.tar.gz` with
`git archive` and `gzip -n`. It signs a GitHub artifact attestation and verifies
the fixed repository/workflow, tag, source and signer commit before uploading
the archive, and only when the release has no asset by that name. An existing asset must be
byte-identical and is never overwritten. It then runs the tap's offline release
renderer and opens or updates the single `auto/homebrew-release-sync` draft PR.
Three runners separate source signing/publication, tap rendering, and draft-PR
publication. The renderer has read-only tokens and no protected environment; it
refuses to execute if the shared App key is visible from repository/organization
secrets. Put that key only in the protected environment. The publisher consumes
an immutable patch artifact from this run and applies only the two formula files
and `SOURCES.json` as regular files. It executes no tap scripts and refuses a tap
base that changed since rendering. The shared App remains a privileged operator
credential across repositories; these jobs do not claim to isolate an independently
compromised, operator-approved workflow elsewhere that holds that key.

With required environment reviewers, source publication and draft-PR publication
have separate approvals. The source archive may already be published while the
second approval is pending; the tap PR does not exist until that stage succeeds.
That transition advances both `Formula/unitares.rb` and the release-coupled
`Formula/unitares-lease-plane.rb` from the same immutable asset.

The renderer accepts only the release-owned
`releases/download/TAG/unitares-VERSION.tar.gz` URL and a full commit plus
SHA-256. It refuses downgrades or missing tracked sources. The v3.5.0 embedded
DATA patch is removed only after the release tree contains the native updater,
service controller, setup/launch entry points and CLI update route. This makes
the removal depend on release contents rather than merely a new tag. The tap's
source manifest also names the reviewed upstream pull request that establishes
the backport floor. The workflow resolves that PR's actual squash-merge commit,
requires it to be an ancestor of the release commit, and refuses an open or
unmerged PR. File presence alone therefore cannot retire the temporary patch.

The automation never marks the tap PR ready, applies a merge label, merges it,
or publishes bottles. The draft remains blocked on review and the tap's exact
installed acceptance run.

## Recovery and reruns

A full rerun ("Re-run all jobs") or a new dispatch at the same tag is
idempotent: the archive must match and the draft branch is regenerated from
current tap `main`. "Re-run failed jobs" reuses the earlier renderer outputs;
if tap `main` advanced, publication keeps refusing until all jobs are rerun or
a new tagged dispatch renders the new base. A different existing asset is a
hard failure and requires investigation. The tap also provides a protected
manual `release-sync.yml` recovery workflow using the same evidence contract.
Dispatch the upstream workflow at the release tag, for example
`gh workflow run homebrew-release-sync.yml --ref "$TAG" -f tag="$TAG"`.
That tag must contain the attesting workflow. Dispatching a newer workflow on
`master` while checking out an older tag cannot certify the older source context.
The tap recovery path refuses unsigned source archives; use a new release
containing the workflow rather than claiming retrospective provenance.

Apple Silicon bottle build and publication are separate protected workflows in
the tap. The build job can write attestations but cannot publish releases or
change branches. Publication verifies the fixed signer workflow, exact commit,
successful run and certificate run/attempt, plus every file in the signed
manifest, before minting its publication token. It publishes the four bottles
and dependency inventory/evidence as non-overwriting assets and opens another
draft PR. Billed macOS execution and asset publication remain explicit operator
actions. See the [consumer verification instructions](../install/HOMEBREW_SETUP.md).
Provenance establishes origin and integrity, not benign behavior or reproducibility.
