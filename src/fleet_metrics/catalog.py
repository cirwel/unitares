"""Catalog of allowed metric names.

Writing to `metrics.series` requires the name to be registered here.
A leaked bearer token therefore cannot inject arbitrary names into the
time-series — it can only write values for catalog-defined series.

New metrics are added by registering a `Metric` instance at import time.
Scrape implementations (in scrapers/ modules or the Chronicler agent) are
separate from catalog entries so the catalog stays a lightweight schema.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    """A catalog entry for a time-series metric.

    The name is dotted (`tokei.unitares.src.code`) for readability; Postgres
    indexes it as plain TEXT, so there is no structural meaning to dots.

    `description` shows up in the catalog GET endpoint and dashboard so that
    a reader can tell what any series represents without digging for the
    scrape source.
    """

    name: str
    description: str
    unit: str = ""  # e.g. "lines", "seconds", "count", "" for dimensionless


catalog: dict[str, Metric] = {}


def register(metric: Metric) -> Metric:
    """Add a metric to the catalog. Idempotent on identical re-registration.

    Also auto-registers a paired ``<name>.error`` twin so Chronicler's
    failure-visibility path (POST ``<name>.error = 1`` on scrape failure)
    isn't silently 404'd by the catalog gate. Metrics whose name already
    ends in ``.error`` skip the auto-twin to avoid ``.error.error`` chains.
    """
    existing = catalog.get(metric.name)
    if existing is not None and existing != metric:
        raise ValueError(
            f"Metric {metric.name!r} is already registered with different "
            f"fields: existing={existing!r}, new={metric!r}"
        )
    catalog[metric.name] = metric
    if not metric.name.endswith(".error"):
        twin_name = f"{metric.name}.error"
        if twin_name not in catalog:
            catalog[twin_name] = Metric(
                name=twin_name,
                description=f"1 when the {metric.name} scraper raised; absence = success.",
                unit="errors",
            )
    return metric


def require(name: str) -> Metric:
    """Look up a metric by name; raise KeyError if the name is not registered."""
    try:
        return catalog[name]
    except KeyError as exc:
        raise KeyError(
            f"Metric {name!r} is not in the catalog. Register it in "
            f"src/fleet_metrics/catalog.py before writing."
        ) from exc


# ---------------------------------------------------------------------------
# Initial catalog
# ---------------------------------------------------------------------------
#
# Every metric defined here is one that answers a question the operator will
# actually ask monthly. New entries should meet the same bar — if nobody will
# read the resulting chart, it pollutes the surface area without paying rent.

register(Metric(
    name="tokei.unitares.src.code",
    description="Lines of code (excluding comments/blanks) in unitares/src/ — Python only.",
    unit="lines",
))

register(Metric(
    name="tests.unitares.count",
    description="Number of `test_*.py` files in unitares/tests/ — rough proxy for test-surface breadth.",
    unit="files",
))

register(Metric(
    name="agents.active.7d",
    description="Distinct agents with any tool call in the last 7 days — fleet liveness curve.",
    unit="agents",
))

register(Metric(
    name="kg.entries.count",
    description="Total discoveries in the knowledge graph — cumulative KG growth.",
    unit="entries",
))

register(Metric(
    name="checkins.7d",
    description="`process_agent_update` calls in the last 7 days — governance traffic (feeds paper v7 corpus-maturity status).",
    unit="calls",
))

# GitHub traffic for the CIRWEL org. The GitHub traffic API only exposes a
# rolling 14-day window, so daily snapshots overlap heavily by design — the
# longitudinal value is the trend curve, not point-in-time deltas. Aggregated
# across all non-archived repos because per-repo series would mean ~64 entries
# in this catalog before any of them earned their rent.
register(Metric(
    name="github.cirwel.traffic.views.14d",
    description="GitHub page-view count summed across non-archived CIRWEL repos. GitHub traffic API rolling 14-day window; not daily delta.",
    unit="views",
))
register(Metric(
    name="github.cirwel.traffic.views.uniques.14d",
    description="GitHub unique-visitor count summed across non-archived CIRWEL repos. GitHub traffic API rolling 14-day window; not daily delta.",
    unit="visitors",
))
register(Metric(
    name="github.cirwel.traffic.clones.14d",
    description="GitHub clone count summed across non-archived CIRWEL repos. GitHub traffic API rolling 14-day window; not daily delta.",
    unit="clones",
))
register(Metric(
    name="github.cirwel.traffic.clones.uniques.14d",
    description="GitHub unique-cloner count summed across non-archived CIRWEL repos. GitHub traffic API rolling 14-day window; not daily delta.",
    unit="cloners",
))

# Numpy ODE step wall-clock — the load-bearing unknown from
# beam-footprint-roadmap-v0.md v0.3 RESOLUTION ("what's in the 7s ODE
# remainder?"). Sampled every 5 minutes from perf_monitor (in-process,
# 1000-sample ring buffer). p50 + p99 only — finer percentiles do not
# answer a question the operator will read.
#
# Naming note: this measures `monitor.process_update` dispatched via the
# default executor — wall-clock includes executor queue-wait AND numpy
# work. Renamed from `ode.compute_ms` to make this honest; see
# ode-profile-decomposition-2026-05-20.md falsifier matrix.
register(Metric(
    name="ode.numpy_step_ms.p50",
    description="Median wall-clock of monitor.process_update (numpy ODE step) over the trailing in-process window. Snapshot every 5min. Includes executor queue-wait time as well as numpy compute.",
    unit="ms",
))
register(Metric(
    name="ode.numpy_step_ms.p99",
    description="p99 wall-clock of monitor.process_update over the trailing in-process window. Tracks the substrate-tax tail at the ODE numpy-step boundary; under saturated default executor, queue-wait can dominate numpy.",
    unit="ms",
))

# Lease-plane client RPC latency — the v0.3.2 amendment's
# "lease-plane Phase A latency instrumentation" gate. Sampled every
# 5min from perf_monitor.
register(Metric(
    name="lease_plane.client.v1.lease.acquire.p50",
    description="Median wall-clock for lease.acquire RPC (Python client to BEAM lease plane). Snapshot every 5min.",
    unit="ms",
))
register(Metric(
    name="lease_plane.client.v1.lease.acquire.p99",
    description="p99 wall-clock for lease.acquire RPC. Tracks substrate-tax tail at the lease boundary (BEAM↔Python).",
    unit="ms",
))
