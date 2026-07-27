#!/usr/bin/env python3
"""Provision the standing dispatcher identity for governed reviewer spawns.

One-time operator step for activating UNITARES_DIALECTIC_GOVERNED_SPAWN (see
docs/proposals/governed-reviewer-spawn-v0.md). Onboards a dedicated identity
via the gov-mcp REST surface and lands one sync_state so the identity has a
durable core.agent_state row — that makes the §6 behavioral veto read a real
(if largely static) posture for this proposer instead of the unknown-proposer
fail-open branch.

Usage:
    python3 scripts/ops/provision-dialectic-dispatcher.py [--url http://127.0.0.1:8767]

Then add to the gov-mcp plist (bootout + bootstrap, NOT kickstart — kickstart
reuses the old env):
    UNITARES_DIALECTIC_DISPATCHER_UUID=<printed uuid>
    UNITARES_DIALECTIC_GOVERNED_SPAWN=1
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _call(url: str, tool: str, arguments: dict) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/tools/call",
        data=json.dumps({"name": tool, "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8767")
    args = parser.parse_args()

    onboarded = _call(
        args.url,
        "onboard",
        {
            "name": "DialecticDispatcher",
            "force_new": True,
            "spawn_reason": "system_service",
        },
    )
    text = json.dumps(onboarded)
    uuid = (
        onboarded.get("uuid")
        or onboarded.get("agent_uuid")
        or (onboarded.get("result") or {}).get("uuid")
    )
    if not uuid:
        # Tool results can arrive as wrapped content; fall back to a scan.
        import re

        m = re.search(
            r'"(?:uuid|agent_uuid)"\s*:\s*"([0-9a-f-]{36})"', text, re.IGNORECASE
        )
        uuid = m.group(1) if m else None
    if not uuid:
        print("onboard did not return a uuid; raw response:\n" + text[:2000])
        return 1

    client_session_id = None
    m2 = None
    if isinstance(onboarded, dict):
        client_session_id = onboarded.get("client_session_id")
    if not client_session_id:
        import re

        m2 = re.search(r'"client_session_id"\s*:\s*"([^"]+)"', text)
        client_session_id = m2.group(1) if m2 else None

    sync_args = {
        "response_text": (
            "DialecticDispatcher provisioned: standing proposer identity for "
            "governed reviewer spawns (agent_spawn effects via /v1/effects). "
            "Server-side system identity; continuity tokens for it are minted "
            "in-process by gov-mcp at dispatch time."
        ),
        "complexity": 0.1,
        "confidence": 0.9,
    }
    if client_session_id:
        sync_args["client_session_id"] = client_session_id
    synced = _call(args.url, "sync_state", sync_args)
    ok = "error" not in json.dumps(synced).lower()[:200]

    print(f"dispatcher uuid: {uuid}")
    print(f"initial sync_state: {'ok' if ok else 'CHECK MANUALLY'}")
    print()
    print("Add to the gov-mcp plist EnvironmentVariables, then bootout+bootstrap:")
    print(f"  UNITARES_DIALECTIC_DISPATCHER_UUID={uuid}")
    print("  UNITARES_DIALECTIC_GOVERNED_SPAWN=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
