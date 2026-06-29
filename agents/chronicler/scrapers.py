"""Scraper functions for Chronicler.

Each scraper maps a catalog metric name to a zero-arg callable that
returns the scalar value at call time. Scrapers are pure "measure
something" functions — Chronicler handles HTTP posting, error emission,
and cadence. That separation keeps each scraper independently testable.

A scraper may raise on failure; Chronicler catches the exception, logs
it, and emits `<name>.error = 1` so silent breakage shows up in the
dashboard instead of as a missing line.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

# Default matches the server's DSN (see .claude/CLAUDE.md — one Postgres
# instance, one database). Overridable so a reflash or remote scrape can
# point somewhere else without code changes.
DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/governance"


def _fetchval(sql: str) -> float:
    """Run ``sql`` against the governance DB and return the scalar as float.

    Uses asyncpg under a one-shot ``asyncio.run`` because Chronicler is a
    daily cron, not a long-running process — the per-call loop is cheap at
    this cadence and keeps scrapers stateless (no shared pool to plumb).
    """
    import asyncpg  # local import: keeps test-time patching simple

    dsn = os.environ.get("CHRONICLER_DB_DSN", DEFAULT_DSN)

    async def _run() -> float:
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            value = await conn.fetchval(sql)
            return float(value or 0)
        finally:
            if conn is not None:
                await conn.close()

    return asyncio.run(_run())


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
    """Distinct governed agents that checked in (wrote state) in the last 7 days.

    Counts participating identities in ``core.agent_state`` — NOT distinct
    ``audit.tool_usage.agent_id``, which is inflated ~130x by per-lease
    presence ids that onboard-and-vanish ephemerals mint (lease.acquire/
    lease.release pairs that never reach a real check-in).
    """
    return _fetchval(
        "SELECT count(DISTINCT identity_id) FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days'"
    )


def kg_entries_count(_repo_root: Path) -> float:
    """Total discoveries in the knowledge graph — cumulative growth."""
    return _fetchval("SELECT count(*) FROM knowledge.discoveries")


def checkins_7d(_repo_root: Path) -> float:
    """process_agent_update calls in the last 7 days — governance traffic."""
    return _fetchval(
        "SELECT count(*) FROM audit.tool_usage "
        "WHERE ts > now() - interval '7 days' AND tool_name = 'process_agent_update'"
    )


# ---------------------------------------------------------------------------
# Governance-health series. The metrics above answer "how big / how busy";
# these answer "how healthy" — the core EISV / verdict / finding signal, which
# was live-only (no time-series) until now. Each reads core.agent_state (every
# non-synthetic check-in) or audit.events over a trailing 7-day window, so they
# share the `.7d` convention and group under "governance" on the dashboard.
# ---------------------------------------------------------------------------


def governance_coherence_mean_7d(_repo_root: Path) -> float:
    """Fleet-mean coherence over the last 7 days — headline governance health.

    avg() across every non-synthetic check-in. A sustained drop is the earliest
    broad signal that the fleet is drifting."""
    return _fetchval(
        "SELECT avg(coherence) FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days' AND synthetic = false"
    )


def governance_risk_mean_7d(_repo_root: Path) -> float:
    """Fleet-mean risk_score over the last 7 days — counterpart to coherence."""
    return _fetchval(
        "SELECT avg(risk_score) FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days' AND synthetic = false"
    )


def governance_guide_7d(_repo_root: Path) -> float:
    """`guide` sub-actions in the last 7 days — soft governance corrections.

    The verdict sub-action is persisted in state_json->>'action'. `guide` is a
    proceed-with-nudge; its count shows how often governance is steering rather
    than just approving."""
    return _fetchval(
        "SELECT count(*) FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days' AND synthetic = false "
        "AND state_json->>'action' = 'guide'"
    )


def governance_pause_7d(_repo_root: Path) -> float:
    """Hard governance interventions in the last 7 days — pauses/blocks/rejects.

    Anything that is neither approve nor guide (e.g. cirs_block, pause, reject).
    Kept open-ended via NOT IN so a new hard-stop action folds in automatically
    instead of being silently dropped."""
    return _fetchval(
        "SELECT count(*) FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days' AND synthetic = false "
        "AND state_json->>'action' IS NOT NULL "
        "AND state_json->>'action' NOT IN ('approve', 'guide')"
    )


def governance_sentinel_findings_7d(_repo_root: Path) -> float:
    """Sentinel findings (incl. forced-release alarms) in the last 7 days.

    Reads the durable audit.events store — the same source /v1/sentinel/backlog
    queries — so the count tracks what the analytical resident is flagging over
    time, not just what's currently in the in-memory ring."""
    return _fetchval(
        "SELECT count(*) FROM audit.events "
        "WHERE event_type IN ('sentinel_finding', 'sentinel_alarm_finding') "
        "AND ts > now() - interval '7 days'"
    )


# ---------------------------------------------------------------------------
# GitHub traffic (CIRWEL org, non-archived repos, rolling 14-day window)
# ---------------------------------------------------------------------------
#
# One process-lifetime fetch backs all four traffic scrapers — Chronicler is
# a one-shot launchd job, so `lru_cache` here means "once per scrape run",
# not "across cron invocations". That keeps the daily snapshot consistent
# (all four series read the same underlying response) and keeps the GitHub
# API call count to one repo-list + 2N traffic calls per day.
#
# Repo set is resolved live via `gh repo list CIRWEL` so the metric tracks
# "current non-archived org surface" — adding a new public repo automatically
# folds it into the next day's snapshot, and archiving one drops it.

# Org whose traffic this Chronicler scrapes. Override per deployment via
# GITHUB_SCRAPE_ORG; the metric *names* below stay `github.cirwel.*` as stable
# keys (renaming them would break stored-metric continuity).
GITHUB_ORG = os.getenv("GITHUB_SCRAPE_ORG", "CIRWEL")


@functools.lru_cache(maxsize=1)
def _fetch_cirwel_traffic() -> dict[str, int]:
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
    list_proc = subprocess.run(
        ["gh", "repo", "list", GITHUB_ORG, "--limit", "200", "--json", "name,isArchived"],
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
                ["gh", "api", f"repos/{GITHUB_ORG}/{name}/traffic/{kind}"],
                check=True, capture_output=True, text=True,
            )
            data = json.loads(proc.stdout)
            totals[count_key] += int(data.get("count", 0))
            totals[uniq_key] += int(data.get("uniques", 0))
    return totals


def github_cirwel_traffic_views_14d(_repo_root: Path) -> float:
    return float(_fetch_cirwel_traffic()["views"])


def github_cirwel_traffic_views_uniques_14d(_repo_root: Path) -> float:
    return float(_fetch_cirwel_traffic()["views_uniques"])


def github_cirwel_traffic_clones_14d(_repo_root: Path) -> float:
    return float(_fetch_cirwel_traffic()["clones"])


def github_cirwel_traffic_clones_uniques_14d(_repo_root: Path) -> float:
    return float(_fetch_cirwel_traffic()["clones_uniques"])


# Registry: metric name → scrape callable. Chronicler iterates this on each run.
#
# Keep this in sync with the server-side catalog in
# src/fleet_metrics/catalog.py — the server validates writes against the
# catalog, so a name here without a matching catalog entry is a 404 at
# the POST endpoint.
SCRAPERS: dict[str, Callable[[Path], float]] = {
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
    "github.cirwel.traffic.views.14d": github_cirwel_traffic_views_14d,
    "github.cirwel.traffic.views.uniques.14d": github_cirwel_traffic_views_uniques_14d,
    "github.cirwel.traffic.clones.14d": github_cirwel_traffic_clones_14d,
    "github.cirwel.traffic.clones.uniques.14d": github_cirwel_traffic_clones_uniques_14d,
}
