"""Round-trip: a real check_working_state envelope through the SDK's get_metrics.

``get_metrics`` in both SDK clients calls the ``check_working_state`` alias,
so what it receives is ``build_experience_envelope(...)`` over the canonical
``get_governance_metrics`` payload. That envelope has no ``metrics`` key, and
``MetricsResult.metrics`` read nothing else, so it was ``{}`` on every call
(same class as #2366 for check-ins). The SDK's own tests
(``agents/sdk/tests/test_metrics_fields.py``) feed trimmed copies of this
output; this module runs the SDK over the live builder's output, produced by
the real handler body on a real monitor (``tests/helpers/metrics_producer.py``),
so the two cannot drift apart with green tests on both sides.

pyproject.toml's pytest ``pythonpath`` puts this tree's ``agents/sdk/src``
first. An environment that resolves ``unitares_sdk`` elsewhere (another
checkout's editable install, say) would test that copy instead, so the first
test fails loudly rather than passing on the wrong code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import unitares_sdk
from src.mcp_handlers.core import unbound_metrics_payload
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_base import success_response
from tests.helpers.metrics_producer import real_metrics_payload
from unitares_sdk.client import GovernanceClient
from unitares_sdk.sync_client import SyncGovernanceClient

_REPO = Path(__file__).resolve().parents[1]


def test_the_sdk_under_test_is_this_tree():
    here = (_REPO / "agents" / "sdk" / "src").resolve()
    loaded = Path(unitares_sdk.__file__).resolve()
    assert here in loaded.parents, (
        f"unitares_sdk imported from {loaded}, not {here}. Put "
        "agents/sdk/src first on PYTHONPATH (or install this tree's SDK)."
    )


async def _envelope(arguments, **producer):
    payload, validated = await real_metrics_payload(arguments, **producer)
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated
    )
    assert "metrics" not in env, "premise: the envelope has no metrics key"
    return payload, env


async def _read_both(env):
    async_client = GovernanceClient(timeout=5.0, retry_delay=0.01)
    async_client.call_tool = AsyncMock(return_value=env)
    sync_client = SyncGovernanceClient(transport="rest")
    sync_client.call_tool = lambda tool_name, arguments, **kwargs: env
    return await async_client.get_metrics(), sync_client.get_metrics()


def _value(node):
    return node.get("value") if isinstance(node, dict) else node


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"verbosity": "standard"}, {"verbosity": "full"}],
    ids=["minimal", "standard", "full"],
)
@pytest.mark.parametrize("check_ins", [2, 30])
async def test_get_metrics_reads_the_canonical_reading_off_the_envelope(
    arguments, check_ins
):
    payload, env = await _envelope(arguments, check_ins=check_ins)

    for result in await _read_both(env):
        for key in ("E", "I", "S", "V"):
            assert result.metrics[key] == pytest.approx(
                _value(payload[key]), abs=1e-5
            ), key
        assert result.coherence == pytest.approx(_value(payload["coherence"]), abs=1e-6)
        assert result.risk == _value(payload["risk_score"])
        assert result.verdict == _value(payload["verdict"])
        assert result.action == "proceed"
        assert result.metrics["primary_eisv_source"] == payload["primary_eisv_source"]


@pytest.mark.asyncio
async def test_a_paused_agent_reads_as_pause():
    _payload, env = await _envelope(
        {}, check_ins=5, status="paused", recent_decisions=["pause"]
    )
    for result in await _read_both(env):
        assert result.action == "pause"


@pytest.mark.asyncio
async def test_an_uninitialized_agent_reads_as_fallback_not_as_measured():
    _payload, env = await _envelope({}, check_ins=0)
    for result in await _read_both(env):
        assert result.verdict == "uninitialized"
        assert result.coherence is None
        assert result.metrics["primary_eisv_source"] == "ode_fallback"


@pytest.mark.asyncio
async def test_an_unbound_read_carries_no_reading():
    payload = __import__("json").loads(
        success_response(unbound_metrics_payload())[0].text
    )
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, {}
    )
    for result in await _read_both(env):
        assert result.metrics == {"verdict": "unbound"}
        assert result.coherence is None and result.risk is None
