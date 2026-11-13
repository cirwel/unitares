"""Scraper functions for Chronicler.

Each scraper maps a catalog metric name to a zero-arg callable that
returns the scalar value at call time. Scrapers are pure "measure
something" functions — Chronicler handles HTTP posting, error emission,
and cadence. That separation keeps each scraper independently testable.

Database-backed fleet metrics are fetched from Core's authenticated
`GET /v1/metrics/snapshot` endpoint (one HTTP round-trip per scrape cycle,
with per-metric error isolation and negative caching) so Chronicler never
opens a direct PostgreSQL connection or imports `src.*`.

A scraper may raise on failure; Chronicler catches the exception, logs
it, and emits `<name>.error = 1` so silent breakage shows up in the
dashboard instead of as a missing line.
"""

from __future__ import annotations

from contextlib import contextmanager
import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx

from agents.common.http_client import (
    resolve_governance_base_url,
    send_with_bearer_fallback,
)

SNAPSHOT_TIMEOUT_S = 10.0

_active_scope: tuple[str | None, str | None, httpx.Client | None] | None = None
_snapshot_cache: tuple[str, Any] | None = None


def clear_snapshot_cache() -> None:
    """Clear the per-run `/v1/metrics/snapshot` cache (positive and negative)."""
    global _snapshot_cache
    _snapshot_cache = None


@contextmanager
def snapshot_scope(
    *,
    base_url: str | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
):
    """Bind `(base_url, token, client)` and bound snapshot caching to one run."""
    global _active_scope
    prev_scope = _active_scope
    clear_snapshot_cache()
    _active_scope = (base_url, token, client)
    try:
        yield
    finally:
        _active_scope = prev_scope
        clear_snapshot_cache()


def _fetch_metrics_snapshot() -> dict[str, Any]:
    """Fetch `/v1/metrics/snapshot` once per scrape run, caching failures too."""
    global _snapshot_cache
    if _snapshot_cache is not None:
        kind, payload = _snapshot_cache
        if kind == "error":
            raise RuntimeError(str(payload))
        return payload

    scoped_url, scoped_token, scoped_client = _active_scope or (None, None, None)
    effective_url = scoped_url or os.environ.get("UNITARES_METRICS_URL", "").strip() or None
    base_url = resolve_governance_base_url(effective_url)
    url = f"{base_url}/v1/metrics/snapshot"

    try:
        if scoped_client is not None:
            resp = send_with_bearer_fallback(
                lambda hdrs: scoped_client.get(
                    url, headers=hdrs, params={}, timeout=SNAPSHOT_TIMEOUT_S,
                ),
                explicit_token=scoped_token,
                base_url=base_url,
            )
        else:
            with httpx.Client() as one_shot:
                resp = send_with_bearer_fallback(
                    lambda hdrs: one_shot.get(
                        url, headers=hdrs, params={}, timeout=SNAPSHOT_TIMEOUT_S,
                    ),
                    explicit_token=scoped_token,
                    base_url=base_url,
                )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GET /v1/metrics/snapshot returned HTTP {resp.status_code}: "
                f"{getattr(resp, 'text', '')[:200]}"
            )
        body = resp.json()
        if not isinstance(body, dict) or not body.get("success"):
            raise RuntimeError(
                f"GET /v1/metrics/snapshot rejected: {body!r}"
            )
    except Exception as exc:
        _snapshot_cache = ("error", f"{type(exc).__name__}: {exc}")
        raise

    _snapshot_cache = ("ok", body)
    return body


def _snapshot_metric(name: str) -> float:
    """Extract one scalar `name` from `/v1/metrics/snapshot`, raising on server error."""
    snap = _fetch_metrics_snapshot()
    errors = snap.get("errors") or {}
    if name in errors:
        raise RuntimeError(f"server snapshot error for {name}: {errors[name]}")
    metrics = snap.get("metrics") or {}
    if name not in metrics or metrics[name] is None:
        raise RuntimeError(f"metric {name!r} missing from /v1/metrics/snapshot")
    return float(metrics[name])


def tokei_unitares_src_code(repo_root: Path) -> float:
    """Count `.py` lines under `src/` of the unitares repo.

    Uses `wc -l` rather than tokei so no extra dep is required. Counts
    total lines (code + comments + blanks together). Absolute accuracy
    doesn't matter as long as the methodology is consistent over time.
    The `find` invocation is locked to `*.py` so language drift in
    subdirectories cannot change the reported value.
    """
    src = repo_root / "src"
    if not src.is_dir():
        raise FileNotFoundError(f"src/ not found at {src}")

    # find + xargs avoids a single huge argv; `wc -l` itself totals to stdout's last line.
    find = subprocess.run(
        ["find", str(src), "-type", "f", "-name", "*.py"],
        check=True, capture_output=True, text=True,
    )
    files = [f for f in find.stdout.splitlines() if f]
    if not files:
        return 0.0

    wc = subprocess.run(
        ["wc", "-l"] + files,
        check=True, capture_output=True, text=True,
    )
    last_line = wc.stdout.strip().splitlines()[-1]
    parts = last_line.split()
    if not parts:
        raise RuntimeError(f"Unexpected wc output: {wc.stdout!r}")
    return float(parts[0])


def tests_unitares_count(repo_root: Path) -> float:
    """Count `test_*.py` files under unitares/tests/.

    Uses `find` and relies on its exit code to catch missing directories.
    Includes tests under subdirectories (e.g. agents/*/tests/ are excluded
    — only unitares/tests/ so the signal stays interpretable).
    """
    tests = repo_root / "tests"
    if not tests.is_dir():
        raise FileNotFoundError(f"tests/ not found at {tests}")
    find = subprocess.run(
        ["find", str(tests), "-type", "f", "-name", "test_*.py"],
        check=True, capture_output=True, text=True,
    )
    files = [f for f in find.stdout.splitlines() if f]
    return float(len(files))


def agents_active_7d(_repo_root: Path) -> float:
    """Distinct governed agents that checked in (wrote state) in the last 7 days."""
    return _snapshot_metric("agents.active.7d")


def kg_entries_count(_repo_root: Path) -> float:
    """Total discoveries in the knowledge graph — cumulative growth."""
    return _snapshot_metric("kg.entries.count")


def checkins_7d(_repo_root: Path) -> float:
    """process_agent_update calls in the last 7 days — governance traffic."""
    return _snapshot_metric("checkins.7d")


# ---------------------------------------------------------------------------
# Governance-state series. The metrics above answer "how big / how busy";
# these retain EISV / verdict / finding time series that were previously
# live-only. Each reads Core's `/v1/metrics/snapshot` trailing 7-day aggregates,
# so they share the `.7d` convention. Individual metrics carry their own
# interpretation; the compatibility coherence mean is not itself health.
# ---------------------------------------------------------------------------


def governance_coherence_mean_7d(_repo_root: Path) -> float:
    """Fleet-mean compatibility coherence over the last 7 days.

    This legacy aggregate predates producer provenance. It must be stratified by
    ``state_json.coherence_form`` before interpretation; legacy_tanh_v movement
    is directional control-feedback movement, not a fleet-health trend."""
    return _snapshot_metric("governance.coherence.mean.7d")


def governance_risk_mean_7d(_repo_root: Path) -> float:
    """Fleet-mean risk_score over the last 7 days — counterpart to coherence."""
    return _snapshot_metric("governance.risk.mean.7d")


def governance_guide_7d(_repo_root: Path) -> float:
    """`guide` sub-actions in the last 7 days — soft governance corrections."""
    return _snapshot_metric("governance.guide.7d")


def governance_pause_7d(_repo_root: Path) -> float:
    """Hard governance interventions in the last 7 days — pauses/blocks/rejects."""
    return _snapshot_metric("governance.pause.7d")


def governance_sentinel_findings_7d(_repo_root: Path) -> float:
    """Sentinel findings (incl. forced-release alarms) in the last 7 days.

    Aggregates `sentinel_finding` + `sentinel_alarm_finding` from Core's
    product-neutral `finding_counts_7d` map on `/v1/metrics/snapshot`.
    """
    snap = _fetch_metrics_snapshot()
    errors = snap.get("errors") or {}
    if "finding_counts_7d" in errors:
        raise RuntimeError(
            f"server snapshot error for finding_counts_7d: {errors['finding_counts_7d']}"
        )
    counts = snap.get("finding_counts_7d")
    if not isinstance(counts, dict):
        raise RuntimeError("finding_counts_7d missing from /v1/metrics/snapshot")
    return float(
        int(counts.get("sentinel_finding", 0))
        + int(counts.get("sentinel_alarm_finding", 0))
    )


# ---------------------------------------------------------------------------
# GitHub traffic (explicitly configured owner, rolling 14-day window)
# ---------------------------------------------------------------------------
#
# One process-lifetime fetch backs all four traffic scrapers — Chronicler is
# a one-shot launchd job, so `lru_cache` here means "once per scrape run",
# not "across cron invocations". That keeps the daily snapshot consistent
# (all four series read the same underlying response) and keeps the GitHub
# API call count to one repo-list + 2N traffic calls per day.
#
# Repo set is resolved live via `gh repo list <configured owner>` so the metric tracks
# "current non-archived org surface" — adding a new public repo automatically
# folds it into the next day's snapshot, and archiving one drops it.

# Traffic scraping is optional. No owner means no GitHub calls and no traffic
# series. A deployment can retain an existing series namespace explicitly.
GITHUB_ORG = os.getenv("GITHUB_SCRAPE_ORG", "").strip()
GITHUB_METRIC_PREFIX = os.getenv("CHRONICLER_GITHUB_METRIC_PREFIX", "github").strip()


@functools.lru_cache(maxsize=1)
def _fetch_github_traffic(owner: str | None = None) -> dict[str, int]:
    """Fetch + aggregate GitHub traffic for non-archived ``GITHUB_ORG`` repos.

    Returns a dict with keys ``views``, ``views_uniques``, ``clones``,
    ``clones_uniques``. Each is a 14-day rolling total summed across the
    org. Cached for the process lifetime — call ``cache_clear()`` from tests
    that need to vary the underlying response.

    Uses the ``gh`` CLI rather than raw HTTP so we inherit the user's
    existing auth (no PAT plumbing needed in launchd). If ``gh`` is missing
    or unauthenticated, ``subprocess.run`` raises and Chronicler emits the
    paired ``.error`` metric.
    """
    owner = GITHUB_ORG if owner is None else owner
    if not owner:
        raise ValueError("GITHUB_SCRAPE_ORG must be configured for traffic scraping")
    list_proc = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "200", "--json", "name,isArchived"],
        check=True, capture_output=True, text=True,
    )
    repos = [r for r in json.loads(list_proc.stdout) if not r.get("isArchived")]

    totals = {"views": 0, "views_uniques": 0, "clones": 0, "clones_uniques": 0}
    for repo in repos:
        name = repo["name"]
        for kind, count_key, uniq_key in (
            ("views", "views", "views_uniques"),
            ("clones", "clones", "clones_uniques"),
        ):
            proc = subprocess.run(
                ["gh", "api", f"repos/{owner}/{name}/traffic/{kind}"],
                check=True, capture_output=True, text=True,
            )
            data = json.loads(proc.stdout)
            totals[count_key] += int(data.get("count", 0))
            totals[uniq_key] += int(data.get("uniques", 0))
    return totals


def github_traffic_views_14d(_repo_root: Path, *, owner: str | None = None) -> float:
    return float(_fetch_github_traffic(owner)["views"])


def github_traffic_views_uniques_14d(_repo_root: Path, *, owner: str | None = None) -> float:
    return float(_fetch_github_traffic(owner)["views_uniques"])


def github_traffic_clones_14d(_repo_root: Path, *, owner: str | None = None) -> float:
    return float(_fetch_github_traffic(owner)["clones"])


def github_traffic_clones_uniques_14d(_repo_root: Path, *, owner: str | None = None) -> float:
    return float(_fetch_github_traffic(owner)["clones_uniques"])


# Registry: metric name → scrape callable. Chronicler iterates this on each run.
#
# Keep this in sync with the server-side catalog — the server validates
# writes against it, so a name here without a matching entry is a 404 at the
# POST endpoint. Product metrics any install can produce are registered in
# src/fleet_metrics/catalog.py; names specific to this deployment (its repo,
# its GitHub org, its reference residents) go in metrics_catalog.json next to
# this file, which the server loads only when UNITARES_METRICS_CATALOG_EXTRA
# names it.
BASE_SCRAPERS: dict[str, Callable[[Path], float]] = {
    "tokei.unitares.src.code": tokei_unitares_src_code,
    "tests.unitares.count": tests_unitares_count,
    "agents.active.7d": agents_active_7d,
    "kg.entries.count": kg_entries_count,
    "checkins.7d": checkins_7d,
    "governance.coherence.mean.7d": governance_coherence_mean_7d,
    "governance.risk.mean.7d": governance_risk_mean_7d,
    "governance.guide.7d": governance_guide_7d,
    "governance.pause.7d": governance_pause_7d,
    "governance.sentinel.findings.7d": governance_sentinel_findings_7d,
}


def build_scrapers(
    owner: str = GITHUB_ORG, prefix: str = GITHUB_METRIC_PREFIX,
) -> dict[str, Callable[[Path], float]]:
    """Register traffic only for an explicit owner; names are deployment data."""
    import re

    scrapers = dict(BASE_SCRAPERS)
    if not owner.strip():
        return scrapers
    if not re.fullmatch(r"[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*", prefix):
        raise ValueError("CHRONICLER_GITHUB_METRIC_PREFIX must be a metric namespace")
    for suffix, scraper in (
        ("views", github_traffic_views_14d),
        ("views.uniques", github_traffic_views_uniques_14d),
        ("clones", github_traffic_clones_14d),
        ("clones.uniques", github_traffic_clones_uniques_14d),
    ):
        scrapers[f"{prefix}.traffic.{suffix}.14d"] = functools.partial(scraper, owner=owner)
    return scrapers


SCRAPERS = build_scrapers()
