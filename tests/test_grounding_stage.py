"""Tests for run_grounding_stage — the #1092 ordering fix.

enrich_grounding previously ran AFTER persist + response-build, so its grounded
E/I/S/coherence were silently discarded. run_grounding_stage runs grounding
early, flag-gated, with a shadow-compare:

  * no flags        -> values unchanged; producer/role metadata attached
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
async def test_no_flags_only_adds_provenance(monkeypatch):
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)
    spy = MagicMock()
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", spy)

    ctx = _ctx(LOGPROBS)
    before = dict(ctx.result["metrics"])
    await run_grounding_stage(ctx)

    metrics = ctx.result["metrics"]
    assert {key: metrics[key] for key in before} == before
    assert metrics["coherence_source"] == "legacy_tanh_v"
    assert metrics["coherence_role"] == "ode_control_feedback"
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

    # behavior-neutral: live values reverted; only winning-producer metadata remains
    m = ctx.result["metrics"]
    assert {key: m[key] for key in before} == before
    assert m["coherence_source"] == "legacy_tanh_v"
    assert m["coherence_role"] == "ode_control_feedback"
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
    assert m["coherence_source"] == "manifold"
    assert m["coherence_role"] == "eis_structural_measurement"
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


# ── coherence_form provenance ──────────────────────────────────────────────
# The stored `coherence` column is written by two different instruments with
# different ranges (legacy tanh(V) is pinned near 0.49; the manifold form spans
# [0, ~0.91]). Untagged, a history of mixed rows cannot be split by instrument,
# so any threshold derived from it is derived from two incommensurable signals.
# These pin the tag to the flag that actually decides which value is persisted.


@pytest.mark.asyncio
async def test_no_flags_tags_legacy_coherence_without_changing_metrics(monkeypatch):
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)

    ctx = _ctx()
    await run_grounding_stage(ctx)

    # Flag-off is itself enough to establish which producer won: the deployed
    # canonical value is legacy tanh(V). The row and returned measurement carry
    # the same producer identity; values remain unchanged.
    assert ctx.coherence_form == "legacy_tanh_v"
    assert ctx.result["metrics"] == {
        "E": 0.72, "I": 0.80, "S": 0.1415, "V": -0.02, "coherence": 0.49,
        "coherence_source": "legacy_tanh_v",
        "coherence_role": "ode_control_feedback",
    }


@pytest.mark.asyncio
async def test_shadow_tags_legacy_without_changing_response(monkeypatch):
    monkeypatch.setenv("UNITARES_GROUNDING_SHADOW", "1")
    monkeypatch.delenv("UNITARES_GROUNDING_APPLY", raising=False)
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", MagicMock())

    ctx = _ctx()
    before = dict(ctx.result["metrics"])
    await run_grounding_stage(ctx)

    # Under SHADOW the reverted (legacy) value is what persists, so that is what
    # the row must be tagged with — regardless of the grounded value computed.
    assert ctx.coherence_form == "legacy_tanh_v"
    # ...and source/role travel with the response without changing its values.
    assert {key: ctx.result["metrics"][key] for key in before} == before
    assert ctx.result["metrics"]["coherence_source"] == "legacy_tanh_v"
    assert ctx.result["metrics"]["coherence_role"] == "ode_control_feedback"
    assert "coherence_form" not in ctx.result["metrics"]


@pytest.mark.asyncio
async def test_apply_tags_the_grounded_source(monkeypatch):
    monkeypatch.setenv("UNITARES_GROUNDING_APPLY", "1")
    monkeypatch.delenv("UNITARES_GROUNDING_SHADOW", raising=False)
    monkeypatch.setattr(audit_log.audit_logger, "log_grounding_shadow", MagicMock())

    ctx = _ctx()
    await run_grounding_stage(ctx)

    # Under APPLY the grounded value persists, so the tag names its instrument
    # rather than the generic "grounded" fallback.
    assert ctx.coherence_form == ctx.result["metrics"]["coherence_source"]
    assert ctx.coherence_form == "manifold"
    assert ctx.result["metrics"]["coherence_role"] == "eis_structural_measurement"
