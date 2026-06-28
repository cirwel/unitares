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
    # The reviewer passes this straight to GovernanceClient(mcp_url=...), whose
    # streamable-http transport needs the /mcp/ path — a bare base URL makes
    # session.initialize() hang then cancel (live-found 2026-06-23). Normalize a
    # pathless URL so either form (base or full mcp_url) works.
    raw = (
        os.environ.get("UNITARES_GOVERNANCE_URL")
        or os.environ.get("GOVERNANCE_URL")
        or "http://127.0.0.1:8767/mcp/"
    )
    from urllib.parse import urlparse

    if urlparse(raw).path in ("", "/"):
        raw = raw.rstrip("/") + "/mcp/"
    return raw


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

    # Propagate the dialectic-on-BEAM routing flag + lease-plane creds so the
    # spawned reviewer's OWN session-row writes (its antithesis phase advance,
    # reviewer-slot claim, and any synthesis resolve it drives) route through
    # BEAM too — matching the gov-mcp process. Forward-only: a key absent from
    # the parent env is simply not set, so the reviewer falls back to the Python
    # path. Stays flag-off-safe (nothing forwarded when gov-mcp is off).
    for _key in (
        "UNITARES_DIALECTIC_BEAM_RESOLUTION",
        "LEASE_PLANE_BEARER_TOKEN",
        "LEASE_PLANE_BASE_URL",
    ):
        _val = os.environ.get(_key)
        if _val:
            env[_key] = _val

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


async def reviewer_crashed_fast(
    agent_id: str,
    *,
    await_seconds: float = 15.0,
) -> bool:
    """Briefly await the spawned reviewer to catch a FAST crash.

    The common reviewer failure modes (bad URL, import error, wrong tool name)
    exit within ~12s — well before gemma4 (~40-70s) could finish. We block on the
    orchestrator's await endpoint for a short window:

    - exited with non-zero status  → True  (crashed without claiming the slot;
      the caller should fall back to the in-process synthetic reviewer inline so
      the session resolves now instead of stranding at antithesis for the 4h reap)
    - exited 0 / still running (504) / any error → False (the reviewer owns the
      review; leave it on the async path — DO NOT also run in-process)

    The short window keeps the whole submit_thesis call inside the dialectic
    router's 90s budget even when a fast crash triggers the in-process fallback.
    A reviewer that crashes AFTER this window (mid-model, rare) still relies on
    the slower reap — acceptable; this closes the common case.
    """
    if not agent_id:
        return False
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        return False
    url = f"{_orchestrator_url()}/v1/agents/{agent_id}/await"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=await_seconds + 5.0) as client:
            resp = await client.post(
                url,
                json={"timeout_ms": int(await_seconds * 1000)},
                headers={"Authorization": f"Bearer {bearer}"},
            )
        if resp.status_code == 504:
            return False  # await_timeout — still running, reviewer owns it
        if resp.status_code != 200:
            return False  # not_found / unexpected — don't double-run
        result = (resp.json() or {}).get("result") or {}
        exit_status = result.get("exit_status")
        crashed = exit_status not in (0, None)
        if crashed:
            logger.warning(
                "[DIALECTIC] orchestrated reviewer %s exited %s without resolving; "
                "falling back to in-process", agent_id, exit_status,
            )
        return bool(crashed)
    except Exception as exc:  # noqa: BLE001 — can't tell ⇒ leave it to the reviewer
        logger.warning("[DIALECTIC] reviewer await check failed (%r); leaving async", exc)
        return False
