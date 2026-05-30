"""Tests for R1 score_trajectory_continuity.

"Test fixture (synthetic)" — six
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
    """Patch get_db to return a backend whose async methods are settable."""
    backend = AsyncMock()
    # Default to successful audit-write so the score primitive can return a
    # score; per-test overrides set return_value=False to exercise the
    # fail-loud join-key contract (test_score_raises_when_audit_write_fails).
    backend.record_r1_score_audit = AsyncMock(return_value=True)
    # v3.3-C default: calibration_status='seeded'. Per-test overrides exercise
    # the calibration_failed verdict-degradation path.
    backend.read_r1_calibration_state = AsyncMock(return_value={
        "calibration_status": "seeded",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    })
    # v3.3-G default: parent has no class metadata → class_tag=None.
    # Per-test overrides set get_identity to return a record with metadata.tags
    # containing a recognized class tag.
    backend.get_identity = AsyncMock(return_value=None)
    graph = AsyncMock()
    graph.add_discovery = AsyncMock(return_value=None)
    backend.public_kg_graph = graph

    def _get_db():
        return backend

    async def _get_knowledge_graph():
        return graph

    # The score primitive imports get_db inside the function (per CLAUDE.md
    # anyio pattern lazy imports); patch the source module.
    monkeypatch.setattr("src.db.get_db", _get_db)
    monkeypatch.setattr("src.knowledge_graph.get_knowledge_graph", _get_knowledge_graph)
    return backend


def _identity_with_tags(*tags):
    """Build a minimal identity record stub for class_tag tests."""
    from types import SimpleNamespace
    return SimpleNamespace(metadata={"tags": list(tags)})


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
async def test_score_calibration_failed_degrades_verdict_to_inconclusive(mocked_db):
    """Per v3.3-C: when calibration_status='calibration_failed', the
    consumer-facing verdict MUST be 'inconclusive' regardless of plausibility.
    Even a genuine pair with plausibility >= 0.70 returns inconclusive.
    The original would-be-verdict is captured in `reasons` for forensic
    access; the audit row's `calibration_status` snapshots 'calibration_failed'
    so analyses know which scoring window was under degraded calibration."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=60, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    # Operator has marked calibration as failed
    mocked_db.read_r1_calibration_state = AsyncMock(return_value={
        "calibration_status": "calibration_failed",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    })

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    # Verdict degraded despite high plausibility
    assert result.verdict == "inconclusive"
    assert result.plausibility >= 0.70  # raw scoring still computed correctly
    assert result.calibration_status == "calibration_failed"
    # Reasons names the degradation explicitly
    assert any("degraded" in r and "calibration_failed" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_score_calibration_earned_does_not_degrade(mocked_db):
    """Per v3.3-C: under `earned`, verdict is shown without caveat — no
    degradation, no extra reason added."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=61, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    mocked_db.read_r1_calibration_state = AsyncMock(return_value={
        "calibration_status": "earned",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    })

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert result.verdict == "plausible"
    assert result.calibration_status == "earned"
    assert not any("degraded" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_score_calibration_status_snapshots_at_scoring_time(mocked_db):
    """The audit row's calibration_status equals the singleton's status at
    scoring time (not the global current status at analysis time). This is
    why v3.3-G also stamps class_tag at scoring time — both snapshots make
    later calibration analyses partition correctly."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=62, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    mocked_db.read_r1_calibration_state = AsyncMock(return_value={
        "calibration_status": "earned",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    })

    result = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    # Audit row carries the status that was current at scoring time
    audit_call = mocked_db.record_r1_score_audit.await_args
    audit_record = audit_call.kwargs if audit_call.kwargs else audit_call.args[0]
    assert audit_record["calibration_status"] == "earned"
    assert result.calibration_status == "earned"


@pytest.mark.asyncio
async def test_score_class_tag_stamped_from_parent_metadata(mocked_db):
    """Per v3.3-G: the audit row carries the parent's class_tag at scoring
    time. Reads from `core.identities.metadata.tags` and picks the most-
    specific recognized tag per _CLASS_TAG_PRIORITY."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=63, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    # Parent is a substrate-anchored persistent agent (e.g. Vigil)
    mocked_db.get_identity = AsyncMock(
        return_value=_identity_with_tags("persistent", "autonomous")
    )

    await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    audit_call = mocked_db.record_r1_score_audit.await_args
    audit_record = audit_call.kwargs if audit_call.kwargs else audit_call.args[0]
    assert audit_record["class_tag"] == "persistent"


@pytest.mark.asyncio
async def test_score_class_tag_priority_picks_most_specific(mocked_db):
    """Per v3.3-G ordering: when parent has multiple class tags, the most-
    specific one is stamped. `engaged_ephemeral` wins over `ephemeral`."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=64, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    mocked_db.get_identity = AsyncMock(
        return_value=_identity_with_tags("ephemeral", "engaged_ephemeral")
    )

    await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    audit_call = mocked_db.record_r1_score_audit.await_args
    audit_record = audit_call.kwargs if audit_call.kwargs else audit_call.args[0]
    assert audit_record["class_tag"] == "engaged_ephemeral"


@pytest.mark.asyncio
async def test_score_class_tag_none_when_parent_has_no_recognized_class(mocked_db):
    """Parent identity has no recognized class tag (or no metadata.tags at
    all) → class_tag=None on audit record. Per v3.3-G: NULL is the honest
    answer when S8a Phase 2 backfill hasn't reached this agent yet."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=65, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )
    # Parent has tags but none are recognized class tags
    mocked_db.get_identity = AsyncMock(
        return_value=_identity_with_tags("autonomous")
    )

    await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    audit_call = mocked_db.record_r1_score_audit.await_args
    audit_record = audit_call.kwargs if audit_call.kwargs else audit_call.args[0]
    assert audit_record["class_tag"] is None


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


# ---------------------------------------------------------------------------
# v3.3-A public KG emission (closes PR 2's deferred work)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_emits_public_kg_node_after_audit(mocked_db):
    """Per v3.3-A: score_trajectory_continuity publishes a redacted node to
    the configured public KG backend, joined to the audit row by
    score_id."""
    import json
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=60, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    score = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    mocked_db.public_kg_graph.add_discovery.assert_awaited_once()
    # Inspect the DiscoveryNode that was passed
    (node,), _kw = mocked_db.public_kg_graph.add_discovery.call_args
    assert node.type == "trajectory_continuity_score"
    assert node.agent_id == "successor-uuid"
    assert node.id.startswith("r1_score:")
    # Strict redaction: details JSON has ONLY the four allowed fields
    public = json.loads(node.details)
    assert set(public.keys()) == {"verdict", "calibration_status", "n_dims_used", "score_id"}
    assert public["score_id"] == score.score_id
    assert public["verdict"] == score.verdict
    # Tags are observability-only; they MAY duplicate verdict/calibration but
    # the source-of-truth shape lives in `details`.
    assert "r1" in node.tags
    assert "trajectory_continuity" in node.tags


@pytest.mark.asyncio
async def test_score_kg_node_id_is_deterministic_per_pair(mocked_db):
    """Per v3.2-D dedupe-by-pair: the node id MUST be deterministic from
    (parent_id, successor_id). Re-scoring the same pair produces the same
    id, so ON CONFLICT (id) DO UPDATE in the active KG backend overwrites
    rather than appends."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=61, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    await score_trajectory_continuity(
        claimed_parent_id="parent-uuid", successor_id="successor-uuid",
    )
    await score_trajectory_continuity(
        claimed_parent_id="parent-uuid", successor_id="successor-uuid",
    )

    assert mocked_db.public_kg_graph.add_discovery.await_count == 2
    first_id = mocked_db.public_kg_graph.add_discovery.call_args_list[0][0][0].id
    second_id = mocked_db.public_kg_graph.add_discovery.call_args_list[1][0][0].id
    assert first_id == second_id
    # Each score has a fresh score_id (audit retains all); the node id is
    # the dedupe key, NOT the score_id.
    first_payload = mocked_db.public_kg_graph.add_discovery.call_args_list[0][0][0].details
    second_payload = mocked_db.public_kg_graph.add_discovery.call_args_list[1][0][0].details
    assert first_payload != second_payload  # score_id differs across calls


@pytest.mark.asyncio
async def test_score_skips_kg_emit_when_n_dims_used_is_zero(mocked_db):
    """When the score had no EISV channels to compute on (n_dims_used=0,
    typical for fresh-agent onboards with no core.agent_state history),
    the verdict is forced inconclusive by _classify_verdict's short-circuit.
    The audit row still writes (forensic anchor), but the public KG node
    is skipped to avoid noise. 2026-05-30: 24 of 26 KG R1 discoveries were
    n_dims=0 from this exact path."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    # Empty series → reconstruct returns no usable data for any channel
    # → n_dims_used=0 → verdict forced to inconclusive.
    empty_series = {"E": [], "I": [], "S": [], "V": []}
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(empty_series, empty_series)
    )

    score = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid",
        successor_id="successor-uuid",
    )

    assert score.n_dims_used == 0
    assert score.verdict == "inconclusive"
    # Audit row written (canonical forensic record).
    mocked_db.record_r1_score_audit.assert_awaited_once()
    # KG node skipped to avoid "I couldn't score this" noise.
    mocked_db.public_kg_graph.add_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_score_kg_node_id_differs_across_pairs(mocked_db):
    """Different (parent, successor) pairs must produce different node ids
    so dedupe-by-pair is per-pair, not global."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    parent, successor = synthetic_trajectory_pair(seed=62, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    await score_trajectory_continuity(
        claimed_parent_id="parent-A", successor_id="successor-1",
    )
    await score_trajectory_continuity(
        claimed_parent_id="parent-B", successor_id="successor-1",
    )
    await score_trajectory_continuity(
        claimed_parent_id="parent-A", successor_id="successor-2",
    )

    ids = [
        mocked_db.public_kg_graph.add_discovery.call_args_list[i][0][0].id
        for i in range(3)
    ]
    assert len(set(ids)) == 3, "expected 3 distinct node ids for 3 distinct pairs"


@pytest.mark.asyncio
async def test_score_kg_emission_failure_is_non_fatal(mocked_db):
    """Per v3.3-A: audit is the durable record; KG is observability. A KG
    emission failure MUST NOT propagate — the score still returns and the
    audit row is durably written."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    mocked_db.public_kg_graph.add_discovery = AsyncMock(side_effect=RuntimeError("kg pool down"))
    parent, successor = synthetic_trajectory_pair(seed=63, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    # Must not raise
    score = await score_trajectory_continuity(
        claimed_parent_id="parent-uuid", successor_id="successor-uuid",
    )

    assert score is not None
    # Audit was still attempted + persisted (record_r1_score_audit default True)
    mocked_db.record_r1_score_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_score_kg_emission_skipped_when_audit_fails(mocked_db):
    """Per v3.3-A join-key durability contract: if audit write fails, the
    score primitive raises BEFORE KG emission — the public node must not
    reference an absent audit row."""
    from src.identity.trajectory_continuity import score_trajectory_continuity

    mocked_db.record_r1_score_audit = AsyncMock(return_value=False)
    parent, successor = synthetic_trajectory_pair(seed=64, kind="genuine")
    mocked_db.reconstruct_eisv_series = AsyncMock(
        side_effect=_stub_reconstruct(parent, successor)
    )

    with pytest.raises(RuntimeError, match="audit write failed"):
        await score_trajectory_continuity(
            claimed_parent_id="parent-uuid", successor_id="successor-uuid",
        )

    mocked_db.public_kg_graph.add_discovery.assert_not_awaited()
