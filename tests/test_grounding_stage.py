"""Tests for run_grounding_stage — the #1092 ordering fix.

enrich_grounding previously ran AFTER persist + response-build, so its grounded
E/I/S/coherence were silently discarded. run_grounding_stage runs grounding
early, flag-gated, with a shadow-compare:

  * no flags        -> no-op (metrics byte-identical to today)
  * GROUNDING_SHADOW -> emit grounding_shadow audit + REVERT metrics (neutral)
  * GROUNDING_APPLY  -> keep grounded values (S becomes logprob-derived, etc.)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.enrichments import run_grounding_stage
from src import audit_log

# a real-shaped logprobs payload (uncertain first token -> non-trivial entropy)
LOGPROBS = [
    {"top_logprobs": [{"logprob": -0.6875}, {"logprob": -0.7241}, {"logprob": -4.5567}]},
    {"top_logprobs": [{"logprob": -0.0}, {"logprob": -10.72}, {"logprob": -12.3}]},
]


def _ctx(logprobs=None):
    ctx = UpdateContext(arguments={"logprobs": logprobs} if logprobs else {})
    ctx.result = {"metrics": {"E": 0.72, "I": 0.80, "S": 0.1415, "V": -0.02, "coherence": 0.49}}
    ctx.meta = None
    ctx.agent_id = "test-agent"
    return ctx


@pytest.mark.asyncio
async def test_no_flags_is_noop(monkeypatch):
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)
    spy = MagicMock()
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", spy)

    ctx = _ctx(LOGPROBS)
    before = dict(ctx.result["metrics"])
    await run_grounding_stage(ctx)

    assert ctx.result["metrics"] == before  # untouched
    assert "s_source" not in ctx.result["metrics"]
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_logs_but_reverts(monkeypatch):
    monkeypatch.setenv("UNITARES_GROUNDING_SHADOW", "1")
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)
    spy = MagicMock()
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", spy)

    ctx = _ctx(LOGPROBS)
    before = dict(ctx.result["metrics"])
    await run_grounding_stage(ctx)

    # behavior-neutral: live metrics reverted, no grounding bookkeeping left
    m = ctx.result["metrics"]
    assert m == before
    assert "s_source" not in m and "S_legacy" not in m

    # but the shadow WAS recorded, with applied=False and a logprob S source
    assert spy.call_count == 1
    kw = spy.call_args.kwargs
    assert kw["applied"] is False
    assert kw["sources"]["S"] == "logprob"
    assert kw["grounded"]["S"] != kw["ungrounded"]["S"]  # grounding would have moved S


@pytest.mark.asyncio
async def test_apply_grounds_live_metrics(monkeypatch):
    monkeypatch.setenv("UNITARES_GROUNDING_APPLY", "1")
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", MagicMock())

    ctx = _ctx(LOGPROBS)
    await run_grounding_stage(ctx)

    m = ctx.result["metrics"]
    # grounded values are now live and tagged
    assert m["s_source"] == "logprob"
    assert m["S_legacy"] == pytest.approx(0.1415)
    assert m["S"] != pytest.approx(0.1415)  # S replaced by logprob entropy
    assert 0.0 <= m["S"] <= 1.0


@pytest.mark.asyncio
async def test_no_logprobs_falls_to_heuristic_under_apply(monkeypatch):
    # Without logprobs, S grounding is heuristic == prior S (no change), but the
    # stage still runs and stamps s_source=heuristic when applied.
    monkeypatch.setenv("UNITARES_GROUNDING_APPLY", "1")
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", MagicMock())

    ctx = _ctx(logprobs=None)
    await run_grounding_stage(ctx)
    m = ctx.result["metrics"]
    assert m.get("s_source") == "heuristic"
    assert m["S"] == pytest.approx(0.1415)  # heuristic S == prior ODE S
