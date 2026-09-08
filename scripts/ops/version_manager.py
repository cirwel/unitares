#!/usr/bin/env python3
"""
Source and Published Version Management

VERSION is the source-version authority. PUBLISHED_VERSION independently pins
verified public artifacts, so preparing a release cannot advance install links
before the tag, GitHub release, and container exist.

Usage:
    python3 scripts/ops/version_manager.py                # Show current version
    python3 scripts/ops/version_manager.py --check        # Check for mismatches
    python3 scripts/ops/version_manager.py --bump minor   # Bump version (major|minor|patch)
    python3 scripts/ops/version_manager.py --update       # Update all version references
"""

import re
import sys
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
PUBLISHED_VERSION_FILE = PROJECT_ROOT / "PUBLISHED_VERSION"


def get_version() -> str:
    """Get current version from VERSION file."""
    if not VERSION_FILE.exists():
        raise FileNotFoundError(f"VERSION file not found at {VERSION_FILE}")

    return VERSION_FILE.read_text().strip()


def set_version(version: str):
    """Set version in VERSION file."""
    VERSION_FILE.write_text(version + "\n")


def get_published_version() -> str:
    """Read the operator-verified public release pin; never infer publication."""
    return PUBLISHED_VERSION_FILE.read_text(encoding="utf-8").strip()


def bump_version(part: str) -> str:
    """Bump version number (major, minor, or patch)."""
    current = get_version()
    major, minor, patch = map(int, current.split('.'))

    if part == 'major':
        major += 1
        minor = 0
        patch = 0
    elif part == 'minor':
        minor += 1
        patch = 0
    elif part == 'patch':
        patch += 1
    else:
        raise ValueError(f"Invalid part: {part}. Use major, minor, or patch.")

    new_version = f"{major}.{minor}.{patch}"
    set_version(new_version)
    return new_version


# Files and patterns to check/update.
# Keep this list tight — every entry runs on --check and must match real text in
# the named file. A stale pattern is itself a check failure so release-facing
# references cannot silently fall out of version management.
VERSION_REFERENCES = [
    ("pyproject.toml", [
        (r'version = "([\d.]+)"', r'version = "{version}"'),
    ]),
    ("CITATION.cff", [
        (r'version: "([\d.]+)"', r'version: "{version}"'),
    ]),
    ("README.md", [
        (r'\*\*Status:\*\* v([\d.]+)\.',
         r'**Status:** v{version}.'),
    ]),
    ("COMPATIBILITY.md", [
        (r'\| UNITARES server \| `v([\d.]+)`',
         r'| UNITARES server | `v{version}`'),
        # Historical plugin bundle evidence is pinned to its inspected tag.
        # A source bump must not rewrite that claim into a new verification.
    ]),
]

# Installation pins move only after RELEASE_PROCESS step 8 verifies the public
# artifacts and the operator updates PUBLISHED_VERSION in a follow-up PR.
PUBLISHED_VERSION_REFERENCES = [
    ("README.md", [
        (r'git clone --branch v([\d.]+) --depth 1',
         r'git clone --branch v{version} --depth 1'),
    ]),
    ("docs/manual/02-install.md", [
        (r'git clone --branch v([\d.]+) --depth 1',
         r'git clone --branch v{version} --depth 1'),
    ]),
    ("docs/public-site/index.md", [
        (r'\[server v([\d.]+)\]\(https://github.com/cirwel/unitares/releases/tag/v[\d.]+\)',
         r'[server v{version}](https://github.com/cirwel/unitares/releases/tag/v{version})'),
        (r'git clone --branch v([\d.]+) --depth 1',
         r'git clone --branch v{version} --depth 1'),
        (r'ghcr\.io/cirwel/unitares:v([\d.]+)',
         r'ghcr.io/cirwel/unitares:v{version}'),
    ]),
    ("agents/sdk/README.md", [
        (r'unitares@v([\d.]+)#subdirectory=agents/sdk',
         r'unitares@v{version}#subdirectory=agents/sdk'),
        (r'Replace `@v([\d.]+)` with another server release tag',
         r'Replace `@v{version}` with another server release tag'),
    ]),
    ("COMPATIBILITY.md", [
        (r'\| Published server/container \| `v([\d.]+)`',
         r'| Published server/container | `v{version}`'),
    ]),
]


def version_reference_groups():
    """Keep source-version claims and verified install pins on their own clocks."""
    return (
        (get_version(), VERSION_REFERENCES),
        (get_published_version(), PUBLISHED_VERSION_REFERENCES),
    )


def check_file_versions(filepath: Path, patterns: List[Tuple[str, str]], expected_version: str) -> list:
    """Check if file has correct version."""
    issues = []

    if not filepath.exists():
        return issues

    with open(filepath) as f:
        content = f.read()
        lines = content.split('\n')

    for pattern, _ in patterns:
        pattern_matched = False
        for i, line in enumerate(lines, 1):
            matches = re.findall(pattern, line)
            if matches:
                pattern_matched = True
            for match in matches:
                if match != expected_version:
                    issues.append({
                        'file': str(filepath),
                        'line': i,
                        'found': match,
                        'expected': expected_version,
                        'text': line.strip()
                    })

        if not pattern_matched:
            issues.append({
                'file': str(filepath),
                'line': 0,
                'found': '<configured pattern did not match>',
                'expected': expected_version,
                'text': pattern,
            })

    return issues


def update_file_versions(filepath: Path, patterns: List[Tuple[str, str]], new_version: str) -> bool:
    """Update version references in file."""
    if not filepath.exists():
        return False

    with open(filepath) as f:
        content = f.read()

    original = content
    for pattern, replacement in patterns:
        content = re.sub(pattern, replacement.format(version=new_version), content)

    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
        return True
    return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Manage project version")
    parser.add_argument('--check', action='store_true', help='Check for version mismatches')
    parser.add_argument('--update', action='store_true', help='Update all version references')
    parser.add_argument('--bump', choices=['major', 'minor', 'patch'], help='Bump version')
    args = parser.parse_args()

    current_version = get_version()

    if args.bump:
        new_version = bump_version(args.bump)
        print(f"✅ Version bumped: {current_version} → {new_version}")
        print(f"   Don't forget to run: python3 scripts/ops/version_manager.py --update")
        sys.exit(0)

    if args.check or not args.update:
        # Check mode
        print(f"Current version: {current_version}")
        print(f"Verified published version: {get_published_version()}")
        all_issues = []

        for expected, references in version_reference_groups():
            for doc_file, patterns in references:
                filepath = PROJECT_ROOT / doc_file
                all_issues.extend(check_file_versions(filepath, patterns, expected))

        if all_issues:
            print(f"\n❌ Found {len(all_issues)} version mismatches:")
            for issue in all_issues:
                print(f"  {issue['file']}:{issue['line']}")
                print(f"    Found: {issue['found']}, Expected: {issue['expected']}")
            sys.exit(1)
        else:
            print("✅ All version references are correct!")
            sys.exit(0)

    if args.update:
        # Update mode
        updated = []
        for expected, references in version_reference_groups():
            for doc_file, patterns in references:
                filepath = PROJECT_ROOT / doc_file
                if update_file_versions(filepath, patterns, expected):
                    updated.append(doc_file)

        if updated:
            print(f"✅ Updated source references to {current_version}; "
                  f"public install pins to {get_published_version()}:")
            for doc_file in sorted(set(updated)):
                print(f"  - {doc_file}")
        else:
            print("✅ All files already have correct version!")


if __name__ == "__main__":
    main()
