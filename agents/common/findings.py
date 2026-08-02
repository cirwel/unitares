"""Shared helper for agents to post findings to /api/findings.

Best-effort fire-and-forget — never raises, never blocks the agent.
Localhost callers bypass bearer auth via _is_trusted_network(); the
token is only sent if UNITARES_HTTP_API_TOKEN is set in env.
"""

from __future__ import annotations

import hashlib
import json
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


def compute_change_token(parts: dict[str, Any]) -> str:
    """16-hex-char SHA-256 prefix for an underlying finding condition.

    Unlike ``fingerprint`` (which names the finding identity), this token names
    the currently observed condition. If the same finding persists unchanged,
    event ingestion can suppress repeat emissions indefinitely; if severity,
    message, or stable context changes, it emits once for the new condition.
    """
    normalized = json.dumps(
        _stable_json_value(parts),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _stable_json_value(value: Any) -> Any:
    """Return a deterministic JSON-ish shape for hashing.

    ``post_finding`` is best-effort and must not raise just because optional
    finding context has non-string keys, tuples, sets, or non-JSON leaf values.
    """
    if isinstance(value, dict):
        return {
            str(k): _stable_json_value(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(v) for v in value]
    if isinstance(value, set):
        return sorted(
            (_stable_json_value(v) for v in value),
            key=lambda item: repr(item),
        )
    return value


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


#: Outcomes of an escalation attempt. ``post_finding`` collapses the first two
#: into True and the rest into False, which is fine for callers that only want
#: to know "did I add something new" — but NOT for a caller deciding whether it
#: has escalated at all. DEDUPED means governance already holds this finding;
#: FAILED means nobody was told. Conflating them is how a detector convinces
#: itself it reported. See ``post_finding_result``.
DELIVERED = "delivered"
DEDUPED = "deduped"
FAILED = "failed"

#: Outcomes where governance demonstrably holds the finding.
REACHED_GOVERNANCE = frozenset({DELIVERED, DEDUPED})


def post_finding_result(
    *,
    event_type: str,
    severity: str,
    message: str,
    agent_id: str,
    agent_name: str,
    fingerprint: str,
    change_token: Optional[str] = None,
    extra: Optional[dict] = None,
    url: str = DEFAULT_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST a finding and report which of DELIVERED / DEDUPED / FAILED happened.

    ``post_finding`` returns a bool that maps dedup and hard failure onto the
    same False, so a caller cannot tell "governance already knows" from "nobody
    was told." That distinction is load-bearing for any caller that records
    having alerted: on DEDUPED the finding is held and recording is correct, on
    FAILED recording it manufactures a delivery that never happened and the
    cooldown then suppresses the retry that would have fixed it.

    This is not hypothetical. ``deploy_drift_doctor`` ran hourly for its entire
    life posting zero findings — its interpreter could not import this module —
    while recording ``last_alert`` on every cycle and logging success-shaped
    lines to a file nobody reads (verified 2026-08-01: no deploy_drift row in
    audit.events, ever).

    MUST NOT raise. Called from hot paths in agent cycles.
    """
    explicit_extra_change_token = None
    if extra and extra.get("change_token") is not None:
        explicit_extra_change_token = str(extra["change_token"])

    resolved_change_token = (
        str(change_token)
        if change_token is not None
        else explicit_extra_change_token
    )
    if resolved_change_token is None:
        stable_extra = {
            k: v for k, v in (extra or {}).items()
            if k != "change_token"
        }
        resolved_change_token = compute_change_token({
            "type": event_type,
            "severity": severity,
            "message": message,
            "extra": stable_extra,
        })

    body: dict = {
        "type": event_type,
        "severity": severity,
        "message": message,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "fingerprint": fingerprint,
        "change_token": resolved_change_token,
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
        log.warning("post_finding failed (%s): %s — finding NOT escalated",
                    type(exc).__name__, exc)
        return FAILED

    if getattr(resp, "status_code", 0) != 200:
        log.warning("post_finding non-200: %s — finding NOT escalated",
                    getattr(resp, "status_code", "?"))
        return FAILED

    try:
        data = resp.json()
    except Exception:
        log.warning("post_finding: malformed response — finding NOT escalated")
        return FAILED
    if not data.get("success"):
        log.warning("post_finding: server rejected — finding NOT escalated")
        return FAILED
    return DEDUPED if data.get("deduped", False) else DELIVERED


def post_finding(**kwargs) -> bool:
    """Back-compatible wrapper: True only for a newly accepted finding.

    Kept byte-for-byte in behaviour for the agent-cycle callers (sentinel,
    vigil, watcher, dogfood) that only care whether they added something new.
    Callers deciding whether they have escalated at all want
    ``post_finding_result`` instead — see its docstring for why.
    """
    return post_finding_result(**kwargs) == DELIVERED
