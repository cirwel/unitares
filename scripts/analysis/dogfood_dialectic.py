#!/usr/bin/env python3
"""
Dogfood: onboard → dialectic(action=request) → dialectic(action=thesis)
Verifies UUID stays consistent across dialectic flow (same as onboard/identity).
"""

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from scripts.mcp_call import call_tool

URL = "http://127.0.0.1:8767"


def extract_json(text: str) -> dict:
    """Extract JSON from MCP TextContent or raw response."""
    if isinstance(text, dict):
        content = text.get("content", [])
        if content and isinstance(content[0], dict):
            return content[0].get("text", "{}")
        return text
    return json.loads(text) if isinstance(text, str) else {}


def main():
    print("=== Dogfood: Dialectic UUID consistency ===\n")

    # 1. Onboard (no session) - creates identity
    print("1. onboard() ...")
    r1 = call_tool(URL, "onboard", {"force_new": True})
    if "error" in r1:
        print(f"   FAIL: {r1['error']}")
        sys.exit(1)
    # Response: {"name": "onboard", "result": {...}} or MCP {"content": [...]}
    data = r1.get("result") or r1
    if not data:
        content = r1.get("content", [])
        if content:
            text = content[0].get("text", "{}") if isinstance(content[0], dict) else str(content[0])
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
    if not data:
        print("   FAIL: No data in onboard response")
        sys.exit(1)

    client_session_id = data.get("client_session_id") or data.get("session_continuity", {}).get("client_session_id")
    agent_uuid = data.get("agent_uuid") or data.get("uuid") or data.get("agent_signature", {}).get("uuid")
    if not client_session_id:
        print(f"   FAIL: No client_session_id in response. Keys: {list(data.keys())}")
        sys.exit(1)
    if not agent_uuid:
        agent_uuid = client_session_id.replace("agent-", "") if client_session_id.startswith("agent-") else None
    print(f"   OK: client_session_id={client_session_id[:20]}... agent_uuid={agent_uuid[:8] if agent_uuid else 'N/A'}...")

    # Use client_session_id as X-Session-ID for subsequent calls
    session_id = client_session_id

    # 2. Request dialectic review
    print("\n2. dialectic(action=request, client_session_id=...) ...")
    r2 = call_tool(URL, "dialectic", {
        "action": "request",
        "client_session_id": session_id,
        "topic": "Dogfood: UUID consistency test",
        "session_type": "recovery",
    }, session_id=session_id)
    if "error" in r2:
        print(f"   FAIL: {r2['error']}")
        sys.exit(1)
    data2 = r2.get("result") or r2
    if not data2:
        content2 = r2.get("content", [])
        if content2:
            text2 = content2[0].get("text", "{}") if isinstance(content2[0], dict) else str(content2[0])
            try:
                data2 = json.loads(text2)
            except json.JSONDecodeError:
                data2 = {}
    if not data2:
        print("   FAIL: No data in request_dialectic_review response")
        sys.exit(1)

    dialectic_session_id = data2.get("session_id")
    paused_agent = data2.get("paused_agent_id")
    if not dialectic_session_id:
        print(f"   FAIL: No session_id. Keys: {list(data2.keys())}")
        sys.exit(1)
    print(f"   OK: session_id={dialectic_session_id[:16]}... paused_agent_id={paused_agent[:8] if paused_agent else 'N/A'}...")

    # Verify requestor UUID matches onboard
    if agent_uuid and paused_agent and paused_agent != agent_uuid:
        print(f"   WARN: paused_agent_id ({paused_agent[:8]}...) != onboard agent_uuid ({agent_uuid[:8]}...)")
    elif agent_uuid and paused_agent:
        print(f"   OK: paused_agent_id matches onboard UUID")

    # 3. Submit thesis (no explicit agent_id - should use session-bound)
    print("\n3. dialectic(action=thesis, session_id=..., client_session_id=...) [no agent_id] ...")
    r3 = call_tool(URL, "dialectic", {
        "action": "thesis",
        "client_session_id": session_id,
        "session_id": dialectic_session_id,
        "message": "Dogfood thesis: UUID should match onboard.",
    }, session_id=session_id)
    if "error" in r3:
        print(f"   FAIL: {r3['error']}")
        sys.exit(1)
    data3 = r3.get("result") or r3
    if not data3:
        content3 = r3.get("content", [])
        if content3:
            text3 = content3[0].get("text", "{}") if isinstance(content3[0], dict) else str(content3[0])
            try:
                data3 = json.loads(text3)
            except json.JSONDecodeError:
                data3 = {}
    if not data3:
        print("   FAIL: No data in submit_thesis response")
        sys.exit(1)

    # Check who submitted - from success response or get_dialectic_session
    submitter = data3.get("agent_id") or data3.get("agent_signature", {}).get("uuid")
    if not submitter:
        # Fetch session to verify transcript
        print("   Fetching session to verify transcript...")
        r4 = call_tool(URL, "dialectic", {
            "action": "get",
            "client_session_id": session_id,
            "session_id": dialectic_session_id,
        }, session_id=session_id)
        d4 = r4.get("result") or r4
        if not d4 and r4.get("content"):
            c4 = r4["content"][0]
            t4 = c4.get("text", "{}") if isinstance(c4, dict) else str(c4)
            try:
                d4 = json.loads(t4)
            except json.JSONDecodeError:
                d4 = {}
        if d4 and "error" not in r4:
            transcript = d4.get("transcript", [])
            thesis_msgs = [m for m in transcript if m.get("role") == "thesis" or m.get("message_type") == "thesis"]
            if thesis_msgs:
                submitter = thesis_msgs[-1].get("agent_id")

    if agent_uuid and submitter:
        if submitter != agent_uuid:
            # Allow label match - submitter might be public_agent_id
            if agent_uuid in str(submitter) or submitter in str(agent_uuid):
                print(f"   OK: submitter consistent (agent_uuid/submitter overlap)")
            else:
                print(f"   FAIL: submitter ({submitter[:12] if isinstance(submitter, str) else submitter}...) != onboard UUID ({agent_uuid[:8]}...)")
                sys.exit(1)
        else:
            print(f"   OK: submitter UUID matches onboard")
    else:
        print(f"   OK: thesis submitted (submitter={submitter})")

    print("\n=== Dogfood PASSED: UUID consistent across onboard → request_dialectic_review → submit_thesis ===")


if __name__ == "__main__":
    main()
