#!/usr/bin/env python3
"""
Validate markdown files against standardization guidelines.

Checks:
- ISO date format (YYYY-MM-DD)
- Date metadata labels (**Last Updated:**)
- Relative links
- Header usage (minimal)
- Code block language tags

Usage:
    python3 scripts/validate_markdown_formatting.py [file...]
    python3 scripts/validate_markdown_formatting.py --all  # Check all files
    python3 scripts/validate_markdown_formatting.py --fix  # Auto-fix issues
"""

import sys
import re
from pathlib import Path
from typing import List, Dict, Tuple
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Approved files (exempt from strict checks)
APPROVED_FILES = {
    'README.md',
    'docs/CHANGELOG.md',
    'docs/guides/START_HERE.md',
    'docs/dev/CANONICAL_SOURCES.md',
    'docs/UNIFIED_ARCHITECTURE.md',
}

# Archive directories (exempt from checks - historical records)
ARCHIVE_DIRS = {'archive', 'Archive'}

# ISO date pattern
ISO_DATE_PATTERN = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')

# Non-ISO date patterns (to detect)
NON_ISO_DATE_PATTERNS = [
    re.compile(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b'),
    re.compile(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}\b'),
    re.compile(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b'),
]

# Date metadata patterns
DATE_METADATA_PATTERNS = [
    re.compile(r'\*\*Last Updated:\*\*', re.IGNORECASE),
    re.compile(r'\*\*Date:\*\*', re.IGNORECASE),
    re.compile(r'\*\*Created:\*\*', re.IGNORECASE),
]

# Absolute path patterns (should be relative)
ABSOLUTE_PATH_PATTERN = re.compile(r'\]\(/(?!docs/|scripts/)')

# Code block without language
CODE_BLOCK_WITHOUT_LANG = re.compile(r'```\s*$', re.MULTILINE)


class MarkdownValidator:
    """Validate markdown files against standards."""
    
    def validate(self, filepath: Path) -> Dict[str, List[str]]:
        """Validate a markdown file and return issues."""
        issues: Dict[str, List[str]] = {
            'non_iso_dates': [],
            'missing_date_metadata': [],
            'absolute_paths': [],
            'code_blocks_without_lang': [],
            'agent_attribution': [],
        }
        
        try:
            content = filepath.read_text()
            lines = content.split('\n')
            
            # Check for non-ISO dates
            for i, line in enumerate(lines, 1):
                for pattern in NON_ISO_DATE_PATTERNS:
                    if pattern.search(line):
                        issues['non_iso_dates'].append(f"Line {i}: {line.strip()[:60]}")
            
            # Check for date metadata (should use **Last Updated:**)
            has_date_metadata = any(pattern.search(content) for pattern in DATE_METADATA_PATTERNS)
            if not has_date_metadata and filepath.name not in {'README.md'} and str(filepath) != 'docs/CHANGELOG.md':
                # Check first 20 lines for date metadata
                header_section = '\n'.join(lines[:20])
                if not any(pattern.search(header_section) for pattern in DATE_METADATA_PATTERNS):
                    issues['missing_date_metadata'].append("No date metadata found in header")
            
            # Check for absolute paths (except docs/ and scripts/)
            for i, line in enumerate(lines, 1):
                if ABSOLUTE_PATH_PATTERN.search(line):
                    issues['absolute_paths'].append(f"Line {i}: {line.strip()[:60]}")
            
            # Check for code blocks without language
            code_blocks = CODE_BLOCK_WITHOUT_LANG.finditer(content)
            for match in code_blocks:
                line_num = content[:match.start()].count('\n') + 1
                issues['code_blocks_without_lang'].append(f"Line {line_num}: Code block without language tag")
            
            # Check for agent attribution in headers (should be minimal)
            header_section = '\n'.join(lines[:30])
            if re.search(r'\*\*(Created|Updated):\*\*.*by.*agent', header_section, re.IGNORECASE):
                issues['agent_attribution'].append("Agent attribution in header (tracked in git, not needed in doc)")
            
        except Exception as e:
            issues['errors'] = [f"Error reading file: {e}"]
        
        return issues
    
    def fix_issues(self, filepath: Path, issues: Dict[str, List[str]]) -> bool:
        """Attempt to auto-fix issues. Returns True if file was modified."""
        try:
            content = filepath.read_text()
            modified = False
            
            # Fix non-ISO dates (basic conversion)
            for pattern in NON_ISO_DATE_PATTERNS:
                matches = list(pattern.finditer(content))
                for match in reversed(matches):  # Reverse to preserve positions
                    date_str = match.group()
                    # Try to parse and convert to ISO
                    try:
                        # This is a simplified conversion - may need manual review
                        # For now, just flag it
                        pass
                    except:
                        pass
            
            # Add date metadata if missing
            if issues.get('missing_date_metadata') and not any(pattern.search(content) for pattern in DATE_METADATA_PATTERNS):
                # Add after title if present
                lines = content.split('\n')
                if lines[0].startswith('#'):
                    # Insert after title
                    today = datetime.now().strftime('%Y-%m-%d')
                    lines.insert(1, f'**Last Updated:** {today}')
                    content = '\n'.join(lines)
                    modified = True
            
            if modified:
                filepath.write_text(content)
                return True
            
        except Exception as e:
            print(f"Error fixing {filepath}: {e}", file=sys.stderr)
        
        return False


def should_skip_file(filepath: Path) -> bool:
    """Determine if file should be skipped."""
    rel_path = str(filepath.relative_to(project_root))
    
    # Skip approved files
    if rel_path in APPROVED_FILES:
        return True
    
    # Skip archive directories
    if any(part in ARCHIVE_DIRS for part in filepath.parts):
        return True
    
    return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Validate markdown formatting')
    parser.add_argument('files', nargs='*', help='Files to check (default: staged files)')
    parser.add_argument('--all', action='store_true', help='Check all markdown files')
    parser.add_argument('--fix', action='store_true', help='Auto-fix issues where possible')
    parser.add_argument('--strict', action='store_true', help='Fail on any issue')
    args = parser.parse_args()
    
    validator = MarkdownValidator()
    
    # Determine files to check
    if args.all:
        # Find all markdown files
        files_to_check = list(project_root.rglob('*.md'))
        files_to_check.extend(project_root.glob('*.md'))
    elif args.files:
        files_to_check = [Path(f) for f in args.files]
    else:
        # Check staged files
        import subprocess
        result = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=A'],
                              capture_output=True, text=True)
        staged_files = [f.strip() for f in result.stdout.split('\n') if f.strip()]
        files_to_check = [project_root / f for f in staged_files if f.endswith('.md')]
    
    # Filter out skipped files
    files_to_check = [f for f in files_to_check if not should_skip_file(f)]
    
    if not files_to_check:
        print("✅ No markdown files to check")
        return 0
    
    total_issues = 0
    files_with_issues = []
    
    for filepath in files_to_check:
        if not filepath.exists():
            continue
        
        issues = validator.validate(filepath)
        
        # Count non-empty issue categories
        issue_count = sum(len(v) for k, v in issues.items() if k != 'errors' and v)
        
        if issue_count > 0:
            files_with_issues.append((filepath, issues))
            total_issues += issue_count
            
            rel_path = str(filepath.relative_to(project_root))
            print(f"\n📄 {rel_path}")
            
            for category, items in issues.items():
                if category == 'errors':
                    continue
                if items:
                    print(f"  {category.replace('_', ' ').title()}:")
                    for item in items[:5]:  # Show first 5
                        print(f"    - {item}")
                    if len(items) > 5:
                        print(f"    ... and {len(items) - 5} more")
            
            # Auto-fix if requested
            if args.fix:
                if validator.fix_issues(filepath, issues):
                    print(f"  ✅ Auto-fixed some issues")
    
    # Summary
    print("\n" + "=" * 70)
    print(f"Checked {len(files_to_check)} files")
    print(f"Found {total_issues} issues in {len(files_with_issues)} files")
    
    if files_with_issues:
        print("\n💡 Tips:")
        print("  - Use ISO date format: YYYY-MM-DD")
        print("  - Add **Last Updated:** YYYY-MM-DD to headers")
        print("  - Use relative links: [text](docs/path.md)")
        print("  - Add language tags to code blocks: ```python")
        print("  - Remove agent attribution from headers (tracked in git)")
    
    if args.strict and total_issues > 0:
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
