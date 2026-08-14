#!/usr/bin/env python3
"""
Documentation Tool Count Updater - Prevent Drift

Automatically updates tool count references in documentation files.
Run this after adding/removing tools to keep docs in sync.

Usage:
    python3 scripts/diagnostics/update_docs_tool_count.py --check  # Check for mismatches
    python3 scripts/diagnostics/update_docs_tool_count.py --update # Update all docs
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_tool_count() -> int:
    """Count via the runtime registry; degrade to 0 when deps are missing.

    Imported at call time (not module level) so the doc-validation CI runner,
    which installs no project dependencies, warns and skips instead of
    crashing — and so tests can patch the counter on its defining module.
    """
    from scripts.diagnostics.count_tools import get_total_count

    try:
        return get_total_count()
    except ModuleNotFoundError as exc:
        print(f"WARNING: Tool count unavailable ({exc})", file=sys.stderr)
        return 0

# Files that reference tool count
DOC_FILES = [
    "README.md",
    "docs/guides/START_HERE.md",
]

# Patterns to match and replace
PATTERNS = [
    # "43 tools" or "47 tools"
    (r'\*\*(\d+) tools\*\*', r'**{count} tools**'),
    (r'(\d+) tools\)', r'{count} tools)'),
    (r'count: (\d+)\)', r'count: {count})'),
    (r'(\d+)\+ tools', r'{count} tools'),  # Replace "38+ tools" with exact count
]


def check_file(filepath: Path, actual_count: int) -> list:
    """Check if file has correct tool count."""
    issues = []

    if not filepath.exists():
        return issues

    with open(filepath) as f:
        content = f.read()
        lines = content.split('\n')

    for pattern, _ in PATTERNS:
        for i, line in enumerate(lines, 1):
            matches = re.findall(pattern, line)
            for match in matches:
                found_count = int(match.rstrip('+'))
                if found_count != actual_count:
                    issues.append({
                        'file': str(filepath),
                        'line': i,
                        'found': found_count,
                        'expected': actual_count,
                        'text': line.strip()
                    })

    return issues


def update_file(filepath: Path, actual_count: int) -> bool:
    """Update tool count in file."""
    if not filepath.exists():
        return False

    with open(filepath) as f:
        content = f.read()

    original = content
    for pattern, replacement in PATTERNS:
        content = re.sub(pattern, replacement.format(count=actual_count), content)

    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
        return True
    return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Update tool count in documentation")
    parser.add_argument('--check', action='store_true', help='Check for mismatches (no changes)')
    parser.add_argument('--update', action='store_true', help='Update all documentation files')
    args = parser.parse_args()

    actual_count = load_tool_count()
    print(f"Actual tool count: {actual_count}")

    if actual_count == 0:
        if args.update:
            # Never write a zero count into the docs.
            print("❌ Refusing to update docs with a zero tool count")
            sys.exit(1)
        print("Tool count check skipped (no tools detected in this environment)")
        sys.exit(0)

    if args.check or not args.update:
        # Check mode
        all_issues = []
        for doc_file in DOC_FILES:
            filepath = PROJECT_ROOT / doc_file
            issues = check_file(filepath, actual_count)
            all_issues.extend(issues)

        if all_issues:
            print(f"\n❌ Found {len(all_issues)} mismatches:")
            for issue in all_issues:
                print(f"  {issue['file']}:{issue['line']}")
                print(f"    Found: {issue['found']}, Expected: {issue['expected']}")
                print(f"    {issue['text']}")
            sys.exit(1)
        else:
            print("\n✅ All documentation has correct tool count!")
            sys.exit(0)

    if args.update:
        # Update mode
        updated = []
        for doc_file in DOC_FILES:
            filepath = PROJECT_ROOT / doc_file
            if update_file(filepath, actual_count):
                updated.append(doc_file)

        if updated:
            print(f"\n✅ Updated {len(updated)} files:")
            for doc_file in updated:
                print(f"  - {doc_file}")
        else:
            print("\n✅ All files already up to date!")


if __name__ == "__main__":
    main()
