"""Real get_governance_metrics output for envelope tests.

Envelope tests that feed hand-built payloads pass behind shapes the handler
never emits: the lifecycle conformance fixture supplied a ``guidance`` the
minimal tier only sets for an uninitialized agent, so a missing
``next_action`` went unnoticed. This runs the real handler body on a real
``UNITARESMonitor`` stepped through real ``process_update`` calls, and wraps
the result with the real ``success_response``.

``check_ins``: 0 is uninitialized, 1-2 is a provisional (ODE cold-start)
verdict, and 3 or more is behavioral. ``arguments`` are schema-validated the
way ``/mcp/`` validates them, and the same dict is returned for the envelope,
since that is what ``apply_experience_envelope`` receives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, patch

AGENT_UUID = "1856bb5c-2550-4d0e-9a4c-7f1e2b3c4d5e"
PUBLIC_AGENT_ID = "Claude_Opus_5_5_20260926"
DISPLAY_NAME = "claude_code-opus_1856bb5c"


def agent_signature(
    *,
    proof_origin: str = "caller_asserted",
    session_source: str = "explicit_client_session_id",
) -> Dict[str, Any]:
    """The agent_signature success_response attaches for a binding."""
    from src.services.identity_payloads import build_identity_signature_payload

    return build_identity_signature_payload(
        agent_uuid=AGENT_UUID,
        agent_id=PUBLIC_AGENT_ID,
        display_name=DISPLAY_NAME,
        label_source="auto",
        session_resolution_source=session_source,
        client_hint="claude_code",
        model_type="claude-opus-5-5",
        proof_origin=proof_origin,
    )


async def real_metrics_payload(
    arguments: Optional[Dict[str, Any]] = None,
    *,
    check_ins: int = 2,
    signature: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(canonical payload, validated arguments) for one metrics read."""
    from src.governance_monitor import UNITARESMonitor
    from src.mcp_handlers.response_base import success_response
    from src.mcp_handlers.schemas.core import GetGovernanceMetricsParams
    from src.services.runtime_queries import get_governance_metrics_data

    monitor = UNITARESMonitor(f"metrics-producer-{check_ins}", load_state=False)
    for step in range(check_ins):
        monitor.process_update(
            {
                "response_text": f"step {step}: edited two files and ran the unit tests",
                "complexity": 0.4,
            },
            confidence=0.8,
            task_type="mixed",
        )
    meta = SimpleNamespace(
        public_agent_id=PUBLIC_AGENT_ID,
        structured_id=PUBLIC_AGENT_ID,
        label=DISPLAY_NAME,
        display_name=DISPLAY_NAME,
        purpose=None,
        status="active",
        recent_decisions=["proceed"] * min(check_ins, 5),
        total_updates=check_ins,
    )
    server = SimpleNamespace(
        get_or_create_monitor=lambda _agent_id: monitor,
        agent_metadata={AGENT_UUID: meta},
    )
    validated = GetGovernanceMetricsParams.model_validate(
        dict(arguments or {})
    ).model_dump()
    with patch(
        "src.agent_monitor_state.hydrate_from_db_if_fresh",
        new=AsyncMock(return_value=False),
    ):
        data = await get_governance_metrics_data(
            AGENT_UUID, dict(validated), server=server
        )
    with patch(
        "src.mcp_handlers.support.agent_auth.compute_agent_signature",
        return_value=signature if signature is not None else agent_signature(),
    ):
        payload = json.loads(success_response(data)[0].text)
    return payload, validated
