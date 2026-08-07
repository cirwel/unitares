"""Stalled draft-PR check — a canary for work that has no exit condition.

Background: this repo's delivery contract makes a draft PR the default for
*every* change (see ``CLAUDE.md`` — "draft PR for everything", a human
maintainer is the merge gate). That makes "is a draft" useless as a signal:
almost everything is one. Green-and-waiting is likewise the *designed* state,
not a fault — it means the change is parked on the human gate, exactly where
the contract wants it.

The state with no exit condition is the intersection: **draft + failing CI +
untouched**. Nothing will auto-merge it (drafts are excluded from auto-merge),
CI will not retry itself, and no existing surface nags about it. It simply
sits. On 2026-08-06 ``unitares#1511`` — a security fix on the Invariant 4
provenance filter — sat draft-and-red for ~23h and was surfaced only by a
third-party tool that happened to be watching the org. That is the gap this
check closes, inside the fleet rather than outside it.

Design notes:

  * **Two-stage, cheap filter first.** The org-wide draft list is one search
    call carrying ``updatedAt``; only PRs that are *already* past the stale
    window get the per-PR check-status call. Typical steady state inspects
    zero to a handful, so the check costs ~1 API call on a healthy fleet.
  * **Free by construction.** Everything goes through the ``gh`` CLI on the
    ambient ``GITHUB_TOKEN`` — no metered model API, per the repo's
    execution-cost policy.
  * **Indeterminate is not a failure.** A missing/unauthenticated ``gh``, a
    network error, or malformed output all return ok=True at ``info``. A
    check that cannot see the fleet must not manufacture a page.

Honest scope notes (no silent caps):

  * The search is bounded by ``VIGIL_STALLED_PR_LIMIT`` and the per-PR
    inspection by ``VIGIL_STALLED_PR_MAX_INSPECT``. When either bound bites,
    the summary says so explicitly and names the count that went unexamined —
    a truncated sweep must never read as a clean one.
  * Transition-emit means one page per healthy -> unhealthy edge, so a *second*
    PR going stalled while a first is already stalled does not re-page. The
    count is carried in ``detail`` and in the check summary, so the next cycle's
    state still reflects reality; only the notification is deduplicated.
  * "Failing" counts a check-run conclusion of failure/timed_out/cancelled or a
    failing commit status. Infra flakes (a runner that never picked the job up)
    are indistinguishable from real breakage here and will be reported — which
    is correct, since a flake that nobody re-runs strands the PR just as surely.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Iterable, List, Optional, Tuple

from .base import CheckResult

GH_BIN = os.environ.get("VIGIL_GH_BIN", "gh")
# Which GitHub owner's drafts to sweep. The fleet lives under one owner.
OWNER = os.environ.get("VIGIL_STALLED_PR_OWNER", "cirwel")
# A draft untouched for longer than this is a candidate. Wide enough that
# an in-progress change during a working session never trips it.
STALE_HOURS = float(os.environ.get("VIGIL_STALLED_PR_STALE_HOURS", "12"))
# Bound on the search sweep and on per-PR status calls. Both are reported
# when they bite.
SEARCH_LIMIT = int(os.environ.get("VIGIL_STALLED_PR_LIMIT", "60"))
MAX_INSPECT = int(os.environ.get("VIGIL_STALLED_PR_MAX_INSPECT", "15"))
GH_TIMEOUT = float(os.environ.get("VIGIL_STALLED_PR_GH_TIMEOUT", "20"))

# Check-run conclusions that mean "this PR is not going anywhere on its own".
FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}
FAILING_STATES = {"FAILURE", "ERROR"}


class GhUnavailable(Exception):
    """gh could not be run, or did not return usable JSON."""


async def _gh_json(args: List[str], timeout: float) -> Any:
    """Run `gh <args> and parse stdout as JSON. Raises GhUnavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            GH_BIN,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        raise GhUnavailable(f"cannot execute {GH_BIN!r}: {e}") from e

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise GhUnavailable(f"gh timed out after {timeout:.0f}s") from e

    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        raise GhUnavailable(detail[-1] if detail else f"gh exited {proc.returncode}")

    try:
        return json.loads((out or b"").decode("utf-8", "replace") or "[]")
    except json.JSONDecodeError as e:
        raise GhUnavailable(f"gh returned non-JSON: {e}") from e


def _parse_iso8601_utc(value: str) -> Optional[float]:
    """Parse GitHub's ISO-8601 Z timestamps to an epoch float, or None."""
    if not value:
        return None
    import datetime as _dt

    try:
        # Python's fromisoformat handles "+00:00" but historically not "Z".
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def select_stale(prs: Iterable[dict], now: float, stale_hours: float) -> List[dict]:
    """Pure: keep PRs whose last update is older than the stale window.

    Rows with an unparseable/absent ``updatedAt`` are skipped rather than
    treated as infinitely old — a parse failure must not invent a finding.
    Returns rows annotated with ``age_hours``, oldest first.
    """
    out: List[dict] = []
    for pr in prs:
        ts = _parse_iso8601_utc(pr.get("updatedAt") or "")
        if ts is None:
            continue
        age_h = (now - ts) / 3600.0
        if age_h > stale_hours:
            row = dict(pr)
            row["age_hours"] = round(age_h, 2)
            out.append(row)
    out.sort(key=lambda r: r["age_hours"], reverse=True)
    return out


def failing_check_names(rollup: Iterable[dict] | None) -> List[str]:
    """Pure: names of failing entries in a statusCheckRollup payload.

    Handles both node shapes GitHub returns — CheckRun (``conclusion``) and
    StatusContext (``state``). Anything still running is not failing.
    """
    names: List[str] = []
    for node in rollup or []:
        if not isinstance(node, dict):
            continue
        conclusion = (node.get("conclusion") or "").upper()
        state = (node.get("state") or "").upper()
        if conclusion in FAILING_CONCLUSIONS or state in FAILING_STATES:
            names.append(node.get("name") or node.get("context") or "?")
    return names


def assess(
    stalled: List[dict],
    *,
    stale_hours: float,
    candidates_examined: int,
    candidates_skipped: int,
    search_truncated: bool,
    search_limit: int,
    max_inspect: int,
) -> CheckResult:
    """Pure: turn the stalled set into a CheckResult.

    ok=False only when at least one draft is both stale and red. Bound-hit
    reporting is folded into the summary in both the ok and not-ok branches so
    a truncated sweep can never be mistaken for an exhaustive one.
    """
    caveats = []
    if search_truncated:
        caveats.append(
            f"search hit the {search_limit}-PR limit — older drafts went unexamined"
        )
    if candidates_skipped:
        caveats.append(
            f"{candidates_skipped} stale draft(s) not status-checked (cap {max_inspect})"
        )
    caveat_str = f" [{'; '.join(caveats)}]" if caveats else ""

    if not stalled:
        # Emit the full key set, not just the count: _collect_health_state
        # merges detail into the persisted state with dict.update(), so a key
        # omitted here would keep last cycle's value and leave a recovered
        # fleet still advertising stale PRs.
        return CheckResult(
            ok=True,
            summary=(
                f"No stalled draft PRs — {candidates_examined} draft(s) past "
                f"{stale_hours:.0f}h checked, all green or in progress{caveat_str}"
            ),
            detail={
                "stalled_pr_count": 0,
                "stalled_prs": [],
                "stalled_pr_search_truncated": search_truncated,
                "stalled_pr_unexamined": candidates_skipped,
            },
        )

    worst = stalled[0]
    failing = worst["failing"]
    shown = ", ".join(failing[:3])
    if len(failing) > 3:
        shown += f", +{len(failing) - 3} more"
    lead = (
        f"{worst['repo']}#{worst['number']} draft + red for "
        f"{worst['age_hours']:.0f}h ({shown})"
    )
    more = f" +{len(stalled) - 1} more" if len(stalled) > 1 else ""
    return CheckResult(
        ok=False,
        summary=(
            f"Stalled draft PR: {lead}{more}. Nothing will merge it — "
            f"re-run CI or push a fix{caveat_str}"
        ),
        detail={
            "stalled_pr_count": len(stalled),
            "stalled_prs": [
                {
                    "repo": r["repo"],
                    "number": r["number"],
                    "age_hours": r["age_hours"],
                    "failing": r["failing"],
                    "url": r.get("url", ""),
                }
                for r in stalled
            ],
            "stalled_pr_search_truncated": search_truncated,
            "stalled_pr_unexamined": candidates_skipped,
        },
        severity="warning",
        fingerprint_key="stalled_draft_pr",
    )


async def gather(
    owner: str,
    *,
    now: float,
    stale_hours: float,
    search_limit: int,
    max_inspect: int,
    timeout: float,
) -> Tuple[List[dict], int, int, bool]:
    """Fetch drafts, filter to stale, then status-check only those.

    Returns (stalled, candidates_examined, candidates_skipped, search_truncated).
    """
    prs = await _gh_json(
        [
            "search", "prs",
            "--owner", owner,
            "--state", "open",
            "--draft",
            "--limit", str(search_limit),
            "--json", "number,repository,title,updatedAt,url",
        ],
        timeout,
    )
    if not isinstance(prs, list):
        raise GhUnavailable("gh search prs did not return a list")

    search_truncated = len(prs) >= search_limit
    candidates = select_stale(prs, now, stale_hours)
    examined = candidates[:max_inspect]
    skipped = len(candidates) - len(examined)

    stalled: List[dict] = []
    for pr in examined:
        repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        number = pr.get("number")
        if not repo or number is None:
            continue
        try:
            view = await _gh_json(
                ["pr", "view", str(number), "-R", repo, "--json", "statusCheckRollup"],
                timeout,
            )
        except GhUnavailable:
            # One unreadable PR must not blind the whole sweep; it simply
            # cannot be judged, and is counted as unexamined.
            skipped += 1
            continue
        failing = failing_check_names((view or {}).get("statusCheckRollup"))
        if failing:
            stalled.append(
                {
                    "repo": repo,
                    "number": number,
                    "age_hours": pr["age_hours"],
                    "failing": failing,
                    "url": pr.get("url", ""),
                }
            )
    return stalled, len(examined), skipped, search_truncated


class StalledDraftPR:
    name = "stalled_draft_pr"
    # Distinct from "governance"/"lumen" so _collect_health_state gives this
    # check its own {svc}_healthy bookkeeping — that is what makes the finding
    # page once on the healthy -> unhealthy edge instead of every cycle.
    service_key = "github"

    async def run(self) -> CheckResult:
        # Re-read module-level config so test monkeypatching takes effect,
        # matching resident_tag_hygiene / plugin_hook_liveness.
        from . import stalled_draft_pr as _this
        import time

        try:
            stalled, examined, skipped, truncated = await _this.gather(
                _this.OWNER,
                now=time.time(),
                stale_hours=_this.STALE_HOURS,
                search_limit=_this.SEARCH_LIMIT,
                max_inspect=_this.MAX_INSPECT,
                timeout=_this.GH_TIMEOUT,
            )
        except GhUnavailable as e:
            return CheckResult(
                ok=True,
                summary=f"Stalled draft-PR check: indeterminate ({e})",
                severity="info",
                fingerprint_key="stalled_draft_pr_indeterminate",
            )

        return _this.assess(
            stalled,
            stale_hours=_this.STALE_HOURS,
            candidates_examined=examined,
            candidates_skipped=skipped,
            search_truncated=truncated,
            search_limit=_this.SEARCH_LIMIT,
            max_inspect=_this.MAX_INSPECT,
        )
