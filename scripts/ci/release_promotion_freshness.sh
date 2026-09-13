#!/usr/bin/env bash
# Fail closed when a release-promotion mutation is re-run against stale evidence.
set -euo pipefail

die() {
  echo "$*" >&2
  exit 1
}

mode="${1:-}"
case "$mode" in
  promote|pin) ;;
  *) die "usage: $0 {promote|pin}" ;;
esac

: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${VERSION:?VERSION is required}"
: "${SOURCE_SHA:?SOURCE_SHA is required}"
: "${DIGEST:?DIGEST is required}"
: "${REGISTRY:?REGISTRY is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"

[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid release tag: $RELEASE_TAG"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid release version: $VERSION"
[[ "$RELEASE_TAG" == "v$VERSION" ]] || die "Release tag $RELEASE_TAG does not match version $VERSION"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || die "Invalid release source SHA: $SOURCE_SHA"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid release digest: $DIGEST"

current_source=$(git rev-list -n 1 "$RELEASE_TAG" 2>/dev/null) || \
  die "$RELEASE_TAG is no longer a readable tag."
if [ "$current_source" != "$SOURCE_SHA" ]; then
  die "$RELEASE_TAG moved from verified source $SOURCE_SHA to $current_source; refusing stale release evidence."
fi

newest=$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
if [ "$newest" != "$RELEASE_TAG" ]; then
  die "$RELEASE_TAG is no longer the newest server tag ($newest); refusing to move release state backwards."
fi

published=$(tr -d '[:space:]' < PUBLISHED_VERSION)
[[ "$published" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid PUBLISHED_VERSION: $published"
if [ "$(printf '%s\n%s\n' "$published" "$VERSION" | sort -V | tail -1)" != "$VERSION" ] || \
  [ "$published" = "$VERSION" ]; then
  die "PUBLISHED_VERSION is now $published; $RELEASE_TAG is not a fresh forward pin."
fi

set +e
git ls-remote --exit-code --heads origin "publish/$RELEASE_TAG" >/dev/null 2>&1
branch_status=$?
set -e
case "$branch_status" in
  0) die "publish/$RELEASE_TAG already exists; refusing to repeat its promotion." ;;
  2) ;;
  *) die "Could not verify whether publish/$RELEASE_TAG already exists." ;;
esac

if ! tag_digest=$(docker buildx imagetools inspect \
  "$REGISTRY/$IMAGE_NAME:$RELEASE_TAG" --format '{{json .Manifest}}' | jq -r .digest); then
  die "Could not resolve the current digest for $RELEASE_TAG."
fi
[[ "$tag_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid current digest for $RELEASE_TAG: $tag_digest"
if [ "$tag_digest" != "$DIGEST" ]; then
  die "$RELEASE_TAG now resolves to $tag_digest, not verified digest $DIGEST."
fi

if ! current_latest=$(docker buildx imagetools inspect \
  "$REGISTRY/$IMAGE_NAME:latest" --format '{{json .Manifest}}' | jq -r .digest); then
  die "Could not resolve the current latest digest."
fi
[[ "$current_latest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid current latest digest: $current_latest"

if [ "$mode" = "promote" ]; then
  : "${PRIOR_LATEST:?PRIOR_LATEST is required for promotion}"
  [[ "$PRIOR_LATEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid pre-approval latest digest: $PRIOR_LATEST"
  if [ "$current_latest" != "$PRIOR_LATEST" ] && [ "$current_latest" != "$DIGEST" ]; then
    die "latest changed during approval from $PRIOR_LATEST to $current_latest; refusing to overwrite it."
  fi
elif [ "$current_latest" != "$DIGEST" ]; then
  die "latest now serves $current_latest, not verified digest $DIGEST; refusing a stale pin."
fi
