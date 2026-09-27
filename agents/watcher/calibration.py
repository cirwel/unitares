"""Pure-function calibration math for Watcher self-calibration.

This module is I/O-free. It computes:

- ``jeffreys_lower_bound(successes, failures)`` — the 2.5% lower quantile
  of the Beta(0.5+s, 0.5+f) posterior. Used as the precision floor
  estimator. Compared to Wilson, it accepts fractional counts (we use
  decay-weighted observations) and has no degenerate behavior at small N.

- ``classify_file(path)`` — maps an absolute file path to one of six
  coarse classes ``{app, test, migration, generated, config, doc}``.
  Patterns calibrate per-(pattern × file_class) so a regex that's noisy
  on tests but precise on application code doesn't get globally demoted.

- ``decay_weight(detected_at, now, half_life_days)`` — exponential decay
  weight. Replaces a hard 90-day cutoff so rare patterns retain history
  and fast-moving ones weight recent reality.

- ``precision_by_pattern_and_class(findings, ...)`` — aggregates the
  above into the per-bucket precision dict consumed by floor_state.py.

Persistence and CLI wiring live elsewhere — keeping this file pure makes
the math trivially testable.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Jeffreys interval
# ---------------------------------------------------------------------------


def jeffreys_lower_bound(successes: float, failures: float) -> float:
    """2.5% lower quantile of the Beta(0.5+s, 0.5+f) posterior.

    Accepts non-integer counts (decay-weighted observations). Returns a
    value in [0, 1]. At s=f=0 the posterior is Beta(0.5, 0.5) and the
    lower bound is ~0.0015 — callers that distinguish "unmeasured" from
    "measured-as-zero" should gate on N separately.
    """
    if successes < 0 or failures < 0:
        raise ValueError(
            f"jeffreys_lower_bound: counts must be non-negative, "
            f"got successes={successes}, failures={failures}"
        )

    alpha = 0.5 + successes
    beta = 0.5 + failures
    return _beta_lower_quantile(alpha, beta, q=0.025)


def _beta_lower_quantile(alpha: float, beta: float, q: float) -> float:
    """Inverse-CDF of Beta(alpha, beta) at probability ``q``. Bisection on
    the regularized incomplete beta function. We use bisection rather than
    scipy to keep Watcher dependency-free.

    40 iterations of bisection over [0, 1] gives <1e-12 precision, well
    below the threshold-tuning resolution we care about (0.3).
    """
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if _beta_cdf(mid, alpha, beta) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _beta_cdf(x: float, alpha: float, beta: float) -> float:
    """Regularized incomplete beta function I_x(alpha, beta).

    Implementation: continued-fraction expansion (Numerical Recipes §6.4).
    Stable for the alpha/beta ranges Watcher produces (0.5 ≤ a ≤ ~10^4).
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(alpha + beta)
        - math.lgamma(alpha)
        - math.lgamma(beta)
        + alpha * math.log(x)
        + beta * math.log(1.0 - x)
    )
    bt = math.exp(log_bt)
    if x < (alpha + 1.0) / (alpha + beta + 2.0):
        return bt * _betacf(x, alpha, beta) / alpha
    return 1.0 - bt * _betacf(1.0 - x, beta, alpha) / beta


def _betacf(x: float, a: float, b: float, max_iter: int = 200, eps: float = 3e-7) -> float:
    """Continued-fraction evaluation for the incomplete beta. Lentz's
    method. ``max_iter=200`` is overkill for our parameter range; typical
    convergence is <30 iterations."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h


# ---------------------------------------------------------------------------
# File classifier
# ---------------------------------------------------------------------------


class FileClass:
    """Closed enum of file classes used as the second calibration axis.

    Strings (not IntEnum) so they round-trip through pattern_floor.json
    losslessly without requiring a converter. Ordering matters in
    ``classify_file`` — first match wins, with TEST checked before APP
    so test files inside src/ trees still classify as tests.
    """

    APP = "app"
    TEST = "test"
    MIGRATION = "migration"
    GENERATED = "generated"
    CONFIG = "config"
    DOC = "doc"


_TEST_FRAGMENTS = ("/tests/", "/test/")
_TEST_NAME_PREFIX = "test_"
_TEST_NAME_SUFFIX = "_test"

_MIGRATION_FRAGMENTS = ("/migrations/",)

_GENERATED_FRAGMENTS = ("/build/", "/dist/", "/__pycache__/", "/.venv/", "/node_modules/")
_GENERATED_SUFFIXES = (".pb.go", ".pb.py", ".min.js", ".pyc")

_CONFIG_FRAGMENTS = ("/.github/",)
_CONFIG_BASENAMES = frozenset({
    "pyproject.toml", "setup.cfg", "setup.py", "Makefile", "tox.ini",
    ".gitignore", ".dockerignore", "Dockerfile", "docker-compose.yml",
    "pre-commit-config.yaml", ".pre-commit-config.yaml",
})
_CONFIG_SUFFIXES = (".yml", ".yaml", ".toml", ".ini", ".cfg")

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_DOC_BASENAMES = frozenset({"README", "CHANGELOG", "LICENSE", "NOTICE", "AUTHORS"})


def classify_file(path: str) -> str:
    """Map a file path to a FileClass.

    Order: test → migration → generated → doc → config → app. TEST wins
    over APP because tests/ trees often live inside src/ trees and the
    regex precision on test fixtures is qualitatively different from
    application code.
    """
    p = Path(path)
    name = p.name
    norm = "/" + str(p).strip("/")

    if any(frag in norm for frag in _TEST_FRAGMENTS):
        return FileClass.TEST
    if name.startswith(_TEST_NAME_PREFIX):
        return FileClass.TEST
    stem = p.stem
    if stem.endswith(_TEST_NAME_SUFFIX):
        return FileClass.TEST

    if any(frag in norm for frag in _MIGRATION_FRAGMENTS):
        return FileClass.MIGRATION

    if any(frag in norm for frag in _GENERATED_FRAGMENTS):
        return FileClass.GENERATED
    if any(name.endswith(suf) for suf in _GENERATED_SUFFIXES):
        return FileClass.GENERATED

    if name in _DOC_BASENAMES:
        return FileClass.DOC
    if any(name.endswith(suf) for suf in _DOC_SUFFIXES):
        return FileClass.DOC

    if any(frag in norm for frag in _CONFIG_FRAGMENTS):
        return FileClass.CONFIG
    if name in _CONFIG_BASENAMES:
        return FileClass.CONFIG
    if any(name.endswith(suf) for suf in _CONFIG_SUFFIXES):
        return FileClass.CONFIG

    return FileClass.APP


# ---------------------------------------------------------------------------
# Decay weighting
# ---------------------------------------------------------------------------


def parse_iso_z(value):
    """Tolerant parser for the two timestamp formats present in
    findings.jsonl. Returns None on any failure — callers exclude the
    row from the weighted aggregate rather than crashing the loop.
    """
    if not value or not isinstance(value, str):
        return None
    candidates = (
        ("%Y-%m-%dT%H:%M:%SZ", value),
        ("%Y-%m-%dT%H:%M:%S%z", value.replace("Z", "+0000")),
    )
    for fmt, raw in candidates:
        try:
            ts = datetime.strptime(raw, fmt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except (TypeError, ValueError):
            continue
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (TypeError, ValueError):
        return None


def decay_weight(detected_at: datetime, now: datetime, half_life_days: float) -> float:
    """Exponential decay weight: ``2^(-age_days / half_life_days)``.

    Future timestamps clamp to 1.0 (clock skew shouldn't produce
    ``> 1.0`` weights). Both arguments must be timezone-aware; comparing
    naive and aware datetimes raises a TypeError, which we surface as a
    ValueError so callers don't silently miscompute against a wall-clock
    age that's actually wrong by the local UTC offset.
    """
    if detected_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("decay_weight: both timestamps must be timezone-aware")
    age_seconds = (now - detected_at).total_seconds()
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / 86400.0
    return 0.5 ** (age_days / half_life_days)


# ---------------------------------------------------------------------------
# Per-(pattern × file_class) aggregation
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from typing import Iterable, Mapping


# Reasons that count as a true negative (i.e. "the finding was wrong").
# wont_fix / out_of_scope / dup / unclear / stale are excluded — they
# represent operator decisions that are not statements about the finding's
# correctness. Legacy free-text reasons are also excluded (they predate the
# enum and have no consistent meaning).
PRECISION_REASONS_TRUE_NEGATIVE = frozenset({"fp"})

# The current revision of a pattern's detection rule. Every finding records
# its pattern's revision when it is created (``Finding.rule_version``), and a
# resolution grades the rule that produced the finding. So for a listed
# pattern, only findings stamped with the revision below count toward its
# precision: unstamped rows, and rows from a checkout running an older or a
# newer rule, measure a different rule. Watcher state is shared across
# worktrees and each checkout's hook runs its own ``agent.py``, so a wall-clock
# cutoff would still count findings from a checkout that has not caught up.
# Bump a pattern's revision (or add it) whenever a change alters what it flags.
#
# P006 at 1: #2424 and #2447 dropped handlers that already react (log,
# re-raise, return an error). Every fp dismissal behind the P006 floor came
# from one 2026-09-23/24 triage of the earlier rule, where most flagged
# handlers were of exactly that kind.
PATTERN_RULE_VERSIONS: dict[str, int] = {
    "P006": 1,
}


@dataclass(frozen=True)
class BucketStats:
    """Per-(pattern, file_class) precision summary.

    ``ci_lower`` is None when ``weighted_n < min_weighted_n`` — callers
    must distinguish 'unmeasured' from 'measured-as-zero' to avoid the
    cold-trap the council flagged. Demotion logic should never fire on
    a None ci_lower.
    """

    pattern: str
    file_class: str
    weighted_confirmed: float
    weighted_dismissed: float
    weighted_n: float
    ci_lower: float | None
    latest_observation: str | None


def precision_by_pattern_and_class(
    findings: Iterable[Mapping[str, object]],
    *,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    min_weighted_n: float = 10.0,
    true_negative_reasons: Iterable[str] = PRECISION_REASONS_TRUE_NEGATIVE,
    rule_versions: Mapping[str, int] | None = None,
) -> dict[tuple[str, str], BucketStats]:
    """Aggregate findings into per-(pattern, file_class) precision stats.

    Only ``confirmed`` and qualifying ``dismissed`` rows contribute. A
    dismissed row qualifies when its ``resolution_reason`` is in
    ``true_negative_reasons`` (default: just ``{'fp'}``). Legacy rows
    with free-text reasons or no reason are excluded from the dismissed
    count — they don't represent a precision-relevant signal.

    A row of a pattern listed in ``rule_versions`` (default
    ``PATTERN_RULE_VERSIONS``) counts only when its ``rule_version`` equals
    that pattern's entry.

    Returns ``{(pattern, file_class): BucketStats}``. Buckets with
    ``weighted_n < min_weighted_n`` carry ``ci_lower=None`` so callers
    can distinguish 'unmeasured' from 'measured-as-zero'.
    """
    reference = now or datetime.now(timezone.utc)
    tn_reasons = frozenset(true_negative_reasons)
    versions = PATTERN_RULE_VERSIONS if rule_versions is None else rule_versions

    aggregates: dict[tuple[str, str], dict] = {}

    for row in findings:
        status = row.get("status")
        if status not in ("confirmed", "dismissed"):
            continue
        pattern = row.get("pattern")
        file_path = row.get("file")
        if not isinstance(pattern, str) or not isinstance(file_path, str):
            continue

        # Decay clock anchors on the most relevant timestamp:
        # confirmed_at / dismissed_at when present (resolution time is
        # what we're calibrating), falling back to detected_at.
        resolved_at = row.get("confirmed_at") if status == "confirmed" else row.get("dismissed_at")
        ts_raw = resolved_at if isinstance(resolved_at, str) else row.get("detected_at")
        if not isinstance(ts_raw, str):
            continue
        ts = parse_iso_z(ts_raw)
        if ts is None:
            continue

        if status == "dismissed":
            reason = row.get("resolution_reason")
            if not isinstance(reason, str) or reason not in tn_reasons:
                continue

        if pattern in versions and row.get("rule_version") != versions[pattern]:
            continue

        file_class = classify_file(file_path)
        key = (pattern, file_class)
        weight = decay_weight(ts, reference, half_life_days)
        bucket = aggregates.setdefault(
            key,
            {"confirmed": 0.0, "dismissed": 0.0, "latest": None},
        )
        if status == "confirmed":
            bucket["confirmed"] = float(bucket["confirmed"]) + weight
        else:
            bucket["dismissed"] = float(bucket["dismissed"]) + weight

        latest = bucket["latest"]
        if latest is None or (isinstance(latest, str) and ts_raw > latest):
            bucket["latest"] = ts_raw

    out: dict[tuple[str, str], BucketStats] = {}
    for (pattern, file_class), agg in aggregates.items():
        wc = float(agg["confirmed"])
        wd = float(agg["dismissed"])
        wn = wc + wd
        ci = jeffreys_lower_bound(wc, wd) if wn >= min_weighted_n else None
        latest_value = agg["latest"]
        out[(pattern, file_class)] = BucketStats(
            pattern=pattern,
            file_class=file_class,
            weighted_confirmed=wc,
            weighted_dismissed=wd,
            weighted_n=wn,
            ci_lower=ci,
            latest_observation=latest_value if isinstance(latest_value, str) else None,
        )
    return out


# ---------------------------------------------------------------------------
# ε-greedy probe selection
# ---------------------------------------------------------------------------


def probe_rate_for_n(weighted_n: float) -> float:
    """Adaptive ε for the demotion exploration policy.

    Below the min-N gate (``< 10``) we don't demote at all — return 1.0
    for completeness (no demotion → all surface). Above min-N, the rate
    steps down with bucket maturity. Stepped (not continuous) so the
    behavior is easy to reason about and tweak.
    """
    if weighted_n < 10.0:
        return 1.0
    if weighted_n < 30.0:
        return 1.0 / 3.0
    if weighted_n < 100.0:
        return 1.0 / 5.0
    return 1.0 / 10.0


def should_probe(fingerprint: str, *, date_iso: str, probe_rate: float) -> bool:
    """Deterministic per-(fingerprint, day) probe decision.

    Hashing on ``(fingerprint, day)`` means a given finding is either
    probed for the whole day or not — no flicker as the surface hook
    fires repeatedly within a session. Across days the choice
    re-randomizes, so a demoted bucket has a path back to visibility.

    The hash is sha256 over the joined string; we take the first 8 hex
    chars as an unsigned int and divide by 2**32. Cryptographic strength
    isn't required, but using sha256 means we don't need to import
    Python's per-process randomized hash() and the test suite is
    reproducible across runs.
    """
    if probe_rate >= 1.0:
        return True
    if probe_rate <= 0.0:
        return False
    import hashlib

    digest = hashlib.sha256(f"{fingerprint}|{date_iso}".encode()).hexdigest()
    bucket = int(digest[:8], 16) / float(0x1_00000000)
    return bucket < probe_rate
