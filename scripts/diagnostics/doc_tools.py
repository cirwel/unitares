#!/usr/bin/env python3
"""
Consolidated documentation tools - check small markdowns, cleanup docs, generate org guide.

Usage:
    python3 scripts/doc_tools.py check-small          # Check for small markdown files
    python3 scripts/doc_tools.py cleanup              # Cleanup redundant docs
    python3 scripts/doc_tools.py generate-org         # Generate organization guide
    python3 scripts/doc_tools.py all                  # Run all tools
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def check_small_markdowns():
    """Check for small markdown files (from check_small_markdowns.py)."""
    archive_path = project_root / "scripts" / "archive"
    if str(archive_path) not in sys.path:
        sys.path.insert(0, str(archive_path))
    from check_small_markdowns import find_small_markdowns
    
    docs_dir = project_root / "docs"
    small_files = find_small_markdowns(docs_dir)
    
    if small_files:
        print(f"Found {len(small_files)} small markdown files:")
        for file, words, chars in small_files:
            print(f"  {file.relative_to(project_root)}: {words} words, {chars} chars")
    else:
        print("✅ No small markdown files found")
    
    return len(small_files)


def cleanup_docs():
    """Cleanup redundant docs (from cleanup_docs.py)."""
    archive_path = project_root / "scripts" / "archive"
    if str(archive_path) not in sys.path:
        sys.path.insert(0, str(archive_path))
    from cleanup_docs import find_redundant_fix_files, check_outdated_references
    
    print("Checking for redundant fix files...")
    redundant = find_redundant_fix_files()
    if redundant:
        print(f"Found {len(redundant)} potentially redundant fix files:")
        for f in redundant:
            print(f"  {f.relative_to(project_root)}")
    else:
        print("✅ No redundant fix files found")
    
    print("\nChecking for outdated references...")
    outdated = check_outdated_references()
    if outdated:
        print(f"Found {len(outdated)} potentially outdated references:")
        for file, pattern, message in outdated:
            print(f"  {file.relative_to(project_root)}: {message}")
    else:
        print("✅ No outdated references found")
    
    return len(redundant) + len(outdated)


def generate_org_guide():
    """Generate organization guide (from generate_org_guide.py)."""
    archive_path = project_root / "scripts" / "archive"
    if str(archive_path) not in sys.path:
        sys.path.insert(0, str(archive_path))
    from generate_org_guide import generate_organization_guide
    
    print("Generating organization guide...")
    generate_organization_guide()
    print("✅ Organization guide generated")
    return 0


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Documentation tools')
    parser.add_argument('command', choices=['check-small', 'cleanup', 'generate-org', 'all'],
                       help='Command to run')
    
    args = parser.parse_args()
    
    exit_code = 0
    
    if args.command == 'check-small' or args.command == 'all':
        exit_code = max(exit_code, check_small_markdowns())
        print()
    
    if args.command == 'cleanup' or args.command == 'all':
        exit_code = max(exit_code, cleanup_docs())
        print()
    
    if args.command == 'generate-org' or args.command == 'all':
        exit_code = max(exit_code, generate_org_guide())
        print()
    
    sys.exit(exit_code)


if __name__ == '__main__':
    main()

