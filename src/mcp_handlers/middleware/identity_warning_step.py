"""Post-execution step: surface identity warnings on successful responses.

#1351 item 2 (hardening follow-up to the #1319 incident). When a caller
presents a ``continuity_token`` that fails verification but the call still
succeeds via another proof (valid ``client_session_id``, fingerprint pin,
...), nothing tells them their token is bad — they discover it only when the
fallback proof rotates, at which point their resume is refused with no prior
signal. ``session.py`` has always tracked the invalid-token fact internally
(``_mark("continuity_token_invalid")``), but that record is overwritten by
whichever proof wins; ``_identity_notifications`` reaches callers on
check-ins only.

This step reads the sticky per-request flag set by ``derive_session_key``
(``context.mark_continuity_token_invalid``) and appends a structured warning
to the successful response of ANY tool:

    "identity_warnings": [
      {
        "code": "continuity_token_invalid",
        "resolved_via": "<the proof that won>",
        "message": "..."
      }
    ]

Runs AFTER apply_experience_envelope so the warning lands at the top level
of whichever shape (raw or envelope) the caller actually receives. Error
responses pass through untouched — the failure contract carries its own
context, and a refusal for a bad token already explains itself (#1319 fix).

Like every post-execution step: any failure returns the result unmodified
(the runner also guards).
"""

from __future__ import annotations

import json
from typing import Any, Dict

from mcp.types import TextContent

from src.logging_utils import get_logger

logger = get_logger(__name__)


async def apply_identity_warnings(name: str, arguments: Dict[str, Any], ctx, result):
    """POST_EXECUTION step. Append identity_warnings to a successful response
    when this request presented a continuity_token that failed verification."""
    try:
        from ..context import (
            get_continuity_token_invalid,
            get_session_resolution_source,
        )

        if not get_continuity_token_invalid():
            return result

        if not (isinstance(result, (list, tuple)) and result and hasattr(result[0], "text")):
            return result
        payload = json.loads(result[0].text)
        if not isinstance(payload, dict):
            return result
        if payload.get("success") is False or "error" in payload:
            return result  # failure/refusal responses explain themselves

        resolved_via = get_session_resolution_source() or "unknown"
        warning = {
            "code": "continuity_token_invalid",
            "resolved_via": resolved_via,
            "message": (
                "The continuity_token presented with this call failed "
                "verification (malformed, expired, or signed with a rotated "
                f"secret); the call succeeded via '{resolved_via}' instead. "
                "That fallback proof can rotate without notice — refresh your "
                "token via identity() or re-onboard before relying on it."
            ),
        }
        existing = payload.get("identity_warnings")
        if isinstance(existing, list):
            if any(
                isinstance(w, dict) and w.get("code") == "continuity_token_invalid"
                for w in existing
            ):
                return result
            existing.append(warning)
        else:
            payload["identity_warnings"] = [warning]

        return [
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
            *result[1:],
        ]
    except Exception:
        logger.warning(
            "identity warning step failed for %r - returning raw response",
            name,
            exc_info=True,
        )
        return result
