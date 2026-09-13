#!/usr/bin/env python3
"""Report whether the server's source, release, and publication surfaces agree.

This is a scheduled reporter, not a pull-request gate. Releasing, verifying,
promoting, and merging a maintenance tag back are separate human acts, and any
one of them may lag the others on purpose. A lag must not become invisible:
the weekly workflow turns every disagreement into one deduplicated issue and
closes that issue once the surfaces converge.

Each surface is read from its own authority:

* source intent: ``VERSION``;
* verified publication: ``PUBLISHED_VERSION``;
* release trigger: the local ``v*`` tag set, and its ancestry on this checkout;
* release page: the GitHub releases API;
* installable artifact: the GHCR manifest digests of ``latest`` and each tag.

Usage:
    python3 scripts/ci/server_release_sync.py
    python3 scripts/ci/server_release_sync.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "cirwel/unitares"
SERVER_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
GHCR_TOKEN = f"https://ghcr.io/token?scope=repository:{REPOSITORY}:pull&service=ghcr.io"
GHCR_MANIFEST = f"https://ghcr.io/v2/{REPOSITORY}/manifests/{{ref}}"
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _key(version: str) -> tuple[int, int, int]:
    match = SERVER_TAG.fullmatch(f"v{version}")
    if match is None:
        raise ValueError(f"not a server version: {version!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _read_version(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8").strip()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def tagged_versions(root: Path = REPO_ROOT) -> set[str]:
    result = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/tags/v*")
    result.check_returncode()
    return {
        tag[1:] for tag in result.stdout.splitlines() if SERVER_TAG.fullmatch(tag)
    }


def tag_on_head(root: Path, version: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", f"v{version}", "HEAD").returncode == 0


def _ssl_context() -> ssl.SSLContext:
    # The system Python on macOS may not be wired to the Keychain CA bundle.
    try:
        import certifi
    except ImportError:  # pragma: no cover - GitHub's OS trust store is enough
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def released_versions() -> set[str]:
    """Versions with a published (non-draft) GitHub release page."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "unitares-server-release-sync/1",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(RELEASES_API, headers=headers)
    with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
        payload = json.load(response)
    return {
        release["tag_name"][1:]
        for release in payload
        if not release.get("draft") and SERVER_TAG.fullmatch(release.get("tag_name", ""))
    }


def image_digests(refs: list[str]) -> dict[str, str | None]:
    """Anonymous GHCR digest per ref; ``None`` when the ref does not exist."""
    context = _ssl_context()
    with urllib.request.urlopen(GHCR_TOKEN, timeout=30, context=context) as response:
        token = json.load(response)["token"]
    digests: dict[str, str | None] = {}
    for ref in refs:
        request = urllib.request.Request(
            GHCR_MANIFEST.format(ref=ref),
            method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                digests[ref] = response.headers["Docker-Content-Digest"]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            digests[ref] = None
    return digests


def assess(
    *,
    source: str,
    published: str,
    tagged: set[str],
    on_head: set[str],
    released: set[str],
    digests: dict[str, str | None],
) -> dict[str, Any]:
    """Return every disagreement; never collapse an unread surface to clean."""
    issues: list[dict[str, str]] = []
    newest = max(tagged, key=_key) if tagged else None

    if published not in tagged:
        issues.append(
            {
                "code": "published_tag_missing",
                "detail": f"PUBLISHED_VERSION is {published}, but v{published} is absent",
            }
        )
    if published not in released:
        issues.append(
            {
                "code": "published_release_page_missing",
                "detail": f"PUBLISHED_VERSION is {published}, but v{published} has no published release page",
            }
        )
    published_digest = digests.get(f"v{published}")
    if published_digest is None:
        issues.append(
            {
                "code": "published_image_missing",
                "detail": f"PUBLISHED_VERSION is {published}, but ghcr.io/{REPOSITORY}:v{published} is absent",
            }
        )

    latest_digest = digests.get("latest")
    if latest_digest is None:
        issues.append(
            {
                "code": "latest_image_missing",
                "detail": f"ghcr.io/{REPOSITORY}:latest is absent",
            }
        )
    elif published_digest is not None and latest_digest != published_digest:
        matches = sorted(
            (ref for ref, digest in digests.items() if ref != "latest" and digest == latest_digest),
            key=lambda ref: _key(ref[1:]),
        )
        serves = ", ".join(matches) if matches else "an image no checked tag names"
        issues.append(
            {
                "code": "latest_not_published_version",
                "detail": f"latest serves {serves}, but PUBLISHED_VERSION is {published}",
            }
        )

    if newest is not None:
        if _key(newest) > _key(published):
            issues.append(
                {
                    "code": "release_awaiting_publication",
                    "detail": (
                        f"v{newest} is tagged, but PUBLISHED_VERSION is still {published}; "
                        "verify and promote it, or record why it is skipped"
                    ),
                }
            )
        if newest not in on_head:
            issues.append(
                {
                    "code": "newest_tag_not_on_master",
                    "detail": f"v{newest} is not an ancestor of master; forward-merge it before the next release",
                }
            )
        if _key(source) < _key(newest):
            issues.append(
                {
                    "code": "source_version_behind_tag",
                    "detail": f"VERSION is {source}, older than the existing tag v{newest}",
                }
            )

    return {
        "status": "synced" if not issues else "drift",
        "synced": not issues,
        "source_version": source,
        "published_version": published,
        "newest_tag": f"v{newest}" if newest else None,
        "digests": digests,
        "issues": issues,
    }


def collect(root: Path = REPO_ROOT) -> dict[str, Any]:
    source = _read_version(root, "VERSION")
    published = _read_version(root, "PUBLISHED_VERSION")
    tagged = tagged_versions(root)
    on_head = {version for version in tagged if tag_on_head(root, version)}
    newest = sorted(tagged, key=_key)[-5:]
    refs = ["latest", *{f"v{version}" for version in [*newest, published]}]
    try:
        released = released_versions()
        digests = image_digests(sorted(refs))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "indeterminate",
            "synced": False,
            "source_version": source,
            "published_version": published,
            "newest_tag": f"v{newest[-1]}" if newest else None,
            "digests": {},
            "issues": [
                {
                    "code": "registry_unreadable",
                    "detail": f"release pages or GHCR could not be read: {type(exc).__name__}: {exc}",
                }
            ],
        }
    return assess(
        source=source,
        published=published,
        tagged=tagged,
        on_head=on_head,
        released=released,
        digests=digests,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = collect()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            "Server release sync: "
            f"source={report['source_version']} "
            f"published={report['published_version']} "
            f"newest_tag={report['newest_tag']} "
            f"status={report['status']}"
        )
        for issue in report["issues"]:
            print(f"- {issue['code']}: {issue['detail']}")

    # Drift is reported through the scheduled issue, not by reddening unrelated
    # work. An actual script crash still fails the workflow normally.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
