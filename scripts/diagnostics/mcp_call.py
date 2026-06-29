#!/usr/bin/env python3
"""
MCP Tool Caller - Clean CLI for calling MCP tools without shell quoting issues.

Usage:
    # List all tools
    python scripts/diagnostics/mcp_call.py --list

    # Call a tool
    python scripts/diagnostics/mcp_call.py process_agent_update agent_id=my_agent update_type=reflection content="Hello world"

    # With session binding
    python scripts/diagnostics/mcp_call.py --session my_session bind_identity agent_id=my_agent

    # Show tool schema
    python scripts/diagnostics/mcp_call.py --describe update_agent_metadata
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_URL = "http://127.0.0.1:8767"
SESSION_CACHE_DIR = ".unitares"
SESSION_CACHE_PREFIX = "session"
SESSION_CACHE_SUFFIX = ".json"
SESSION_CACHE_SKIP_TOOLS = {"onboard", "start_session"}
AUTO_LINEAGE_TOOLS = {"process_agent_update", "sync_state"}


def call_tool(
    base_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Call an MCP tool via HTTP."""
    url = f"{base_url}/v1/tools/call"
    data = json.dumps({"name": tool_name, "arguments": arguments}).encode()

    headers = {"Content-Type": "application/json"}
    if session_id:
        headers["X-Session-ID"] = session_id

    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "body": e.read().decode()}
    except urllib.error.URLError as e:
        return {"error": f"Connection failed: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _session_cache_paths(workspace: Path) -> list[Path]:
    cache_dir = workspace / SESSION_CACHE_DIR
    if not cache_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in cache_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == f"{SESSION_CACHE_PREFIX}{SESSION_CACHE_SUFFIX}":
            paths.append(path)
        elif (
            path.name.startswith(f"{SESSION_CACHE_PREFIX}-")
            and path.name.endswith(SESSION_CACHE_SUFFIX)
        ):
            paths.append(path)
    return paths


def _parse_timestamp(raw: Any, fallback: float) -> tuple[float, float]:
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(), fallback
        except ValueError:
            pass
    return 0.0, fallback


def latest_session_cache(workspace: Path) -> dict[str, Any] | None:
    """Return the newest local v2-ish session cache entry, if any.

    The diagnostics caller is stateless HTTP. Rehydrating the local
    ``client_session_id`` into both the JSON arguments and ``X-Session-ID``
    makes it behave like the normal adapter path without treating a legacy
    continuity token as a cross-process credential.
    """
    candidates: list[tuple[tuple[float, float], dict[str, Any]]] = []
    for path in _session_cache_paths(workspace):
        payload = _read_json(path)
        client_session_id = payload.get("client_session_id")
        uuid = payload.get("uuid")
        if not isinstance(client_session_id, str) or not client_session_id:
            continue
        if uuid is not None and not isinstance(uuid, str):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        sort_key = _parse_timestamp(payload.get("updated_at"), mtime)
        entry = dict(payload)
        entry["_path"] = str(path)
        candidates.append((sort_key, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def write_session_cache(
    workspace: Path,
    *,
    uuid: str,
    client_session_id: str,
    parent_agent_id: str | None = None,
) -> None:
    cache_dir = workspace / SESSION_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_slot = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in client_session_id
    )[:64]
    path = cache_dir / f"{SESSION_CACHE_PREFIX}-{safe_slot}{SESSION_CACHE_SUFFIX}"
    payload: dict[str, Any] = {
        "schema_version": 2,
        "uuid": uuid,
        "client_session_id": client_session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_agent_id:
        payload["parent_agent_id"] = parent_agent_id
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def resolve_session_binding(
    tool_name: str,
    arguments: dict[str, Any],
    explicit_session_id: str | None,
    *,
    workspace: Path,
    use_cache: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the session header and maybe inject ``client_session_id``.

    Explicit CLI/session arguments win. A supplied ``client_session_id`` also
    becomes the HTTP session header, which is the diagnostic-path gap this
    helper closes.
    """
    if explicit_session_id:
        return explicit_session_id, None

    client_session_id = arguments.get("client_session_id")
    if isinstance(client_session_id, str) and client_session_id:
        return client_session_id, None

    if not use_cache or tool_name in SESSION_CACHE_SKIP_TOOLS:
        return None, None

    entry = latest_session_cache(workspace)
    if not entry:
        return None, None
    cached_session_id = entry.get("client_session_id")
    if isinstance(cached_session_id, str) and cached_session_id:
        arguments.setdefault("client_session_id", cached_session_id)
        return cached_session_id, entry
    return None, entry


def is_identity_required(result: dict[str, Any]) -> bool:
    payload = result.get("result")
    if not isinstance(payload, dict):
        return False
    return payload.get("status") == "identity_required"


def maybe_retry_with_lineage(
    *,
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    workspace: Path,
    cached_entry: dict[str, Any] | None,
    result: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    """Mint a fresh lineage identity for check-ins after context rollover.

    This is intentionally limited to check-in tools. Arbitrary diagnostic calls
    should not mint identities as a side effect.
    """
    if not enabled or tool_name not in AUTO_LINEAGE_TOOLS:
        return result
    if not is_identity_required(result):
        return result

    entry = cached_entry or latest_session_cache(workspace)
    if not entry:
        return result
    parent_agent_id = entry.get("uuid") or entry.get("parent_agent_id")
    if not isinstance(parent_agent_id, str) or not parent_agent_id:
        return result

    onboard_args = {
        "force_new": True,
        "parent_agent_id": parent_agent_id,
        "spawn_reason": "new_session",
        "response_mode": "minimal",
    }
    onboard_result = call_tool(base_url, "onboard", onboard_args)
    onboard_payload = onboard_result.get("result")
    if not isinstance(onboard_payload, dict) or not onboard_payload.get("success"):
        return result

    new_uuid = onboard_payload.get("uuid")
    new_session_id = onboard_payload.get("client_session_id")
    if not isinstance(new_uuid, str) or not isinstance(new_session_id, str):
        return result

    write_session_cache(
        workspace,
        uuid=new_uuid,
        client_session_id=new_session_id,
        parent_agent_id=parent_agent_id,
    )
    arguments["client_session_id"] = new_session_id
    retried = call_tool(base_url, tool_name, arguments, session_id=new_session_id)
    if isinstance(retried, dict):
        retried["_mcp_call_auto_lineage"] = {
            "parent_agent_id": parent_agent_id,
            "client_session_id": new_session_id,
        }
    return retried


def list_tools(base_url: str) -> None:
    """List all available tools."""
    url = f"{base_url}/v1/tools"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            tools = data.get("tools", [])
            print(f"Available tools ({len(tools)}):\n")
            for tool in sorted(tools, key=lambda t: t.get("function", {}).get("name", "")):
                func = tool.get("function", {})
                name = func.get("name", "?")
                desc = func.get("description", "")[:60]
                print(f"  {name}")
                if desc:
                    print(f"    {desc}...")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def describe_tool(base_url: str, tool_name: str, session_id: Optional[str] = None) -> None:
    """Show tool schema."""
    result = call_tool(base_url, "describe_tool", {"tool_name": tool_name}, session_id)
    print(json.dumps(result, indent=2))


def parse_value(value: str) -> Any:
    """Parse a string value to appropriate type."""
    # Try JSON first (handles arrays, objects, booleans, numbers)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    # Try numeric
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        pass

    # Boolean strings
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False

    # Keep as string
    return value


def parse_arguments(args: list) -> Dict[str, Any]:
    """Parse key=value arguments."""
    result = {}
    for arg in args:
        if "=" not in arg:
            print(f"Warning: Ignoring argument without '=': {arg}", file=sys.stderr)
            continue
        key, value = arg.split("=", 1)
        result[key] = parse_value(value)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Call MCP tools without shell quoting issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list
  %(prog)s --describe process_agent_update
  %(prog)s --session my_session bind_identity agent_id=my_agent
  %(prog)s process_agent_update agent_id=test update_type=reflection content="test"
  %(prog)s search_knowledge_graph query=migration limit=5
  %(prog)s update_agent_metadata agent_id=test tags='["tag1","tag2"]'
        """,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="MCP server URL")
    parser.add_argument("--session", "-s", help="Session ID for X-Session-ID header")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace used for .unitares/session-*.json discovery",
    )
    parser.add_argument(
        "--no-session-cache",
        action="store_true",
        help="Do not infer client_session_id/X-Session-ID from .unitares cache",
    )
    parser.add_argument(
        "--no-auto-lineage",
        action="store_true",
        help=(
            "Do not mint a fresh lineage identity when a cached check-in "
            "returns identity_required"
        ),
    )
    parser.add_argument("--list", "-l", action="store_true", help="List available tools")
    parser.add_argument("--describe", "-d", metavar="TOOL", help="Describe a tool")
    parser.add_argument("--raw", "-r", action="store_true", help="Output raw JSON")
    parser.add_argument("tool", nargs="?", help="Tool name to call")
    parser.add_argument("args", nargs="*", help="Tool arguments as key=value pairs")

    args = parser.parse_args()

    if args.list:
        list_tools(args.url)
        return

    workspace = Path(args.workspace).expanduser().resolve()

    if args.describe:
        describe_tool(args.url, args.describe, args.session)
        return

    if not args.tool:
        parser.print_help()
        sys.exit(1)

    arguments = parse_arguments(args.args)
    session_id, cached_entry = resolve_session_binding(
        args.tool,
        arguments,
        args.session,
        workspace=workspace,
        use_cache=not args.no_session_cache,
    )
    result = call_tool(args.url, args.tool, arguments, session_id)
    result = maybe_retry_with_lineage(
        base_url=args.url,
        tool_name=args.tool,
        arguments=arguments,
        workspace=workspace,
        cached_entry=cached_entry,
        result=result,
        enabled=not args.no_auto_lineage,
    )

    if args.raw:
        print(json.dumps(result))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
