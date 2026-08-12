"""Grounding stays explicit across the pre-persist and response stages."""

import pytest

# Import enrichments to trigger registration.
import src.mcp_handlers.updates.enrichments  # noqa: F401

from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.enrichments import run_grounding_stage
from src.mcp_handlers.updates.pipeline import run_enrichment_pipeline
from src.services.update_response_service import build_process_update_response_data


def _ctx() -> UpdateContext:
    ctx = UpdateContext(arguments={})
    ctx.agent_id = "grounding-e2e"
    ctx.agent_uuid = "grounding-e2e-uuid"
    ctx.response_text = ""
    ctx.complexity = 0.1
    ctx.confidence = 0.5
    ctx.result = {
        "metrics": {
            "E": 0.6,
            "I": 0.7,
            "S": 0.3,
            "V": -0.1,
            "coherence": 0.72,
            "health_status": "ok",
        }
    }
    return ctx


@pytest.mark.asyncio
async def test_flag_off_pipeline_never_applies_grounding_late(monkeypatch):
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)
    ctx = _ctx()

    await run_grounding_stage(ctx)
    before_pipeline = dict(ctx.result["metrics"])
    ctx.response_data = build_process_update_response_data(
        result=ctx.result,
        agent_id=ctx.agent_id,
        identity_assurance={},
    )
    await run_enrichment_pipeline(ctx)

    assert ctx.result["metrics"] == before_pipeline
    assert "coherence_legacy" not in ctx.result["metrics"]
    assert ctx.response_data["metrics"]["coherence"] == 0.72
    assert ctx.response_data["metrics"]["coherence_source"] == "legacy_tanh_v"
    assert ctx.response_data["metrics"]["coherence_role"] == "ode_control_feedback"


@pytest.mark.asyncio
async def test_apply_pipeline_keeps_explicit_grounding(monkeypatch):
    monkeypatch.setenv("UNITARES_GROUNDING_APPLY", "1")
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    ctx = _ctx()

    await run_grounding_stage(ctx)
    grounded = dict(ctx.result["metrics"])
    ctx.response_data = build_process_update_response_data(
        result=ctx.result,
        agent_id=ctx.agent_id,
        identity_assurance={},
    )
    await run_enrichment_pipeline(ctx)

    assert ctx.result["metrics"] == grounded
    assert ctx.result["metrics"]["coherence_legacy"] == 0.72
    assert ctx.response_data["metrics"]["coherence_source"] == "manifold"
    assert (
        ctx.response_data["metrics"]["coherence_role"]
        == "eis_structural_measurement"
    )
