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
  * Each stalled PR is its own finding with its own lifetime. The check keeps
    an open set in Vigil's state (``stalled_pr_open``): a PR enters it the
    first cycle it is seen stalled and leaves it only when a direct read of
    that PR shows why it is no longer stalled (merged, closed, marked ready,
    CI green, or CI re-run). ``agent.py`` posts one ``vigil_finding`` when a PR
    enters and one ``vigil_resolution_finding`` when it leaves, so a second PR
    stalling while a first is already stalled is reported, and how long each
    stall lasted is on record. Before this, the check posted once on the
    aggregate healthy -> unhealthy edge and never again: ``fleet-ops#171`` sat
    red for 135h in November 2026 behind a single post about a different PR.
  * A PR that merely dropped out of the sweep (inspection cap, wall-clock
    budget, unreadable, ``gh`` down) is never treated as closed. It stays in
    the open set until it can be read.
  * The PR body's ``UNITARES agent:`` / ``Model:`` signature is read and
    carried on the finding as the owner. A PR without one is reported with no
    owner; nothing is guessed from the branch name or the GitHub login.
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
import re
import time
from typing import Any, List, Optional, Tuple

from .base import CheckResult

GH_BIN = os.environ.get("VIGIL_GH_BIN", "gh")
# Optional GitHub owner. An unconfigured check makes no network requests.
OWNER = os.environ.get("VIGIL_STALLED_PR_OWNER", "").strip()
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
# Closure checks: PRs in the open set that the sweep no longer reports stalled
# are each read directly. This runs after the sweep on its own small budget so
# a slow closure read cannot eat the sweep's time or Vigil's cycle.
MAX_RESOLVE = int(os.environ.get("VIGIL_STALLED_PR_MAX_RESOLVE", "5"))
RESOLVE_BUDGET = float(os.environ.get("VIGIL_STALLED_PR_RESOLVE_BUDGET", "8"))
# Bound on the open set itself, so a state file cannot grow without limit.
MAX_OPEN = int(os.environ.get("VIGIL_STALLED_PR_MAX_OPEN", "100"))

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


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_OWNER_UUID_RE = re.compile(rf"^UNITARES agent:[ \t]*({_UUID})[ \t]*$", re.MULTILINE)
_OWNER_MODEL_RE = re.compile(r"^Model:[ \t]*(\S[^\r\n]{0,80}?)[ \t]*$", re.MULTILINE)


def parse_owner(body: Any) -> dict:
    """Pure: the provenance signature of a PR body, as ``{agent_uuid, model}``.

    The fleet's convention ends an agent-authored PR body with ``Model:`` and
    ``UNITARES agent:`` lines. The last occurrence wins, because a body can
    quote an earlier signature above its own. Missing lines yield empty
    strings: an unsigned PR has no recorded owner, and none is inferred.
    """
    if not isinstance(body, str):
        return {"agent_uuid": "", "model": ""}
    uuids = _OWNER_UUID_RE.findall(body)
    models = _OWNER_MODEL_RE.findall(body)
    return {
        "agent_uuid": uuids[-1].lower() if uuids else "",
        "model": models[-1].strip() if models else "",
    }


def pr_key(repo: Any, number: Any) -> str:
    """Pure: the open-set key for one PR."""
    return f"{repo}#{number}"


def classify_closure(view: Any, *, now: float, stale_hours: float) -> Optional[str]:
    """Pure: why a previously stalled PR is no longer stalled, or None.

    ``view`` is a direct ``gh pr view`` of that PR. None means "still stalled,
    or cannot tell": the PR stays in the open set. Outcomes:

      * ``merged`` / ``closed``: the PR is gone.
      * ``ready``: no longer a draft, so the merge queue and its own gates now
        own it. This check only ever covered drafts.
      * ``green``: still a draft, and nothing is failing.
      * ``retriggered``: still a draft and failing, but the newest failing run
        completed inside the window, so someone or something re-ran CI. If it
        then sits red again it re-enters the open set as a new stall.
    """
    if not isinstance(view, dict):
        return None
    state = str(view.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if state != "OPEN":
        return None
    is_draft = view.get("isDraft")
    if is_draft is False:
        return "ready"
    if is_draft is not True:
        return None
    rollup = view.get("statusCheckRollup")
    if not isinstance(rollup, list):
        # A draft whose checks cannot be read is not known to be green.
        return None
    if not failing_nodes(rollup):
        return "green"
    since = red_since(rollup)
    if since is None:
        return None
    if (now - since) / 3600.0 <= stale_hours:
        return "retriggered"
    return None


def reconcile(
    prev_open: Any,
    stalled: List[dict],
    closures: dict,
    *,
    now: float,
    max_open: int,
) -> Tuple[dict, List[str], List[dict]]:
    """Pure: advance the open set by one cycle.

    ``closures`` maps an open-set key to a ``classify_closure`` outcome for the
    PRs that were read directly this cycle. Returns ``(open, opened, resolved)``:
    the new open set, the keys that entered it this cycle, and one row per PR
    that left it. A PR that is neither stalled now nor in ``closures`` is held
    unchanged, which is what keeps a truncated sweep from closing anything.
    """
    prev = prev_open if isinstance(prev_open, dict) else {}
    new_open: dict = {}
    opened: List[str] = []
    resolved: List[dict] = []

    for row in stalled:
        key = pr_key(row["repo"], row["number"])
        held = prev.get(key) if isinstance(prev.get(key), dict) else None
        entry = {
            "repo": row["repo"],
            "number": row["number"],
            "url": row.get("url", ""),
            "failing": list(row.get("failing") or []),
            "red_hours": row["red_hours"],
            "owner_agent_uuid": row.get("owner_agent_uuid", ""),
            "owner_model": row.get("owner_model", ""),
            "first_seen": held.get("first_seen", now) if held else now,
        }
        if held is None:
            opened.append(key)
        new_open[key] = entry

    for key, entry in prev.items():
        if key in new_open or not isinstance(entry, dict):
            continue
        outcome = closures.get(key)
        if outcome is None:
            new_open[key] = entry
            continue
        first_seen = entry.get("first_seen")
        open_hours = (
            round(max(0.0, now - first_seen) / 3600.0, 2)
            if isinstance(first_seen, (int, float))
            else None
        )
        resolved.append({**entry, "outcome": outcome, "open_hours": open_hours})

    if len(new_open) > max_open:
        # Keep the longest-open entries; a newly opened one that does not fit
        # is not announced, so it can be opened on a later cycle instead of
        # being announced and then forgotten.
        keep = sorted(new_open, key=lambda k: new_open[k].get("first_seen", now))[:max_open]
        dropped = set(new_open) - set(keep)
        new_open = {k: new_open[k] for k in keep}
        opened = [k for k in opened if k not in dropped]

    return new_open, opened, resolved


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
    open_set: Optional[dict] = None,
    opened: Optional[List[str]] = None,
    resolved: Optional[List[dict]] = None,
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
            # Lifetime tracking. `stalled_pr_open` is the state carried between
            # cycles; `opened` and `resolved` are this cycle's edges only.
            "stalled_pr_open": dict(open_set or {}),
            "stalled_pr_opened": list(opened or []),
            "stalled_pr_resolved": list(resolved or []),
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
                 "--json", "statusCheckRollup,body"],
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
            owner = parse_owner(view.get("body"))
            if red_h > stale_hours:
                stalled.append({
                    "repo": pr["repo"],
                    "number": pr["number"],
                    "red_hours": round(red_h, 2),
                    "failing": names,
                    "url": pr.get("url", ""),
                    "owner_agent_uuid": owner["agent_uuid"],
                    "owner_model": owner["model"],
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


async def read_closures(
    keys: List[str],
    prev_open: dict,
    *,
    now: float,
    stale_hours: float,
    timeout: float,
    budget: float,
    max_resolve: int,
) -> dict:
    """Read previously stalled PRs directly and say which ones closed.

    Returns ``{key: outcome}`` for the PRs that are verifiably no longer
    stalled. Anything that could not be read, was not reached inside the
    budget, or is still stalled is simply absent, and so stays open. Longest
    open first, so the cap never starves the oldest stall.
    """
    deadline = time.monotonic() + budget
    ordered = sorted(
        (k for k in keys if isinstance(prev_open.get(k), dict)),
        key=lambda k: prev_open[k].get("first_seen", now),
    )[: max(0, max_resolve)]
    out: dict = {}
    for key in ordered:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        entry = prev_open[key]
        try:
            view = await _gh_json(
                ["pr", "view", str(entry["number"]), "-R", str(entry["repo"]),
                 "--json", "state,isDraft,statusCheckRollup"],
                min(timeout, remaining),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        outcome = classify_closure(view, now=now, stale_hours=stale_hours)
        if outcome is not None:
            out[key] = outcome
    return out


class StalledDraftPR:
    name = "stalled_draft_pr"
    # Distinct from service-health keys so _collect_health_state gives this
    # check its own {svc}_healthy bookkeeping — that is what makes the finding
    # page once on the healthy -> unhealthy edge instead of every cycle. It is
    # also listed in agent.CONDITION_SERVICE_KEYS so detect_changes does not
    # read that flag as "GitHub is unreachable" and write outage notes.
    service_key = "github"
    # agent.py posts this check's findings per PR from `detail`, not from the
    # aggregate healthy -> unhealthy edge it uses for every other check.
    emits_own_findings = True

    async def run(self, prev_state: dict | None = None) -> CheckResult:
        # Re-read module-level config so test monkeypatching takes effect,
        # matching resident_tag_hygiene / plugin_hook_liveness.
        from . import stalled_draft_pr as _this

        prev_ok = bool((prev_state or {}).get("github_healthy", True))
        prev_open = (prev_state or {}).get("stalled_pr_open")
        if not isinstance(prev_open, dict):
            prev_open = {}
        common = dict(
            stale_hours=_this.STALE_HOURS,
            search_limit=_this.SEARCH_LIMIT,
            max_inspect=_this.MAX_INSPECT,
        )
        if not _this.OWNER:
            return _this.assess(
                [], drafts_read=0, cap_skipped=0, unreadable=0, deadline_skipped=0,
                search_truncated=False, indeterminate=True,
                indeterminate_reason="VIGIL_STALLED_PR_OWNER is not configured",
                prev_ok=prev_ok, open_set=prev_open, **common,
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
                open_set=prev_open, **common,
            )

        now = time.time()
        stalled_keys = {pr_key(r["repo"], r["number"]) for r in res["stalled"]}
        to_verify = [k for k in prev_open if k not in stalled_keys]
        closures: dict = {}
        if to_verify:
            try:
                closures = await _this.read_closures(
                    to_verify,
                    prev_open,
                    now=now,
                    stale_hours=_this.STALE_HOURS,
                    timeout=_this.GH_TIMEOUT,
                    budget=_this.RESOLVE_BUDGET,
                    max_resolve=_this.MAX_RESOLVE,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed closure pass closes nothing; the sweep's own result
                # still stands.
                closures = {}
        open_set, opened, resolved = _this.reconcile(
            prev_open, res["stalled"], closures, now=now, max_open=_this.MAX_OPEN,
        )

        return _this.assess(
            res["stalled"],
            open_set=open_set,
            opened=opened,
            resolved=resolved,
            drafts_read=res["drafts_read"],
            cap_skipped=res["cap_skipped"],
            unreadable=res["unreadable"],
            deadline_skipped=res["deadline_skipped"],
            search_truncated=res["search_truncated"],
            **common,
        )
