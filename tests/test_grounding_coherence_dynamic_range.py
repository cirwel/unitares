"""Regression guard: canonical `coherence` must be the responsive manifold form,
not a silent passthrough of the inert legacy thermodynamic value.

Context — the "stuck at ~0.49" investigation. There are two coherence fields:

  * ``coherence_legacy`` — ``C(V,Θ) = 0.5·(1+tanh(C₁·V))``. The ODE damps V to
    ≈0, so ``tanh(C₁·V)≈0`` and this value is structurally pinned near 0.49.
    Correct by design, but it has almost no dynamic range.
  * ``coherence`` (canonical, since PR #26) — the grounded manifold form
    ``1 − ‖(E,I,S) − healthy‖ / Δ_max``, which *should* move with the agent's
    state from 1.0 (at the healthy operating point) down to 0.0 (a full radius
    off).

The trap: ``compute_coherence`` silently falls back to ``_compute_heuristic``
when ``metrics["E"/"I"/"S"]`` are missing/non-numeric, and the heuristic just
re-emits ``metrics["coherence"]`` — i.e. the legacy ≈0.49. A canonical
coherence stuck at 0.49 with ``coherence_source == "heuristic"`` is therefore
this degradation, not honest math. These tests pin the responsive behavior and
make the degradation mode explicit so it can't regress unnoticed.
"""
from types import SimpleNamespace

import pytest

from config.governance_config import get_healthy_operating_point
from src.grounding.coherence import compute_coherence
from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.enrichments import enrich_grounding


def _ctx(agent_class="default"):
    return SimpleNamespace(agent_class=agent_class)


def test_manifold_coherence_responds_to_state():
    """Coherence must span its range with state — not sit pinned near 0.49.

    At the class healthy operating point it reads ≈1.0; a full radius off it
    reads ≈0.0; a partial deviation lands strictly between. If any future
    change pins coherence (e.g. a silent legacy passthrough), the spread
    assertion fails.
    """
    ctx = _ctx("default")
    hE, hI, hS = get_healthy_operating_point("default")

    legacy = 0.49  # the inert thermodynamic value we must NOT echo

    at_healthy = compute_coherence(ctx, {"E": hE, "I": hI, "S": hS, "coherence": legacy})
    off_point = compute_coherence(ctx, {"E": 0.5, "I": 0.5, "S": 0.5, "coherence": legacy})
    partial = compute_coherence(ctx, {"E": hE, "I": hI, "S": hS + 0.10, "coherence": legacy})

    # All three take the manifold path, not the legacy-echoing heuristic.
    assert at_healthy.source == "manifold"
    assert off_point.source == "manifold"
    assert partial.source == "manifold"

    # Genuine dynamic range, and none of them collapses onto the legacy 0.49.
    assert at_healthy.value == pytest.approx(1.0, abs=1e-6)
    assert off_point.value == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < partial.value < 1.0
    assert at_healthy.value - off_point.value > 0.5, "coherence has no dynamic range"
    assert abs(partial.value - legacy) > 0.01 or partial.source == "manifold"


def test_missing_eis_exposes_legacy_passthrough_as_heuristic():
    """Reproduce the 'stuck at 0.49' failure mode and pin its signature.

    When E/I/S are absent the manifold cannot compute and coherence silently
    echoes the legacy value — but it MUST stamp ``source == "heuristic"`` so the
    degradation is observable rather than masquerading as a real reading.
    """
    ctx = _ctx("default")
    result = compute_coherence(ctx, {"coherence": 0.49})

    assert result.value == pytest.approx(0.49, abs=1e-9)
    assert result.source == "heuristic", (
        "legacy passthrough must be flagged heuristic, not silently surfaced as manifold"
    )


@pytest.mark.asyncio
async def test_enrichment_coherence_differs_from_legacy_for_offpoint_agent():
    """End-to-end through enrich_grounding: an off-healthy agent's canonical
    coherence is the manifold value and is distinct from coherence_legacy.

    Guards the swap site so a regression that drops E/I/S (forcing the heuristic
    legacy echo) would surface here as ``coherence == coherence_legacy`` with a
    heuristic source.
    """
    ctx = UpdateContext()
    ctx.arguments = {}
    ctx.result = {
        "metrics": {"E": 0.5, "I": 0.5, "S": 0.5, "V": -0.1, "coherence": 0.49},
    }
    ctx.response_data = {}

    await enrich_grounding(ctx)
    m = ctx.result["metrics"]

    assert m["coherence_source"] == "manifold"
    assert m["coherence_legacy"] == 0.49
    # (0.5,0.5,0.5) is a full radius off the default healthy point → manifold 0.0,
    # which is decisively different from the inert legacy 0.49.
    assert m["coherence"] != m["coherence_legacy"]
    assert m["coherence"] == pytest.approx(0.0, abs=1e-6)
