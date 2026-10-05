#!/usr/bin/env python3
"""
UNITARES Lite — Simple wrapper for the 3 essential tools

Usage:
    python3 scripts/unitares_lite.py onboard [name="YourName"]
    python3 scripts/unitares_lite.py update "What you did" [complexity=0.5] [confidence=0.7]
    python3 scripts/unitares_lite.py metrics
    python3 scripts/unitares_lite.py status  # Quick status check

This is a simple wrapper that makes the 3 essential tools easy to use.
For full functionality, use the MCP tools directly.
"""

import sys
import os
import json
import urllib.request
import urllib.error
from pathlib import Path

# Session file to persist client_session_id
SESSION_FILE = Path(__file__).parent.parent.parent / ".mcp_session"

# Default server URL (Streamable HTTP)
DEFAULT_URL = os.getenv("UNITARES_URL", "http://127.0.0.1:8767")


def load_session() -> str:
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
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(session_id)
    except Exception:
        pass


def call_tool(tool_name, arguments=None):
    """Call an MCP tool via HTTP and return the result."""
    url = f"{DEFAULT_URL}/v1/tools/call"
    args = arguments or {}
    
    # Inject session ID if available
    session_id = load_session()
    if session_id and "client_session_id" not in args:
        args["client_session_id"] = session_id
    
    data = json.dumps({"name": tool_name, "arguments": args}).encode()
    headers = {"Content-Type": "application/json"}
    
    if session_id:
        headers["X-Session-ID"] = session_id
    
    req = urllib.request.Request(url, data=data, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            envelope = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ""
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}", "body": error_body}
    except urllib.error.URLError as e:
        return {"success": False, "error": f"Connection failed: {e.reason}. Is the server running at {DEFAULT_URL}?"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    # /v1/tools/call wraps the tool's own payload as {"name", "result",
    # "success"}; the session id and any identity refusal are inside it.
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope

    # Save session ID if returned
    if result.get("client_session_id"):
        save_session(result["client_session_id"])

    # An identity refusal is success-shaped by design, so report it as the
    # failure it is rather than as a call that worked.
    refusal = _identity_refusal_status(result)
    if refusal:
        detail = result.get("hint") or result.get("next_step") or ""
        return {"success": False, "refused": refusal,
                "error": f"identity refused ({refusal}). {detail}".strip()}

    return result


def _identity_refusal_status(payload) -> str | None:
    """Mirror of identity_bootstrap.identity_refusal_status for this
    standalone client: a typed strict-identity refusal carries
    rollout_flag=STRICT_IDENTITY_REQUIRED and its status."""
    if not isinstance(payload, dict) or payload.get("rollout_flag") != "STRICT_IDENTITY_REQUIRED":
        return None
    return str(payload.get("status") or "identity_required")


def onboard_cmd(name=None):
    """Call onboard() tool for a fresh identity.

    force_new is explicit: under strict identity (the server default) an
    onboard with neither force_new nor parent_agent_id is refused as
    lineage_declaration_required.
    """
    args = {"force_new": True}
    if name:
        args["name"] = name
    
    result = call_tool("onboard", args)
    
    if result.get("success"):
        agent_id = result.get("agent_id") or result.get("result", {}).get("agent_id")
        uuid = result.get("uuid") or result.get("result", {}).get("uuid")
        print(f"✅ Onboarded!")
        if agent_id:
            print(f"   Agent ID: {agent_id}")
        if uuid:
            print(f"   UUID: {uuid[:8]}...")
        print(f"\n💡 Next: Use 'update' to log your work")
        return result
    else:
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
        return result


def update_cmd(response_text, complexity=None, confidence=None):
    """Call process_agent_update() tool."""
    args = {"response_text": response_text}
    
    if complexity is not None:
        args["complexity"] = float(complexity)
    if confidence is not None:
        args["confidence"] = float(confidence)
    
    result = call_tool("process_agent_update", args)
    
    if result.get("success") or "decision" in result or "metrics" in result:
        # Extract key info
        decision = result.get("decision") or result.get("result", {}).get("decision", {})
        metrics = result.get("metrics") or result.get("result", {}).get("metrics", {})
        
        action = decision.get("action", "unknown")
        verdict_emoji = "✅" if action == "proceed" else "⚠️" if action == "caution" else "⏸️"
        
        print(f"{verdict_emoji} Verdict: {action.upper()}")
        
        if decision.get("reason"):
            print(f"   Reason: {decision['reason']}")
        
        if metrics:
            e = metrics.get("E", "?")
            i = metrics.get("I", "?")
            s = metrics.get("S", "?")
            coherence = metrics.get("coherence", "?")
            print(f"\n📊 Metrics:")
            print(f"   Energy (E): {e}")
            print(f"   Integrity (I): {i}")
            print(f"   Entropy (S): {s}")
            print(f"   Coherence: {coherence}")
        
        return result
    else:
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
        return result


def metrics_cmd():
    """Call get_governance_metrics() tool."""
    result = call_tool("get_governance_metrics", {})
    
    if result.get("success") or "E" in result or "metrics" in result:
        metrics = result.get("metrics") or result.get("result", {}).get("metrics", {}) or result
        
        e = metrics.get("E", "?")
        i = metrics.get("I", "?")
        s = metrics.get("S", "?")
        v = metrics.get("V", "?")
        coherence = metrics.get("coherence", "?")
        status = metrics.get("status") or result.get("status", "?")
        risk = metrics.get("risk_score", "?")
        
        print(f"📊 Your Current State:")
        print(f"   Status: {status}")
        print(f"   Energy (E): {e}")
        print(f"   Integrity (I): {i}")
        print(f"   Entropy (S): {s}")
        print(f"   Void (V): {v}")
        print(f"   Coherence: {coherence}")
        print(f"   Risk Score: {risk}")
        
        return result
    else:
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
        return result


def status_cmd():
    """Quick status check - combines identity and metrics."""
    print("🔍 Checking status...\n")
    
    # Get identity
    identity_result = call_tool("identity", {})
    if identity_result.get("success") or "agent_id" in identity_result:
        agent_id = identity_result.get("agent_id") or identity_result.get("result", {}).get("agent_id", "Unknown")
        print(f"👤 Agent ID: {agent_id}\n")
    
    # Get metrics
    metrics_cmd()


def parse_args():
    """Parse command line arguments."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1].lower()
    args = {}
    
    # Parse key=value arguments
    for arg in sys.argv[2:]:
        if "=" in arg:
            key, value = arg.split("=", 1)
            # Try to parse as number
            try:
                if "." in value:
                    args[key] = float(value)
                else:
                    args[key] = int(value)
            except ValueError:
                args[key] = value
        else:
            # Positional argument (for update command)
            if command == "update" and "response_text" not in args:
                args["response_text"] = arg
    
    return command, args


def main():
    """Main entry point."""
    command, args = parse_args()
    
    if command == "onboard":
        name = args.get("name")
        onboard_cmd(name)
    
    elif command == "update":
        response_text = args.get("response_text")
        if not response_text:
            print("❌ Error: 'update' requires a response_text")
            print("Usage: unitares_lite.py update 'What you did' [complexity=0.5] [confidence=0.7]")
            sys.exit(1)
        update_cmd(
            response_text,
            complexity=args.get("complexity"),
            confidence=args.get("confidence")
        )
    
    elif command == "metrics":
        metrics_cmd()
    
    elif command == "status":
        status_cmd()
    
    else:
        print(f"❌ Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Interrupted")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
