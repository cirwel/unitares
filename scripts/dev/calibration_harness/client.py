"""Thin REST wrapper over the UNITARES governance server.

Transport contract (confirmed live 2026-06-16):
    POST {base}/v1/tools/call
    headers: Authorization: Bearer <token>, Content-Type: application/json
    body:    {"name": <tool>, "arguments": {...}}
    resp:    {"name": ..., "success": true, "result": {...}}  (result is the
             tool's own dict; on failure success=false / no result)

Only stdlib urllib is used (matches src/mcp_server_std.py's proxy), so the
harness has no third-party transport dependency.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import LOCUS, Transport


class GovernanceError(RuntimeError):
    """Raised when a tools/call returns success=false or HTTP errors."""


@dataclass
class Identity:
    """A bound harness identity; the quarantine boundary for a class."""

    agent_uuid: str
    client_session_id: str | None
    raw: dict[str, Any]
    # Signed ownership proof. Reaches strong tier (lifting the 0.55 cap) ONLY
    # over the MCP transport; ignored by the REST surface. See client_mcp.py.
    continuity_token: str | None = None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


class GovernanceClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self.t = transport or Transport()

    # --- low-level -------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"name": name, "arguments": arguments or {}}).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.t.token:
            headers["Authorization"] = f"Bearer {self.t.token}"
        req = urllib.request.Request(
            f"{self.t.base_url}/v1/tools/call", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.t.timeout_s) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:  # noqa: PERF203 - want the body
            raise GovernanceError(f"{name}: HTTP {e.code}: {e.read().decode()[:300]}") from e
        if isinstance(payload, dict) and payload.get("success") is True and "result" in payload:
            return payload["result"]
        raise GovernanceError(f"{name}: non-success payload: {json.dumps(payload)[:300]}")

    # --- governance verbs ------------------------------------------------
    def onboard(self, display_name: str, *, spawn_reason: str = "new_session") -> Identity:
        res = self.call(
            "onboard",
            {
                "force_new": True,
                "name": display_name,
                "spawn_reason": spawn_reason,
            },
        )
        # Prefer the canonical `uuid`; `agent_id` may be a slot alias (anon_*).
        agent_uuid = _first(res, "uuid", "agent_uuid", "agent_id")
        if not agent_uuid:
            raise GovernanceError(f"onboard returned no agent uuid; keys={list(res)}")
        csid = _first(res, "client_session_id", "session_id")
        ct = res.get("continuity_token")
        return Identity(agent_uuid=str(agent_uuid), client_session_id=csid, raw=res, continuity_token=ct)

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
        """process_agent_update; returns the minted tactical prediction_id."""
        res = self.call(
            "process_agent_update",
            {
                "agent_id": ident.agent_uuid,
                "client_session_id": ident.client_session_id,
                "confidence": confidence,
                "complexity": complexity,
                "task_type": task_type,
                "task_label": task_label,
                "response_text": response_text,
                "provenance_context": {"locus": LOCUS, "harness_class": ident.agent_uuid},
            },
        )
        pred_id = _first(res, "prediction_id")
        if not pred_id:
            raise GovernanceError(
                f"check_in minted no prediction_id (confidence required); keys={list(res)}"
            )
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
                "client_session_id": ident.client_session_id,
                "outcome_type": outcome_type or ("test_failed" if is_bad else "test_passed"),
                "is_bad": is_bad,
                "outcome_score": outcome_score,
                "prediction_id": prediction_id,
                "verification_source": "external_signal",  # -> externally_verified, weight 1.0
                "decision_action": "proceed",
                "detail": detail,
            },
        )

    def calibration_check(self, ident: Identity) -> dict[str, Any]:
        return self.call(
            "calibration",
            {
                "action": "check",
                "agent_id": ident.agent_uuid,
                "client_session_id": ident.client_session_id,
            },
        )
