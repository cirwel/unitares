"""MCP streamable-HTTP transport client — the v2 path that lifts the 0.55 cap.

The REST surface ignores `continuity_token` and resolves identity by
ip_ua_fingerprint, so it is stuck at weak tier (confidence capped at 0.55). The
MCP transport DOES honor the signed `continuity_token`: passing it on every call
reaches `strong` tier (`proof_origin: caller_asserted`, `session_source:
continuity_token`), which removes the cap and unlocks the 0.6-1.0 confidence
bins. Verified live 2026-06-16.

Same interface as client.GovernanceClient (onboard / check_in / record_outcome /
calibration_check), so the runner/sampler/report are unchanged — only the
transport differs. Each call opens a short-lived MCP session (continuity_token is
portable proof, so a fresh session still resolves to strong); that trades a
handshake per call for zero persistent-session state.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .client import GovernanceError, Identity, _first
from .config import LOCUS, Transport


class MCPGovernanceClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self.t = transport or Transport()

    def _mcp_url(self) -> str:
        return self.t.base_url.rstrip("/") + "/mcp/"

    # --- transport -------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call_once(name, arguments or {}))

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.t.token}"} if self.t.token else None
        async with streamablehttp_client(self._mcp_url(), headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments)
                for c in res.content:
                    if getattr(c, "type", None) == "text":
                        return json.loads(c.text)
        raise GovernanceError(f"{name}: no text content in MCP result")

    # --- governance verbs (continuity_token threaded for strong tier) ----
    def onboard(self, display_name: str, *, spawn_reason: str = "new_session") -> Identity:
        res = self.call("onboard", {"force_new": True, "name": display_name, "spawn_reason": spawn_reason})
        agent_uuid = _first(res, "uuid", "agent_uuid", "agent_id")
        if not agent_uuid:
            raise GovernanceError(f"onboard returned no agent uuid; keys={list(res)}")
        ct = res.get("continuity_token")
        if not ct:
            raise GovernanceError("onboard returned no continuity_token; cannot reach strong tier")
        return Identity(
            agent_uuid=str(agent_uuid),
            client_session_id=_first(res, "client_session_id", "session_id"),
            raw=res,
            continuity_token=ct,
        )

    def check_in(
        self,
        ident: Identity,
        *,
        confidence: float,
        response_text: str,
        task_label: str,
        task_type: str = "testing",
        complexity: float = 0.5,
    ) -> str:
        res = self.call(
            "process_agent_update",
            {
                "agent_id": ident.agent_uuid,
                "continuity_token": ident.continuity_token,
                "confidence": confidence,
                "complexity": complexity,
                "task_type": task_type,
                "task_label": task_label,
                "response_text": response_text,
                "provenance_context": {"locus": LOCUS},
            },
        )
        pred_id = _first(res, "prediction_id")
        if not pred_id:
            raise GovernanceError(f"check_in minted no prediction_id; keys={list(res)}")
        return str(pred_id)

    def record_outcome(
        self,
        ident: Identity,
        *,
        prediction_id: str,
        is_bad: bool,
        outcome_score: float,
        detail: dict[str, Any],
        outcome_type: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "outcome_event",
            {
                "agent_id": ident.agent_uuid,
                "continuity_token": ident.continuity_token,
                "outcome_type": outcome_type or ("test_failed" if is_bad else "test_passed"),
                "is_bad": is_bad,
                "outcome_score": outcome_score,
                "prediction_id": prediction_id,
                "verification_source": "external_signal",
                "decision_action": "proceed",
                "detail": detail,
            },
        )

    def calibration_check(self, ident: Identity) -> dict[str, Any]:
        return self.call(
            "calibration",
            {"action": "check", "agent_id": ident.agent_uuid, "continuity_token": ident.continuity_token},
        )
