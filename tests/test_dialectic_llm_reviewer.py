"""Tests for the heterogeneous structured-JSON dialectic reviewer.

Covers generate_antithesis / generate_synthesis: the structured (JSON-schema)
path, the graceful free-text fallback (never silent-empty), and the
list-typed field contract that the llm-assisted handler consumes.
"""

import pytest
from unittest.mock import AsyncMock, patch

MOD = "src.mcp_handlers.support.llm_delegation"


@pytest.mark.asyncio
async def test_antithesis_structured_path():
    """Structured JSON success yields list concerns + _structured=True."""
    from src.mcp_handlers.support.llm_delegation import generate_antithesis

    structured = {
        "concerns": ["risk_score alone ignores trajectory", "removes audit checkpoint"],
        "counter_reasoning": "Low instantaneous risk is not a safe trajectory.",
        "grounding_cited": "calibration curve + valence trend",
        "position": "dispute",
        "suggested_conditions": ["gate on coherence > 0.85"],
    }
    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=structured)), \
         patch(f"{MOD}.call_local_llm", new=AsyncMock(return_value="should-not-be-called")):
        result = await generate_antithesis(
            {"root_cause": "auto-resume below 0.3", "proposed_conditions": [], "reasoning": "x"},
            agent_state={"risk_score": 0.2, "coherence": 0.4, "V": -0.1},
        )

    assert result["_structured"] is True
    assert result["_degraded"] is False
    assert isinstance(result["concerns"], list) and len(result["concerns"]) == 2
    assert result["position"] == "dispute"
    assert result["grounding_cited"]


@pytest.mark.asyncio
async def test_antithesis_falls_back_to_prose_never_silent_empty():
    """When structured returns None/empty, capture free-text as counter_reasoning."""
    from src.mcp_handlers.support.llm_delegation import generate_antithesis

    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=None)), \
         patch(f"{MOD}.call_local_llm", new=AsyncMock(return_value="A prose antithesis with concerns.")):
        result = await generate_antithesis(
            {"root_cause": "rc", "proposed_conditions": [], "reasoning": ""},
        )

    assert result["_structured"] is False
    assert result["_degraded"] is True
    assert "prose antithesis" in result["counter_reasoning"]
    assert result["concerns"] == []  # explicit empty, not silently dropped


@pytest.mark.asyncio
async def test_antithesis_structured_empty_concerns_triggers_fallback():
    """A schema-valid object with empty concerns (the qwen degenerate case) must
    NOT be accepted as a real antithesis — fall back to prose."""
    from src.mcp_handlers.support.llm_delegation import generate_antithesis

    degenerate = {"concerns": [], "counter_reasoning": "", "grounding_cited": "",
                  "position": "dispute", "suggested_conditions": []}
    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=degenerate)), \
         patch(f"{MOD}.call_local_llm", new=AsyncMock(return_value="fallback prose")):
        result = await generate_antithesis({"root_cause": "rc"})

    assert result["_degraded"] is True
    assert result["counter_reasoning"] == "fallback prose"


@pytest.mark.asyncio
async def test_antithesis_returns_none_when_all_unavailable():
    from src.mcp_handlers.support.llm_delegation import generate_antithesis
    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=None)), \
         patch(f"{MOD}.call_local_llm", new=AsyncMock(return_value=None)):
        assert await generate_antithesis({"root_cause": "rc"}) is None


@pytest.mark.asyncio
async def test_synthesis_structured_path_normalizes_recommendation():
    from src.mcp_handlers.support.llm_delegation import generate_synthesis

    structured = {
        "agreed_root_cause": "scalar risk is insufficient",
        "reasoning": "merged both sides",
        "merged_conditions": ["coherence gate", "summary dialectic pass"],
        "recommendation": "resume please",  # lenient normalization
    }
    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=structured)):
        result = await generate_synthesis(
            {"root_cause": "rc", "proposed_conditions": ["c1"]},
            {"concerns": ["a", "b"], "counter_reasoning": "cr", "suggested_conditions": ["s1"]},
        )

    assert result["_structured"] is True
    assert result["recommendation"] == "RESUME"
    assert isinstance(result["merged_conditions"], list) and len(result["merged_conditions"]) == 2


@pytest.mark.asyncio
async def test_synthesis_fallback_defaults_escalate():
    """Free-text fallback that won't commit defaults to ESCALATE (honest)."""
    from src.mcp_handlers.support.llm_delegation import generate_synthesis

    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=None)), \
         patch(f"{MOD}.call_local_llm", new=AsyncMock(return_value="some ambivalent prose")):
        result = await generate_synthesis(
            {"root_cause": "rc"}, {"concerns": ["a", "b"]},
        )

    assert result["_degraded"] is True
    assert result["recommendation"] == "ESCALATE"


@pytest.mark.asyncio
async def test_synthesis_tolerates_list_concerns_from_antithesis():
    """generate_synthesis must accept the new list-typed antithesis fields
    without crashing on string ops."""
    from src.mcp_handlers.support.llm_delegation import generate_synthesis

    structured = {"agreed_root_cause": "x", "reasoning": "y",
                  "merged_conditions": ["c"], "recommendation": "COOLDOWN"}
    with patch(f"{MOD}.call_local_llm_structured", new=AsyncMock(return_value=structured)):
        result = await generate_synthesis(
            {"root_cause": "rc", "proposed_conditions": []},
            {"concerns": ["list", "of", "concerns"],
             "counter_reasoning": "cr",
             "suggested_conditions": ["s1", "s2"]},
        )
    assert result["recommendation"] == "COOLDOWN"
