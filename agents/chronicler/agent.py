#!/usr/bin/env python3
"""Chronicler — daily scraper of fleet metrics into `metrics.series`.

One-shot invocation (launchd drives cadence). Runs each scraper in
`scrapers.py`, POSTs the value to the governance server, emits a
`.error` metric on failure so silent breakage stays visible. After the
scrape loop, checks in to governance via `process_agent_update` so
Chronicler appears as a first-class resident with its own EISV
trajectory alongside Vigil/Sentinel/Watcher.

Environment:
    UNITARES_METRICS_URL        base URL (default http://127.0.0.1:8767)
    UNITARES_HTTP_API_TOKEN     bearer token; optional if running locally
                                (trusted-network bypass handles 127.0.0.1)
    CHRONICLER_REPO_ROOT        repo to scrape (default: working directory)
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
import os
import sys
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
) -> tuple[int, int]:
    """Run every registered scraper. Returns (successes, failures)."""
    successes = 0
    failures = 0

    with httpx.Client() as client:
        for name, scraper in sorted(SCRAPERS.items()):
            try:
                value = float(scraper(repo_root))
            except Exception as exc:
                failures += 1
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

            if dry_run:
                log.info("DRY %s = %s", name, value)
                successes += 1
                continue

            try:
                post_metric(client, base_url, token, name, value)
                successes += 1
                log.info("recorded %s = %s", name, value)
            except Exception as exc:
                failures += 1
                log.warning("could not post %s: %s", name, exc)

    log.info("chronicler done: success=%d fail=%d", successes, failures)
    return successes, failures


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
        from src.lease_plane.advisory import lease_advisory_scope, new_holder_uuid

        # Migrated from "chronicler:scrape" → "resident:/chronicler_scrape" per RFC v0.8 §7.2.1.
        with lease_advisory_scope(
            surface_id="resident:/chronicler_scrape",
            holder_agent_uuid=new_holder_uuid(),
            ttl_s=120,
            intent="chronicler daily scrape",
        ):
            return await self._run_cycle_inner()

    async def _run_cycle_inner(self) -> CycleResult | None:
        # Scrapers are sync (subprocess + httpx.Client); push to a thread so
        # the MCP anyio task group isn't blocked by their blocking I/O.
        successes, failures = await asyncio.to_thread(
            run, self.base_url, self.token, self.repo_root, self.dry_run,
        )

        if self.dry_run:
            # Dry run is a diagnostic — skip the check-in so we don't pollute
            # the trajectory with ad-hoc operator invocations.
            return None

        total = successes + failures
        summary = f"Chronicler: {successes}/{total} scrapers ok"
        # Clean runs are routine + deterministic (low complexity, high
        # confidence); any failure bumps both dimensions to reflect the
        # transient-vs-persistent uncertainty.
        complexity = 0.4 if failures > 0 else 0.1
        confidence = 0.5 if failures > 0 else 0.9
        return CycleResult(
            summary=summary,
            complexity=complexity,
            confidence=confidence,
        )


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
        _, failures = run(base_url, token, repo_root, dry_run=True)
        return 0 if failures == 0 else 1

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
