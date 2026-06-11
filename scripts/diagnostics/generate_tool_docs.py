#!/usr/bin/env python3
"""
Auto-generate tool documentation from @mcp_tool decorators.

Scans all handler files for @mcp_tool decorated functions and extracts:
- Tool name
- Description (from docstring)
- Timeout value
- Parameters (from function signature and docstring)

Generates tools/README.md with comprehensive tool documentation.
"""

import ast
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ToolInfo:
    """Information about an MCP tool."""
    name: str
    function_name: str
    description: str
    timeout: float
    category: str
    docstring: str
    file_path: str
    rate_limit_exempt: bool = False

    @property
    def short_description(self) -> str:
        """Get first line of description."""
        if self.description:
            return self.description.split('\n')[0].strip()
        return "No description available"


class ToolDocExtractor:
    """Extract tool documentation from @mcp_tool decorated functions."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.handlers_dir = project_root / "src" / "mcp_handlers"
        self.tools: Dict[str, ToolInfo] = {}

    def extract_decorator_info(self, node) -> Optional[Dict[str, Any]]:
        """Extract info from @mcp_tool decorator."""
        for decorator in node.decorator_list:
            # Handle @mcp_tool(...) and @mcp_tool
            if isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Name) and decorator.func.id == 'mcp_tool':
                    info = {}
                    # Extract positional arguments (first arg is tool name if provided)
                    if decorator.args and isinstance(decorator.args[0], ast.Constant):
                        info['name'] = decorator.args[0].value

                    # Extract keyword arguments
                    for keyword in decorator.keywords:
                        if keyword.arg == 'name' and isinstance(keyword.value, ast.Constant):
                            info['name'] = keyword.value.value
                        elif keyword.arg == 'timeout' and isinstance(keyword.value, ast.Constant):
                            info['timeout'] = keyword.value.value
                        elif keyword.arg == 'description' and isinstance(keyword.value, ast.Constant):
                            info['description'] = keyword.value.value
                        elif keyword.arg == 'rate_limit_exempt' and isinstance(keyword.value, ast.Constant):
                            info['rate_limit_exempt'] = keyword.value.value
                    return info
            elif isinstance(decorator, ast.Name) and decorator.id == 'mcp_tool':
                return {}  # Decorator without arguments
        return None

    def extract_from_file(self, file_path: Path) -> List[ToolInfo]:
        """Extract all @mcp_tool decorated functions from a file."""
        tools = []

        try:
            with open(file_path, 'r') as f:
                content = f.read()

            tree = ast.parse(content)

            # Determine category from filename
            category = file_path.stem  # e.g., 'core', 'lifecycle', 'admin'

            # Only look at module-level functions (not nested)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorator_info = self.extract_decorator_info(node)

                    if decorator_info is not None:
                        # Get function name and docstring
                        function_name = node.name
                        docstring = ast.get_docstring(node) or ""

                        # Tool name: from decorator or function name without 'handle_' prefix
                        tool_name = decorator_info.get('name') or function_name.replace('handle_', '')

                        # Description: from decorator or first line of docstring
                        description = decorator_info.get('description') or docstring

                        # Timeout: from decorator or default
                        timeout = decorator_info.get('timeout', 30.0)

                        # Rate limit exempt
                        rate_limit_exempt = decorator_info.get('rate_limit_exempt', False)

                        tools.append(ToolInfo(
                            name=tool_name,
                            function_name=function_name,
                            description=description.strip(),
                            timeout=timeout,
                            category=category,
                            docstring=docstring.strip(),
                            file_path=str(file_path.relative_to(self.project_root)),
                            rate_limit_exempt=rate_limit_exempt
                        ))

        except Exception as e:
            print(f"⚠️  Error parsing {file_path}: {e}")

        return tools

    def scan_handlers(self):
        """Scan all handler files for @mcp_tool decorated functions."""
        handler_files = list(self.handlers_dir.glob("*.py"))

        for file_path in handler_files:
            if file_path.name.startswith('_'):
                continue  # Skip __init__.py, etc.

            tools = self.extract_from_file(file_path)
            for tool in tools:
                self.tools[tool.name] = tool

        print(f"✅ Found {len(self.tools)} tools across {len(handler_files)} handler files")

    def categorize_tools(self) -> Dict[str, List[ToolInfo]]:
        """Group tools by category."""
        categories = {}
        for tool in self.tools.values():
            if tool.category not in categories:
                categories[tool.category] = []
            categories[tool.category].append(tool)

        # Sort tools within each category by name
        for category in categories:
            categories[category].sort(key=lambda t: t.name)

        return categories

    def generate_markdown(self) -> str:
        """Generate README.md content."""
        categories = self.categorize_tools()

        # Category display names and order
        category_info = {
            'core': ('🎯 Core Governance', 'Main governance cycle operations'),
            'lifecycle': ('🔄 Agent Lifecycle', 'Agent creation, archival, and management'),
            'observability': ('📊 Observability', 'Monitoring, metrics, and anomaly detection'),
            'config': ('⚙️ Configuration', 'Threshold and configuration management'),
            'export': ('📤 Export', 'Data export and history retrieval'),
            'knowledge_graph': ('🧠 Knowledge Graph', 'Fast, indexed knowledge storage'),
            'dialectic': ('💭 Dialectic Protocol', 'Circuit breaker recovery and collaborative review'),
            'admin': ('🔧 Admin & Health', 'System administration and health checks'),
            'utils': ('🛠️ Utilities', 'Common utilities and helpers'),
        }

        md = []
        md.append("# MCP Tools - Auto-Generated Documentation\n")
        md.append("**⚠️ IMPORTANT: This file is auto-generated. Do not edit manually.**\n")
        md.append(f"**Last Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        md.append(f"**Total Tools:** {len(self.tools)}\n")
        md.append("---\n")

        md.append("## 🚀 Quick Start\n")
        md.append("**If you have MCP access (Cursor, Claude Desktop, etc.):**\n")
        md.append("- ✅ **Use MCP tools directly** - Full feature set via MCP protocol\n")
        md.append("- ✅ **Discovery:** Call `list_tools()` to see all available tools\n")
        md.append("- ✅ **No scripts needed** - Tools are the primary interface\n")
        md.append("---\n")

        md.append("## 📋 Table of Contents\n\n")
        for category_key in category_info.keys():
            if category_key in categories:
                name, _ = category_info[category_key]
                md.append(f"- [{name}](#{category_key.replace('_', '-')})\n")
        md.append("\n---\n")

        # Generate sections for each category
        for category_key, (category_name, category_desc) in category_info.items():
            if category_key not in categories:
                continue

            tools = categories[category_key]
            md.append(f"\n## {category_name}\n")
            md.append(f"*{category_desc}*\n\n")

            for tool in tools:
                md.append(f"### `{tool.name}`\n\n")
                md.append(f"**Description:** {tool.short_description}\n\n")
                md.append(f"**Timeout:** {tool.timeout}s")
                if tool.rate_limit_exempt:
                    md.append(" (rate limit exempt)")
                md.append("\n\n")

                if tool.docstring and len(tool.docstring) > len(tool.short_description):
                    # Include full docstring if it has more detail
                    md.append("**Details:**\n```\n")
                    md.append(tool.docstring)
                    md.append("\n```\n\n")

                md.append(f"**Source:** `{tool.file_path}`\n\n")
                md.append("---\n\n")

        md.append("\n## 📚 Additional Resources\n\n")
        md.append("- **Start Here:** `docs/guides/START_HERE.md`\n")
        md.append("- **Architecture:** `docs/UNIFIED_ARCHITECTURE.md`\n")
        md.append("- **Canonical Sources:** `docs/dev/CANONICAL_SOURCES.md`\n")
        md.append("- **Runtime Discovery:** Call `list_tools()` MCP tool for up-to-date tool list\n\n")

        md.append("---\n\n")
        md.append("**Auto-generated by:** `scripts/generate_tool_docs.py`  \n")
        md.append("**Regenerate:** `python3 scripts/generate_tool_docs.py`\n")

        return ''.join(md)


def main():
    """Main entry point."""
    project_root = Path(__file__).parent.parent.parent
    output_file = project_root / "tools" / "README.md"

    print("🔍 Scanning for @mcp_tool decorated functions...")
    extractor = ToolDocExtractor(project_root)
    extractor.scan_handlers()

    print("📝 Generating documentation...")
    markdown = extractor.generate_markdown()

    print(f"💾 Writing to {output_file}...")
    output_file.parent.mkdir(exist_ok=True)
    with open(output_file, 'w') as f:
        f.write(markdown)

    print(f"✅ Documentation generated successfully!")
    print(f"   Total tools documented: {len(extractor.tools)}")
    print(f"   Output: {output_file}")


if __name__ == "__main__":
    main()
