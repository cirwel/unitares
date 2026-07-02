"""Tests for cohort_prior_source.py — per-class cohort prior assembly (shadow, read-only)."""

from unittest.mock import patch

import pytest

from src.agent_behavioral_baseline import AgentBehavioralBaseline
from src.cohort_prior import CohortPrior
from src.cohort_prior_source import (
    build_cohort_priors,
    get_cached_cohort_priors,
    group_baselines_by_class,
    load_cohort_priors,
    observe_seed_gap,
    reset_cohort_prior_cache,
)

SIGNAL = "coherence"


def _stats(values):
    b = AgentBehavioralBaseline()
    for v in values:
        b.update(SIGNAL, v)
    return b.to_dict()


def _fixed_classifier(mapping):
    return lambda agent_id: mapping.get(agent_id)


class TestGrouping:
    def test_buckets_by_class(self):
        baselines = {
            "a": _stats([0.5] * 8),
            "b": _stats([0.6] * 8),
            "c": _stats([0.9] * 8),
        }
        classify = _fixed_classifier({"a": "ephemeral", "b": "ephemeral", "c": "default"})
        grouped = group_baselines_by_class(baselines, classify)
        assert set(grouped) == {"ephemeral", "default"}
        assert len(grouped["ephemeral"]) == 2
        assert len(grouped["default"]) == 1

    def test_unclassified_agents_dropped(self):
        baselines = {"a": _stats([0.5] * 8), "ghost": _stats([0.5] * 8)}
        classify = _fixed_classifier({"a": "ephemeral"})  # ghost -> None
        grouped = group_baselines_by_class(baselines, classify)
        assert set(grouped) == {"ephemeral"}
        assert len(grouped["ephemeral"]) == 1


class TestBuild:
    def test_prior_per_class(self):
        baselines = {
            f"e{i}": _stats([0.5 + 0.01 * i] * 8) for i in range(3)
        }
        baselines["solo"] = _stats([0.9] * 8)
        classify = _fixed_classifier(
            {"e0": "ephemeral", "e1": "ephemeral", "e2": "ephemeral", "solo": "embodied"}
        )
        priors = build_cohort_priors(baselines, classify)

        # ephemeral has 3 contributors -> usable prior
        assert priors["ephemeral"].prior_for(SIGNAL) is not None
        mean, _std, _total, n = priors["ephemeral"].prior_for(SIGNAL)
        assert n == 3
        assert mean == pytest.approx(0.51, abs=1e-6)

        # embodied is N=1 -> below min_contributors -> empty prior, no KeyError
        assert priors["embodied"].prior_for(SIGNAL) is None

    def test_named_resident_n1_gets_no_prior(self):
        """A resident class (population of one) can never form a cohort prior."""
        baselines = {"Lumen": _stats([0.5] * 30)}
        priors = build_cohort_priors(baselines, _fixed_classifier({"Lumen": "Lumen"}))
        assert priors["Lumen"].prior_for(SIGNAL) is None

    def test_undercharacterized_contributor_excluded(self):
        baselines = {
            "a": _stats([0.5] * 8),
            "b": _stats([0.5] * 8),
            "thin": _stats([0.5, 0.5]),  # count 2 < gate
        }
        classify = _fixed_classifier({"a": "ephemeral", "b": "ephemeral", "thin": "ephemeral"})
        priors = build_cohort_priors(baselines, classify)
        _, _, total, n = priors["ephemeral"].prior_for(SIGNAL)
        assert n == 2  # thin excluded
        assert total == 16


class TestLoad:
    @pytest.mark.asyncio
    async def test_load_reads_db_and_builds(self):
        class FakeDB:
            async def load_all_behavioral_baselines(self):
                return {
                    "a": _stats([0.5] * 8),
                    "b": _stats([0.52] * 8),
                }

        classify = _fixed_classifier({"a": "ephemeral", "b": "ephemeral"})
        priors = await load_cohort_priors(FakeDB(), classify=classify)
        assert isinstance(priors["ephemeral"], CohortPrior)
        assert priors["ephemeral"].prior_for(SIGNAL) is not None

    @pytest.mark.asyncio
    async def test_load_empty_db_returns_empty(self):
        class EmptyDB:
            async def load_all_behavioral_baselines(self):
                return {}

        priors = await load_cohort_priors(EmptyDB(), classify=_fixed_classifier({}))
        assert priors == {}


class TestObserveSeedGap:
    def _priors(self):
        baselines = {f"e{i}": _stats([0.5 + 0.01 * i] * 8) for i in range(3)}
        return build_cohort_priors(
            baselines, _fixed_classifier({f"e{i}": "ephemeral" for i in range(3)})
        )

    def test_reports_seed_for_known_class(self):
        obs = observe_seed_gap("ephemeral", self._priors())
        assert SIGNAL in obs
        assert obs[SIGNAL]["contributors"] == 3
        assert obs[SIGNAL]["samples"] == 24
        assert obs[SIGNAL]["seed_std"] >= 0.0

    def test_none_for_unknown_or_thin_class(self):
        priors = self._priors()
        assert observe_seed_gap("nonesuch", priors) is None


class TestCache:
    @pytest.mark.asyncio
    async def test_ttl_cache_avoids_second_db_load(self):
        reset_cohort_prior_cache()
        calls = {"n": 0}

        class CountingDB:
            async def load_all_behavioral_baselines(db_self):
                calls["n"] += 1
                return {"a": _stats([0.5] * 8), "b": _stats([0.5] * 8)}

        classify = _fixed_classifier({"a": "ephemeral", "b": "ephemeral"})
        db = CountingDB()
        await get_cached_cohort_priors(db, classify=classify)
        await get_cached_cohort_priors(db, classify=classify)
        assert calls["n"] == 1  # second call served from cache
        reset_cohort_prior_cache()
        await get_cached_cohort_priors(db, classify=classify)
        assert calls["n"] == 2  # reset forces a rebuild


class TestShadowHook:
    @pytest.mark.asyncio
    async def test_flag_off_is_noop_and_baseline_stays_cold(self, monkeypatch):
        """Default (flag OFF): resume creates a cold baseline, no observation."""
        monkeypatch.delenv("UNITARES_COHORT_PRIOR", raising=False)
        from src import agent_behavioral_baseline as abl

        abl._baselines.pop("cold-agent", None)
        with patch("src.db.get_db") as get_db:
            await abl._maybe_observe_cohort_seed("cold-agent")
            get_db.assert_not_called()  # flag gate short-circuits before any DB touch

    @pytest.mark.asyncio
    async def test_flag_on_observes_without_mutating(self, monkeypatch, caplog):
        monkeypatch.setenv("UNITARES_COHORT_PRIOR", "on")
        reset_cohort_prior_cache()
        from src import agent_behavioral_baseline as abl

        class FakeDB:
            async def load_all_behavioral_baselines(self):
                return {f"e{i}": _stats([0.5] * 8) for i in range(3)}

        # Create the fresh (cold) baseline exactly as the resume path does.
        abl._baselines["new-eph"] = AgentBehavioralBaseline()
        with patch("src.db.get_db", return_value=FakeDB()), patch(
            "src.cohort_prior_source.default_classifier",
            return_value=_fixed_classifier(
                {"new-eph": "ephemeral", "e0": "ephemeral", "e1": "ephemeral", "e2": "ephemeral"}
            ),
        ):
            await abl._maybe_observe_cohort_seed("new-eph")

        # The observed agent's baseline is UNCHANGED — still cold, still inert.
        b = abl._baselines["new-eph"]
        assert b.sample_count == 0
        assert b.z_score(SIGNAL, 100.0) == 0.0
        abl._baselines.pop("new-eph", None)
        reset_cohort_prior_cache()
