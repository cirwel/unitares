"""Catalog of allowed metric names.

Writing to `metrics.series` requires the name to be registered here.
A leaked bearer token therefore cannot inject arbitrary names into the
time-series — it can only write values for catalog-defined series.

The catalog has two layers. The core layer below is registered at import
time and ships to every install: it names only product metrics, the ones any
deployment can produce. A deployment that runs its own producer (a reference
resident, an operator's scraper) declares that producer's metrics in a JSON
file named by ``UNITARES_METRICS_CATALOG_EXTRA``; unset, the catalog is the
core layer alone. Scrape implementations live with their producer, not here,
so the catalog stays a lightweight schema.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from src.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Metric:
    """A catalog entry for a time-series metric.

    The name is dotted (`governance.risk.mean.7d`) for readability; Postgres
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
            f"src/fleet_metrics/catalog.py, or declare it in the file named "
            f"by {EXTRA_CATALOG_ENV}, before writing."
        ) from exc


# ---------------------------------------------------------------------------
# Core catalog
# ---------------------------------------------------------------------------
#
# Every metric defined here ships to every install, so it must be one any
# deployment can produce: nothing that names one operator's repos, org or
# residents. Those go in the deployment's extra catalog file (see
# load_extra_catalog below). Each entry answers a question an operator will
# actually ask monthly. New entries should meet the same bar — if nobody will
# read the resulting chart, it pollutes the surface area without paying rent.

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

# Governance-health series — the core EISV / verdict / finding signal over
# time. Live state has always exposed these, but they were never historized,
# so "is the fleet trending healthier or worse this month?" had no chart. Each
# is a trailing-7-day aggregate over core.agent_state or audit.events, scraped
# daily by whichever scraper the deployment runs (the reference one is
# agents/chronicler/).
register(Metric(
    name="governance.coherence.mean.7d",
    description="Fleet-mean compatibility coherence over the last 7 days (non-synthetic check-ins). Stratify by producer before interpretation; mixed or legacy_tanh_v rows are not a health trend.",
    unit="coherence",
))
register(Metric(
    name="governance.risk.mean.7d",
    description="Fleet-mean risk_score over the last 7 days (non-synthetic check-ins). Counterpart to coherence.",
    unit="risk",
))
register(Metric(
    name="governance.guide.7d",
    description="`guide` sub-actions in the last 7 days — soft governance corrections (proceed-with-nudge). From core.agent_state.state_json->>'action'.",
    unit="verdicts",
))
register(Metric(
    name="governance.pause.7d",
    description="Hard governance interventions in the last 7 days — actions other than approve/guide (cirs_block, pause, reject). Open-ended so new hard-stop actions fold in.",
    unit="verdicts",
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


# ---------------------------------------------------------------------------
# Deployment extra catalog
# ---------------------------------------------------------------------------
#
# A JSON file of the shape
#
#   {"metrics": [{"name": "...", "description": "...", "unit": "..."}]}
#
# whose entries are registered on top of the core layer, `.error` twins
# included. The reference resident's file is
# agents/chronicler/metrics_catalog.json. Unset (the default) registers
# nothing, so an install that runs no such producer advertises only product
# metrics and a POST of any other name is still refused.
EXTRA_CATALOG_ENV = "UNITARES_METRICS_CATALOG_EXTRA"

# Everything one malformed entry can raise while being built or registered:
# KeyError (name or description absent), TypeError (a field is not a string),
# ValueError (an empty name, or a name already registered with different
# fields — a core entry is never overridden).
_ENTRY_ERRORS = (KeyError, TypeError, ValueError)


def _metric_from_entry(entry: object) -> Metric:
    """Build one Metric from an extra-catalog entry, raising on bad input."""
    if not isinstance(entry, dict):
        raise TypeError(f"entry must be an object, got {type(entry).__name__}")
    name = entry["name"]
    description = entry["description"]
    unit = entry.get("unit", "")
    for key, value in (("name", name), ("description", description), ("unit", unit)):
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a string, got {type(value).__name__}")
    if not name.strip():
        raise ValueError("name must not be empty")
    return Metric(name=name, description=description, unit=unit)


def load_extra_catalog(path: str | Path | None = None) -> list[Metric]:
    """Register the metrics declared in a deployment's extra catalog file.

    Reads ``path`` if given, else ``UNITARES_METRICS_CATALOG_EXTRA``. Unset or
    empty registers nothing: the user-agnostic default. A file that is missing,
    unreadable, unparseable or not of the documented shape registers nothing
    and logs a WARNING naming it; a malformed or conflicting entry is skipped
    with a WARNING naming it and the rest still load. It degrades rather than
    raising because the catalog is imported lazily from inside a running
    server (the metrics routes and background persistence), where a raise
    could not stop the start it would need to stop — the same reasoning as the
    resident-progress manifest loader. Returns the metrics it registered.
    """
    raw = str(path) if path is not None else os.environ.get(EXTRA_CATALOG_ENV, "")
    if not raw.strip():
        return []
    catalog_path = Path(raw)
    try:
        doc = json.loads(catalog_path.read_text())
    except FileNotFoundError:
        logger.warning(
            "metrics extra catalog %s not found; registering no extra metrics",
            catalog_path,
        )
        return []
    except (OSError, ValueError, RecursionError) as e:
        logger.warning(
            "metrics extra catalog %s unreadable (%s: %s); "
            "registering no extra metrics",
            catalog_path, type(e).__name__, e,
        )
        return []
    entries = doc.get("metrics") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        logger.warning(
            "metrics extra catalog %s has no \"metrics\" list; "
            "registering no extra metrics",
            catalog_path,
        )
        return []
    loaded: list[Metric] = []
    for index, entry in enumerate(entries):
        try:
            loaded.append(register(_metric_from_entry(entry)))
        except _ENTRY_ERRORS as e:
            label = entry.get("name") if isinstance(entry, dict) else None
            logger.warning(
                "metrics extra catalog %s entry %d (%r) is malformed "
                "(%s: %s); skipping it",
                catalog_path, index, label, type(e).__name__, e,
            )
    return loaded


load_extra_catalog()
