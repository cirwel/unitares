#!/usr/bin/env python3
"""
Pre-commit hook: Check for incomplete EISV metric reporting.

This script scans code and documentation for patterns that might indicate
incomplete EISV reporting (e.g., "E, I, S" without V).

Usage:
    python3 scripts/check_eisv_completeness.py [--fix]

Exit codes:
    0 - All EISV references are complete
    1 - Found incomplete EISV references
"""

import re
import sys
from pathlib import Path
from typing import List, Tuple

NUMBER_RE = r'[+-]?\d+(?:\.\d+)?'

# Patterns that indicate incomplete EISV reporting. Keep these focused on
# concrete emitted/reported shapes; prose references to the E/I/S manifold are
# valid because V is derived from those axes.
INCOMPLETE_PATTERNS = [
    # "E=... I=... S=..." without V
    (
        rf'E\s*=\s*{NUMBER_RE}\s*[,\s]+I\s*=\s*{NUMBER_RE}\s*[,\s]+'
        rf'S\s*=\s*{NUMBER_RE}(?![\d.])(?!(?:\s*[,\s]+V\s*=))',
        'E=X I=Y S=Z without V=',
    ),
    # Dict/JSON with E, I, S but not V
    (r'\{[^}]*["\']E["\'][^}]*["\']I["\'][^}]*["\']S["\'][^}]*\}(?![^{]*["\']V["\'])', 'Dict with E,I,S but no V'),
]

# Files to skip (known false positives or intentional examples)
SKIP_FILES = {
    'check_eisv_completeness.py',  # This file
    'eisv_format.py',  # Enforcement infrastructure (has examples)
    'eisv_validator.py',  # Enforcement infrastructure (has validation sets)
    'mcp_server_std.py',  # MCP server (complete eisv_labels dicts)
    'dynamics.py',  # Core dynamics (complete State constructors)
    'README.md',  # Documentation (complete examples)
    'EISV_COMPLETENESS.md',  # Documentation about the problem
    'EISV_REPORTING_PROMPT.md',  # System prompt with examples
    'test_eisv_completeness.py',  # Test file with intentional incomplete examples
    'test_agent_update.py',  # Test file with intentional comment
    'MCP_SERVER_RENAME_ROLLBACK_20251201.md',  # Historical doc
}

# Directories to skip
SKIP_DIRS = {
    '.git',
    '.claude',
    '.hypothesis',
    '.mypy_cache',
    '.ruff_cache',
    '.tox',
    '.venv',
    '__pycache__',
    '_build',
    '.pytest_cache',
    'archive',
    'build',
    'dist',
    'htmlcov',
    'node_modules',
    'data',  # Historical data and knowledge entries
    'site-packages',
    'tests',  # Test files often have intentional incomplete examples
    'venv',
}


def should_check_file(filepath: Path) -> bool:
    """Determine if file should be checked for EISV completeness."""
    # Skip if in skip list
    if filepath.name in SKIP_FILES:
        return False

    if any(part in SKIP_DIRS for part in filepath.parts):
        return False

    # Only check relevant file types
    return filepath.suffix in {'.py', '.md', '.txt', '.json'}


def _looks_complete_on_same_line(line: str, match: re.Match[str], pattern_name: str) -> bool:
    if 'without v' in line.lower() or 'missing v' in line.lower():
        return False
    if pattern_name == 'Dict with E,I,S but no V':
        return bool(re.search(r'["\']V["\']', line))
    return bool(re.search(r'\bV\b', line[match.end():]))


def check_file(filepath: Path) -> List[Tuple[int, str, str]]:
    """
    Check file for incomplete EISV references.

    Returns:
        List of (line_number, pattern_name, line_content) tuples
    """
    issues = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                for pattern, pattern_name in INCOMPLETE_PATTERNS:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match and not _looks_complete_on_same_line(line, match, pattern_name):
                        issues.append((line_num, pattern_name, line.strip()))
    except Exception as e:
        print(f"Warning: Could not check {filepath}: {e}", file=sys.stderr)

    return issues


def main():
    """Main entry point."""
    fix_mode = '--fix' in sys.argv

    project_root = Path(__file__).parent.parent.parent
    all_issues = []

    print("🔍 Checking for incomplete EISV metric reporting...")
    print()

    # Scan all relevant files
    for filepath in project_root.rglob('*'):
        if not filepath.is_file():
            continue

        if not should_check_file(filepath):
            continue

        issues = check_file(filepath)
        if issues:
            all_issues.append((filepath, issues))

    # Report results
    if not all_issues:
        print("✅ All EISV references are complete!")
        print("   All metrics (E, I, S, V) reported together.")
        return 0

    print(f"❌ Found {sum(len(issues) for _, issues in all_issues)} incomplete EISV reference(s):\n")

    for filepath, issues in all_issues:
        rel_path = filepath.relative_to(project_root)
        print(f"📄 {rel_path}")
        for line_num, pattern_name, line_content in issues:
            print(f"   Line {line_num}: {pattern_name}")
            print(f"   > {line_content}")
        print()

    if fix_mode:
        print("⚠️  Auto-fix not implemented yet.")
        print("   Please manually update the files to include V.")
    else:
        print("💡 To see suggestions for fixing, run:")
        print("   python3 scripts/check_eisv_completeness.py --fix")

    print()
    print("📖 See docs/guides/EISV_COMPLETENESS.md for correct usage.")

    return 1


if __name__ == '__main__':
    sys.exit(main())
