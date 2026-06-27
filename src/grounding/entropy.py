"""Shannon entropy of the agent's response distribution — spec §3.1 S.

Three tiers in preference order:
  1. logprob  — per-token entropy from model logprobs (requires plugin instrumentation)
  2. multisample — k-sample self-consistency over semantic equivalence classes
  3. heuristic — wraps the legacy [0,1] complexity/drift-driven S (degraded mode)
"""
import math
from typing import Any, Dict, List

from src.grounding.types import GroundedValue


def compute_entropy(ctx: Any, metrics: Dict[str, Any]) -> GroundedValue:
    """Return grounded S value. Always succeeds (tier-3 is a safe fallback)."""
    args = getattr(ctx, "arguments", {}) or {}

    if "logprobs" in args:
        try:
            return _compute_from_logprobs(args["logprobs"])
        except NotImplementedError:
            pass

    if "samples" in args:
        try:
            return _compute_from_samples(args["samples"])
        except NotImplementedError:
            pass

    return _compute_heuristic(metrics)


def _candidate_logprobs(entry: Any) -> List[float]:
    """Extract one token position's top-k candidate logprobs as floats.

    Accepts the canonical form (a list/tuple of logprob floats) and the richer
    provider shapes: a dict carrying ``top_logprobs`` (list of floats, or list of
    ``{"logprob": float}``), or a single ``{"logprob": float}`` (k=1 position).
    """
    if isinstance(entry, dict):
        top = entry.get("top_logprobs")
        if top is None and "logprob" in entry:
            top = [entry]  # single-candidate position
        entry = top
    if not isinstance(entry, (list, tuple)):
        raise ValueError("token entry is not a candidate list")
    out: List[float] = []
    for c in entry:
        if isinstance(c, dict):
            c = c.get("logprob")
        if c is None:
            continue
        out.append(float(c))
    return out


def _token_entropy(logprobs: List[float]) -> float:
    """Normalized Shannon entropy in [0,1] over one token's top-k candidates."""
    k = len(logprobs)
    if k <= 1:
        return 0.0  # no representable uncertainty
    probs = [math.exp(lp) for lp in logprobs]
    total = sum(probs)
    if total <= 0.0:
        return 0.0
    probs = [p / total for p in probs]
    h = -sum(p * math.log(p) for p in probs if p > 0.0)
    return max(0.0, min(1.0, h / math.log(k)))


def _compute_from_logprobs(logprobs: list) -> GroundedValue:
    """Tier-1 S: mean per-token normalized entropy over returned top-k logprobs.

    Normalization (the load-bearing modeling choice): each token's Shannon
    entropy is divided by ``log(k)`` — the entropy of a uniform distribution over
    the k candidates the provider returned — yielding a value in [0,1] regardless
    of k. This is a *consistent proxy*, not absolute entropy: top-k truncation
    omits the distribution's tail, so it under-estimates true response-
    distribution entropy. Set ``top_logprobs`` as high as the provider allows to
    tighten the estimate. Raises NotImplementedError when no usable per-token
    candidates are present, so compute_entropy() falls through to the next tier.
    """
    if not isinstance(logprobs, (list, tuple)) or not logprobs:
        raise NotImplementedError("no usable per-token logprobs supplied")
    token_entropies: List[float] = []
    for entry in logprobs:
        try:
            cands = _candidate_logprobs(entry)
        except (ValueError, TypeError):
            continue
        if cands:
            token_entropies.append(_token_entropy(cands))
    if not token_entropies:
        raise NotImplementedError("logprobs present but no parseable token candidates")
    s = sum(token_entropies) / len(token_entropies)
    return GroundedValue(value=max(0.0, min(1.0, s)), source="logprob")


def _compute_from_samples(samples: list) -> GroundedValue:
    raise NotImplementedError(
        "tier-2 (multisample) entropy requires a semantic-equivalence classifier; "
        "deferred from Phase 1"
    )


def _compute_heuristic(metrics: Dict[str, Any]) -> GroundedValue:
    raw = metrics.get("S", 0.5)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 0.5
    val = max(0.0, min(1.0, val))
    return GroundedValue(value=val, source="heuristic")
