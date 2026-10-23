#!/usr/bin/env python3
"""Regenerate the plugin and SDK rows of docs/COMPATIBILITY.md from the release.

The Promote Release `pin` job regenerates the published-server row
(`compat_published_row.py`), but the plugin and SDK rows and the public site's
SDK pins were edited by hand. v3.5.0 shipped plugin v0.4.22 and unitares-sdk
0.6.0 while every one of them still named v0.4.21 and 0.5.0: the pins agreed
with each other, so `test_public_sdk_install_commands_agree` stayed green, and
nothing compared them with what was published (#3044).

Each row now states only what this script establishes from the release:

- SDK: the newest `sdk-v*` tag contained in the release tag, which PyPI must
  serve. Every `unitares-sdk==X` install command, PyPI project link and
  "Python SDK X" label in the advertised files moves to it.
- Plugin: the version the release's changelog entry names with
  `<!-- plugin-bundle-recut: vX.Y.Z -->`, whose GitHub release must exist. A
  release without the marker changed no skills, so the plugin row is left as
  it is.

What changed in each component lives on the pages the rows link. The
Compatibility policy bullet for a release is prose and is not generated;
`tests/test_compat_component_rows.py` requires one for VERSION instead.

Usage:
    python scripts/ci/compat_component_rows.py --version 3.5.0 --release-tag v3.5.0
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPATIBILITY = REPO_ROOT / "docs" / "COMPATIBILITY.md"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"
SDK_PIN_FILES = ("docs/COMPATIBILITY.md", "docs/public-site/index.md")
SDK_ROW_PREFIX = "| `unitares-sdk` |"
PLUGIN_ROW_PREFIX = "| `unitares-governance` plugin |"
PLUGIN_RELEASES = "https://github.com/cirwel/unitares-governance-plugin/releases/tag/"
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SDK_TAG = re.compile(r"^sdk-v(\d+\.\d+\.\d+)$")
_RECUT = re.compile(r"<!--\s*plugin-bundle-recut:\s*(v\d+\.\d+\.\d+)\s*-->")
_SDK_PINS = (
    (re.compile(r"unitares-sdk==\d+\.\d+\.\d+"), "unitares-sdk=={v}"),
    (re.compile(r"pypi\.org/project/unitares-sdk/\d+\.\d+\.\d+/"), "pypi.org/project/unitares-sdk/{v}/"),
    (re.compile(r"Python SDK \d+\.\d+\.\d+\]"), "Python SDK {v}]"),
)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def newest_sdk_version(tags: list[str]) -> str:
    versions = [m.group(1) for m in map(_SDK_TAG.match, tags) if m]
    if not versions:
        raise ValueError("the release tag contains no sdk-v* tag")
    return max(versions, key=_key)


def recut_plugin_version(changelog: str, version: str) -> str | None:
    """The bundle carrier the `## [version]` changelog entry names, if any."""
    heading = re.search(rf"^## \[{re.escape(version)}\][^\n]*$", changelog, re.M)
    if not heading:
        raise ValueError(f"docs/CHANGELOG.md has no [{version}] entry")
    following = re.search(r"^## \[", changelog[heading.end():], re.M)
    section = changelog[heading.end(): heading.end() + following.start() if following else None]
    found = set(_RECUT.findall(section))
    if len(found) > 1:
        raise ValueError(f"the [{version}] entry names several plugin recuts: {sorted(found)}")
    return found.pop() if found else None


def render_sdk_row(sdk: str, server: str) -> str:
    return (
        f"{SDK_ROW_PREFIX} `{sdk}` | Published Python client for resident and custom "
        f"integrations. `sdk-v{sdk}` is the newest SDK tag in server `v{server}`, and "
        f"PyPI serves it. Install it with `pip install unitares-sdk=={sdk}`; use a "
        "server Git tag only when deliberately testing an unreleased SDK build. "
        "What changed: [changelog](CHANGELOG.md). |"
    )


def render_plugin_row(plugin: str, server: str) -> str:
    return (
        f"{PLUGIN_ROW_PREFIX} `{plugin}` | Carries the skills bundle for server "
        f"`v{server}`, whose changelog entry names it as the bundle carrier "
        f"(`plugin-bundle-recut: {plugin}`). The plugin's [release notes]"
        f"({PLUGIN_RELEASES}{plugin}) record the mirrored commit and the "
        "file-by-file comparison. This is bundle parity, not a new end-to-end host "
        "test. The previously recorded host baseline is Claude Code 2.1.220+ and "
        "Codex CLI 0.146.0+. |"
    )


def replace_row(text: str, prefix: str, row: str) -> str:
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(hits) != 1:
        raise ValueError(f"expected exactly one {prefix!r} row, found {len(hits)}")
    lines[hits[0]] = row
    return "\n".join(lines)


def repin_sdk(text: str, sdk: str) -> str:
    for pattern, template in _SDK_PINS:
        text = pattern.sub(template.format(v=sdk), text)
    return text


def _git_tags_merged(release_tag: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "tag", "--merged", release_tag, "-l", "sdk-v*"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.split()


def _url_exists(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False


def regenerate(
    root: Path,
    version: str,
    release_tag: str,
    *,
    sdk_tags: Callable[[str], list[str]] = _git_tags_merged,
    exists: Callable[[str], bool] = _url_exists,
) -> list[str]:
    """Rewrite the rows and pins under root; return what was done."""
    if not _VERSION.match(version) or release_tag != f"v{version}":
        raise ValueError(f"release tag {release_tag!r} does not name version {version!r}")
    done = []

    sdk = newest_sdk_version(sdk_tags(release_tag))
    if not exists(f"https://pypi.org/pypi/unitares-sdk/{sdk}/json"):
        raise ValueError(f"PyPI does not serve unitares-sdk {sdk}")
    compat = root / "docs" / "COMPATIBILITY.md"
    compat.write_text(replace_row(compat.read_text(), SDK_ROW_PREFIX, render_sdk_row(sdk, version)))
    for relative in SDK_PIN_FILES:
        path = root / relative
        path.write_text(repin_sdk(path.read_text(), sdk))
    done.append(f"SDK row and pins are {sdk}")

    plugin = recut_plugin_version((root / "docs" / "CHANGELOG.md").read_text(), version)
    if plugin is None:
        done.append(f"no plugin recut in [{version}]; plugin row unchanged")
    else:
        if not exists(f"{PLUGIN_RELEASES}{plugin}"):
            raise ValueError(f"no published plugin release {plugin}")
        compat.write_text(
            replace_row(compat.read_text(), PLUGIN_ROW_PREFIX, render_plugin_row(plugin, version))
        )
        done.append(f"plugin row is {plugin}")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        done = regenerate(args.root, args.version, args.release_tag)
    except (ValueError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"[compat-component-rows] {exc}", file=sys.stderr)
        return 1
    for line in done:
        print(f"[compat-component-rows] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
