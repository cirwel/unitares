#!/usr/bin/env python3
"""
Diagnostic script to check date-context MCP server connection issues.

This script helps identify why date-context MCP keeps losing connection.
"""

import asyncio
import sys
import json
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("⚠️  MCP SDK not available. Install with: pip install mcp")


async def test_date_context_stdio():
    """Test date-context via stdio transport."""
    print("\n🔍 Testing date-context via stdio transport...")
    
    if not MCP_AVAILABLE:
        print("❌ MCP SDK not available")
        return False
    
    # Common date-context server paths
    possible_paths = [
        "npx",
        "node",
        "python3",
        "date-context",
    ]
    
    # Try to find date-context
    import shutil
    date_context_cmd = None
    for cmd in possible_paths:
        if shutil.which(cmd):
            # Check if it's date-context specific
            if cmd == "date-context":
                date_context_cmd = [cmd]
                break
    
    if not date_context_cmd:
        print("⚠️  Could not find date-context command")
        print("   Common locations:")
        print("   - npx @modelcontextprotocol/server-date-context")
        print("   - Installed globally: date-context")
        return False
    
    try:
        server_params = StdioServerParameters(
            command=date_context_cmd[0],
            args=date_context_cmd[1:] if len(date_context_cmd) > 1 else []
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Test get_current_date
                print("✅ Connected successfully")
                print("   Testing get_current_date...")
                
                result = await session.call_tool("get_current_date", {})
                print(f"✅ Tool call successful: {len(result.content)} content items")
                
                for item in result.content:
                    if hasattr(item, 'text'):
                        print(f"   Response: {item.text[:100]}...")
                
                return True
                
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


async def test_sse_keepalive():
    """Test governance server connection."""
    print("\n🔍 Testing governance server connection...")

    try:
        import httpx

        # Test governance server health endpoint
        url = "http://127.0.0.1:8767/health"

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                print("✅ Governance server is reachable")
                data = response.json()
                print(f"   Status: {data.get('status')}")
                print(f"   Version: {data.get('version')}")
            else:
                print(f"⚠️  Governance server returned {response.status_code}")
                
    except Exception as e:
        print(f"⚠️  Could not test SSE: {e}")
        print("   (This is expected if governance server is not running)")


def check_cursor_config():
    """Check Cursor MCP configuration for date-context."""
    print("\n🔍 Checking Cursor MCP configuration...")
    
    # Common Cursor config locations
    config_paths = [
        Path.home() / ".cursor" / "mcp.json",
        Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "mcp.json",
        Path.home() / ".config" / "cursor" / "mcp.json",
    ]
    
    found_config = False
    for config_path in config_paths:
        if config_path.exists():
            found_config = True
            print(f"✅ Found config at: {config_path}")
            
            try:
                with open(config_path) as f:
                    config = json.load(f)
                
                mcp_servers = config.get("mcpServers", {})
                
                if "date-context" in mcp_servers:
                    print("✅ date-context is configured")
                    date_config = mcp_servers["date-context"]
                    
                    # Check transport type
                    if "url" in date_config:
                        print(f"   Transport: SSE (URL: {date_config['url']})")
                        print("   ⚠️  SSE connections need keepalive messages")
                        print("   💡 Consider switching to stdio if available")
                    elif "command" in date_config:
                        print(f"   Transport: stdio (command: {date_config['command']})")
                        print("   ✅ stdio is more stable for long connections")
                    
                    # Check for timeout settings
                    if "timeout" in date_config:
                        print(f"   Timeout: {date_config['timeout']}s")
                    else:
                        print("   ⚠️  No explicit timeout configured")
                        
                else:
                    print("⚠️  date-context not found in configuration")
                    print(f"   Available servers: {list(mcp_servers.keys())}")
                    
            except Exception as e:
                print(f"⚠️  Error reading config: {e}")
    
    if not found_config:
        print("⚠️  Could not find Cursor MCP configuration")
        print("   Common locations:")
        for path in config_paths:
            print(f"   - {path}")


def print_recommendations():
    """Print recommendations for fixing connection issues."""
    print("\n" + "="*60)
    print("📋 RECOMMENDATIONS")
    print("="*60)
    
    print("\n1. **Check date-context server keepalive**")
    print("   - SSE connections need periodic comment lines (':\\n\\n')")
    print("   - Without keepalive, connections timeout after 30-60 seconds")
    print("   - Check if date-context server sends keepalive messages")
    
    print("\n2. **Switch to stdio transport if possible**")
    print("   - stdio is more stable for long-lived connections")
    print("   - Update Cursor config to use 'command' instead of 'url'")
    
    print("\n3. **Check network/proxy settings**")
    print("   - Ensure no intermediate proxies timeout connections")
    print("   - Check firewall settings")
    
    print("\n4. **Monitor connection patterns**")
    print("   - Use governance server's get_connection_diagnostics() tool")
    print("   - Look for reconnection patterns")
    
    print("\n5. **Update date-context server**")
    print("   - Check for updates: npm update -g @modelcontextprotocol/server-date-context")
    print("   - Or: npx @modelcontextprotocol/server-date-context@latest")
    
    print("\n6. **Check Cursor/Claude Desktop logs**")
    print("   - Look for connection timeout errors")
    print("   - Check MCP server logs")


async def main():
    """Run all diagnostics."""
    print("="*60)
    print("🔧 date-context MCP Connection Diagnostics")
    print("="*60)
    print(f"Time: {datetime.now().isoformat()}")
    
    # Run diagnostics
    await test_sse_keepalive()
    check_cursor_config()
    await test_date_context_stdio()
    
    # Print recommendations
    print_recommendations()
    
    print("\n" + "="*60)
    print("✅ Diagnostics complete")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())

