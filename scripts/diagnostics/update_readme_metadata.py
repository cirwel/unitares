#!/usr/bin/env python3
"""
Auto-update README.md metadata to prevent consistency issues.

Updates:
- Version number (from VERSION file)
- Tool count (from decorator registry)
- Handler file count (from filesystem)
- Last Updated date (current date)

Usage:
    python3 scripts/update_readme_metadata.py          # Apply updates
    python3 scripts/update_readme_metadata.py --dry-run # Preview only
"""

import re
import sys
from pathlib import Path
from datetime import datetime


class ReadmeMetadataUpdater:
    """Update README.md with accurate metadata."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.readme_path = project_root / "README.md"
        self.version_file = project_root / "VERSION"
        self.handlers_dir = project_root / "src" / "mcp_handlers"

        self.changes_made = []

    def get_version(self) -> str:
        """Get version from VERSION file."""
        if not self.version_file.exists():
            print(f"⚠️  VERSION file not found at {self.version_file}")
            return None
        return self.version_file.read_text().strip()

    def get_tool_count(self) -> int:
        """Get tool count from decorator registry."""
        try:
            # Add src to path
            sys.path.insert(0, str(self.project_root))
            from src.mcp_handlers.decorators import list_registered_tools
            tools = list_registered_tools()
            return len(tools)
        except Exception as e:
            print(f"⚠️  Error counting tools: {e}")
            return None

    def get_handler_file_count(self) -> int:
        """Get count of handler files."""
        if not self.handlers_dir.exists():
            print(f"⚠️  Handlers directory not found at {self.handlers_dir}")
            return None

        handler_files = [
            f for f in self.handlers_dir.iterdir()
            if f.is_file() and f.suffix == '.py' and not f.name.startswith('_')
        ]
        return len(handler_files)

    def get_current_date(self) -> str:
        """Get current date in YYYY-MM-DD format."""
        return datetime.now().strftime('%Y-%m-%d')

    def update_readme(self, dry_run: bool = False) -> bool:
        """Update README.md with current metadata."""
        if not self.readme_path.exists():
            print(f"❌ README.md not found at {self.readme_path}")
            return False

        # Read current README
        content = self.readme_path.read_text()
        original_content = content

        # Get current values
        version = self.get_version()
        tool_count = self.get_tool_count()
        handler_count = self.get_handler_file_count()
        current_date = self.get_current_date()

        if version is None or tool_count is None or handler_count is None:
            print("❌ Failed to get metadata. Aborting.")
            return False

        print(f"📊 Current metadata:")
        print(f"   Version: {version}")
        print(f"   Tools: {tool_count}")
        print(f"   Handler files: {handler_count}")
        print(f"   Date: {current_date}")
        print()

        # 1. Update version in title
        pattern = r'^# UNITARES Governance Framework v[\d.]+$'
        replacement = f'# UNITARES Governance Framework v{version}'
        content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
        if count > 0:
            self.changes_made.append(f"Updated title version to v{version}")

        # 2. Update tool count references (multiple patterns)
        patterns = [
            # "47 tools" standalone
            (r'\b\d+ tools\b', f'{tool_count} tools'),
            # "all 47 tools"
            (r'\ball \d+ tools\b', f'all {tool_count} tools'),
            # "(47 tools documented)"
            (r'\(\d+ tools documented\)', f'({tool_count} tools documented)'),
        ]

        for pattern, replacement in patterns:
            new_content, count = re.subn(pattern, replacement, content)
            if count > 0 and new_content != content:
                self.changes_made.append(f"Updated tool count to {tool_count} ({count} location(s))")
                content = new_content

        # 3. Update handler file count
        # Pattern: "47 tools across 13 handler files"
        pattern = r'\b\d+ tools across \d+ handler files?\b'
        replacement = f'{tool_count} tools across {handler_count} handler files'
        content, count = re.subn(pattern, replacement, content)
        if count > 0:
            self.changes_made.append(f"Updated handler count to {handler_count} ({count} location(s))")

        # Pattern: "Handler registry (47 tools across 13 handler files)"
        pattern = r'Handler registry \(\d+ tools across \d+ handler files?\)'
        replacement = f'Handler registry ({tool_count} tools across {handler_count} handler files)'
        content, count = re.subn(pattern, replacement, content)
        if count > 0:
            self.changes_made.append(f"Updated registry description ({count} location(s))")

        # 4. Update "Last Updated" date
        pattern = r'\*\*Last Updated:\*\* \d{4}-\d{2}-\d{2}'
        replacement = f'**Last Updated:** {current_date}'
        content, count = re.subn(pattern, replacement, content)
        if count > 0:
            self.changes_made.append(f"Updated date to {current_date}")

        # Check if anything changed
        if content == original_content:
            print("✅ No changes needed - README.md is already up-to-date!")
            return True

        # Show changes
        if self.changes_made:
            print("📝 Changes to be made:")
            for i, change in enumerate(self.changes_made, 1):
                print(f"   {i}. {change}")
            print()

        if dry_run:
            print("🔍 DRY RUN - No changes written")
            print("\nPreview of changes:")
            print("=" * 60)
            # Show first few changed lines
            old_lines = original_content.split('\n')
            new_lines = content.split('\n')
            shown = 0
            for i, (old, new) in enumerate(zip(old_lines, new_lines)):
                if old != new and shown < 5:
                    print(f"Line {i+1}:")
                    print(f"  - {old}")
                    print(f"  + {new}")
                    shown += 1
            if shown == 5:
                print("  ... (more changes)")
            print("=" * 60)
            return True

        # Write updated README
        self.readme_path.write_text(content)
        print(f"✅ Updated {self.readme_path}")
        return True


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Update README.md metadata automatically'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without writing'
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent

    print("🔄 README Metadata Updater")
    print("=" * 60)
    print()

    updater = ReadmeMetadataUpdater(project_root)
    success = updater.update_readme(dry_run=args.dry_run)

    if success:
        if args.dry_run:
            print("\n✅ Dry run completed successfully")
        else:
            print("\n✅ README.md updated successfully")
        sys.exit(0)
    else:
        print("\n❌ Failed to update README.md")
        sys.exit(1)


if __name__ == "__main__":
    main()
