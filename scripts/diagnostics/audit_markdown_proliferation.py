#!/usr/bin/env python3
"""
Audit markdown file proliferation and suggest consolidation/migration.

Usage:
    python scripts/audit_markdown_proliferation.py                    # Full audit
    python scripts/audit_markdown_proliferation.py --check-new        # Check for new files
    python scripts/audit_markdown_proliferation.py --suggest-consolidation  # Find consolidation candidates
    python scripts/audit_markdown_proliferation.py --suggest-migration      # Find migration candidates
    python scripts/audit_markdown_proliferation.py --stats              # Show statistics
"""

import sys
import re
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Set

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Approved markdown files for the legacy proliferation audit.
APPROVED_FILES = {
    'README.md',
    'docs/CHANGELOG.md',
    'docs/UNIFIED_ARCHITECTURE.md',
    'docs/guides/START_HERE.md',
    'docs/guides/TROUBLESHOOTING.md',
    'docs/guides/CIRS_PROTOCOL.md',
    'docs/operations/DEFINITIVE_PORTS.md',
    'docs/operations/OPERATOR_RUNBOOK.md',
    'docs/operations/database_architecture.md',
    'docs/dev/CANONICAL_SOURCES.md',
    'docs/dev/CIRCUIT_BREAKER_DIALECTIC.md',
    'docs/dev/TOOL_REGISTRATION.md',
    'scripts/README.md',
    'data/README.md',
    'tools/README.md',
}

# Max total files allowed (hard limit)
MAX_MARKDOWN_FILES = 50

# Directories that should be mostly migrated
MIGRATION_TARGET_DIRS = {
    'analysis',
    'fixes',
    'reflection',
    'proposals',
}

# Directories that can have more files (guides, reference)
GUIDE_DIRS = {
    'guides',
    'reference',
    'operations',
    'dev',
    'engineering',
    'meta',
}


def get_file_stats(filepath: Path) -> Dict:
    """Get statistics for a markdown file."""
    try:
        content = filepath.read_text()
        lines = content.split('\n')
        word_count = len(content.split())
        char_count = len(content)
        
        # Count headers
        headers = [line for line in lines if line.strip().startswith('#')]
        
        # Estimate if it's comprehensive (> 1000 words)
        is_comprehensive = word_count > 1000
        
        return {
            'path': filepath,
            'rel_path': str(filepath.relative_to(project_root)),
            'lines': len(lines),
            'words': word_count,
            'chars': char_count,
            'headers': len(headers),
            'is_comprehensive': is_comprehensive,
            'size_kb': filepath.stat().st_size / 1024,
        }
    except Exception as e:
        return {
            'path': filepath,
            'rel_path': str(filepath.relative_to(project_root)),
            'error': str(e),
        }


def classify_file(filepath: Path, stats: Dict) -> str:
    """Classify file as: approved, keep, consolidate, migrate, archive."""
    rel_path = str(filepath.relative_to(project_root))
    
    # Check if approved
    if rel_path in APPROVED_FILES:
        return 'approved'
    
    # Check if in archive (already archived)
    if 'archive' in filepath.parts:
        return 'archived'
    
    # Check if comprehensive guide
    if stats.get('is_comprehensive', False):
        parent_dir = filepath.parent.name
        if parent_dir in GUIDE_DIRS:
            return 'keep'  # Comprehensive guide in guide directory
    
    # Check if in migration target directory
    parent_dir = filepath.parent.name
    if parent_dir in MIGRATION_TARGET_DIRS:
        return 'migrate'
    
    # Check if small file (< 500 lines, < 1000 words)
    if stats.get('lines', 0) < 500 and stats.get('words', 0) < 1000:
        return 'migrate'
    
    # Check if old file (> 6 months)
    try:
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
        age_days = (datetime.now() - mtime).days
        if age_days > 180:  # 6 months
            return 'archive'
    except:
        pass
    
    # Default: keep but review
    return 'review'


def find_similar_files(files: List[Path]) -> List[List[Path]]:
    """Find groups of similar files that could be consolidated."""
    groups = defaultdict(list)
    
    for filepath in files:
        # Group by parent directory
        parent = filepath.parent.name
        groups[parent].append(filepath)
        
        # Group by filename patterns
        stem = filepath.stem.upper()
        # Extract common patterns
        if 'FIX' in stem or 'BUG' in stem:
            groups['fixes'].append(filepath)
        if 'ANALYSIS' in stem or 'ASSESSMENT' in stem:
            groups['analysis'].append(filepath)
        if 'IMPROVEMENT' in stem or 'ENHANCEMENT' in stem:
            groups['improvements'].append(filepath)
        if 'REFLECTION' in stem:
            groups['reflection'].append(filepath)
    
    # Return groups with 2+ files
    return [group for group in groups.values() if len(group) >= 2]


def check_new_files(files: List[Path], threshold_days: int = 7) -> List[Path]:
    """Find recently created files (potential new proliferation)."""
    new_files = []
    threshold = datetime.now().timestamp() - (threshold_days * 24 * 60 * 60)
    
    for filepath in files:
        try:
            if 'archive' in filepath.parts:
                continue
            
            mtime = filepath.stat().st_mtime
            if mtime > threshold:
                rel_path = str(filepath.relative_to(project_root))
                if rel_path not in APPROVED_FILES:
                    new_files.append(filepath)
        except:
            pass
    
    return sorted(new_files, key=lambda p: p.stat().st_mtime, reverse=True)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Audit markdown proliferation')
    parser.add_argument('--check-new', action='store_true', help='Check for new files')
    parser.add_argument('--suggest-consolidation', action='store_true', help='Suggest consolidation')
    parser.add_argument('--suggest-migration', action='store_true', help='Suggest migration')
    parser.add_argument('--stats', action='store_true', help='Show statistics')
    parser.add_argument('--days', type=int, default=7, help='Days threshold for new files')
    args = parser.parse_args()
    
    # Find all markdown files
    docs_dir = project_root / 'docs'
    markdown_files = list(docs_dir.rglob('*.md'))
    
    # Also check root
    root_md = list(project_root.glob('*.md'))
    markdown_files.extend(root_md)
    
    if not args.check_new and not args.suggest_consolidation and not args.suggest_migration:
        args.stats = True  # Default to stats
    
    # Get stats for all files
    file_stats = {}
    for filepath in markdown_files:
        stats = get_file_stats(filepath)
        file_stats[filepath] = stats
    
    # Classify files
    classifications = defaultdict(list)
    for filepath, stats in file_stats.items():
        if 'error' in stats:
            continue
        classification = classify_file(filepath, stats)
        classifications[classification].append((filepath, stats))
    
    # Statistics
    if args.stats:
        print("=" * 70)
        print("MARKDOWN FILE AUDIT")
        print("=" * 70)
        print(f"\nTotal markdown files: {len(markdown_files)}")
        print(f"\nClassification:")
        for cls, files in sorted(classifications.items()):
            print(f"  {cls:15} {len(files):4} files")
        
        # Size statistics
        total_lines = sum(s.get('lines', 0) for s in file_stats.values())
        total_words = sum(s.get('words', 0) for s in file_stats.values())
        total_size_kb = sum(s.get('size_kb', 0) for s in file_stats.values())
        
        print(f"\nSize statistics:")
        print(f"  Total lines:  {total_lines:,}")
        print(f"  Total words:   {total_words:,}")
        print(f"  Total size:    {total_size_kb:.1f} KB")
        
        # Approved vs unapproved
        approved_count = len(classifications.get('approved', []))
        unapproved_count = len(markdown_files) - approved_count
        print(f"\nApproval status:")
        print(f"  Approved:      {approved_count}")
        print(f"  Unapproved:    {unapproved_count}")
        print(f"  Target:        < 50 files")
        print(f"  Reduction needed: {unapproved_count - 50} files")
    
    # Check for new files
    if args.check_new:
        print("\n" + "=" * 70)
        print("NEW FILES (last {} days)".format(args.days))
        print("=" * 70)
        new_files = check_new_files(markdown_files, args.days)
        
        if new_files:
            print(f"\n⚠️  Found {len(new_files)} new markdown files:\n")
            for filepath in new_files:
                rel_path = str(filepath.relative_to(project_root))
                stats = file_stats.get(filepath, {})
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                print(f"  {rel_path}")
                print(f"    Created: {mtime.strftime('%Y-%m-%d %H:%M')}")
                print(f"    Lines: {stats.get('lines', '?')}, Words: {stats.get('words', '?')}")
                if rel_path not in APPROVED_FILES:
                    print(f"    ⚠️  NOT ON APPROVED LIST - Consider using store_knowledge()")
                print()
        else:
            print(f"\n✅ No new markdown files in last {args.days} days")
    
    # Suggest consolidation
    if args.suggest_consolidation:
        print("\n" + "=" * 70)
        print("CONSOLIDATION CANDIDATES")
        print("=" * 70)
        
        similar_groups = find_similar_files(markdown_files)
        
        if similar_groups:
            print(f"\nFound {len(similar_groups)} groups of similar files:\n")
            for i, group in enumerate(similar_groups[:10], 1):  # Show top 10
                print(f"Group {i}: {len(group)} files")
                for filepath in group[:5]:  # Show first 5
                    rel_path = str(filepath.relative_to(project_root))
                    stats = file_stats.get(filepath, {})
                    print(f"  - {rel_path} ({stats.get('lines', '?')} lines)")
                if len(group) > 5:
                    print(f"  ... and {len(group) - 5} more")
                print()
        else:
            print("\n✅ No obvious consolidation candidates found")
    
    # Suggest migration
    if args.suggest_migration:
        print("\n" + "=" * 70)
        print("MIGRATION CANDIDATES")
        print("=" * 70)
        
        migrate_files = classifications.get('migrate', [])
        
        if migrate_files:
            print(f"\nFound {len(migrate_files)} files that should be consolidated or archived:\n")
            for filepath, stats in migrate_files[:20]:  # Show first 20
                rel_path = str(filepath.relative_to(project_root))
                print(f"  {rel_path}")
                print(f"    Lines: {stats.get('lines', '?')}, Words: {stats.get('words', '?')}")
                print(f"    → Consolidate into existing doc or archive")
                print()
            
            if len(migrate_files) > 20:
                print(f"  ... and {len(migrate_files) - 20} more")
        else:
            print("\n✅ No consolidation candidates found")
        
        # Archive candidates
        archive_files = classifications.get('archive', [])
        if archive_files:
            print(f"\nFound {len(archive_files)} files that should be archived:\n")
            for filepath, stats in archive_files[:10]:  # Show first 10
                rel_path = str(filepath.relative_to(project_root))
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                age_days = (datetime.now() - mtime).days
                print(f"  {rel_path} ({age_days} days old)")
    
    # Summary recommendations
    if args.stats or args.suggest_migration or args.suggest_consolidation:
        print("\n" + "=" * 70)
        print("RECOMMENDATIONS")
        print("=" * 70)
        
        migrate_count = len(classifications.get('migrate', []))
        consolidate_groups = len(find_similar_files(markdown_files))
        archive_count = len(classifications.get('archive', []))
        
        print(f"\n1. Consolidate {migrate_count} small files into existing docs")
        print(f"   → Review files and merge related content")
        
        print(f"\n2. Consolidate {consolidate_groups} groups of similar files")
        print(f"   → Review consolidation candidates above")
        
        print(f"\n3. Archive {archive_count} old files")
        print(f"   → Move to docs/archive/ or migrate to knowledge layer")
        
        print(f"\n4. Target: Reduce from {len(markdown_files)} to < 50 files")
        reduction_needed = len(markdown_files) - 50
        print(f"   → Need to reduce by {reduction_needed} files")


if __name__ == '__main__':
    main()
