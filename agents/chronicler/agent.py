#!/usr/bin/env python3
"""Chronicler — daily scraper of fleet metrics into `metrics.series`.

One-shot invocation (launchd drives cadence). Runs each scraper in
`scrapers.py`, POSTs the value to the governance server, emits a
`.error` metric on failure so silent breakage stays visible. After the
scrape loop, checks in to governance via `process_agent_update` so
Chronicler appears as a first-class resident with its own EISV
trajectory alongside Vigil/Sentinel/Watcher.

It also writes one KG digest per run naming what moved. That is not
decoration. For its first four months Chronicler wrote 14 numbers a day
into a chart and said nothing anywhere an operator reads, so the only
available reading of a healthy resident was "it isn't doing anything" —
104 unbroken daily points and 4 KG entries, none since 2026-05-31. A
scraper whose output nobody encounters is indistinguishable from a dead
one, and the fix belongs here rather than in whoever is expected to go
looking.

Environment:
    UNITARES_METRICS_URL        base URL (default http://127.0.0.1:8767)
    UNITARES_HTTP_API_TOKEN     bearer token; optional if running locally
                                (trusted-network bypass handles 127.0.0.1)
    CHRONICLER_REPO_ROOT        repo to scrape (default: working directory)
    CHRONICLER_KG_DIGEST        set to 0 to suppress the per-run KG digest
                                (default on — see above for why)
    UNITARES_FIRST_RUN          set to 1 once to mint Chronicler's identity;
                                subsequent runs resume via the anchor

Usage:
    python3 agents/chronicler/agent.py          # run all scrapers once, check in
    python3 agents/chronicler/agent.py --dry    # print metrics; skip POST and check-in

First-time bootstrap (mints UUID into ~/.unitares/anchors/chronicler.json):
    UNITARES_FIRST_RUN=1 python3 agents/chronicler/agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# Make sibling package importable when invoked via launchd (no sys.path magic
# otherwise; the launchd plist sets PYTHONPATH to the repo root).
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from agents.chronicler.scrapers import SCRAPERS
from unitares_sdk.agent import CycleResult, GovernanceAgent
from unitares_sdk.client import GovernanceClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s chronicler: %(message)s",
)
log = logging.getLogger("chronicler")


DEFAULT_URL = "http://127.0.0.1:8767"

# How far back to look for the previous reading of a metric. Chronicler's
# series are daily, so this is a handful of rows per metric — wide enough
# that a multi-week outage still yields a real prior instead of silently
# reporting every metric as a first reading.
PRIOR_WINDOW_DAYS = 30

# The digest is a nicety bolted onto the scrape, so it gets a hard budget and
# must never be able to cost the primary job. `ChroniclerAgent` runs under a
# 120s `cycle_timeout_seconds`; a per-call 10s timeout across 14 metrics would
# put 140s of prior-reads alone inside that budget and cancel the cycle before
# the check-in — a cosmetic feature taking down the thing it decorates.
# (The POST path carries the same pre-existing shape at 14x10s; that is not
# introduced here and is left alone, but the reads must not compound it.)
PRIOR_READ_TIMEOUT_S = 2.0
PRIOR_READ_BUDGET_S = 20.0

# `ephemeral` is mandatory and load-bearing: the digest is a reading taken at
# a moment, with a timestamp rather than a resolution condition, so without
# the tag every later KG sweep re-reads it as unfinished work. The tag is a
# claim about the content's shelf life, never about the writer's.
DIGEST_TAGS = ["ephemeral", "chronicler", "metrics"]

# Three outcomes, deliberately not two. Collapsing "the server would not tell
# us" into "there is no prior" writes a false claim into a durable artifact:
# the digest would report a first reading for a metric with months of history.
PRIOR_READ = "read"            # a previous value exists and we have it
PRIOR_ABSENT = "absent"        # the series really is empty in the window
PRIOR_UNREADABLE = "unreadable"  # error, or the read budget ran out

# Likewise three, so the check-in can stay quiet about a deliberate opt-out
# while still reporting a write that is broken.
DIGEST_STORED = "stored"
DIGEST_DISABLED = "disabled"
DIGEST_FAILED = "failed"


@dataclass(frozen=True)
class Movement:
    """One metric's reading, alongside the previous one when there was one."""

    name: str
    value: float
    prior: float | None
    prior_status: str = PRIOR_UNREADABLE


@dataclass
class ScrapeReport:
    """Outcome of one scrape loop: the counts, and what actually moved."""

    successes: int = 0
    failures: int = 0
    movements: list[Movement] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def fetch_prior(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    name: str,
    budget_left: float | None = None,
) -> tuple[float | None, str]:
    """Most recent recorded value for `name`, as ``(value, status)``.

    Status is one of `PRIOR_READ` / `PRIOR_ABSENT` / `PRIOR_UNREADABLE`. The
    caller needs all three: "no history" and "could not read the history" look
    identical at the call site but are different claims to write down.

    Called *before* the POST, so "prior" is genuinely the previous run's
    reading rather than the one about to be written.

    Two traps, both hit while building this. The read route is
    `/v1/metrics/series`; plain `/v1/metrics` is POST-only and answers a GET
    with 405. And the series sorts `ts ASC`, so `limit=1` would return the
    *oldest* point — the window plus `points[-1]` is what gets the newest one.

    Never raises, and with no `budget_left` does not even dial out. The digest
    is a nicety layered on top of the scrape; a metric must not go unrecorded,
    nor the cycle time out, because its history was slow or unreadable.

    `budget_left` is remaining time *spent on reads*, not wall-clock since the
    run began. The first version used an absolute deadline and the GitHub
    traffic scraper — ~30s on its own — burned the whole allowance before the
    reads had used any of it, so twelve of fourteen metrics reported
    "History unreadable" on a completely healthy server.
    """
    if budget_left is not None and budget_left <= 0:
        log.warning("prior lookup for %s skipped: read budget exhausted", name)
        return None, PRIOR_UNREADABLE

    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    since = (
        datetime.now(timezone.utc) - timedelta(days=PRIOR_WINDOW_DAYS)
    ).isoformat()
    try:
        resp = client.get(
            f"{base_url}/v1/metrics/series",
            headers=headers,
            params={"name": name, "since": since},
            timeout=min(PRIOR_READ_TIMEOUT_S, budget_left)
            if budget_left is not None
            else PRIOR_READ_TIMEOUT_S,
        )
        if resp.status_code >= 400:
            # Warn, don't shrug. A wrong route answers every metric the same
            # way an empty series does, which is how a 405 once survived a
            # full run looking like fourteen first readings.
            log.warning(
                "prior lookup for %s returned HTTP %s", name, resp.status_code
            )
            return None, PRIOR_UNREADABLE
        points = resp.json().get("points") or []
        if not points:
            return None, PRIOR_ABSENT
        return float(points[-1]["value"]), PRIOR_READ
    except Exception as exc:
        log.warning("prior lookup for %s failed: %s", name, exc)
        return None, PRIOR_UNREADABLE


def post_metric(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    name: str,
    value: float,
) -> None:
    """POST one `(name, value)` point. Raises on HTTP error."""
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    resp = client.post(
        f"{base_url}/v1/metrics",
        headers=headers,
        content=json.dumps({"name": name, "value": value}),
        timeout=10.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"POST /v1/metrics failed for {name}: "
            f"{resp.status_code} {resp.text[:200]}"
        )


def run(
    base_url: str,
    token: str | None,
    repo_root: Path,
    dry_run: bool = False,
) -> ScrapeReport:
    """Run every registered scraper, recording counts and what moved."""
    report = ScrapeReport()
    # Time spent inside fetch_prior only. Scraper wall-clock must not count
    # against it — see fetch_prior's docstring for what that cost last time.
    read_spent = 0.0

    with httpx.Client() as client:
        for name, scraper in sorted(SCRAPERS.items()):
            try:
                value = float(scraper(repo_root))
            except Exception as exc:
                report.failures += 1
                report.failed.append(name)
                log.warning("scraper %s failed: %s", name, exc)
                if not dry_run:
                    # Best-effort: the error metric may itself fail if the
                    # server is unreachable; swallow silently in that case,
                    # there's nothing useful to do with the inner error.
                    try:
                        post_metric(client, base_url, token, f"{name}.error", 1.0)
                    except Exception as inner:
                        log.warning("could not post error metric for %s: %s", name, inner)
                continue

            # Read the prior before writing, so the digest compares this run
            # against the last one rather than against itself.
            _read_started = time.monotonic()
            prior, prior_status = fetch_prior(
                client, base_url, token, name,
                budget_left=PRIOR_READ_BUDGET_S - read_spent,
            )
            read_spent += time.monotonic() - _read_started
            movement = Movement(
                name=name, value=value, prior=prior, prior_status=prior_status
            )

            if dry_run:
                log.info("DRY %s = %s", name, value)
                report.successes += 1
                report.movements.append(movement)
                continue

            try:
                post_metric(client, base_url, token, name, value)
                report.successes += 1
                report.movements.append(movement)
                log.info("recorded %s = %s", name, value)
            except Exception as exc:
                report.failures += 1
                report.failed.append(name)
                log.warning("could not post %s: %s", name, exc)

    log.info("chronicler done: success=%d fail=%d", report.successes, report.failures)
    return report


# Everything the digest prints is rounded to this, so movement is judged at
# the same precision. Raw float inequality classified `0.48136191` against
# `0.48136192` as movement and then rendered it `0.4814 -> 0.4814 (+0.0000)`:
# a metric flagged as moved whose before, after, and delta all say it did not.
# SQL `avg()` over a sliding 7-day window produces exactly that, daily.
DISPLAY_DP = 4


def _fmt(value: float) -> str:
    """Render a metric value. The series mixes counts with means, so whole
    numbers print as integers and everything else to DISPLAY_DP."""
    if math.isfinite(value) and value == int(value):
        return str(int(value))
    return f"{value:.{DISPLAY_DP}f}"


def _fmt_delta(delta: float) -> str:
    sign = "+" if delta >= 0 else "-"
    return f"{sign}{_fmt(abs(delta))}"


def visibly_moved(value: float, prior: float) -> bool:
    """Did the metric move by enough that the rendered digest will show it?"""
    return round(value, DISPLAY_DP) != round(prior, DISPLAY_DP)


def format_digest(report: ScrapeReport) -> tuple[str, str]:
    """Render a run as the (summary, details) of a KG entry.

    Pure — no server, no clock — so the wording is testable on its own.
    """
    read = [m for m in report.movements if m.prior_status == PRIOR_READ]
    moved = sorted(
        (m for m in read if visibly_moved(m.value, m.prior)),
        key=lambda m: m.name,
    )
    flat = sorted(m.name for m in read if not visibly_moved(m.value, m.prior))
    first = sorted(
        m.name for m in report.movements if m.prior_status == PRIOR_ABSENT
    )
    unreadable = sorted(
        m.name for m in report.movements if m.prior_status == PRIOR_UNREADABLE
    )

    total = report.successes + report.failures
    summary = (
        f"Chronicler daily: {report.successes}/{total} scrapers ok, "
        f"{len(moved)} moved"
    )

    sections: list[str] = []
    if moved:
        rows = "\n".join(
            f"  {m.name}: {_fmt(m.prior)} -> {_fmt(m.value)} "
            f"({_fmt_delta(m.value - m.prior)})"
            for m in moved
        )
        sections.append(f"Moved ({len(moved)}):\n{rows}")
    if flat:
        sections.append(f"Unchanged ({len(flat)}): {', '.join(flat)}")
    if first:
        # Distinguished from "unchanged" deliberately: no prior inside the
        # window is a different claim from a prior that matched, and an
        # operator reading a run after an outage needs to tell them apart.
        sections.append(
            f"No prior reading in {PRIOR_WINDOW_DAYS}d ({len(first)}): "
            f"{', '.join(first)}"
        )
    if unreadable:
        # Never fold these into "no prior reading". That sentence asserts the
        # series is empty; this one admits we do not know. A metric with
        # months of history must never be described as a first reading just
        # because the read failed or the budget ran out.
        sections.append(
            f"History unreadable ({len(unreadable)}): {', '.join(unreadable)}"
        )
    if report.failed:
        sections.append(
            f"Failed ({len(report.failed)}): {', '.join(sorted(report.failed))}"
        )
    if not sections:
        sections.append("No scrapers ran.")

    return summary, "\n\n".join(sections)


class ChroniclerAgent(GovernanceAgent):
    """GovernanceAgent wrapper that runs one scrape cycle and checks in.

    One-shot: launchd drives cadence, so this uses ``run_once()`` not
    ``run_forever()``. Identity is persistent; the anchor lives at
    ``~/.unitares/anchors/chronicler.json`` (the SDK default).
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        repo_root: Path,
        dry_run: bool = False,
    ):
        # Governance tools use the MCP endpoint; metrics POSTs hit the REST
        # endpoint. Derive the MCP URL from the same base so both aim at the
        # same server when UNITARES_METRICS_URL is overridden.
        mcp_url = base_url.rstrip("/") + "/mcp/"
        # Resolve log file: launchd plist owns stdout/stderr, but when run
        # manually we still want bounded logs. CHRONICLER_LOG_FILE env var
        # overrides; unset = no rotation (launchd handles it).
        log_file_env = os.environ.get("CHRONICLER_LOG_FILE", "").strip()
        log_file_path = Path(log_file_env) if log_file_env else None
        super().__init__(
            name="Chronicler",
            mcp_url=mcp_url,
            persistent=True,
            refuse_fresh_onboard=True,
            log_file=log_file_path,
            max_log_lines=10_000,
            cycle_timeout_seconds=120.0,
        )
        self.base_url = base_url
        self.token = token
        self.repo_root = repo_root
        self.dry_run = dry_run

    async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
        """Run one daily scrape cycle.

        Phase A advisory lease wraps the cycle so concurrent Chronicler
        invocations (rare — daily launchd, but operator manual + a stale
        --dry could overlap) surface in telemetry. Outcome does NOT gate
        execution per RFC v0.5 §6.1.
        """
        from unitares_sdk.lease_plane.advisory import lease_advisory_scope, new_holder_uuid

        # Migrated from "chronicler:scrape" → "resident:/chronicler_scrape" per RFC v0.8 §7.2.1.
        with lease_advisory_scope(
            surface_id="resident:/chronicler_scrape",
            holder_agent_uuid=new_holder_uuid(),
            ttl_s=120,
            intent="chronicler daily scrape",
        ):
            return await self._run_cycle_inner(client)

    async def _run_cycle_inner(self, client: GovernanceClient) -> CycleResult | None:
        # Scrapers are sync (subprocess + httpx.Client); push to a thread so
        # the MCP anyio task group isn't blocked by their blocking I/O.
        report = await asyncio.to_thread(
            run, self.base_url, self.token, self.repo_root, self.dry_run,
        )
        successes, failures = report.successes, report.failures

        if self.dry_run:
            # Dry run is a diagnostic — skip the check-in so we don't pollute
            # the trajectory with ad-hoc operator invocations.
            return None

        digest_status = await self._store_digest(client, report)

        total = successes + failures
        summary = f"Chronicler: {successes}/{total} scrapers ok"
        # Clean runs are routine + deterministic (low complexity, high
        # confidence); any failure bumps both dimensions to reflect the
        # transient-vs-persistent uncertainty.
        complexity = 0.4 if failures > 0 else 0.1
        confidence = 0.5 if failures > 0 else 0.9

        # A digest that fails forever must not read as a clean run. Reporting
        # "14/14 scrapers ok" at confidence 0.9 while the KG write has been
        # dead for months would reproduce, on the check-in channel, the exact
        # invisibility this whole feature exists to end — and the check-in is
        # the channel anyone actually watches. `disabled` is deliberate
        # operator intent and stays silent; only `failed` speaks up.
        if digest_status == DIGEST_FAILED:
            summary += "; KG digest FAILED"
            complexity = max(complexity, 0.4)
            confidence = min(confidence, 0.5)

        return CycleResult(
            summary=summary,
            complexity=complexity,
            confidence=confidence,
        )

    async def _store_digest(
        self, client: GovernanceClient, report: ScrapeReport
    ) -> str:
        """Write one KG entry naming what moved. Returns a DIGEST_* status.

        Three outcomes, not two: the caller must tell a deliberate opt-out
        from a broken write, because only the second belongs on the check-in.

        No search-before-write. The KG discipline asks for a search so a
        related entry gets corrected or superseded rather than duplicated,
        but a recurring snapshot has nothing to supersede — yesterday's
        digest was true yesterday and stays true. What closes it is the
        `ephemeral` tag: KnowledgeGraphLifecycle archives after seven days,
        retrievable, never deleted. Same posture as agents/triage_scribe.

        A failure here is logged and swallowed rather than raised: the metrics
        have already landed by this point, so losing the digest must not lose
        the scrape. It is reported on the check-in instead — swallowed is not
        the same as hidden.
        """
        if os.getenv("CHRONICLER_KG_DIGEST", "1").strip().lower() in (
            "0", "false", "no",
        ):
            return DIGEST_DISABLED

        summary, details = format_digest(report)
        try:
            await client.call_tool(
                "knowledge",
                {
                    "action": "store",
                    "summary": summary,
                    "details": details,
                    "discovery_type": "observation",
                    "tags": DIGEST_TAGS,
                },
            )
        except Exception as exc:
            log.warning("could not store KG digest: %s", exc)
            return DIGEST_FAILED
        log.info("stored KG digest: %s", summary)
        return DIGEST_STORED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chronicler metrics scraper.")
    parser.add_argument("--dry", action="store_true", help="print metrics without posting")
    args = parser.parse_args(argv)

    base_url = os.environ.get("UNITARES_METRICS_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("UNITARES_HTTP_API_TOKEN") or None
    repo_root = Path(os.environ.get("CHRONICLER_REPO_ROOT", os.getcwd())).resolve()

    log.info("chronicler start: url=%s repo=%s scrapers=%d", base_url, repo_root, len(SCRAPERS))
    # --dry is a diagnostic — skip the governance connect + identity dance
    # entirely so operators can debug scrapers without first bootstrapping
    # the Chronicler anchor (refuse_fresh_onboard would otherwise block).
    if args.dry:
        report = run(base_url, token, repo_root, dry_run=True)
        # Print the digest the live path would store, so an operator can see
        # the wording without writing to the KG.
        digest_summary, digest_details = format_digest(report)
        log.info("DRY digest: %s\n%s", digest_summary, digest_details)
        return 0 if report.failures == 0 else 1

    agent = ChroniclerAgent(
        base_url=base_url, token=token, repo_root=repo_root, dry_run=False,
    )
    try:
        asyncio.run(agent.run_once())
    except Exception as e:
        log.error("chronicler failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
