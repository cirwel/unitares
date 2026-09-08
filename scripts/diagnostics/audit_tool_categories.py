#!/usr/bin/env python3
"""
Audit tool categorization

Identifies tools that are missing from TOOL_CATEGORIES in tool_modes.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.tool_schemas import get_tool_definitions
from src.tool_modes import TOOL_CATEGORIES, advertised_tool_names_full

def audit_tool_categories():
    """Find uncategorized tools"""

    # The roster is what a full-mode server advertises: registered dispatch
    # tools plus the workflow aliases. The schema list is only consulted for
    # descriptions; it also carries register=False delegates that are not
    # tools and must not read as "uncategorized".
    all_tools = get_tool_definitions()
    all_tool_names = advertised_tool_names_full()
    
    # Get all categorized tools
    categorized_tools = set()
    for category, tools in TOOL_CATEGORIES.items():
        categorized_tools.update(tools)
    
    # Find uncategorized tools
    uncategorized = all_tool_names - categorized_tools
    
    # Also check for tools in categories that don't exist
    non_existent = categorized_tools - all_tool_names
    
    print("=" * 80)
    print("TOOL CATEGORIZATION AUDIT")
    print("=" * 80)
    print()
    print(f"Advertised tools (registered + workflow aliases): {len(all_tool_names)}")
    print(f"Tools in categories: {len(categorized_tools)}")
    print(f"Uncategorized tools: {len(uncategorized)}")
    print(f"Non-existent tools in categories: {len(non_existent)}")
    print()
    
    if uncategorized:
        print("UNCATEGORIZED TOOLS:")
        print("-" * 80)
        for tool_name in sorted(uncategorized):
            # Try to find the tool definition to get description
            tool_def = next((t for t in all_tools if t.name == tool_name), None)
            description = ""
            if tool_def and tool_def.description:
                # Get first line of description
                first_line = tool_def.description.split('\n')[0].strip()
                description = f" - {first_line[:60]}..." if len(first_line) > 60 else f" - {first_line}"
            
            print(f"  • {tool_name}{description}")
        
        print()
        print("SUGGESTED CATEGORIES:")
        print("-" * 80)
        
        # Group by likely category based on name patterns
        suggestions = {
            "admin": [],
            "core": [],
            "dialectic": [],
            "identity": [],
            "knowledge": [],
            "lifecycle": [],
            "observability": [],
            "export": [],
            "config": [],
            "other": []
        }
        
        for tool_name in sorted(uncategorized):
            name_lower = tool_name.lower()
            if any(x in name_lower for x in ["health", "server", "telemetry", "calibration", "lock", "workspace"]):
                suggestions["admin"].append(tool_name)
            elif any(x in name_lower for x in ["process", "governance", "simulate", "metrics"]):
                suggestions["core"].append(tool_name)
            elif any(x in name_lower for x in ["dialectic", "thesis", "antithesis", "synthesis", "review"]):
                suggestions["dialectic"].append(tool_name)
            elif any(x in name_lower for x in ["identity", "bind", "recall", "spawn", "api_key"]):
                suggestions["identity"].append(tool_name)
            elif any(x in name_lower for x in ["knowledge", "discovery", "graph", "question", "note"]):
                suggestions["knowledge"].append(tool_name)
            elif any(x in name_lower for x in ["agent", "archive", "delete", "metadata", "lifecycle", "response"]):
                suggestions["lifecycle"].append(tool_name)
            elif any(x in name_lower for x in ["observe", "compare", "anomaly", "aggregate"]):
                suggestions["observability"].append(tool_name)
            elif any(x in name_lower for x in ["export", "history", "file"]):
                suggestions["export"].append(tool_name)
            elif any(x in name_lower for x in ["threshold", "config", "setting"]):
                suggestions["config"].append(tool_name)
            else:
                suggestions["other"].append(tool_name)
        
        for category, tools in suggestions.items():
            if tools:
                print(f"\n{category.upper()}:")
                for tool in tools:
                    print(f"  - {tool}")
    
    if non_existent:
        print()
        print("NON-EXISTENT TOOLS IN CATEGORIES (should be removed):")
        print("-" * 80)
        for tool_name in sorted(non_existent):
            # Find which category it's in
            categories = [cat for cat, tools in TOOL_CATEGORIES.items() if tool_name in tools]
            print(f"  • {tool_name} (in categories: {', '.join(categories)})")
    
    print()
    print("=" * 80)
    print("CATEGORY SUMMARY:")
    print("=" * 80)
    for category, tools in sorted(TOOL_CATEGORIES.items()):
        print(f"{category}: {len(tools)} tools")
    
    print()
    print("=" * 80)
    print("RECOMMENDATIONS:")
    print("=" * 80)
    print("1. Add uncategorized tools to appropriate categories in tool_modes.py")
    print("2. Remove non-existent tools from categories")
    print("3. Consider creating new categories if needed (e.g., 'workspace')")
    print()


if __name__ == "__main__":
    audit_tool_categories()

