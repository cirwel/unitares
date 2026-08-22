#!/usr/bin/env python3
"""Check public claims against published artifacts, not against repo intent.

Every version guard in this repository binds a claim to something inside the
repository: `version_manager.py` binds documentation to `VERSION`, and
`test_install_operator_contract.py` used to bind `COMPATIBILITY.md` to the
version declared in `agents/sdk/pyproject.toml`.

That is not the same property. On 2026-08-21, #1800 bumped the declared SDK
version to 0.2.1. The bind-to-intent test then *required* COMPATIBILITY.md to
advertise `pip install unitares-sdk==0.2.1` under a column reading "Published
Python client" while PyPI still served 0.2.0. The command errored. Every check
was green, because every check was comparing the repository to itself.

This one compares an advertised version to the tag that publishes it. The tag is
the right reference: `publish-sdk.yml` fires on `sdk-v*` and refuses a tag whose
version differs from `pyproject.toml`, so a tagged version is one that was
actually pushed to PyPI, while a declared version is only one someone intends to
push. Checking the registry directly would be better still, but a gate that
needs the network to pass fails for reasons that have nothing to do with the
claim.

Requires tags: run it where the checkout has them (fetch-depth: 0). It fails
rather than skips when they are missing, because "I could not check" and "the
claim is fine" must not produce the same exit code.

Usage:
    python scripts/ci/published_claims.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Surface -> pattern capturing the advertised version.
SDK_SURFACES = {
    "COMPATIBILITY.md": r"pip install unitares-sdk==([\d.]+)",
    "docs/public-site/index.md": r"pip install unitares-sdk==([\d.]+)",
}
SDK_TAG = re.compile(r"sdk-v(\d+\.\d+\.\d+)")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def published_sdk_versions() -> set[str]:
    out = _git("for-each-ref", "--format=%(refname:short)", "refs/tags/sdk-v*")
    return {m.group(1) for m in (SDK_TAG.fullmatch(t) for t in out.splitlines()) if m}


def advertised_sdk_versions() -> dict[str, str]:
    found: dict[str, str] = {}
    for relative, pattern in SDK_SURFACES.items():
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        match = re.search(pattern, text)
        if match:
            found[relative] = match.group(1)
    return found


def main() -> int:
    published = published_sdk_versions()
    if not published:
        print("[published-claims] no sdk-v* tags visible — cannot verify any "
              "advertised SDK version. Fetch tags (fetch-depth: 0) and re-run.",
              file=sys.stderr)
        return 1

    advertised = advertised_sdk_versions()
    if not advertised:
        print("[published-claims] no public surface advertises an SDK install "
              "command; nothing to check", file=sys.stderr)
        return 1

    failures = []
    for relative, version in sorted(advertised.items()):
        if version in published:
            print(f"[published-claims] {relative}: unitares-sdk {version} is published")
        else:
            failures.append((relative, version))

    distinct = set(advertised.values())
    if len(distinct) > 1:
        failures.append(("<surfaces disagree>", ", ".join(sorted(distinct))))

    if not failures:
        return 0

    print()
    for relative, version in failures:
        if relative == "<surfaces disagree>":
            print(f"[published-claims] public surfaces advertise different "
                  f"versions: {version}")
        else:
            print(f"[published-claims] {relative} advertises unitares-sdk "
                  f"{version}, which has no sdk-v{version} tag")
    print()
    print(f"published SDK versions: {', '.join(sorted(published))}")
    print()
    print("Either cut the tag so the claim becomes true, or change the surface")
    print("to name a version that is actually published. Do not advertise a")
    print("version the repository merely intends to release.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
