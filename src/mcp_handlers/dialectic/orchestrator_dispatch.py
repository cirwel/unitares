"""Dispatch an independent dialectic reviewer through the agent-orchestrator.

This is the orchestrator's first live consumer (Decision A of the 2026-06-24
Wave-3 gate). Design (b), escalation tier: the in-process synthetic reviewer
stays the default and the fallback — orchestrated dispatch is opt-in via
``UNITARES_DIALECTIC_ORCHESTRATED_REVIEW`` and ANY failure here returns None so
``handle_submit_thesis`` degrades to the in-process path. The orchestrator being
down therefore never breaks dialectic; it is an enhancement (a governed,
own-identity, lease-capable reviewer process), not a dependency.

The spawned reviewer (``python3 -m agents.dialectic_reviewer``) onboards with its
OWN identity, claims the still-open reviewer slot via the multi-agent path
(submit_antithesis), submits its verdict, and exits — so on successful dispatch
the handler must NOT also run the in-process review.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Repo root: src/mcp_handlers/dialectic/orchestrator_dispatch.py -> repo
_REPO_ROOT = Path(__file__).resolve().parents[3]


def orchestrated_review_enabled() -> bool:
    """Opt-in gate for routing reviews through the orchestrator (default OFF).

    OFF preserves today's behaviour exactly (in-process synthetic reviewer). ON
    makes the orchestrator the first-choice reviewer with in-process as fallback.
    """
    return os.environ.get(
        "UNITARES_DIALECTIC_ORCHESTRATED_REVIEW", "0"
    ).strip().lower() in ("1", "true", "yes", "on")


def _orchestrator_url() -> str:
    return os.environ.get("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8789").rstrip("/")


def _governance_url() -> str:
    return (
        os.environ.get("UNITARES_GOVERNANCE_URL")
        or os.environ.get("GOVERNANCE_URL")
        or "http://127.0.0.1:8767"
    )


def _build_spec(session_id: str, thesis: Dict[str, Any], parent_agent_id: Optional[str]) -> Dict[str, Any]:
    """Translate the thesis into an orchestrator POST /v1/agents spec.

    The reviewer reads everything from env (see Thesis.from_env + reviewer.main).
    PYTHONPATH must include the repo root and the SDK src so the spawned process
    can import ``agents.dialectic_reviewer`` and ``unitares_sdk``.
    """
    conditions = thesis.get("proposed_conditions") or []
    if not isinstance(conditions, list):
        conditions = [str(conditions)]

    pythonpath = os.pathsep.join(
        [str(_REPO_ROOT), str(_REPO_ROOT / "agents" / "sdk" / "src")]
    )
    existing_pp = os.environ.get("PYTHONPATH")
    if existing_pp:
        pythonpath = pythonpath + os.pathsep + existing_pp

    env: Dict[str, str] = {
        "DIALECTIC_SESSION_ID": session_id,
        "DIALECTIC_THESIS_ROOT_CAUSE": thesis.get("root_cause") or "",
        "DIALECTIC_THESIS_CONDITIONS": json.dumps(conditions),
        "DIALECTIC_THESIS_REASONING": thesis.get("reasoning") or "",
        "DIALECTIC_THESIS_SITUATION": thesis.get("situation") or "",
        "UNITARES_GOVERNANCE_URL": _governance_url(),
        "PYTHONPATH": pythonpath,
    }
    if parent_agent_id:
        env["UNITARES_PARENT_AGENT_ID"] = parent_agent_id

    return {
        "cmd": sys.executable,  # the MCP process's own interpreter has the deps
        "args": ["-m", "agents.dialectic_reviewer"],
        "cd": str(_REPO_ROOT),
        "env": env,
    }


async def dispatch_orchestrated_review(
    session_id: str,
    thesis: Dict[str, Any],
    parent_agent_id: Optional[str],
    *,
    timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """POST a reviewer-spawn spec to the orchestrator. Returns its JSON (the spawned
    agent id/status) on success, or None on ANY failure (caller falls back to the
    in-process synthetic reviewer)."""
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        logger.warning(
            "[DIALECTIC] orchestrated review enabled but AGENT_ORCHESTRATOR_BEARER_TOKEN "
            "unset; falling back to in-process synthetic reviewer"
        )
        return None

    spec = _build_spec(session_id, thesis, parent_agent_id)
    url = f"{_orchestrator_url()}/v1/agents"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url, json=spec, headers={"Authorization": f"Bearer {bearer}"}
            )
        if resp.status_code not in (200, 201, 202):
            logger.warning(
                "[DIALECTIC] orchestrator spawn returned %s: %s; falling back",
                resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()
        # The orchestrator returns {"ok": true, "agent_id": ...} (not "id").
        logger.info(
            "[DIALECTIC] orchestrated reviewer spawned for session %s: %s",
            session_id[:16], data.get("agent_id") or data.get("id") or data,
        )
        return data
    except Exception as exc:  # noqa: BLE001 — any failure degrades to in-process
        logger.warning(
            "[DIALECTIC] orchestrated dispatch failed (%r); falling back to in-process", exc
        )
        return None
