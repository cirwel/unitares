"""Read agent-signed pull requests from GitHub and report what happened to them.

    python3 -m agents.github_observer --repo OWNER/NAME --dry-run

The dry run prints one JSON event per line on stdout, exactly the object a
write would store, and a one-line summary on stderr. It writes nothing: not to
UNITARES, and not to the fingerprint store. Writing waits on the route the
operator chose for it (see the package docstring).

GitHub is read with GET requests only, using ``GITHUB_TOKEN`` (or
``GH_TOKEN``). The token needs to read the repository and nothing else.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from agents.github_observer.events import (
    DEFAULT_SKIPPED_CHECKS,
    EVENT_TYPES,
    agent_uuid_from_body_html,
    ci_events,
    ci_fingerprint,
    pr_state_events,
    review_events,
    run_belongs_to_pr,
    trusted_pr_author,
)
from agents.github_observer.store import read_only_fingerprints

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_SINCE_DAYS = 7
MAX_PAGES = 10
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    """A GitHub read failed. Carries no token and no response body."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would resend the token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DEFAULT_OPENER = urllib.request.build_opener(_NoRedirect).open


class GitHubClient:
    """Read-only GitHub REST client. It has no method that is not a GET."""

    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API_URL,
        opener: Callable[..., Any] = _DEFAULT_OPENER,
    ):
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._opener = opener

    def _request(self, url: str) -> tuple[Any, str | None]:
        if not url.startswith(self._api_url + "/"):
            raise GitHubError("refusing to follow a link outside the configured GitHub API")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                # ``full`` adds ``body_html``: GitHub's own rendering of a body,
                # which is what the footer is read from.
                "Accept": "application/vnd.github.full+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "unitares-github-observer",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
                link = response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"GitHub returned HTTP {exc.code} for a read") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # URLError and TimeoutError are OSErrors; a body cut off mid-read
            # is an HTTPException or a ConnectionError.
            raise GitHubError(f"GitHub read failed: {type(exc).__name__}") from None
        match = _NEXT_LINK_RE.search(link or "")
        return body, match.group(1) if match else None

    def get_all(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        key: str | None = None,
        stop: Callable[[list], bool] | None = None,
    ):
        """Every item of a paged list.

        ``stop`` ends paging early once a page satisfies it, for a list sorted
        so that everything after that page is out of range. A list still
        unfinished after ``MAX_PAGES`` pages of 100 is an error, never a short
        answer: the missing tail would hold the newest facts.
        """
        query = urllib.parse.urlencode({"per_page": 100, **(params or {})})
        url: str | None = f"{self._api_url}{path}?{query}"
        items: list[Any] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            body, url = self._request(url)
            page = body.get(key, []) if key and isinstance(body, dict) else body
            if not isinstance(page, list):
                raise GitHubError("GitHub returned an unexpected shape for a list read")
            items.extend(page)
            if stop is not None and stop(page):
                return items
        if url is not None:
            raise GitHubError(f"a GitHub list ran past {MAX_PAGES} pages; refusing a partial read")
        return items

    def get_one(self, path: str) -> Any:
        body, _ = self._request(f"{self._api_url}{path}")
        return body


class Reader(Protocol):
    """What the observer needs to read. ``GitHubReader`` is the live one."""

    def pulls(self, repo: str, since: datetime) -> list[dict]: ...
    def timeline(self, repo: str, number: int) -> list[dict]: ...
    def comments(self, repo: str, number: int) -> list[dict]: ...
    def commit_shas(self, repo: str, number: int) -> list[str]: ...
    def workflow_runs(self, repo: str, branch: str) -> list[dict]: ...
    def run_attempt(self, repo: str, run_id: int, attempt: int) -> dict: ...
    def check_runs(self, repo: str, sha: str) -> list[dict]: ...
    def failed_jobs(self, repo: str, run_id: int, attempt: int) -> list[str]: ...


class GitHubReader:
    def __init__(self, client: GitHubClient):
        self._client = client

    def pulls(self, repo: str, since: datetime) -> list[dict]:
        """Open pull requests, and closed ones updated since ``since``."""
        floor = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        def older(pr: Mapping[str, Any]) -> bool:
            return str(pr.get("updated_at") or "") < floor

        found = self._client.get_all(f"/repos/{repo}/pulls", {"state": "open"})
        closed = self._client.get_all(
            f"/repos/{repo}/pulls",
            {"state": "closed", "sort": "updated", "direction": "desc"},
            stop=lambda page: bool(page) and older(page[-1]),
        )
        found.extend(pr for pr in closed if not older(pr))
        return found

    def timeline(self, repo: str, number: int) -> list[dict]:
        return self._client.get_all(f"/repos/{repo}/issues/{number}/timeline")

    def comments(self, repo: str, number: int) -> list[dict]:
        return self._client.get_all(f"/repos/{repo}/issues/{number}/comments")

    def commit_shas(self, repo: str, number: int) -> list[str]:
        commits = self._client.get_all(f"/repos/{repo}/pulls/{number}/commits")
        return [str(c["sha"]) for c in commits if c.get("sha")]

    def workflow_runs(self, repo: str, branch: str) -> list[dict]:
        return self._client.get_all(
            f"/repos/{repo}/actions/runs", {"branch": branch}, key="workflow_runs"
        )

    def run_attempt(self, repo: str, run_id: int, attempt: int) -> dict:
        run = self._client.get_one(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
        if not isinstance(run, dict):
            raise GitHubError("GitHub returned an unexpected shape for a run attempt")
        return run

    def check_runs(self, repo: str, sha: str) -> list[dict]:
        # GitHub's default (filter=latest) returns only the newest run of each
        # check name, which would hide an earlier failure that a re-run passed.
        return self._client.get_all(
            f"/repos/{repo}/commits/{sha}/check-runs", {"filter": "all"}, key="check_runs"
        )

    def failed_jobs(self, repo: str, run_id: int, attempt: int) -> list[str]:
        jobs = self._client.get_all(
            f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs"
        )
        return [str(j.get("name")) for j in jobs if j.get("conclusion") == "failure"]


def collect_pr_events(
    reader: Reader,
    repo: str,
    pr: Mapping[str, Any],
    agent_uuid: str,
    *,
    trusted_apps: Mapping[int, int],
    skipped_checks: Iterable[str] = DEFAULT_SKIPPED_CHECKS,
    seen: Callable[[str], bool] = lambda fingerprint: False,
) -> tuple[list[dict], int]:
    """Every event for one signed pull request, oldest first.

    ``seen`` keeps the extra reads (an earlier attempt of a re-run, the failed
    jobs of a failed attempt) to facts not yet written.
    """
    number = int(pr["number"])
    head = pr.get("head") or {}
    shas = {*reader.commit_shas(repo, number), *filter(None, [head.get("sha")])}
    listed = reader.workflow_runs(repo, str(head.get("ref") or "")) if head.get("ref") else []
    runs: list[Mapping[str, Any]] = []
    for run in listed:
        if run.get("id") is None or not run_belongs_to_pr(run, pr, shas):
            continue
        runs.append(run)
        # The run list carries only each run's latest attempt. An earlier
        # attempt that failed before a re-run passed is the "caught by CI"
        # fact, so read each one not yet written.
        for earlier in range(1, int(run.get("run_attempt") or 1)):
            if not seen(ci_fingerprint(pr, run["id"], earlier)):
                runs.append(reader.run_attempt(repo, int(run["id"]), earlier))
    # A run kept on its association alone ran on a head the PR no longer
    # lists; its commit was this PR's, so its earlier attempts count too.
    shas.update(str(run["head_sha"]) for run in runs if run.get("head_sha"))
    failed: dict[tuple[int, int], list[str]] = {}
    for run in runs:
        if run.get("status") != "completed" or run.get("conclusion") != "failure":
            continue
        key = (int(run["id"]), int(run.get("run_attempt") or 1))
        if not seen(ci_fingerprint(pr, *key)):
            failed[key] = reader.failed_jobs(repo, *key)
    checks = reader.check_runs(repo, str(head["sha"])) if head.get("sha") else []
    reviews, untrusted = review_events(
        repo, pr, reader.comments(repo, number), agent_uuid, trusted_apps=trusted_apps
    )
    events = [
        *pr_state_events(repo, pr, reader.timeline(repo, number), agent_uuid),
        *ci_events(
            repo,
            pr,
            runs,
            checks,
            agent_uuid,
            pr_commit_shas=shas,
            failed_jobs=failed,
            skipped_checks=skipped_checks,
        ),
        *reviews,
    ]
    events.sort(key=lambda e: (str(e.get("observed_at") or ""), e["fingerprint"]))
    return events, untrusted


class Writer(Protocol):
    def write(self, event: dict) -> bool: ...


class DryRunWriter:
    """Prints the object a write would store, one JSON line per event."""

    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, event: dict) -> bool:
        self._stream.write(json.dumps(event, sort_keys=True) + "\n")
        return True


COUNTS = (
    "repos_failed",
    "prs_read",
    "prs_unsigned",
    "prs_untrusted_author",
    "prs_failed",
    "events_new",
    "events_known",
    "markers_untrusted",
    "writes_failed",
)
# What one unreadable or malformed pull request can raise. It is counted and
# the poll goes on; MalformedPullRequest is a ValueError.
_PR_ERRORS = (GitHubError, KeyError, TypeError, ValueError, AttributeError)


def run_cycle(
    reader: Reader,
    repos: Iterable[str],
    writer: Writer,
    *,
    seen: Callable[[str], bool],
    mark: Callable[[str, str], Any],
    since: datetime,
    trusted_apps: Mapping[int, int],
    skipped_checks: Iterable[str] = DEFAULT_SKIPPED_CHECKS,
) -> dict[str, int]:
    """One poll. Returns counts.

    A repository or pull request that cannot be read is counted, never guessed
    at, and does not stop the others. A pull request with no footer, or whose
    footer was not written by someone the deployment trusts, is counted and
    skipped: anyone can type another agent's uuid into a pull request body.
    """
    counts = dict.fromkeys(COUNTS, 0)
    this_cycle: set[str] = set()
    for repo in repos:
        try:
            pulls = reader.pulls(repo, since)
        except GitHubError:
            counts["repos_failed"] += 1
            continue
        for pr in pulls:
            try:
                # A missing ``body_html`` (the media type was not honoured) is
                # a failed read, not an unsigned pull request.
                agent_uuid = agent_uuid_from_body_html(pr["body_html"])
                if agent_uuid is None:
                    counts["prs_unsigned"] += 1
                    continue
                if not trusted_pr_author(pr, trusted_apps):
                    counts["prs_untrusted_author"] += 1
                    continue
                events, untrusted = collect_pr_events(
                    reader,
                    repo,
                    pr,
                    agent_uuid,
                    trusted_apps=trusted_apps,
                    skipped_checks=skipped_checks,
                    seen=seen,
                )
            except _PR_ERRORS:
                counts["prs_failed"] += 1
                continue
            counts["prs_read"] += 1
            counts["markers_untrusted"] += untrusted
            for event in events:
                if event["type"] not in EVENT_TYPES:
                    raise AssertionError(f"unexpected event type {event['type']!r}")
                fingerprint = event["fingerprint"]
                if fingerprint in this_cycle or seen(fingerprint):
                    counts["events_known"] += 1
                    continue
                this_cycle.add(fingerprint)
                if writer.write(event):
                    mark(fingerprint, event["type"])
                    counts["events_new"] += 1
                else:
                    counts["writes_failed"] += 1
    return counts


def parse_trusted_apps(values: Iterable[str]) -> dict[int, int]:
    """``APP_ID:BOT_ACTOR_ID`` pairs, as the deployment registered them."""
    apps: dict[int, int] = {}
    for value in values:
        for pair in filter(None, (p.strip() for p in value.split(","))):
            app_id, sep, actor_id = pair.partition(":")
            if not sep or not app_id.isdigit() or not actor_id.isdigit():
                raise ValueError(f"trusted app must be APP_ID:BOT_ACTOR_ID, got {pair!r}")
            apps[int(app_id)] = int(actor_id)
    return apps


def default_state_path() -> Path:
    configured = os.environ.get("UNITARES_GITHUB_OBSERVER_STATE")
    if configured:
        return Path(configured)
    return Path.home() / ".unitares" / "github_observer" / "emitted.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m agents.github_observer", description=__doc__)
    parser.add_argument("--repo", action="append", required=True, help="OWNER/NAME; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="print events, write nothing")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument(
        "--trusted-app",
        action="append",
        default=[],
        help="APP_ID:BOT_ACTOR_ID of an App whose review records are admitted; repeatable. "
        "Also read from GITHUB_OBSERVER_TRUSTED_APPS (comma-separated)",
    )
    parser.add_argument(
        "--skip-check",
        action="append",
        default=[],
        help="check or workflow name to leave out, in addition to the review check; repeatable",
    )
    args = parser.parse_args(argv)

    for repo in args.repo:
        if not _REPO_RE.match(repo):
            parser.error(f"--repo must be OWNER/NAME, got {repo!r}")
    if args.since_days < 1:
        parser.error("--since-days must be at least 1")
    if not args.dry_run:
        print(
            "github_observer: only --dry-run is available in this build; the write path "
            "(POST /v1/external/events) lands with that route.",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("github_observer: set GITHUB_TOKEN (read access to the repository)", file=sys.stderr)
        return 2
    try:
        trusted_apps = parse_trusted_apps(
            [*args.trusted_app, os.environ.get("GITHUB_OBSERVER_TRUSTED_APPS", "")]
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        known = read_only_fingerprints(args.state_path or default_state_path())
    except sqlite3.Error:
        print("github_observer: the fingerprint store could not be read", file=sys.stderr)
        return 2
    reader = GitHubReader(
        GitHubClient(token, os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL)
    )
    try:
        counts = run_cycle(
            reader,
            args.repo,
            DryRunWriter(sys.stdout),
            seen=known.__contains__,
            mark=lambda fingerprint, event_type: None,
            since=datetime.now(timezone.utc) - timedelta(days=args.since_days),
            trusted_apps=trusted_apps,
            skipped_checks=DEFAULT_SKIPPED_CHECKS | set(args.skip_check),
        )
    except GitHubError as exc:
        print(f"github_observer: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"dry_run": True, **counts}, sort_keys=True), file=sys.stderr)
    # A poll that could not read something is not a clean poll.
    return 1 if counts["repos_failed"] or counts["prs_failed"] else 0
