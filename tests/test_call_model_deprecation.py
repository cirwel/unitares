"""call_model is deprecated in favour of consult(effort='standard') (2026-10-16).

The name stays registered and callable for the deprecation window: the gateway
intent classifier and the KG stale-entry check still call the handler, and
consult takes no model, temperature or max_tokens. These tests pin that the
deprecation is visible on every surface that reports one, and that nothing
was removed along with it.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import src.mcp_handlers  # noqa: F401  registers every @mcp_tool handler
from src.mcp_handlers.decorators import (
    get_tool_metadata,
    get_tool_registry,
    is_tool_deprecated,
)
from src.mcp_handlers.introspection.tool_catalog import (
    describe_tool_deprecation_block,
)
from src.tool_descriptions import TOOL_DESCRIPTIONS


def test_call_model_is_deprecated_but_still_registered():
    assert "call_model" in get_tool_registry()
    assert is_tool_deprecated("call_model")
    assert get_tool_metadata("call_model")["superseded_by"] == "consult"


def test_describe_tool_carries_the_migration():
    block = describe_tool_deprecation_block("call_model")
    assert block is not None
    assert block["deprecated_since"] == "2026-10-16"
    assert block["superseded_by"] == "consult"
    assert "consult(brief=..., effort='standard')" in block["migration"]
    # The migration names the controls that retire with the tool rather than
    # pretending consult covers them, and states the deprecation window.
    for knob in ("model", "temperature", "max_tokens"):
        assert knob in block["migration"]
    assert "no earlier than 2026-12-02" in block["migration"]


def test_consult_is_not_deprecated():
    assert not is_tool_deprecated("consult")
    assert describe_tool_deprecation_block("consult") is None


def test_no_description_still_recommends_call_model():
    """Descriptions may name call_model only as deprecated, never as advice."""
    assert TOOL_DESCRIPTIONS["call_model"].startswith("Deprecated since 2026-10-16")
    assert "for a raw completion use call_model" not in TOOL_DESCRIPTIONS["delegate_inference"]
    assert "Use call_model or delegate_inference" not in TOOL_DESCRIPTIONS["consult"]
    assert "call_model or delegate_inference" not in TOOL_DESCRIPTIONS["describe_inference_host"]
