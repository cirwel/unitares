"""Turn GitHub API JSON into ``github.*`` events. Pure functions, no I/O.

Every event is one fact with a fingerprint that names that fact and nothing
else, so reading GitHub again does not produce it twice. ``<repo>`` in a
fingerprint is GitHub's numeric repository id, which a rename or a change of
letter case in the configured name does not move:

========================  ==================================================
``github.pr_opened``      ``<repo>#<pr>:opened`` (opening is not a GitHub
                          timeline event, so there is no event id to use)
``github.pr_merged``      ``<repo>#<pr>:state:<timeline event id>``
``github.pr_closed``      same; the ``closed`` event GitHub emits alongside
``github.pr_reopened``    a merge is folded into ``pr_merged``
``github.ci_result``      Actions: ``<repo>#<pr>:ci:<run id>:<attempt>``;
                          another check app:
                          ``<repo>#<pr>:check:<check run id>:<conclusion>``
``github.review_record``  ``<repo>#<pr>:review:<comment id>``
========================  ==================================================

``agent_uuid`` on an event is the subject: the agent the pull request's footer
names. ``pr_author`` is GitHub's own record of who opened the pull request, so
a reader can tell a footer the agent's own tooling wrote from one a stranger
typed. The producer (this observer) is recorded separately by the writer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from agents.github_observer.footer import visible_paragraph_lines

EVENT_PREFIX = "github."
EVENT_TYPES = frozenset(
    EVENT_PREFIX + name
    for name in ("pr_opened", "pr_merged", "pr_closed", "pr_reopened", "ci_result", "review_record")
)

REVIEW_MARKER = "unitares-review v1"
REVIEW_VERDICTS = frozenset({"CLEAN", "FINDINGS", "FAILED"})
# The author associations whose marker comment the review gate admits
# (``TRUSTED_ASSOCIATIONS`` in ``scripts/dev/review_gate.py``; a test pins the
# two together). A registered App is admitted through ``trusted_apps``.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ACTIONS_APP_SLUG = "github-actions"
# The repository's own review check restates what review records already carry.
DEFAULT_SKIPPED_CHECKS = frozenset({"review"})
# A run waiting for someone to approve it lists as completed with this
# conclusion, then finishes under the same run id and attempt. It is not a
# result, and reporting it would use up the fingerprint the result needs.
_ACTIONS_NOT_A_RESULT = frozenset({"action_required"})

_RECORD_RE = re.compile(r"<!--\s*" + re.escape(REVIEW_MARKER) + r"\s+(?P<attrs>[^>]*?)\s*-->")
_FOOTER_RE = re.compile(
    r"UNITARES agent:[ \t]*"
    r"(?P<uuid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})",
    re.IGNORECASE,
)
_STATE_EVENTS = {"merged": "pr_merged", "closed": "pr_closed", "reopened": "pr_reopened"}


class MalformedPullRequest(ValueError):
    """A pull request object lacks a field every fingerprint needs."""


def repo_key(pr: Mapping[str, Any]) -> str:
    """GitHub's numeric id of the repository the pull request targets."""
    repo_id = ((pr.get("base") or {}).get("repo") or {}).get("id")
    if type(repo_id) is not int:
        raise MalformedPullRequest("pull request has no base.repo.id")
    return str(repo_id)


def _number(pr: Mapping[str, Any]) -> int:
    number = pr.get("number")
    if type(number) is not int:
        raise MalformedPullRequest("pull request has no number")
    return number


def ci_fingerprint(pr: Mapping[str, Any], run_id: int, attempt: int) -> str:
    return f"{repo_key(pr)}#{_number(pr)}:ci:{int(run_id)}:{int(attempt)}"


def run_names_pr(run: Mapping[str, Any], pr: Mapping[str, Any]) -> bool:
    """Whether a workflow run's own ``pull_requests`` association names this
    pull request, by number and by the numeric id of its base repository.

    A run on a head the agent later amended, rebased or force-pushed away is
    no longer in the PR's commit list, but its association can still name the
    PR (GitHub fills it while the PR is open). The base repository id is
    required: GitHub also lists pull requests of other repositories (a
    fork's) whose head is this branch.
    """
    number, repo_id = _number(pr), int(repo_key(pr))
    for assoc in run.get("pull_requests") or []:
        if not isinstance(assoc, Mapping):
            continue
        base_repo = (assoc.get("base") or {}).get("repo") or {}
        if assoc.get("number") == number and base_repo.get("id") == repo_id:
            return True
    return False


def run_belongs_to_pr(run: Mapping[str, Any], pr: Mapping[str, Any], shas: Iterable[str]) -> bool:
    """A run counts for a pull request when its head commit is one of the PR's
    commits, or when GitHub explicitly associates the run with the PR."""
    return run.get("head_sha") in set(shas) or run_names_pr(run, pr)


def agent_uuid_from_body(body: str | None) -> str | None:
    """The agent a pull request body's footer names, or ``None``.

    The footer is a ``UNITARES agent: <uuid>`` line a reader of the rendered
    body would see as the author's own (see ``agents.github_observer.footer``).
    The last one wins: the delivery contract keeps the opening author's
    signature last, and a successor's above it.
    """
    found: str | None = None
    for line in visible_paragraph_lines(body or ""):
        match = _FOOTER_RE.fullmatch(line)
        if match:
            found = match.group("uuid").lower()
    return found


def parse_review_marker(body: str | None) -> dict[str, Any] | None:
    """The first review-gate marker in a comment body, as its raw attributes.

    Tokenised as the gate's ``parse_record`` does. Returns ``None`` when there
    is no marker or its verdict is not one the gate writes. ``disposed`` is a
    flag, not a count. ``head_sha`` is ``None`` for a legacy record written
    before the gate recorded ``head=``.
    """
    match = _RECORD_RE.search(body or "")
    if not match:
        return None
    attrs = dict(kv.split("=", 1) for kv in match.group("attrs").split() if "=" in kv)
    verdict = attrs.get("verdict")
    if verdict not in REVIEW_VERDICTS or not attrs.get("key"):
        return None
    try:
        findings = int(attrs.get("findings", "0"))
    except ValueError:
        return None
    return {
        "key": attrs["key"],
        "verdict": verdict,
        "findings": findings,
        "disposed": attrs.get("disposed") == "1",
        "reviewer": attrs.get("reviewer") or None,
        "head_sha": attrs.get("head") or None,
    }


def trusted_actor(obj: Mapping[str, Any], trusted_apps: Mapping[int, int]) -> bool:
    """Whether GitHub's own provenance for a comment or pull request is one the
    review gate admits (its ``trusted_review_comment``).

    Never a login or a body claiming an App: an owner, member or collaborator,
    or a bot acting through an App whose id the deployment registered together
    with that App's bot actor id.
    """
    if obj.get("author_association") in TRUSTED_ASSOCIATIONS:
        return True
    app = obj.get("performed_via_github_app")
    user = obj.get("user")
    if not isinstance(app, Mapping) or not isinstance(user, Mapping):
        return False
    app_id, actor_id = app.get("id"), user.get("id")
    return (
        type(app_id) is int
        and type(actor_id) is int
        and user.get("type") == "Bot"
        and trusted_apps.get(app_id) == actor_id
    )


trusted_review_comment = trusted_actor


def trusted_pr_author(pr: Mapping[str, Any], trusted_apps: Mapping[int, int]) -> bool:
    """Whether the pull request was opened by someone whose footer can be taken
    as the agent's own.

    A pull request carries no ``performed_via_github_app``, so a registered
    App is recognised by its bot actor id alone.
    """
    if pr.get("author_association") in TRUSTED_ASSOCIATIONS:
        return True
    user = pr.get("user")
    if not isinstance(user, Mapping):
        return False
    actor_id = user.get("id")
    return (
        type(actor_id) is int
        and user.get("type") == "Bot"
        and actor_id in set(trusted_apps.values())
    )


def _pr_author(pr: Mapping[str, Any]) -> dict[str, Any]:
    user = pr.get("user") if isinstance(pr.get("user"), Mapping) else {}
    return {
        "association": pr.get("author_association"),
        "id": user.get("id"),
        "type": user.get("type"),
    }


def _base(event: str, repo: str, pr: Mapping[str, Any], agent_uuid: str, **fields: Any) -> dict:
    return {
        "type": EVENT_PREFIX + event,
        "agent_uuid": agent_uuid,
        "repo": repo,
        "pr": _number(pr),
        "pr_author": _pr_author(pr),
        **fields,
    }


def pr_state_events(
    repo: str, pr: Mapping[str, Any], timeline: Iterable[Mapping[str, Any]], agent_uuid: str
) -> list[dict]:
    """The PR's opening and each later merge, close and reopen.

    ``head_sha_at_observation`` is the head when the observer read the pull
    request, not the head at the moment of the event, which GitHub's timeline
    does not record.
    """
    prefix = f"{repo_key(pr)}#{_number(pr)}"
    head_sha = (pr.get("head") or {}).get("sha")
    events = [
        _base(
            "pr_opened",
            repo,
            pr,
            agent_uuid,
            head_sha_at_observation=head_sha,
            observed_at=pr.get("created_at"),
            fingerprint=f"{prefix}:opened",
        )
    ]
    changes = [e for e in timeline if e.get("event") in _STATE_EVENTS and e.get("id") is not None]
    merged = pr.get("merged_at") is not None or any(e["event"] == "merged" for e in changes)
    folded_close = None
    if merged:
        # A merged pull request cannot be reopened, so its last ``closed`` is
        # the one GitHub emits alongside the merge, whatever its timestamp.
        closes = [e for e in changes if e["event"] == "closed"]
        folded_close = closes[-1]["id"] if closes else None
    for change in changes:
        if change["event"] == "closed" and change["id"] == folded_close:
            continue
        event = _base(
            _STATE_EVENTS[change["event"]],
            repo,
            pr,
            agent_uuid,
            head_sha_at_observation=head_sha,
            observed_at=change.get("created_at"),
            fingerprint=f"{prefix}:state:{change['id']}",
        )
        if change["event"] == "merged":
            event["merge_commit_sha"] = change.get("commit_id")
        events.append(event)
    return events


def ci_events(
    repo: str,
    pr: Mapping[str, Any],
    workflow_runs: Iterable[Mapping[str, Any]],
    check_runs: Iterable[Mapping[str, Any]],
    agent_uuid: str,
    *,
    pr_commit_shas: Iterable[str],
    failed_jobs: Mapping[tuple[int, int], list[str]] | None = None,
    skipped_checks: Iterable[str] = DEFAULT_SKIPPED_CHECKS,
) -> list[dict]:
    """One event per completed Actions run attempt and per other-app check.

    ``workflow_runs`` holds one object per attempt. GitHub's run list carries
    only each run's latest attempt, so the caller adds the earlier ones.

    A run counts for this pull request when its head commit is one of the
    PR's commits (a branch name can be reused, a commit cannot), or when
    GitHub's own ``pull_requests`` association names this PR, which keeps the
    results of heads that were later amended or force-pushed away. Check runs
    the Actions app created are left to their workflow run.
    """
    prefix = f"{repo_key(pr)}#{_number(pr)}"
    shas = set(pr_commit_shas)
    head_sha = (pr.get("head") or {}).get("sha")
    if head_sha:
        shas.add(head_sha)
    skipped = set(skipped_checks)
    failed_jobs = failed_jobs or {}
    events: list[dict] = []
    for run in workflow_runs:
        if run.get("status") != "completed" or not run_belongs_to_pr(run, pr, shas):
            continue
        if run.get("name") in skipped or run.get("conclusion") in _ACTIONS_NOT_A_RESULT:
            continue
        run_id, attempt = int(run["id"]), int(run.get("run_attempt") or 1)
        events.append(
            _base(
                "ci_result",
                repo,
                pr,
                agent_uuid,
                head_sha=run.get("head_sha"),
                source="actions",
                workflow=run.get("name"),
                run_id=run_id,
                attempt=attempt,
                conclusion=run.get("conclusion"),
                failed_jobs=sorted(failed_jobs.get((run_id, attempt), [])),
                observed_at=run.get("updated_at"),
                fingerprint=ci_fingerprint(pr, run_id, attempt),
            )
        )
    for check in check_runs:
        app = (check.get("app") or {}).get("slug")
        if check.get("status") != "completed" or app == ACTIONS_APP_SLUG:
            continue
        if check.get("name") in skipped or check.get("head_sha") not in shas:
            continue
        check_id, conclusion = int(check["id"]), check.get("conclusion")
        events.append(
            _base(
                "ci_result",
                repo,
                pr,
                agent_uuid,
                head_sha=check.get("head_sha"),
                source="check",
                app=app,
                workflow=check.get("name"),
                check_run_id=check_id,
                conclusion=conclusion,
                failed_jobs=[],
                observed_at=check.get("completed_at"),
                # An app may rewrite a completed check run with a new conclusion.
                fingerprint=f"{prefix}:check:{check_id}:{conclusion}",
            )
        )
    return events


def review_events(
    repo: str,
    pr: Mapping[str, Any],
    comments: Iterable[Mapping[str, Any]],
    agent_uuid: str,
    *,
    trusted_apps: Mapping[int, int],
) -> tuple[list[dict], int]:
    """Review records on the PR, and how many marker comments were not admitted.

    Every admitted record comment is its own fact: a disposition, a second
    model family on the same head, a FAILED record and its retry all stay.
    The fields are the marker's raw values (``raw: true``); the gate's own
    reading adjusts some of them, so a reader applies the gate's rules.
    """
    prefix = f"{repo_key(pr)}#{_number(pr)}"
    events: list[dict] = []
    untrusted = 0
    for comment in comments:
        record = parse_review_marker(comment.get("body"))
        if record is None or type(comment.get("id")) is not int:
            continue
        if not trusted_review_comment(comment, trusted_apps):
            untrusted += 1
            continue
        events.append(
            _base(
                "review_record",
                repo,
                pr,
                agent_uuid,
                **record,
                raw=True,
                comment_id=comment["id"],
                observed_at=comment.get("created_at"),
                fingerprint=f"{prefix}:review:{comment['id']}",
            )
        )
    return events, untrusted
