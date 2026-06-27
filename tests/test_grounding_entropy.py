"""Tests for the entropy grounding module."""
import pytest
from unittest.mock import MagicMock

from src.grounding.entropy import compute_entropy
from src.grounding.types import GroundedValue


def _mk_ctx(arguments=None):
    ctx = MagicMock()
    ctx.arguments = arguments or {}
    return ctx


def test_tier3_heuristic_wraps_legacy_s():
    result = compute_entropy(_mk_ctx(), metrics={"S": 0.42})
    assert isinstance(result, GroundedValue)
    assert result.source == "heuristic"
    assert result.value == 0.42


def test_tier3_missing_metric_returns_neutral():
    result = compute_entropy(_mk_ctx(), metrics={})
    assert result.source == "heuristic"
    assert result.value == 0.5


def test_tier3_clamps_out_of_range_metric():
    assert compute_entropy(_mk_ctx(), metrics={"S": 1.3}).value == 1.0
    assert compute_entropy(_mk_ctx(), metrics={"S": -0.05}).value == 0.0


def test_tier1_logprobs_computes_grounded_s():
    ctx = _mk_ctx(arguments={"logprobs": [[-0.1, -0.3, -0.8]]})
    result = compute_entropy(ctx, metrics={"S": 0.2})
    assert result.source == "logprob"
    # near-flat top-3 → high normalized entropy; legacy 0.2 is NOT used
    assert result.value == pytest.approx(0.965, abs=0.01)


def test_tier1_uniform_topk_is_max_entropy():
    ctx = _mk_ctx(arguments={"logprobs": [[-0.5, -0.5, -0.5]]})
    assert compute_entropy(ctx, metrics={"S": 0.2}).value == pytest.approx(1.0, abs=1e-9)


def test_tier1_peaked_distribution_is_near_zero():
    ctx = _mk_ctx(arguments={"logprobs": [[0.0, -20.0, -20.0]]})
    result = compute_entropy(ctx, metrics={"S": 0.9})
    assert result.source == "logprob"
    assert result.value == pytest.approx(0.0, abs=1e-3)


def test_tier1_single_candidate_is_zero_entropy():
    ctx = _mk_ctx(arguments={"logprobs": [[-0.2]]})
    assert compute_entropy(ctx, metrics={"S": 0.9}).value == 0.0


def test_tier1_mean_over_tokens():
    # token A uniform (→1.0), token B peaked (→~0) ⇒ mean ≈ 0.5
    ctx = _mk_ctx(arguments={"logprobs": [[-0.5, -0.5, -0.5], [0.0, -20.0, -20.0]]})
    assert compute_entropy(ctx, metrics={"S": 0.2}).value == pytest.approx(0.5, abs=1e-3)


def test_tier1_accepts_openai_dict_shape():
    # OpenAI-style: per-token entry carrying top_logprobs of {token, logprob}
    entry = {"top_logprobs": [{"logprob": -0.5}, {"logprob": -0.5}, {"logprob": -0.5}]}
    ctx = _mk_ctx(arguments={"logprobs": [entry]})
    result = compute_entropy(ctx, metrics={"S": 0.2})
    assert result.source == "logprob"
    assert result.value == pytest.approx(1.0, abs=1e-9)


def test_tier1_unparseable_logprobs_fall_through_to_heuristic():
    # garbage / empty → NotImplementedError inside, compute_entropy degrades
    assert compute_entropy(_mk_ctx({"logprobs": ["x"]}), {"S": 0.2}).source == "heuristic"
    assert compute_entropy(_mk_ctx({"logprobs": []}), {"S": 0.2}).source == "heuristic"


def test_tier2_samples_stub_falls_through_to_heuristic():
    ctx = _mk_ctx(arguments={"samples": ["a", "b", "c"]})
    result = compute_entropy(ctx, metrics={"S": 0.3})
    assert result.source == "heuristic"
    assert result.value == 0.3
