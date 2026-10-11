"""Tests for Vigil's retrieval-eval step (KG hygiene v1).

Covers config-tag derivation from env vars, baseline-pick by config match,
the eval step itself (disabled-default, no-baseline warning, regression alert).
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# config tag derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({"UNITARES_EMBEDDING_MODEL": "bge-m3"}, "bge_m3"),
    ({"UNITARES_EMBEDDING_MODEL": "bge-m3", "UNITARES_ENABLE_HYBRID": "1"}, "hybrid_rrf"),
    (
        {"UNITARES_EMBEDDING_MODEL": "bge-m3",
         "UNITARES_ENABLE_HYBRID": "1",
         "UNITARES_ENABLE_GRAPH_EXPANSION": "1"},
        "hybrid_graph",
    ),
    (
        {"UNITARES_EMBEDDING_MODEL": "bge-m3",
         "UNITARES_ENABLE_RERANKER": "1"},
        "bge_m3_reranked",
    ),
])
def test_derive_eval_config_tag(env, expected, monkeypatch):
    from agents.vigil.agent import _derive_eval_config_tag

    for k in ("UNITARES_EMBEDDING_MODEL", "UNITARES_ENABLE_HYBRID",
              "UNITARES_ENABLE_GRAPH_EXPANSION", "UNITARES_ENABLE_RERANKER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    assert _derive_eval_config_tag() == expected


def test_pick_eval_baseline_matches_config(tmp_path):
    """Picks the newest baseline file matching the live config tag by mtime."""
    from agents.vigil.agent import _pick_eval_baseline
    import os, time

    older = tmp_path / "baseline_2026-04-19_bge_m3.json"
    other = tmp_path / "baseline_2026-04-20_hybrid_rrf.json"
    newest_match = tmp_path / "baseline_2026-04-21_hybrid_rrf.json"
    older.write_text("{}")
    other.write_text("{}")
    time.sleep(0.01)
    newest_match.write_text("{}")
    # Force mtime ordering
    os.utime(older, (older.stat().st_atime, older.stat().st_mtime - 100))
    os.utime(other, (other.stat().st_atime, other.stat().st_mtime - 50))

    picked = _pick_eval_baseline(tmp_path, config_tag="hybrid_rrf")
    assert picked == newest_match


def test_pick_eval_baseline_returns_none_when_no_match(tmp_path):
    """No matching baseline → None (caller logs warning, no regression alert)."""
    from agents.vigil.agent import _pick_eval_baseline
    (tmp_path / "baseline_2026-04-20_bge_m3.json").write_text("{}")
    assert _pick_eval_baseline(tmp_path, config_tag="hybrid_graph") is None


def test_pick_eval_baseline_missing_dir_returns_none(tmp_path):
    """Nonexistent baseline_dir → None, no exception."""
    from agents.vigil.agent import _pick_eval_baseline
    assert _pick_eval_baseline(tmp_path / "does-not-exist", config_tag="bge_m3") is None


# ---------------------------------------------------------------------------
# eval step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_eval_step_disabled_by_default():
    from agents.vigil.agent import VigilAgent
    vigil = VigilAgent()
    assert vigil.with_eval is False
    result = await vigil._run_eval_step()
    assert result["ran"] is False


@pytest.mark.asyncio
async def test_run_eval_step_no_baseline_returns_warning(tmp_path, monkeypatch):
    from agents.vigil.agent import VigilAgent

    monkeypatch.setenv("UNITARES_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.delenv("UNITARES_ENABLE_HYBRID", raising=False)
    monkeypatch.delenv("UNITARES_ENABLE_GRAPH_EXPANSION", raising=False)
    monkeypatch.delenv("UNITARES_ENABLE_RERANKER", raising=False)

    vigil = VigilAgent(with_eval=True)
    eval_metrics = {"nDCG@10": 0.7, "Recall@20": 0.85, "MRR": 0.6,
                    "latency_p50": 50, "latency_p95": 120}

    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=eval_metrics):
        result = await vigil._run_eval_step()

    assert result["ran"] is True
    assert result["baseline"] is None
    assert "no_baseline_warning" in result
    assert result["regression"] is False


@pytest.mark.asyncio
async def test_run_eval_step_regression_alert(tmp_path, monkeypatch):
    """nDCG@10 drops by more than threshold → regression flag set."""
    from agents.vigil.agent import VigilAgent

    monkeypatch.setenv("UNITARES_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
    monkeypatch.delenv("UNITARES_ENABLE_GRAPH_EXPANSION", raising=False)

    baseline = tmp_path / "baseline_2026-04-20_hybrid_rrf.json"
    baseline.write_text(json.dumps({
        "metrics": {"nDCG@10": 0.80, "Recall@20": 0.90, "MRR": 0.65,
                    "latency_p50": 45, "latency_p95": 110}
    }))

    eval_metrics = {"nDCG@10": 0.70, "Recall@20": 0.88, "MRR": 0.60,
                    "latency_p50": 50, "latency_p95": 120}
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=eval_metrics):
        result = await vigil._run_eval_step()

    assert result["ran"] is True
    assert result["regression"] is True
    assert result["delta"]["nDCG@10"] == pytest.approx(-0.10, abs=1e-6)


@pytest.mark.asyncio
async def test_run_eval_step_within_threshold_no_regression(tmp_path, monkeypatch):
    """Small nDCG@10 drop within threshold → no regression flag."""
    from agents.vigil.agent import VigilAgent

    monkeypatch.setenv("UNITARES_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
    monkeypatch.delenv("UNITARES_ENABLE_GRAPH_EXPANSION", raising=False)

    baseline = tmp_path / "baseline_2026-04-20_hybrid_rrf.json"
    baseline.write_text(json.dumps({
        "metrics": {"nDCG@10": 0.80}
    }))

    eval_metrics = {"nDCG@10": 0.78}  # only 0.02 drop, below 0.05 threshold
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=eval_metrics):
        result = await vigil._run_eval_step()

    assert result["ran"] is True
    assert result["regression"] is False


@pytest.mark.asyncio
async def test_run_eval_step_subprocess_failure_returns_not_ran(tmp_path):
    """Empty subprocess output → ran=False, no exception."""
    from agents.vigil.agent import VigilAgent

    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value={}):
        result = await vigil._run_eval_step()

    assert result["ran"] is False


def _nested_eval(ndcg: float) -> dict:
    """Shape written by scripts/eval/retrieval_eval.py and the baselines."""
    return {"aggregate": {
        "ndcg@10": {"mean": ndcg, "median": 1.0, "min": 0.0, "max": 1.0},
        "recall@20": {"mean": 0.9},
        "mrr": {"mean": 0.8},
        "latency_ms": {"p50": 20.0, "p95": 100.0, "max": 200.0},
    }}


def _hybrid_env(monkeypatch):
    monkeypatch.setenv("UNITARES_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
    monkeypatch.delenv("UNITARES_ENABLE_GRAPH_EXPANSION", raising=False)
    monkeypatch.delenv("UNITARES_ENABLE_RERANKER", raising=False)


@pytest.mark.asyncio
async def test_run_eval_step_regression_alert_real_nested_shape(tmp_path, monkeypatch):
    """Real harness/baseline shape (aggregate -> ndcg@10 -> mean) can flag."""
    from agents.vigil.agent import VigilAgent

    _hybrid_env(monkeypatch)
    (tmp_path / "baseline_2026-04-20_hybrid_rrf.json").write_text(
        json.dumps(_nested_eval(0.80)))
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=_nested_eval(0.70)):
        result = await vigil._run_eval_step()

    assert result["regression"] is True
    assert result["delta"]["nDCG@10"] == pytest.approx(-0.10, abs=1e-6)


@pytest.mark.asyncio
async def test_run_eval_step_nested_within_threshold_no_regression(tmp_path, monkeypatch):
    from agents.vigil.agent import VigilAgent

    _hybrid_env(monkeypatch)
    (tmp_path / "baseline_2026-04-20_hybrid_rrf.json").write_text(
        json.dumps(_nested_eval(0.80)))
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=_nested_eval(0.78)):
        result = await vigil._run_eval_step()

    assert result["regression"] is False
    assert "nDCG@10" in result["delta"]


@pytest.mark.asyncio
async def test_run_eval_step_corpus_mismatch_is_incomparable(tmp_path, monkeypatch):
    """Different pair_count: big nDCG drop is reported incomparable, not a regression."""
    from agents.vigil.agent import VigilAgent

    _hybrid_env(monkeypatch)
    base = _nested_eval(0.80)
    base["corpus"] = {"pair_count": 20, "scored_pair_count": 20, "schema_version": 1}
    cur = _nested_eval(0.60)
    cur["corpus"] = {"pair_count": 22, "scored_pair_count": 22, "schema_version": 1}
    (tmp_path / "baseline_2026-04-20_hybrid_rrf.json").write_text(json.dumps(base))
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=cur):
        result = await vigil._run_eval_step()

    assert result["regression"] is False
    assert "pair_count" in result["incomparable"]
    assert result["delta"]["nDCG@10"] == pytest.approx(-0.20, abs=1e-6)


@pytest.mark.asyncio
async def test_run_eval_step_matching_corpus_still_flags_regression(tmp_path, monkeypatch):
    from agents.vigil.agent import VigilAgent

    _hybrid_env(monkeypatch)
    corpus = {"pair_count": 20, "scored_pair_count": 20, "schema_version": 1}
    base = {**_nested_eval(0.80), "corpus": corpus}
    cur = {**_nested_eval(0.60), "corpus": dict(corpus)}
    (tmp_path / "baseline_2026-04-20_hybrid_rrf.json").write_text(json.dumps(base))
    vigil = VigilAgent(with_eval=True)
    with patch("agents.vigil.agent.RETRIEVAL_EVAL_DIR", tmp_path), \
         patch("agents.vigil.agent._run_eval_subprocess", return_value=cur):
        result = await vigil._run_eval_step()

    assert result["regression"] is True
    assert "incomparable" not in result
