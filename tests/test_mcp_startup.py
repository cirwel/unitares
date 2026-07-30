#!/usr/bin/env python3
"""
Test MCP server startup and tool loading
"""
import sys
import traceback

def test_mcp_startup():
    """Test all components needed for MCP server startup"""
    errors = []
    
    print("Testing MCP server startup components...\n")
    
    # Test 1: Tool schemas
    try:
        print("1. Loading tool schemas...")
        from src.tool_schemas import get_tool_definitions
        from src.mcp_compat import get_tool_input_schema
        tools = get_tool_definitions()
        print(f"   ✓ Loaded {len(tools)} tools")

        # Validate each tool
        for tool in tools:
            if not tool.name:
                errors.append(f"Tool missing name: {tool}")
            schema = get_tool_input_schema(tool)
            if not schema:
                errors.append(f"{tool.name}: Missing input schema")
            if not isinstance(schema, dict):
                errors.append(f"{tool.name}: input schema must be dict")
            elif schema.get('type') != 'object':
                errors.append(f"{tool.name}: input schema.type must be 'object'")
    except Exception as e:
        errors.append(f"Tool schemas: {e}")
        traceback.print_exc()
    
    # Test 2: Tool handlers
    try:
        print("2. Loading tool handlers...")
        from src.mcp_handlers import TOOL_HANDLERS
        print(f"   ✓ Loaded {len(TOOL_HANDLERS)} handlers")
        
        # Check for mismatches
        from src.tool_schemas import get_tool_definitions
        schema_tools = {t.name for t in get_tool_definitions()}
        handler_tools = set(TOOL_HANDLERS.keys())
        
        missing_handlers = schema_tools - handler_tools
        extra_handlers = handler_tools - schema_tools
        
        if missing_handlers:
            errors.append(f"Tools in schema but no handler: {missing_handlers}")
        if extra_handlers:
            errors.append(f"Handlers but no schema: {extra_handlers}")
    except Exception as e:
        errors.append(f"Tool handlers: {e}")
        traceback.print_exc()
    
    # Test 3: MCP server import
    try:
        print("3. Importing MCP server...")
        from src.mcp_server_std import server
        print("   ✓ Server imported")

        # Check list_tools registration. mcp 1.x exposes the decorator method
        # `list_tools`; 2.x registers handlers by method string in
        # `_request_handlers` ("tools/list").
        has_list_tools = hasattr(server, "list_tools") or (
            "tools/list" in getattr(server, "_request_handlers", {})
        )
        if has_list_tools:
            print("   ✓ list_tools registered")
        else:
            errors.append("server.list_tools not found")
    except Exception as e:
        errors.append(f"MCP server import: {e}")
        traceback.print_exc()
    
    # Test 4: JSON serialization
    try:
        print("4. Testing JSON serialization...")
        import json
        from src.tool_schemas import get_tool_definitions
        from src.mcp_compat import get_tool_input_schema
        tools = get_tool_definitions()

        for tool in tools:
            try:
                json.dumps(get_tool_input_schema(tool))
            except Exception as e:
                errors.append(f"{tool.name}: JSON serialization failed: {e}")
        
        print(f"   ✓ All {len(tools)} tools serialize to JSON")
    except Exception as e:
        errors.append(f"JSON serialization: {e}")
        traceback.print_exc()
    
    # Summary
    print("\n" + "="*60)
    if errors:
        print(f"❌ Found {len(errors)} errors:")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print("✅ All startup checks passed!")
        print(f"   - {len(tools)} tools ready")
        print(f"   - {len(TOOL_HANDLERS)} handlers registered")
        print("   - MCP server should start successfully")
        return 0

if __name__ == "__main__":
    sys.exit(test_mcp_startup())

