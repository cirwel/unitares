#!/usr/bin/env python3
"""
Consolidated validation script - validates project docs, layer consistency, and semantic drift.

Usage:
    python3 scripts/validate_all.py                    # Run all validations
    python3 scripts/validate_all.py --docs-only        # Only project docs
    python3 scripts/validate_all.py --semantic-only    # Only semantic drift
    python3 scripts/validate_all.py --verbose          # Verbose output
"""

import sys
from pathlib import Path
from typing import List, Tuple

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Import validators (from archive)
archive_path = Path(__file__).parent / "archive"
sys.path.insert(0, str(archive_path))

try:
    from validate_project_docs import GovernanceDocValidator
    DOCS_VALIDATOR_AVAILABLE = True
except ImportError:
    DOCS_VALIDATOR_AVAILABLE = False
    print("⚠️  Warning: validate_project_docs not available")


class SemanticDriftValidator:
    """Validate semantic consistency - catch deprecated terms in documentation."""

    # Deprecated terms mapping: old_term -> (new_term, context)
    DEPRECATED_TERMS = {
        'risk_score': ('attention_score', 'Renamed Nov 2025 - measures attention/complexity, not risk'),
        'risk score': ('attention score', 'Renamed Nov 2025 - measures attention/complexity, not risk'),
    }

    # Approved exceptions (files that can mention deprecated terms for backward compat docs)
    EXCEPTION_FILES = {
        'docs/migrations/',  # All migration docs are historical
        'docs/sessions/',    # Historical session logs
        'docs/fixes/',       # Historical bug fix records
        'docs/DEPRECATION_REMOVAL_PLAN.md',  # About the deprecation itself
        'docs/guides/BRIDGE_SYNC_AUTOMATION.md',  # Documents the migration itself
        'docs/analysis/SYSTEM_AUDIT_20251208.md',  # Audit report mentions both terms
        'scripts/validate_all.py',  # Contains deprecated terms in config by design
        'scripts/sync_bridge_with_mcp.py',  # Syncs deprecated → current terms
        'docs/CHANGELOG.md',
        'docs/archive/',
    }

    def __init__(self, project_root: Path, verbose: bool = False):
        self.project_root = project_root
        self.verbose = verbose
        self.issues = []

    def is_exception_file(self, file_path: Path) -> bool:
        """Check if file is allowed to contain deprecated terms."""
        rel_path = str(file_path.relative_to(self.project_root))

        # Exact matches
        if rel_path in self.EXCEPTION_FILES:
            return True

        # Prefix matches (e.g., docs/archive/)
        for exception in self.EXCEPTION_FILES:
            if exception.endswith('/') and rel_path.startswith(exception):
                return True

        return False

    def validate_file(self, file_path: Path) -> List[Tuple[str, str, int]]:
        """Check a single file for deprecated terms. Returns list of (term, new_term, line_num)."""
        if self.is_exception_file(file_path):
            return []

        issues = []

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    for old_term, (new_term, context) in self.DEPRECATED_TERMS.items():
                        if old_term in line.lower():
                            issues.append((old_term, new_term, line_num, context))
        except (UnicodeDecodeError, PermissionError):
            pass  # Skip binary files or protected files

        return issues

    def validate_all(self) -> int:
        """Validate all documentation files. Returns exit code."""
        print("="*70)
        print("SEMANTIC DRIFT VALIDATION")
        print("="*70)
        print()

        # Patterns to check
        patterns = [
            'README.md',
            'docs/**/*.md',
            'scripts/*.py',
        ]

        total_issues = 0
        files_with_issues = 0

        for pattern in patterns:
            for file_path in self.project_root.glob(pattern):
                if not file_path.is_file():
                    continue

                issues = self.validate_file(file_path)
                if issues:
                    files_with_issues += 1
                    total_issues += len(issues)

                    rel_path = file_path.relative_to(self.project_root)
                    print(f"⚠️  {rel_path}")

                    for old_term, new_term, line_num, context in issues:
                        print(f"   Line {line_num}: '{old_term}' → should be '{new_term}'")
                        if self.verbose:
                            print(f"   Context: {context}")
                    print()

        print("="*70)
        if total_issues == 0:
            print("✅ No deprecated terms found")
            return 0
        else:
            print(f"⚠️  Found {total_issues} deprecated term(s) in {files_with_issues} file(s)")
            print()
            print("Recommendations:")
            print("1. Update deprecated terms to current terminology")
            print("2. Or add file to EXCEPTION_FILES if it's a migration doc")
            return 1


def main():
    """Run all validation checks."""
    import argparse

    parser = argparse.ArgumentParser(description='Validate project documentation and semantic consistency')
    parser.add_argument('--docs-only', action='store_true', help='Only validate project docs')
    parser.add_argument('--semantic-only', action='store_true', help='Only validate semantic drift')
    parser.add_argument('--report', action='store_true', help='Generate detailed report')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    exit_code = 0

    # Validate project docs
    if not args.semantic_only and DOCS_VALIDATOR_AVAILABLE:
        print("="*70)
        print("PROJECT DOCUMENTATION VALIDATION")
        print("="*70)
        try:
            validator = GovernanceDocValidator(project_root)
            validator.verbose = args.verbose
            doc_exit = validator.validate_all()
            if doc_exit > 0:
                exit_code = max(exit_code, doc_exit)
        except Exception as e:
            print(f"⚠️  Documentation validation failed: {e}")
        print()

    # Validate semantic drift
    if not args.docs_only:
        drift_validator = SemanticDriftValidator(project_root, verbose=args.verbose)
        drift_exit = drift_validator.validate_all()
        if drift_exit > 0:
            exit_code = max(exit_code, drift_exit)
        print()

    if exit_code == 0:
        print("✅ All validations passed!")
    else:
        print(f"❌ Validation failed with exit code {exit_code}")

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
