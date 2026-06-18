#!/usr/bin/env python3
"""
MCP Agent Shorthand - Zero-boilerplate tool caller for agents

This script eliminates the "wrapper tax" - agents never need to write async/await,
import httpx, or deal with f-string escaping. Just call tools directly.

Usage:
    python3 scripts/mcp_agent.py <tool_name> [key=value ...]
    python3 scripts/mcp_agent.py <tool_name> --json '{"key": "value"}'
    python3 scripts/mcp_agent.py list_tools
    python3 scripts/mcp_agent.py process_agent_update response_text="My work" complexity=0.6

Features:
    - Uses Streamable HTTP (/mcp), the UNITARES transport (auto-detected from
      the URL). SSE remains only as a fallback for non-/mcp URLs — UNITARES no
      longer serves /sse.
    - Handles session continuity (saves/loads .mcp_session)
    - Zero boilerplate
    - Clean JSON output

Examples:
    # Simple tool call
    python3 scripts/mcp_agent.py identity
    
    # With arguments (key=value format)
    python3 scripts/mcp_agent.py process_agent_update response_text="Completed analysis" complexity=0.7
    
    # With JSON input (for complex nested structures)
    python3 scripts/mcp_agent.py store_knowledge_graph --json '{"discovery_type": "insight", "summary": "Found pattern", "tags": ["mcp", "architecture"]}'
"""

import sys
import json
import asyncio
import argparse
import httpx
from pathlib import Path
from typing import Dict, Any, Optional

# Session file to persist client_session_id
SESSION_FILE = Path(".mcp_session")

def load_session() -> Optional[str]:
    """Load session ID from file."""
    if SESSION_FILE.exists():
        try:
            return SESSION_FILE.read_text().strip()
        except Exception:
            pass
    return None

def save_session(session_id: str):
    """Save session ID to file."""
    try:
        SESSION_FILE.write_text(session_id)
    except Exception:
        pass

def parse_key_value_args(args: list) -> Dict[str, Any]:
    """
    Parse key=value arguments into a dictionary.
    Handles simple types: strings, numbers, booleans, null.
    """
    result = {}
    for arg in args:
        if '=' not in arg:
            continue
        
        key, value = arg.split('=', 1)
        key = key.strip()
        value = value.strip()
        
        # Remove quotes if present
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        
        # Try to parse as number/bool/null
        if value.lower() == 'true':
            result[key] = True
        elif value.lower() == 'false':
            result[key] = False
        elif value.lower() == 'null' or value.lower() == 'none':
            result[key] = None
        elif value.startswith('[') and value.endswith(']'):
            # JSON-style list: [a,b,c] -> ["a", "b", "c"]
            # Remove brackets and split
            inner = value[1:-1]
            if not inner:
                result[key] = []
            else:
                # Naive split - assumes no commas in values
                items = [item.strip().strip("'").strip('"') for item in inner.split(',')]
                result[key] = items
        elif ',' in value and not value.replace('.', '').isdigit():
            # Comma-separated list: a,b,c -> ["a", "b", "c"] (if not a number with comma decimal)
            items = [item.strip() for item in value.split(',')]
            result[key] = items
        elif value.replace('.', '', 1).replace('-', '', 1).isdigit():
            # Integer or float
            if '.' in value:
                result[key] = float(value)
            else:
                result[key] = int(value)
        else:
            # String (default)
            result[key] = value
    
    return result

def parse_json_args(json_str: str) -> Dict[str, Any]:
    """Parse JSON string into dictionary."""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(json.dumps({
            "success": False,
            "error": "Invalid JSON",
            "details": str(e),
            "input": json_str
        }), file=sys.stderr)
        sys.exit(1)

async def call_tool(tool_name: str, arguments: Dict[str, Any], url: str) -> Dict[str, Any]:
    """
    Call an MCP tool and return the result as JSON.
    Auto-detects transport based on URL.
    """
    # Inject session ID if available and not explicitly provided
    client_session_id = load_session()
    if client_session_id and "client_session_id" not in arguments:
        arguments["client_session_id"] = client_session_id

    # Determine transport
    is_streamable = "/mcp" in url
    
    try:
        from mcp.client.session import ClientSession
        
        transport_context = None
        
        if is_streamable:
            from mcp.client.streamable_http import streamable_http_client
            # Use httpx with http2 for Streamable HTTP
            http_client = httpx.AsyncClient(http2=True, timeout=30.0)
            transport_context = streamable_http_client(url, http_client=http_client)
        else:
            from mcp.client.sse import sse_client
            transport_context = sse_client(url)

        async with transport_context as streams:
            # Streamable HTTP returns (read, write, messages) tuple, SSE returns (read, write)
            if is_streamable:
                read, write, _ = streams
            else:
                read, write = streams

            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Special case: list_tools
                if tool_name == "list_tools":
                    tools = await session.list_tools()
                    return {
                        "success": True,
                        "tools": [{"name": t.name, "description": getattr(t, 'description', '')} for t in tools.tools],
                        "count": len(tools.tools)
                    }
                
                # Call the tool
                result = await session.call_tool(tool_name, arguments)
                
                # Capture session ID from response if available (from onboard/identity)
                # Parse output to find client_session_id
                final_result = {"success": True}
                json_parsed = False
                
                # MCP returns TextContent objects
                parsed_content = []
                for content in result.content:
                    if hasattr(content, 'text'):
                        text = content.text
                        parsed_content.append(text)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                final_result.update(data)
                                json_parsed = True
                                # Check for new session ID
                                if "client_session_id" in data:
                                    save_session(data["client_session_id"])
                        except json.JSONDecodeError:
                            pass
                
                # Only include raw text if JSON parsing failed (fallback for non-JSON responses)
                if not json_parsed and parsed_content:
                    final_result["text"] = "\n".join(parsed_content)
                
                return final_result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "tool_name": tool_name
        }

def main():
    parser = argparse.ArgumentParser(
        description="MCP Agent Shorthand - Zero-boilerplate tool caller",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('tool_name', help='Name of the MCP tool to call')
    parser.add_argument('arguments', nargs='*', help='Tool arguments in key=value format')
    parser.add_argument('--json', dest='json_input', help='Provide arguments as JSON string')
    parser.add_argument('--url', default='http://127.0.0.1:8767/mcp/', help='MCP server URL')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON output')
    
    args = parser.parse_args()
    
    # Parse arguments
    if args.json_input:
        tool_args = parse_json_args(args.json_input)
    else:
        tool_args = parse_key_value_args(args.arguments)
    
    # Call tool
    result = asyncio.run(call_tool(args.tool_name, tool_args, args.url))
    
    # Output
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))
    
    if not result.get('success', True):
        sys.exit(1)

if __name__ == "__main__":
    main()