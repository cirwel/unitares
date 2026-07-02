"""Shadow, read-only source that assembles per-class cohort priors.

Follow-up to `src/cohort_prior.py` (the pure primitive). This module does the
DB-touching + classification legwork the primitive deliberately does not: it
bulk-loads stored Welford baselines, groups them by calibration class using the
*same* `classify_agent` logic the rest of the system uses, and hands back one
`CohortPrior` per class.

Read-only end to end: it never mutates a baseline, never persists, and is on no
live path unless a caller opts in. Wiring a seed into the resume path is gated
separately behind `cohort_prior.cohort_prior_enabled()` (default OFF); this
module is the source those consumers would read from.

Class grouping notes
--------------------
- Classes are the calibration classes from `src.grounding.class_indicator`
  (`ephemeral`, `resident_persistent`, `embodied`, ..., `default`).
- Named residents are N=1 classes by construction (class == agent), so they can
  never reach the `min_contributors` floor and correctly get no cohort prior —
  you cannot warm-start a unique resident from itself.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from src.agent_behavioral_baseline import AgentBehavioralBaseline
from src.cohort_prior import (
    CohortPrior,
    DEFAULT_MIN_AGENT_COUNT,
    DEFAULT_PSEUDO_COUNT,
    DEFAULT_WIDEN,
    MIN_CONTRIBUTORS,
)

# agent_id -> class name (or None to exclude the agent from any cohort).
ClassifyFn = Callable[[str], Optional[str]]


def default_classifier() -> ClassifyFn:
    """Return an `agent_id -> class` function backed by the metadata cache.

    Mirrors `governance_monitor._resolve_agent_class` production path: the
    `agent_metadata` cache carries each agent's label/tags, and `classify_agent`
    maps those to a calibration class. An agent absent from the cache classifies
    as `None` and is excluded from cohorts (we do not guess a class for it).

    Falls back to a classify-nothing function if the imports are unavailable
    (e.g. a minimal test process), so callers never crash on import.
    """
    try:
        from src.agent_state import agent_metadata
        from src.grounding.class_indicator import classify_agent
    except Exception:
        return lambda _agent_id: None

    def _classify(agent_id: str) -> Optional[str]:
        meta = agent_metadata.get(agent_id)
        if meta is None:
            return None
        return classify_agent(meta)

    return _classify


def group_baselines_by_class(
    baselines_by_agent: Dict[str, dict],
    classify: ClassifyFn,
) -> Dict[str, List[AgentBehavioralBaseline]]:
    """Reconstruct stored baselines and bucket them by class. Pure.

    Agents that classify as `None` are dropped rather than pooled into a
    catch-all — an unknown-class agent should not shape any cohort's prior.
    """
    grouped: Dict[str, List[AgentBehavioralBaseline]] = {}
    for agent_id, stats in baselines_by_agent.items():
        cls = classify(agent_id)
        if cls is None:
            continue
        grouped.setdefault(cls, []).append(AgentBehavioralBaseline.from_dict(stats))
    return grouped


def build_cohort_priors(
    baselines_by_agent: Dict[str, dict],
    classify: ClassifyFn,
    *,
    min_agent_count: int = DEFAULT_MIN_AGENT_COUNT,
    min_contributors: int = MIN_CONTRIBUTORS,
) -> Dict[str, CohortPrior]:
    """Build one `CohortPrior` per class from raw stored baselines. Pure.

    A class with too few characterized contributors yields a `CohortPrior` with
    no signals (the primitive's own `min_contributors` guard), so a caller can
    always look up `priors.get(cls)` and get either a usable prior or an empty
    one — never a `KeyError`-shaped surprise.
    """
    grouped = group_baselines_by_class(baselines_by_agent, classify)
    return {
        cls: CohortPrior.build(
            members,
            min_agent_count=min_agent_count,
            min_contributors=min_contributors,
        )
        for cls, members in grouped.items()
    }


async def load_cohort_priors(
    db,
    *,
    classify: Optional[ClassifyFn] = None,
    min_agent_count: int = DEFAULT_MIN_AGENT_COUNT,
    min_contributors: int = MIN_CONTRIBUTORS,
) -> Dict[str, CohortPrior]:
    """Bulk-load stored baselines from `db` and build per-class priors.

    Read-only. `classify` defaults to `default_classifier()`. Returns `{}` if
    there are no stored baselines.
    """
    baselines_by_agent = await db.load_all_behavioral_baselines()
    if not baselines_by_agent:
        return {}
    return build_cohort_priors(
        baselines_by_agent,
        classify or default_classifier(),
        min_agent_count=min_agent_count,
        min_contributors=min_contributors,
    )


# --- Shadow-observe path (cold-start resume) --------------------------------
#
# When cohort priors are ENABLED (opt-in, default OFF), a cold-start agent's
# resume logs the seed it WOULD receive so an operator can validate calibration
# lift before anything is applied. Nothing here mutates or persists a baseline.

_CACHE_TTL_S = 300.0
_cache: Dict[str, object] = {"priors": None, "built_at": 0.0}


async def get_cached_cohort_priors(
    db,
    *,
    ttl_s: float = _CACHE_TTL_S,
    classify: Optional[ClassifyFn] = None,
) -> Dict[str, CohortPrior]:
    """Per-class priors with a coarse in-process TTL cache.

    Avoids a full bulk-load on every cold-start resume without standing up a
    background refresh task. `time.monotonic()` is used so a clock change can't
    wedge the TTL. Not thread-safe by design — a redundant concurrent rebuild is
    harmless (both produce the same read-only snapshot).
    """
    now = time.monotonic()
    priors = _cache.get("priors")
    if priors is not None and (now - float(_cache["built_at"])) < ttl_s:
        return priors  # type: ignore[return-value]
    priors = await load_cohort_priors(db, classify=classify)
    _cache["priors"] = priors
    _cache["built_at"] = now
    return priors


def reset_cohort_prior_cache() -> None:
    """Clear the TTL cache (tests, or after a known baseline-population change)."""
    _cache["priors"] = None
    _cache["built_at"] = 0.0


def observe_seed_gap(
    agent_class: str,
    priors: Dict[str, CohortPrior],
    *,
    pseudo_count: int = DEFAULT_PSEUDO_COUNT,
    widen: float = DEFAULT_WIDEN,
) -> Optional[Dict[str, dict]]:
    """Describe the seed a cold-start agent of `agent_class` WOULD receive.

    Returns per-signal `{seed_mean, seed_std, contributors, samples}`, or `None`
    if the class has no usable prior. Pure and read-only — this is the datum the
    shadow path logs, not a mutation.
    """
    prior = priors.get(agent_class)
    if prior is None:
        return None
    obs: Dict[str, dict] = {}
    for signal in AgentBehavioralBaseline.TRACKED_SIGNALS:
        p = prior.prior_for(signal)
        if p is None:
            continue
        _mean, _std, total, n = p
        seed = prior.seed_welford(signal, pseudo_count=pseudo_count, widen=widen)
        obs[signal] = {
            "seed_mean": round(seed.mean, 6),
            "seed_std": round(seed.std, 6),
            "contributors": n,
            "samples": total,
        }
    return obs or None
