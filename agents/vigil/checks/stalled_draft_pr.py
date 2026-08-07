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

The clock is ``completedAt`` of the newest *failing* check-run, not the PR's
``updatedAt``. That distinction is the whole correctness story. ``updatedAt``
advances on any activity, and this fleet's PRs carry a doc-validation bot that
comments once per workflow run — on ``#1511``, nine of twelve comments were
``github-actions``. Keying off ``updatedAt`` would make the metric
*anti-correlated* with the condition: the more CI churns red, the more bot
comments, the further the stall clock is pushed back. Worse, a human who
comments "known, fixing tomorrow" would silence the check on precisely the PR
that was acknowledged and not fixed. "How long has CI been red with nothing
done about it" is the question, and the failing run's own completion time is
the only field that answers it.

Design notes:

  * **Bounded by wall-clock, not by call count.** Vigil's whole cycle has a
    120s budget (``CYCLE_TIMEOUT``), and a sibling step already reserves 60s of
    it. A check that blows the cycle takes down the state write, the check-in,
    and every other check's result — the check would silence the agent it runs
    inside. So the sweep carries an aggregate deadline and runs the per-PR
    calls concurrently; whatever does not finish in budget is reported as
    unexamined rather than waited on.
  * **Free by construction.** Everything goes through the ``gh`` CLI on
    whatever credentials ``gh`` already holds — no metered model API, per the
    repo's execution-cost policy.
  * **Indeterminate is not a failure, and must not be a recovery either.** A
    missing/unauthenticated ``gh`` returns the *previous* verdict rather than
    ``ok=True``. Returning healthy would flip the transition edge, fabricate a
    "recovered" note, and re-page the same unchanged PR on the next cycle.

Honest scope notes (no silent caps):

  * Every open draft costs one status call — there is no cheap pre-filter,
    because the ``updatedAt`` field that would provide one is the very field
    established above as untrustworthy. The search is ordered oldest-touched
    first so that when a bound bites, the drops are the freshest PRs.
  * Three distinct bounds are counted and reported separately: the search
    limit, the per-cycle inspection cap, and the wall-clock deadline. A PR that
    could not be read is never counted as read.
  * Transition-emit means one page per healthy -> unhealthy edge, so a *second*
    PR going stalled while a first is already stalled does not re-page. The
    count stays live in ``detail`` and in the summary; only the notification is
    deduplicated.
  * ``service_key`` is listed in ``agent.CONDITION_SERVICE_KEYS`` so the
    ``{svc}_healthy`` flag drives transition dedup *without* being mistaken for
    a reachable service. Without that, ``detect_changes`` would write
    "Github is down" / "Github unreachable for N cycles" outage notes into the
    shared KG every third cycle for as long as one PR stayed stalled.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, List, Optional, Tuple

from .base import CheckResult

GH_BIN = os.environ.get("VIGIL_GH_BIN", "gh")
# Which GitHub owner's drafts to sweep. The fleet lives under one owner.
OWNER = os.environ.get("VIGIL_STALLED_PR_OWNER", "cirwel")
# CI red for longer than this, with nothing done about it, is a stall.
STALE_HOURS = float(os.environ.get("VIGIL_STALLED_PR_STALE_HOURS", "12"))
# Bound on the search sweep and on per-PR status calls.
SEARCH_LIMIT = int(os.environ.get("VIGIL_STALLED_PR_LIMIT", "60"))
MAX_INSPECT = int(os.environ.get("VIGIL_STALLED_PR_MAX_INSPECT", "15"))
# Per-call timeout, and the aggregate wall-clock budget for the whole sweep.
# BUDGET is the load-bearing one: it is what keeps this check from eating
# Vigil's 120s cycle no matter how badly gh degrades. GH_TIMEOUT is now only a
# per-call ceiling — every call is additionally clamped to the budget remaining
# at the moment it starts, so this value cannot extend the sweep.
GH_TIMEOUT = float(os.environ.get("VIGIL_STALLED_PR_GH_TIMEOUT", "20"))
BUDGET = float(os.environ.get("VIGIL_STALLED_PR_BUDGET", "25"))
CONCURRENCY = int(os.environ.get("VIGIL_STALLED_PR_CONCURRENCY", "4"))

# Check-run conclusions that mean "this PR is not going anywhere on its own".
FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}
FAILING_STATES = {"FAILURE", "ERROR"}


class GhUnavailable(Exception):
    """gh could not be run, or did not return usable JSON."""


async def _gh_json(args: List[str], timeout: float) -> Any:
    """Run `gh <args>` and parse stdout as JSON. Raises GhUnavailable.

    start_new_session puts the child in its own process group so the kill path
    can take out grandchildren too — ``proc.kill()`` alone signals only the
    direct child, and a ``gh`` replaced by a wrapper script would strand them.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            GH_BIN,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as e:
        raise GhUnavailable(f"cannot execute {GH_BIN!r}: {e}") from e

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        # CancelledError is a BaseException and is NOT caught by `except
        # TimeoutError`. Catching it explicitly is what stops an outer deadline
        # (or Vigil's own cycle timeout) from orphaning a live gh process on
        # every aborted cycle.
        _terminate(proc)
        try:
            await proc.wait()  # reap, and close the transports
        except Exception:
            pass
        if isinstance(e, asyncio.CancelledError):
            raise
        raise GhUnavailable(f"gh timed out after {timeout:.0f}s") from e

    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        raise GhUnavailable(detail[-1] if detail else f"gh exited {proc.returncode}")

    raw = (out or b"").decode("utf-8", "replace").strip()
    if not raw:
        # Empty stdout with a zero exit is not "no results" — it is gh not
        # answering. Defaulting it to [] would read as "no drafts exist".
        raise GhUnavailable("gh returned empty output")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise GhUnavailable(f"gh returned non-JSON: {e}") from e


def _terminate(proc: Any) -> None:
    """Kill the child's whole process group, falling back to the child alone."""
    try:
        os.killpg(os.getpgid(proc.pid), 9)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _parse_iso8601_utc(value: Any) -> Optional[float]:
    """Parse GitHub's ISO-8601 Z timestamps to an epoch float, or None.

    A timezone-naive string is pinned to UTC rather than left to
    ``datetime.timestamp()``, which would silently interpret it in the
    machine's local zone — an hours-scale error on this fleet's Denver clock.
    """
    if not value or not isinstance(value, str):
        return None
    import datetime as _dt

    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    try:
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def failing_nodes(rollup: Any) -> List[dict]:
    """Pure: the failing entries of a statusCheckRollup payload.

    Handles both node shapes GitHub returns — CheckRun (``conclusion``) and
    StatusContext (``state``). Anything still running is not failing. A rollup
    that is not a list yields nothing rather than being iterated as a mapping,
    which would silently read a red PR as green.
    """
    if not isinstance(rollup, list):
        return []
    out: List[dict] = []
    for node in rollup:
        if not isinstance(node, dict):
            continue
        conclusion = str(node.get("conclusion") or "").upper()
        state = str(node.get("state") or "").upper()
        if conclusion in FAILING_CONCLUSIONS or state in FAILING_STATES:
            out.append(node)
    return out


def failing_check_names(rollup: Any) -> List[str]:
    """Pure: names of failing entries in a statusCheckRollup payload."""
    return [n.get("name") or n.get("context") or "?" for n in failing_nodes(rollup)]


def red_since(rollup: Any) -> Optional[float]:
    """Pure: epoch of the most recent failing run's completion, or None.

    This is the stall clock. Newest failing completion is the right anchor: if
    CI last went red at T and nothing has happened since, the PR has been red
    and untouched since T.
    """
    stamps = [
        ts
        for n in failing_nodes(rollup)
        for ts in (_parse_iso8601_utc(n.get("completedAt")),)
        if ts is not None
    ]
    return max(stamps) if stamps else None


def assess(
    stalled: List[dict],
    *,
    stale_hours: float,
    drafts_read: int,
    cap_skipped: int,
    unreadable: int,
    deadline_skipped: int,
    search_truncated: bool,
    search_limit: int,
    max_inspect: int,
    indeterminate: bool = False,
    indeterminate_reason: str = "",
    prev_ok: bool = True,
) -> CheckResult:
    """Pure: turn the sweep into a CheckResult.

    ok=False only when at least one draft has been red past the window. Bound
    hits are reported separately by cause, in both branches, so a truncated
    sweep can never be mistaken for an exhaustive one.
    """
    caveats = []
    if search_truncated:
        caveats.append(
            f"search hit the {search_limit}-PR limit — freshest drafts dropped first"
        )
    if cap_skipped:
        caveats.append(f"{cap_skipped} draft(s) beyond the {max_inspect}-PR inspection cap")
    if unreadable:
        caveats.append(f"{unreadable} draft(s) could not be status-checked (gh error)")
    if deadline_skipped:
        caveats.append(f"{deadline_skipped} draft(s) dropped at the wall-clock budget")
    caveat_str = f" [{'; '.join(caveats)}]" if caveats else ""

    # Every branch emits the same key set. _collect_health_state merges detail
    # into persisted state with dict.update(), so a key omitted on one path
    # would keep another path's value and leave the state file lying.
    def _detail(rows: List[dict]) -> dict:
        return {
            "stalled_pr_count": len(rows),
            "stalled_prs": [
                {
                    "repo": r["repo"],
                    "number": r["number"],
                    "red_hours": r["red_hours"],
                    "failing": r["failing"],
                    "url": r.get("url", ""),
                }
                for r in rows
            ],
            "stalled_pr_search_truncated": search_truncated,
            "stalled_pr_unexamined": cap_skipped + unreadable + deadline_skipped,
            "stalled_pr_indeterminate": indeterminate,
        }

    if indeterminate:
        # Hold the previous verdict. Asserting ok=True here would flip the
        # transition edge, emit a false "recovered", and re-page the same
        # unchanged PR next cycle on any transient gh flake.
        return CheckResult(
            ok=prev_ok,
            summary=f"Stalled draft-PR check: indeterminate ({indeterminate_reason})",
            detail=_detail([]),
            severity="info",
            fingerprint_key="",
        )

    if not stalled:
        return CheckResult(
            ok=True,
            summary=(
                f"No stalled draft PRs — {drafts_read} draft(s) status-checked, "
                f"none red past {stale_hours:.0f}h{caveat_str}"
            ),
            detail=_detail([]),
        )

    worst = stalled[0]
    failing = worst["failing"]
    shown = ", ".join(failing[:3])
    if len(failing) > 3:
        shown += f", +{len(failing) - 3} more"
    more = f" +{len(stalled) - 1} more" if len(stalled) > 1 else ""
    return CheckResult(
        ok=False,
        summary=(
            f"Stalled draft PR: {worst['repo']}#{worst['number']} red for "
            f"{worst['red_hours']:.0f}h with no new run ({shown}){more}. "
            f"Nothing will merge it — re-run CI or push a fix{caveat_str}"
        ),
        detail=_detail(stalled),
        severity="warning",
        fingerprint_key="stalled_draft_pr",
    )


def _search_args(owner: str, search_limit: int) -> List[str]:
    # --sort updated --order asc is load-bearing: gh defaults to best-match
    # relevance ordering, which would make the --limit cut an arbitrary subset
    # and could drop the very PR this check exists to find.
    return [
        "search", "prs",
        "--owner", owner,
        "--state", "open",
        "--draft",
        "--sort", "updated",
        "--order", "asc",
        "--limit", str(search_limit),
        "--json", "number,repository,title,updatedAt,url",
    ]


def _valid_rows(prs: Any) -> Tuple[List[dict], int]:
    """Pure: keep well-formed search rows. Returns (rows, dropped_count).

    Malformed rows are counted, never silently discarded.
    """
    if not isinstance(prs, list):
        raise GhUnavailable("gh search prs did not return a list")
    rows, dropped = [], 0
    for pr in prs:
        if not isinstance(pr, dict):
            dropped += 1
            continue
        repo = pr.get("repository")
        repo_name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        if not repo_name or pr.get("number") is None:
            dropped += 1
            continue
        rows.append({**pr, "repo": repo_name})
    return rows, dropped


async def gather(
    owner: str,
    *,
    now: float,
    stale_hours: float,
    search_limit: int,
    max_inspect: int,
    timeout: float,
    budget: float,
    concurrency: int,
) -> dict:
    """Sweep drafts and status-check them under an aggregate wall-clock budget.

    Every open draft costs one call — see the module docstring on why there is
    no cheap pre-filter. Returns a dict of results and per-cause bound counts.
    """
    deadline = time.monotonic() + budget
    prs = await _gh_json(_search_args(owner, search_limit), min(timeout, budget))
    rows, malformed = _valid_rows(prs)

    search_truncated = isinstance(prs, list) and len(prs) >= search_limit
    inspect = rows[:max_inspect]
    cap_skipped = len(rows) - len(inspect)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(pr: dict) -> Tuple[dict, Optional[dict]]:
        async with sem:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            view = await _gh_json(
                ["pr", "view", str(pr["number"]), "-R", pr["repo"],
                 "--json", "statusCheckRollup"],
                min(timeout, remaining),
            )
            if not isinstance(view, dict):
                raise GhUnavailable("gh pr view did not return an object")
            return pr, view

    tasks = [asyncio.ensure_future(one(pr)) for pr in inspect]
    stalled: List[dict] = []
    read_ok = unreadable = 0
    if tasks:
        done, pending = await asyncio.wait(
            tasks, timeout=max(0.0, deadline - time.monotonic())
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            try:
                pr, view = t.result()
            except Exception:
                unreadable += 1
                continue
            read_ok += 1
            rollup = view.get("statusCheckRollup")
            names = failing_check_names(rollup)
            if not names:
                continue
            since = red_since(rollup) or _parse_iso8601_utc(pr.get("updatedAt"))
            if since is None:
                unreadable += 1
                continue
            red_h = (now - since) / 3600.0
            if red_h > stale_hours:
                stalled.append({
                    "repo": pr["repo"],
                    "number": pr["number"],
                    "red_hours": round(red_h, 2),
                    "failing": names,
                    "url": pr.get("url", ""),
                })
        deadline_skipped = len(pending)
    else:
        deadline_skipped = 0

    stalled.sort(key=lambda r: r["red_hours"], reverse=True)
    return {
        "stalled": stalled,
        "drafts_read": read_ok,
        "cap_skipped": cap_skipped,
        "unreadable": unreadable + malformed,
        "deadline_skipped": deadline_skipped,
        "search_truncated": search_truncated,
    }


class StalledDraftPR:
    name = "stalled_draft_pr"
    # Distinct from "governance"/"lumen" so _collect_health_state gives this
    # check its own {svc}_healthy bookkeeping — that is what makes the finding
    # page once on the healthy -> unhealthy edge instead of every cycle. It is
    # also listed in agent.CONDITION_SERVICE_KEYS so detect_changes does not
    # read that flag as "GitHub is unreachable" and write outage notes.
    service_key = "github"

    async def run(self, prev_state: dict | None = None) -> CheckResult:
        # Re-read module-level config so test monkeypatching takes effect,
        # matching resident_tag_hygiene / plugin_hook_liveness.
        from . import stalled_draft_pr as _this

        prev_ok = bool((prev_state or {}).get("github_healthy", True))
        common = dict(
            stale_hours=_this.STALE_HOURS,
            search_limit=_this.SEARCH_LIMIT,
            max_inspect=_this.MAX_INSPECT,
        )
        try:
            res = await _this.gather(
                _this.OWNER,
                now=time.time(),
                timeout=_this.GH_TIMEOUT,
                budget=_this.BUDGET,
                concurrency=_this.CONCURRENCY,
                **common,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Deliberately broad. The alternative is that any unexpected shape
            # escapes to runner.py, which converts a bare exception into a
            # `critical` page — the exact inverse of this module's contract
            # that a check unable to see the fleet stays quiet.
            return _this.assess(
                [], drafts_read=0, cap_skipped=0, unreadable=0, deadline_skipped=0,
                search_truncated=False, indeterminate=True,
                indeterminate_reason=f"{type(e).__name__}: {e}", prev_ok=prev_ok,
                **common,
            )

        return _this.assess(
            res["stalled"],
            drafts_read=res["drafts_read"],
            cap_skipped=res["cap_skipped"],
            unreadable=res["unreadable"],
            deadline_skipped=res["deadline_skipped"],
            search_truncated=res["search_truncated"],
            **common,
        )
