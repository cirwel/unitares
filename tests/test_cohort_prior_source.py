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


class TestSeedHook:
    _CLASSIFY = {"new-eph": "ephemeral", "e0": "ephemeral", "e1": "ephemeral", "e2": "ephemeral"}

    class _FakeDB:
        async def load_all_behavioral_baselines(self):
            return {f"e{i}": _stats([0.5] * 8) for i in range(3)}

    def _patches(self):
        return patch("src.db.get_db", return_value=self._FakeDB()), patch(
            "src.cohort_prior_source.default_classifier",
            return_value=_fixed_classifier(self._CLASSIFY),
        )

    @pytest.mark.asyncio
    async def test_flag_off_is_noop_and_baseline_stays_cold(self, monkeypatch):
        """Default (flag OFF): resume creates a cold baseline, no DB touch."""
        monkeypatch.delenv("UNITARES_COHORT_PRIOR", raising=False)
        from src import agent_behavioral_baseline as abl

        abl._baselines.pop("cold-agent", None)
        with patch("src.db.get_db") as get_db:
            await abl._maybe_seed_cohort_prior("cold-agent")
            get_db.assert_not_called()  # flag gate short-circuits before any DB touch

    @pytest.mark.asyncio
    async def test_observe_mode_is_default_and_does_not_mutate(self, monkeypatch):
        """Flag ON, mode unset -> observe: logs, never mutates the baseline."""
        monkeypatch.setenv("UNITARES_COHORT_PRIOR", "on")
        monkeypatch.delenv("UNITARES_COHORT_PRIOR_MODE", raising=False)
        reset_cohort_prior_cache()
        from src import agent_behavioral_baseline as abl

        abl._baselines["new-eph"] = AgentBehavioralBaseline()
        p1, p2 = self._patches()
        with p1, p2:
            await abl._maybe_seed_cohort_prior("new-eph")

        b = abl._baselines["new-eph"]
        assert b.sample_count == 0  # unchanged — still cold
        assert b.z_score(SIGNAL, 100.0) == 0.0
        abl._baselines.pop("new-eph", None)
        reset_cohort_prior_cache()

    @pytest.mark.asyncio
    async def test_apply_mode_seeds_but_stays_inert_until_earned(self, monkeypatch):
        """Flag ON, mode=apply -> the fresh baseline is replaced with a seeded one,
        carrying the cohort mean, yet STILL inert until the agent earns its own count."""
        monkeypatch.setenv("UNITARES_COHORT_PRIOR", "on")
        monkeypatch.setenv("UNITARES_COHORT_PRIOR_MODE", "apply")
        reset_cohort_prior_cache()
        from src import agent_behavioral_baseline as abl
        from src.cohort_prior import DEFAULT_PSEUDO_COUNT

        abl._baselines["new-eph"] = AgentBehavioralBaseline()
        p1, p2 = self._patches()
        with p1, p2:
            await abl._maybe_seed_cohort_prior("new-eph")

        b = abl._baselines["new-eph"]
        seeded = b._stats[SIGNAL]
        assert seeded.count == DEFAULT_PSEUDO_COUNT       # seeded, not cold
        assert seeded.mean == pytest.approx(0.5, abs=1e-6)  # carries cohort mean
        assert b.z_score(SIGNAL, 100.0) == 0.0            # but still inert (anti-poisoning)
        abl._baselines.pop("new-eph", None)
        reset_cohort_prior_cache()

    @pytest.mark.asyncio
    async def test_apply_mode_unknown_value_falls_back_to_observe(self, monkeypatch):
        monkeypatch.setenv("UNITARES_COHORT_PRIOR", "on")
        monkeypatch.setenv("UNITARES_COHORT_PRIOR_MODE", "yolo")  # not 'apply'
        reset_cohort_prior_cache()
        from src import agent_behavioral_baseline as abl

        abl._baselines["new-eph"] = AgentBehavioralBaseline()
        p1, p2 = self._patches()
        with p1, p2:
            await abl._maybe_seed_cohort_prior("new-eph")

        assert abl._baselines["new-eph"].sample_count == 0  # observe: not mutated
        abl._baselines.pop("new-eph", None)
        reset_cohort_prior_cache()
