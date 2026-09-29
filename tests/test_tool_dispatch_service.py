from unittest.mock import AsyncMock, patch

import pytest

from src.services.tool_dispatch_service import run_tool_dispatch_pipeline


@pytest.mark.asyncio
async def test_run_tool_dispatch_pipeline_executes_handler_after_steps():
    step = AsyncMock(side_effect=lambda n, a, c: (n, a, c))
    handler = AsyncMock(return_value=["ok"])

    with patch("src.mcp_handlers.TOOL_HANDLERS", {"demo": handler}):
        result = await run_tool_dispatch_pipeline(
            name="demo",
            arguments={"x": 1},
            pre_steps=[step],
            post_steps=[],
        )

    assert result == ["ok"]
    handler.assert_awaited_once_with({"x": 1})


@pytest.mark.asyncio
async def test_run_tool_dispatch_pipeline_returns_tool_not_found():
    result = await run_tool_dispatch_pipeline(
        name="missing_demo_tool",
        arguments={},
        pre_steps=[],
        post_steps=[],
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["missing_demo_tool", "status", "hello"])
async def test_unknown_name_is_refused_before_any_pre_step(name):
    """A name no handler or alias answers never reaches resolve_identity.

    That step treats an unknown name as identity-required and auto-mints on a
    session miss, so running it first left a fresh identity behind for a call
    that could only end in TOOL_NOT_FOUND. status and hello were aliases until
    2026-09-28 and are plausible first-call guesses.
    """
    step = AsyncMock(side_effect=lambda n, a, c: (n, a, c))
    result = await run_tool_dispatch_pipeline(
        name=name,
        arguments={},
        pre_steps=[step],
        post_steps=[],
    )
    assert isinstance(result, list)
    assert "TOOL_NOT_FOUND" in result[0].text
    step.assert_not_awaited()


@pytest.mark.asyncio
async def test_alias_of_a_known_tool_still_runs_pre_steps():
    step = AsyncMock(side_effect=lambda n, a, c: ("demo", a, c))
    handler = AsyncMock(return_value=["ok"])

    with patch("src.mcp_handlers.TOOL_HANDLERS", {"demo": handler, "get_governance_metrics": handler}):
        result = await run_tool_dispatch_pipeline(
            name="check_working_state",
            arguments={},
            pre_steps=[step],
            post_steps=[],
        )

    assert result == ["ok"]
    step.assert_awaited_once()
