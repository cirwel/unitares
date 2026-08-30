#!/usr/bin/env python3
"""Report whether every public SDK release surface names the same version.

This is a scheduled reporter, not a pull-request gate.  A version bump on
``master`` is allowed to precede publication, but it must not become invisible:
the weekly workflow turns any disagreement into one deduplicated issue and
closes that issue after the tag, PyPI artifact, and public claims converge.

The four independent surfaces are deliberately read from their authorities:

* release intent: ``agents/sdk/pyproject.toml``;
* release trigger: the local ``sdk-v*`` tag set;
* installable artifact: the PyPI JSON API;
* public promise: the pinned install commands in the compatibility pages.

Usage:
    python3 scripts/ci/sdk_release_sync.py
    python3 scripts/ci/sdk_release_sync.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPI_JSON = "https://pypi.org/pypi/unitares-sdk/json"
SDK_TAG = re.compile(r"sdk-v(\d+\.\d+\.\d+)")
SDK_SURFACES = {
    "COMPATIBILITY.md": r"pip install unitares-sdk==([\d.]+)",
    "docs/public-site/index.md": r"pip install unitares-sdk==([\d.]+)",
}


def declared_version(root: Path = REPO_ROOT) -> str:
    with (root / "agents/sdk/pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def tagged_versions(root: Path = REPO_ROOT) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/tags/sdk-v*",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        match.group(1)
        for tag in result.stdout.splitlines()
        if (match := SDK_TAG.fullmatch(tag))
    }


def advertised_versions(root: Path = REPO_ROOT) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for relative, pattern in SDK_SURFACES.items():
        text = (root / relative).read_text(encoding="utf-8")
        match = re.search(pattern, text)
        versions[relative] = match.group(1) if match else None
    return versions


def pypi_version() -> str:
    # The system Python on macOS may not be wired to the Keychain CA bundle.
    # Use certifi when the repository environment already provides it, while
    # keeping the script dependency-free on GitHub's system Python.
    try:
        import certifi
    except ImportError:  # pragma: no cover - GitHub's OS trust store is enough
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(
        PYPI_JSON,
        headers={"User-Agent": "unitares-sdk-release-sync/1"},
    )
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        payload = json.load(response)
    return str(payload["info"]["version"])


def assess(
    *,
    declared: str,
    published: str,
    tagged: set[str],
    advertised: dict[str, str | None],
) -> dict[str, Any]:
    """Return every disagreement; never collapse an unread surface to clean."""
    issues: list[dict[str, str]] = []

    if declared != published:
        issues.append(
            {
                "code": "declared_registry_mismatch",
                "detail": (
                    f"agents/sdk declares {declared}, but PyPI serves {published}"
                ),
            }
        )
    if declared not in tagged:
        issues.append(
            {
                "code": "declared_tag_missing",
                "detail": f"agents/sdk declares {declared}, but sdk-v{declared} is absent",
            }
        )
    if published not in tagged:
        issues.append(
            {
                "code": "published_tag_missing",
                "detail": f"PyPI serves {published}, but sdk-v{published} is absent",
            }
        )

    for surface, version in sorted(advertised.items()):
        if version is None:
            issues.append(
                {
                    "code": "public_claim_missing",
                    "detail": f"{surface} has no pinned unitares-sdk install command",
                }
            )
        elif version != published:
            issues.append(
                {
                    "code": "public_claim_registry_mismatch",
                    "detail": f"{surface} advertises {version}, but PyPI serves {published}",
                }
            )

    return {
        "status": "synced" if not issues else "drift",
        "synced": not issues,
        "declared_version": declared,
        "published_version": published,
        "expected_tag": f"sdk-v{declared}",
        "tag_present": declared in tagged,
        "advertised_versions": advertised,
        "issues": issues,
    }


def collect(root: Path = REPO_ROOT) -> dict[str, Any]:
    declared = declared_version(root)
    tagged = tagged_versions(root)
    advertised = advertised_versions(root)
    try:
        published = pypi_version()
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "indeterminate",
            "synced": False,
            "declared_version": declared,
            "published_version": None,
            "expected_tag": f"sdk-v{declared}",
            "tag_present": declared in tagged,
            "advertised_versions": advertised,
            "issues": [
                {
                    "code": "registry_unreadable",
                    "detail": f"PyPI version could not be read: {type(exc).__name__}: {exc}",
                }
            ],
        }
    return assess(
        declared=declared,
        published=published,
        tagged=tagged,
        advertised=advertised,
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
            "SDK release sync: "
            f"declared={report['declared_version']} "
            f"published={report['published_version']} "
            f"status={report['status']}"
        )
        for issue in report["issues"]:
            print(f"- {issue['code']}: {issue['detail']}")

    # Drift is reported through the scheduled issue, not by reddening unrelated
    # work. An actual script crash still fails the workflow normally.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
