#!/usr/bin/env python3
"""
Sync bridge script with MCP - Keep claude_code_bridge.py in sync with MCP tools

This script ensures the CLI bridge stays synchronized with the MCP server:
1. Tool count (extract from decorator registry)
2. Terminology (ensure consistent use of attention_score vs deprecated risk_score)
3. CSV headers (update to current metric names)

Usage:
    python3 scripts/sync_bridge_with_mcp.py --dry-run  # Preview changes
    python3 scripts/sync_bridge_with_mcp.py            # Apply changes
"""

import re
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Import tool registry
from src.mcp_handlers.decorators import list_registered_tools


class BridgeSyncer:
    """Sync bridge script with MCP server state"""

    def __init__(self, bridge_path: Path, compat_path: Path, dry_run: bool = False):
        self.bridge_path = bridge_path
        self.compat_path = compat_path
        self.dry_run = dry_run
        self.changes = []

    def get_current_tool_count(self) -> int:
        """Get current tool count from decorator registry"""
        tools = list_registered_tools()
        return len(tools)

    def sync_tool_count(self, content: str, tool_count: int) -> str:
        """Update hardcoded tool counts"""
        changes = []

        # Pattern 1: "Current tool count: 47"
        pattern1 = r'Current tool count: \d+'
        replacement1 = f'Current tool count: {tool_count}'
        if re.search(pattern1, content):
            content = re.sub(pattern1, replacement1, content)
            changes.append(f"Updated 'Current tool count' to {tool_count}")

        # Pattern 2: "all 44+ tools" or "all 47 tools"
        pattern2 = r'all \d+\+ tools'
        replacement2 = f'all {tool_count}+ tools'
        if re.search(pattern2, content):
            content = re.sub(pattern2, replacement2, content)
            changes.append(f"Updated tool count reference to {tool_count}+")

        self.changes.extend(changes)
        return content

    def sync_terminology(self, content: str) -> str:
        """Update deprecated risk_score to attention_score"""
        changes = []

        # CSV header update
        old_header = '"agent_id,time,E,I,S,V,lambda1,coherence,void_event,risk_score,decision'
        new_header = '"agent_id,time,E,I,S,V,lambda1,coherence,void_event,attention_score,decision'
        if old_header in content:
            content = content.replace(old_header, new_header)
            changes.append("Updated CSV header: risk_score → attention_score")

        # Test output update (preserve formatting)
        # Pattern: f"Risk={result['metrics']['risk_score']:.3f}"
        pattern = r"Risk=\{result\['metrics'\]\['risk_score'\]"
        if re.search(pattern, content):
            content = re.sub(pattern, "Attention={result['metrics']['attention_score']", content)
            changes.append("Updated test output: Risk → Attention, risk_score → attention_score")

        # Comment update: "# Use attention_score (new) with fallback to risk_score (deprecated)"
        # This is actually correct - no change needed, it documents the fallback

        self.changes.extend(changes)
        return content

    def sync_bridge_script(self):
        """Sync main bridge script"""
        print(f"🔄 Syncing bridge script: {self.bridge_path}")

        if not self.bridge_path.exists():
            print(f"❌ Bridge script not found: {self.bridge_path}")
            return False

        # Read current content
        content = self.bridge_path.read_text()

        # Get current tool count
        tool_count = self.get_current_tool_count()
        print(f"📊 Current tool count: {tool_count}")

        # Apply syncs
        content = self.sync_tool_count(content, tool_count)
        content = self.sync_terminology(content)

        # Write back if not dry run
        if self.changes:
            if self.dry_run:
                print(f"🔍 DRY RUN - Would make {len(self.changes)} change(s):")
                for change in self.changes:
                    print(f"   - {change}")
            else:
                self.bridge_path.write_text(content)
                print(f"✅ Applied {len(self.changes)} change(s):")
                for change in self.changes:
                    print(f"   - {change}")
        else:
            print("✅ No changes needed - bridge script is in sync")

        return True

    def sync_compat_layer(self):
        """Sync compatibility layer"""
        print(f"\n🔄 Syncing compat layer: {self.compat_path}")

        if not self.compat_path.exists():
            print(f"❌ Compat layer not found: {self.compat_path}")
            return False

        # Read current content
        content = self.compat_path.read_text()

        # Get current tool count
        tool_count = self.get_current_tool_count()

        # Reset changes for compat layer
        compat_changes = []

        # Apply tool count sync
        old_changes = self.changes
        self.changes = []
        content = self.sync_tool_count(content, tool_count)
        compat_changes = self.changes
        self.changes = old_changes + compat_changes

        # Write back if not dry run
        if compat_changes:
            if self.dry_run:
                print(f"🔍 DRY RUN - Would make {len(compat_changes)} change(s):")
                for change in compat_changes:
                    print(f"   - {change}")
            else:
                self.compat_path.write_text(content)
                print(f"✅ Applied {len(compat_changes)} change(s):")
                for change in compat_changes:
                    print(f"   - {change}")
        else:
            print("✅ No changes needed - compat layer is in sync")

        return True

    def run(self):
        """Run sync process"""
        print("="*70)
        print("BRIDGE ↔ MCP SYNCHRONIZATION")
        print("="*70)
        print()

        success = True
        success &= self.sync_bridge_script()
        success &= self.sync_compat_layer()

        print()
        print("="*70)
        if success:
            if self.dry_run:
                print("✅ Dry run complete - no changes written")
            else:
                print(f"✅ Sync complete - {len(self.changes)} total change(s) applied")
        else:
            print("❌ Sync failed - check errors above")
        print("="*70)

        return 0 if success else 1


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sync bridge script with MCP server")
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--bridge-path', type=str,
                       default=str(Path.home() / 'scripts' / 'claude_code_bridge.py'),
                       help='Path to bridge script')
    parser.add_argument('--compat-path', type=str,
                       default='src/mcp_server_compat.py',
                       help='Path to compat layer')

    args = parser.parse_args()

    bridge_path = Path(args.bridge_path)
    compat_path = project_root / args.compat_path

    syncer = BridgeSyncer(bridge_path, compat_path, dry_run=args.dry_run)
    sys.exit(syncer.run())


if __name__ == '__main__':
    main()
