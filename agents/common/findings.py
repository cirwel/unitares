"""Shared helper for agents to post findings to /api/findings.

Best-effort fire-and-forget — never raises, never blocks the agent.
Localhost callers bypass bearer auth via _is_trusted_network(); the
token is only sent if UNITARES_HTTP_API_TOKEN is set in env.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Iterable, Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = os.environ.get(
    "UNITARES_FINDINGS_URL", "http://localhost:8767/api/findings"
)
DEFAULT_TIMEOUT_SECONDS = 3.0

# Wave 3 §3.2 (prereq PR #10): one bounded retry on HTTP 503, honoring the
# server's Retry-After header / retry_after_seconds body field. Capped low —
# post_finding sits on agent-cycle hot paths and must stay near-instant even
# when the server is mid-cutover.
MAX_503_RETRY_SLEEP_SECONDS = 5.0


def compute_fingerprint(parts: Iterable[Any]) -> str:
    """16-hex-char SHA-256 prefix of a pipe-joined identity string.

    Matches the format used by Watcher (agents/watcher/agent.py:Finding.compute_fingerprint).
    Callers pass the identity parts they want hashed, e.g.:
        compute_fingerprint(["sentinel", finding_type, violation_class, agent_id])
    """
    normalized = "|".join(str(p) for p in parts)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _httpx_post(url: str, json: dict, headers: dict, timeout: float):
    """Thin wrapper so tests can monkeypatch this single call."""
    return httpx.post(url, json=json, headers=headers, timeout=timeout)


def _retry_after_from_503(resp: Any) -> float:
    """Bounded server-suggested delay from a 503 response: Retry-After
    header first, then the §3.2 body's retry_after_seconds, else the cap."""
    try:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            raw = resp.json().get("retry_after_seconds")
        seconds = float(raw)
        if seconds < 0:
            return MAX_503_RETRY_SLEEP_SECONDS
        return min(seconds, MAX_503_RETRY_SLEEP_SECONDS)
    except Exception:  # noqa: BLE001 — malformed header/body
        return MAX_503_RETRY_SLEEP_SECONDS


def post_finding(
    *,
    event_type: str,
    severity: str,
    message: str,
    agent_id: str,
    agent_name: str,
    fingerprint: str,
    extra: Optional[dict] = None,
    url: str = DEFAULT_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """POST a finding to the governance event stream.

    Returns True on HTTP 200 with a new (non-deduped) event accepted.
    Returns False on: dedup, network error, non-200 status, or malformed response.

    This function MUST NOT raise. It's called from hot paths in agent cycles.
    """
    body: dict = {
        "type": event_type,
        "severity": severity,
        "message": message,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "fingerprint": fingerprint,
    }
    if extra:
        for k, v in extra.items():
            if k not in body:
                body[k] = v

    headers: dict = {"Content-Type": "application/json"}
    token = os.environ.get("UNITARES_HTTP_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = _httpx_post(url, json=body, headers=headers, timeout=timeout)
        if getattr(resp, "status_code", 0) == 503:
            # §3.2 typed-unavailable from a mid-cutover transport: honor the
            # server's delay (bounded) and retry exactly once. Still never
            # raises; a second 503 falls through to the non-200 return below.
            time.sleep(_retry_after_from_503(resp))
            resp = _httpx_post(url, json=body, headers=headers, timeout=timeout)
    except Exception as exc:
        log.debug("post_finding failed: %s", exc)
        return False

    if getattr(resp, "status_code", 0) != 200:
        log.debug("post_finding non-200: %s", getattr(resp, "status_code", "?"))
        return False

    try:
        data = resp.json()
    except Exception:
        return False
    return bool(data.get("success")) and not data.get("deduped", False)
