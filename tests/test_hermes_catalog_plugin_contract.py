"""The Hermes catalog plugin keeps working against this server.

Hermes users install the UNITARES plugin from the Nous plugin catalog
(``plugin-catalog/unitares.yaml`` in NousResearch/hermes-agent), which pins
one commit of cirwel/unitares-host-adapter. That pin moves only when someone
opens a catalog PR, and the catalog re-validates nothing after merge, so a
server change that drops or reshapes a tool the pinned plugin calls breaks
every new catalog install with nothing upstream reporting it. The plugin
fails open, so the symptom is silence: no identity, no check-ins.

This test holds the server to what the pinned plugin sends. The plugin reads
tools/list before calling anything and refuses to proceed when a name is
missing, so each tool must be advertised in both advertisement modes, and
each payload below must validate against the advertised input schema.

The payloads are copied from the pinned commit (``core.py`` and
``bindings/hermes.py``). When the catalog pin moves, update ``CATALOG_PIN``
and the payloads to the new commit in the same change. When this test fails
because of a deliberate server change, bump the catalog entry to an adapter
release that handles it before the server change ships in a release.
"""

from __future__ import annotations

import jsonschema
import pytest

import src.mcp_handlers  # noqa: F401  (populates the decorator registry)
from src.interface_contract import get_public_tool_definitions
from src.mcp_compat import get_tool_input_schema

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

#: unitares-host-adapter commit the Nous catalog entry pins (version 0.3.1).
CATALOG_PIN = "456e74356fdcc5c0df69615df45692652ac15f5b"

#: The plugin onboards through the first of these that tools/list carries.
ONBOARD_TOOLS = ("onboard", "start_session")

_SESSION = "0123456789abcdef0123456789abcdef"

#: (tool, arguments) for every call the pinned plugin makes, after it has
#: onboarded and echoes the client_session_id it was given.
PLUGIN_CALLS = [
    # on_session_start
    (
        "onboard",
        {
            "name": "Hermes Agent",
            "model_type": "hermes-agent",
            "client_hint": "host-session:20261005_000000_abcdef",
            "force_new": True,
        },
    ),
    # post_llm_call -> checkin
    (
        "sync_state",
        {
            "response_text": "Hermes assistant turn completed",
            "complexity": 0.2,
            "response_mode": "minimal",
            "epistemic_class": "substrate_interpretation",
            "provenance_context": {
                "harness_type": "hermes_plugin",
                "governance_mode": "automatic_turn_checkin",
                "tool_surface": "hermes_lifecycle_hook",
                "transport": "streamable_http",
                "verification_source": "hook_observation",
                "public_operation": "sync_state",
            },
            "client_session_id": _SESSION,
        },
    ),
    # checkin with the adapter's defaults and a caller confidence
    (
        "sync_state",
        {
            "response_text": "purpose",
            "complexity": 0.3,
            "response_mode": "auto",
            "confidence": 0.7,
            "provenance_context": {"public_operation": "sync_state"},
            "client_session_id": _SESSION,
        },
    ),
    # pre_tool_call -> gate
    (
        "sync_state",
        {
            "response_text": "gate:terminal args={}",
            "complexity": 0.1,
            "response_mode": "minimal",
            "client_session_id": _SESSION,
        },
    ),
    # post_tool_call -> outcome_event, success and failure
    *(
        (
            "record_result",
            {
                "outcome_type": outcome_type,
                "detail": {
                    "status": "ok",
                    "error_type": "",
                    "governance_mode": "automatic_tool_outcome",
                    "harness": "hermes_plugin",
                    "verification_source": "hook_observation",
                    "tool_name": "terminal",
                    "public_operation": "record_result",
                },
                "verification_source": "agent_reported_tool_result",
                "client_session_id": _SESSION,
            },
        )
        for outcome_type in ("task_completed", "task_failed")
    ),
]


def _advertised(mode: str) -> dict:
    return {
        tool.name: get_tool_input_schema(tool)
        for tool in get_public_tool_definitions(mode)
    }


@pytest.mark.parametrize("mode", ["progressive", "full"])
def test_plugin_tools_are_advertised(mode):
    advertised = _advertised(mode)
    for name in ("sync_state", "record_result"):
        assert name in advertised, (
            f"tools/list ({mode}) no longer carries {name!r}; the Hermes catalog "
            f"plugin (adapter {CATALOG_PIN[:7]}) refuses to run without it"
        )
    assert any(name in advertised for name in ONBOARD_TOOLS), (
        f"tools/list ({mode}) carries neither onboard nor start_session; the "
        f"Hermes catalog plugin (adapter {CATALOG_PIN[:7]}) cannot onboard"
    )


@pytest.mark.parametrize("mode", ["progressive", "full"])
@pytest.mark.parametrize(
    "tool,arguments",
    PLUGIN_CALLS,
    ids=[f"{tool}-{i}" for i, (tool, _) in enumerate(PLUGIN_CALLS)],
)
def test_plugin_payloads_match_the_advertised_schema(mode, tool, arguments):
    advertised = _advertised(mode)
    if tool == "onboard":
        tool = next(name for name in ONBOARD_TOOLS if name in advertised)
    schema = advertised[tool]
    unknown = sorted(set(arguments) - set(schema.get("properties", {})))
    assert not unknown, (
        f"{tool} ({mode}) no longer declares {unknown}, which the Hermes catalog "
        f"plugin (adapter {CATALOG_PIN[:7]}) sends"
    )
    jsonschema.validate(arguments, schema)
