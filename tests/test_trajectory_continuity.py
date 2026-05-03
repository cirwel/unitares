"""Tests for R1 score_trajectory_continuity.

Per docs/ontology/r1-verify-lineage-claim.md §"Test fixture (synthetic)" — six
deterministic synthetic cases regression-test the threshold cuts. Calibration
of the cuts against real production data is a separate path (shadow-mode
calibration; the primitive ships with status=seeded per v3.3-C until the
operator runs that calibration).

Plus PR 2-specific tests:
- Strict redaction: KG public payload limited to {verdict, calibration_status,
  n_dims_used, score_id} per v3.3-A
- Audit-only persistence captures full record (plausibility, components, etc.)
- Empty-dim skip: per-dimension absence excludes from average, does not score 0
  (v3.3-H.C4)
- Default calibration_status is 'seeded' (v3.3-C)
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers.trajectory_fixtures import synthetic_trajectory_pair


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_reconstruct(parent_series, successor_series):
    """Build a side_effect that returns parent vs successor series by agent_id."""
    async def _impl(agent_id, window, *, epoch=None):
        if agent_id.startswith("parent"):
            return parent_series
        return successor_series
    return _impl


@pytest.fixture
def mocked_db(monkeypatch):
    """Patch get_db to return a backend whose reconstruct_eisv_series is settable."""
    backend = AsyncMock()
    # Default to successful audit-write so the score primitive can return a
    # score; per-test overrides set return_value=False to exercise the
    # fail-loud join-key contract (test_score_raises_when_audit_write_fails).
    backend.record_r1_score_audit = AsyncMock(return_value=True)

    def _get_db():
        return backend

    # The score primitive imports get_db inside the function (per CLAUDE.md
    # anyio pattern lazy imports); patch the source module.
    monkeypatch.setattr("src.db.get_db", _get_db)
    return backend


# ---------------------------------------------------------------------------
# 6 v3.1 synthetic cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_genuine_case_returns_plausible(mocked_db):
    """Genuine: parent stable, successor continues same dynamics → verdict=plausible."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=42, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "plausible"
    assert result.plausibility >= 0.70
    assert result.parent_mature is True
    assert set(result.components.keys()) == {"E", "I", "S", "V"}


@pytest.mark.asyncio
async def test_score_divergent_case_returns_unsupported(mocked_db):
    """Divergent: independent successor → verdict=unsupported."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=43, kind="divergent")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "unsupported"
    assert result.plausibility < 0.55


@pytest.mark.asyncio
async def test_score_drifted_case_returns_inconclusive(mocked_db):
    """Drifted: matched start drifting toward different basin → verdict=inconclusive."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=44, kind="drifted")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "inconclusive"
    # Verdict alone is the threshold-logic regression. The numeric band check
    # would conflate "generator was tuned right" with "thresholds work" —
    # _DRIFT_START_OFFSET in trajectory_fixtures.py was tuned to land here,
    # so a tightening of the band would be a generator-tuning regression, not
    # a threshold-logic failure. Other tests pin the >=0.70 and <0.55 cuts.


@pytest.mark.asyncio
async def test_score_early_case_returns_inconclusive_with_parent_mature(mocked_db):
    """Early: successor < min_observations → inconclusive, parent_mature=True."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=45, kind="early")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "inconclusive"
    assert result.parent_mature is True


@pytest.mark.asyncio
async def test_score_immature_parent_returns_inconclusive(mocked_db):
    """Immature parent: < min_observations on parent side → inconclusive,
    parent_mature=False."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=46, kind="immature")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "inconclusive"
    assert result.parent_mature is False


@pytest.mark.asyncio
async def test_score_dimensional_degradation_averages_over_present_dims_only(mocked_db):
    """Successor only has E dimension; S/I/V successor series are empty.
    plausibility averaged over E only; missing dims listed in reasons.
    Per v3.3-H.C4: empty-dim is skipped, NOT scored as 0.0."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=47, kind="dimensional_degradation")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    # Components dict has E populated; I/S/V are None (skipped, not 0.0)
    assert result.components["E"] is not None
    assert result.components["I"] is None
    assert result.components["S"] is None
    assert result.components["V"] is None
    # The skip-not-zero contract: plausibility comes from E alone, not averaged
    # with three zeros. For genuine-style E rows, plausibility should be >= 0.70.
    assert result.plausibility >= 0.70
    # reasons should mention the missing dimensions
    reasons_text = " ".join(result.reasons)
    assert "I" in reasons_text or "S" in reasons_text or "V" in reasons_text


# ---------------------------------------------------------------------------
# v3.3 normative tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_record_default_calibration_status_is_seeded(mocked_db):
    """Per v3.3-C: every score record stamps calibration_status=seeded by default
    until operator transitions to earned or calibration_failed."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=48, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.calibration_status == "seeded"


@pytest.mark.asyncio
async def test_score_record_audit_write_captures_full_record(mocked_db):
    """Per v3.3-A: audit table holds the full record — score_id, plausibility,
    components, observations, parent_mature, reasons, class_tag, calibration_status."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=49, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    # The audit-write side effect must have fired with the full record.
    assert mocked_db.record_r1_score_audit.await_count == 1
    audit_call = mocked_db.record_r1_score_audit.await_args
    audit_record = audit_call.kwargs if audit_call.kwargs else audit_call.args[0]
    # Tolerate either positional dict or kwargs shape — verify keys present
    if hasattr(audit_record, "keys"):
        keys = set(audit_record.keys())
    else:
        keys = set()
    assert "score_id" in keys
    assert "plausibility" in keys
    assert "components" in keys
    assert "observations" in keys
    assert "parent_mature" in keys
    assert "reasons" in keys
    assert "calibration_status" in keys
    # score_id matches what's on the result dataclass
    assert audit_record["score_id"] == result.score_id


@pytest.mark.asyncio
async def test_score_kg_public_payload_is_strictly_redacted(mocked_db):
    """Per v3.3-A: public KG payload contains ONLY:
      verdict, calibration_status, n_dims_used, score_id.
    No plausibility scalar, no per-dim observations, no parent_mature, no reasons."""
    from src.identity.trajectory_continuity import score_trajectory_continuity, _build_public_payload

    parent, successor = synthetic_trajectory_pair(seed=50, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    public = _build_public_payload(result)

    assert set(public.keys()) == {"verdict", "calibration_status", "n_dims_used", "score_id"}
    # Confirm leak-prone fields are absent
    assert "plausibility" not in public
    assert "components" not in public
    assert "observations" not in public
    assert "parent_mature" not in public
    assert "reasons" not in public


@pytest.mark.asyncio
async def test_score_raises_when_audit_write_fails(mocked_db):
    """Per v3.3-A: score_id must be durably present in audit.r1_score_audit
    before any caller publishes the redacted KG payload that references it.
    If record_r1_score_audit returns False, the primitive MUST raise rather
    than return a score whose audit anchor is absent — otherwise PR 3's
    public KG emission could publish a dangling score_id."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=99, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    # Audit write fails (e.g. CheckViolation, network error) → mixin returns False
    mocked_db.record_r1_score_audit = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match="audit write failed"):
        await score_trajectory_continuity(
            claimed_parent_id="parent-uuid",
            successor_id="successor-uuid",
        )


@pytest.mark.asyncio
async def test_score_dataclass_internal_caller_surface_unchanged(mocked_db):
    """Per v3.3-A: internal callers (the policy layer) get the full dataclass.
    Only the KG-published shape narrows."""
    from src.identity.trajectory_continuity import (
        score_trajectory_continuity,
        TrajectoryContinuityScore,
    )

    parent, successor = synthetic_trajectory_pair(seed=51, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert isinstance(result, TrajectoryContinuityScore)
    # All v3.1 §"Input signature" fields must be present
    for field in ("plausibility", "verdict", "observations", "components",
                  "reasons", "parent_mature", "score_id", "calibration_status"):
        assert hasattr(result, field), f"missing {field}"
