#!/usr/bin/env python3
"""Recover a paused resident by calling self_recovery as that agent.

Usage: recover_paused_resident.py <anchor-name>
  where <anchor-name> matches ~/.unitares/anchors/<anchor-name>.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "sdk" / "src"))
from unitares_sdk import SyncGovernanceClient

GOV_REST_URL = "http://127.0.0.1:8767/v1/tools/call"
ANCHOR_DIR = Path.home() / ".unitares" / "anchors"


def main(arg: str) -> int:
    if len(arg) == 36 and arg.count("-") == 4:
        uuid = arg
        token = None
        print(f"using bare UUID (no anchor): {uuid}")
    else:
        anchor_path = ANCHOR_DIR / f"{arg}.json"
        if not anchor_path.exists():
            print(f"anchor not found: {anchor_path}", file=sys.stderr)
            return 2
        anchor = json.loads(anchor_path.read_text())
        uuid = anchor["agent_uuid"]
        token = anchor.get("continuity_token")

    client = SyncGovernanceClient(rest_url=GOV_REST_URL, transport="rest", timeout=30)
    if token:
        client.continuity_token = token
    try:
        ident = client.identity(agent_uuid=uuid, resume=True)
        print(f"identity bound: uuid={uuid[:12]} ({ident!r})")
        # Refresh client's continuity_token from the identity response so subsequent
        # REST calls carry proof of the bound identity.
        if getattr(ident, "continuity_token", None):
            client.continuity_token = ident.continuity_token
        if getattr(ident, "client_session_id", None):
            client.client_session_id = ident.client_session_id
        print(f"client state: session={client.client_session_id} token_present={bool(client.continuity_token)} uuid={client.agent_uuid}")
    except Exception as e:
        print(f"identity bind failed: {e}", file=sys.stderr)
        return 1

    reflection = (
        "Pause originated 2026-05-15 ~08:30 local when Mac entered sleep — coincident "
        "across Watcher, Sentinel, and Lumen, all silenced in the same minute. Matches "
        "the auto_attest sleep-wake artifact pattern (Mac clamshell / Maintenance-Sleep "
        "spikes risk_score on near-identical inputs and falsely pauses residents). The "
        "in-memory pause persisted across sleep because the governance MCP server stayed "
        "up. Operator was AFK on Mercor/applications and did not see the silence. "
        "Recovery: bulk operator-initiated; no behavior change needed in agent code. "
        "Follow-up tracked: auto_attest sleep-wake artifact memory entry."
    )
    try:
        # Call via call_tool directly so we can pass explicit continuity_token
        # and bypass any path where the sync_client's identity tracking gets lost.
        result = client.call_tool(
            "self_recovery",
            {
                "action": "review",
                "reflection": reflection,
                "agent_id": uuid,
                "continuity_token": client.continuity_token,
                "client_session_id": client.client_session_id,
            },
        )
        print(f"recovery result: {json.dumps(result, default=str, indent=2)}")
        return 0
    except Exception as e:
        print(f"self_recovery (review) failed: {e}", file=sys.stderr)
        try:
            result = client.self_recovery(action="check")
            print(f"diagnostic: {json.dumps(result, default=str, indent=2)}")
        except Exception as e2:
            print(f"diagnostic also failed: {e2}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
