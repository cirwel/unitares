#!/usr/bin/env python3
"""
Test script to verify date-context MCP server connection and functionality.

This script tests the date-context server directly via stdio to verify it's working.
"""

import asyncio
import os
import sys
import json
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# date-context-mcp is a separate repo; set DATE_CONTEXT_MCP_ROOT to point at
# your local checkout. Defaults to a sibling of this checkout:
# $UNITARES_PROJECTS_ROOT/date-context-mcp, where UNITARES_PROJECTS_ROOT
# defaults to the parent directory of this repo.
_PROJECTS_ROOT = Path(
    os.environ.get("UNITARES_PROJECTS_ROOT") or Path(__file__).resolve().parents[3]
)
DATE_CONTEXT_MCP_ROOT = Path(
    os.environ.get("DATE_CONTEXT_MCP_ROOT") or _PROJECTS_ROOT / "date-context-mcp"
)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("❌ MCP SDK not available. Install with: pip install mcp")
    sys.exit(1)


async def test_date_context_connection():
    """Test date-context server connection and tools."""
    print("="*60)
    print("🔧 date-context MCP Connection Test")
    print("="*60)
    print(f"Time: {datetime.now().isoformat()}\n")
    
    # Server configuration (matches Cursor config)
    server_path = DATE_CONTEXT_MCP_ROOT / "src" / "mcp_server.py"

    if not server_path.exists():
        print(f"❌ Server file not found: {server_path}")
        print(f"   Set DATE_CONTEXT_MCP_ROOT to override the default location.")
        return False

    print(f"✅ Server file found: {server_path}")

    # Configure server parameters
    server_params = StdioServerParameters(
        command="python3",
        args=[str(server_path)],
        env={
            "PYTHONPATH": str(DATE_CONTEXT_MCP_ROOT)
        }
    )
    
    print("\n🔌 Attempting to connect to date-context server...")
    
    try:
        async with stdio_client(server_params) as (read, write):
            print("✅ stdio streams established")
            
            async with ClientSession(read, write) as session:
                print("✅ Client session created")
                
                # Initialize the session
                print("\n📡 Initializing session...")
                init_result = await session.initialize()
                print(f"✅ Session initialized: {init_result.serverInfo.name} v{init_result.serverInfo.version}")
                
                # List available tools
                print("\n🔧 Listing available tools...")
                tools_result = await session.list_tools()
                tools = tools_result.tools
                print(f"✅ Found {len(tools)} tool(s):")
                for tool in tools:
                    print(f"   - {tool.name}: {tool.description}")
                
                # Test get_current_date
                print("\n📅 Testing get_current_date tool...")
                try:
                    result = await session.call_tool("get_current_date", {})
                    print(f"✅ Tool call successful")
                    for item in result.content:
                        if hasattr(item, 'text'):
                            print(f"   Response: {item.text[:200]}")
                        elif hasattr(item, 'type'):
                            print(f"   Content type: {item.type}")
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                    return False
                
                # Test get_date_context
                print("\n📊 Testing get_date_context tool...")
                try:
                    result = await session.call_tool("get_date_context", {})
                    print(f"✅ Tool call successful")
                    for item in result.content:
                        if hasattr(item, 'text'):
                            print(f"   Response: {item.text[:200]}")
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                    return False
                
                print("\n✅ All tests passed! Server is working correctly.")
                return True
                
    except BrokenPipeError:
        print("❌ Connection broken - server may have exited")
        return False
    except ConnectionResetError:
        print("❌ Connection reset - server may have crashed")
        return False
    except Exception as e:
        print(f"❌ Connection failed: {type(e).__name__}: {e}")
        import traceback
        print("\nTraceback:")
        print(traceback.format_exc())
        return False


async def check_server_process():
    """Check if date-context server process is running."""
    print("\n" + "="*60)
    print("🔍 Checking for running date-context processes...")
    print("="*60)
    
    import subprocess
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        processes = result.stdout.split('\n')
        date_context_processes = [
            p for p in processes 
            if 'mcp_server.py' in p or 'date-context' in p.lower()
        ]
        
        if date_context_processes:
            print(f"✅ Found {len(date_context_processes)} process(es):")
            for proc in date_context_processes[:5]:  # Show first 5
                # Clean up the process line
                parts = proc.split()
                if len(parts) > 10:
                    print(f"   PID {parts[1]}: {' '.join(parts[10:15])}")
        else:
            print("ℹ️  No date-context processes currently running")
            print("   (This is normal - stdio servers start on-demand)")
            
    except Exception as e:
        print(f"⚠️  Could not check processes: {e}")


def check_cursor_config():
    """Check Cursor MCP configuration."""
    print("\n" + "="*60)
    print("📋 Checking Cursor MCP Configuration")
    print("="*60)
    
    config_path = Path.home() / ".cursor" / "mcp.json"
    
    if not config_path.exists():
        print(f"❌ Config file not found: {config_path}")
        return
    
    print(f"✅ Config file found: {config_path}")
    
    try:
        with open(config_path) as f:
            config = json.load(f)
        
        mcp_servers = config.get("mcpServers", {})
        
        if "date-context" in mcp_servers:
            date_config = mcp_servers["date-context"]
            print("\n✅ date-context configuration:")
            print(f"   Command: {date_config.get('command', 'N/A')}")
            print(f"   Args: {date_config.get('args', [])}")
            print(f"   Env: {date_config.get('env', {})}")
            
            # Verify paths exist
            if "args" in date_config and date_config["args"]:
                server_path = Path(date_config["args"][0])
                if server_path.exists():
                    print(f"   ✅ Server file exists: {server_path}")
                else:
                    print(f"   ❌ Server file NOT found: {server_path}")
        else:
            print("❌ date-context not found in configuration")
            
    except Exception as e:
        print(f"❌ Error reading config: {e}")


async def main():
    """Run all tests."""
    check_cursor_config()
    await check_server_process()
    
    print("\n" + "="*60)
    print("🧪 Running Connection Tests")
    print("="*60)
    
    success = await test_date_context_connection()
    
    print("\n" + "="*60)
    if success:
        print("✅ ALL TESTS PASSED")
        print("\nThe date-context server is working correctly.")
        print("If Cursor shows it as 'disconnected', this may be a UI issue.")
        print("Try using a date-context tool in Cursor to verify.")
    else:
        print("❌ TESTS FAILED")
        print("\nThe date-context server has issues.")
        print("Check the error messages above for details.")
    print("="*60)
    
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

