#!/usr/bin/env python3
"""
Version Management - Single Source of Truth

Manages version number across all files in the project.
Prevents version drift by using VERSION file as authority.

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


def get_version() -> str:
    """Get current version from VERSION file."""
    if not VERSION_FILE.exists():
        raise FileNotFoundError(f"VERSION file not found at {VERSION_FILE}")

    return VERSION_FILE.read_text().strip()


def set_version(version: str):
    """Set version in VERSION file."""
    VERSION_FILE.write_text(version + "\n")


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
# the named file, or --update silently no-ops. Historical entries for README.md
# and src/tool_schemas.py were removed after the README was restructured to a
# badge/hero layout and admin.py moved to dynamic SERVER_VERSION.
VERSION_REFERENCES = [
    ("pyproject.toml", [
        (r'version = "([\d.]+)"', r'version = "{version}"'),
    ]),
    ("CITATION.cff", [
        (r'version: "([\d.]+)"', r'version: "{version}"'),
    ]),
    ("README.md", [
        (r'\*\*Status:\*\* v([\d.]+) public overview',
         r'**Status:** v{version} public overview'),
        (r'git clone --branch v([\d.]+) --depth 1',
         r'git clone --branch v{version} --depth 1'),
    ]),
    ("agents/sdk/README.md", [
        (r'unitares@v([\d.]+)#subdirectory=agents/sdk',
         r'unitares@v{version}#subdirectory=agents/sdk'),
        (r'Replace `@v([\d.]+)` with another server release tag',
         r'Replace `@v{version}` with another server release tag'),
    ]),
    ("COMPATIBILITY.md", [
        (r'\| UNITARES server \| `v([\d.]+)`',
         r'| UNITARES server | `v{version}`'),
        (r'aligned with server `v([\d.]+)`',
         r'aligned with server `v{version}`'),
    ]),
]


def check_file_versions(filepath: Path, patterns: List[Tuple[str, str]], expected_version: str) -> list:
    """Check if file has correct version."""
    issues = []

    if not filepath.exists():
        return issues

    with open(filepath) as f:
        content = f.read()
        lines = content.split('\n')

    for pattern, _ in patterns:
        for i, line in enumerate(lines, 1):
            matches = re.findall(pattern, line)
            for match in matches:
                if match != expected_version:
                    issues.append({
                        'file': str(filepath),
                        'line': i,
                        'found': match,
                        'expected': expected_version,
                        'text': line.strip()
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
        all_issues = []

        for doc_file, patterns in VERSION_REFERENCES:
            filepath = PROJECT_ROOT / doc_file
            issues = check_file_versions(filepath, patterns, current_version)
            all_issues.extend(issues)

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
        for doc_file, patterns in VERSION_REFERENCES:
            filepath = PROJECT_ROOT / doc_file
            if update_file_versions(filepath, patterns, current_version):
                updated.append(doc_file)

        if updated:
            print(f"✅ Updated {len(updated)} files to version {current_version}:")
            for doc_file in updated:
                print(f"  - {doc_file}")
        else:
            print("✅ All files already have correct version!")


if __name__ == "__main__":
    main()
