#!/usr/bin/env python3
"""review_gate.py — the PR review pass, as reflexive as the test gate.

The delivery contract says an agent marks its PR ready once "CI green, review
round joined" (docs/operations/github-workflow-conventions.md §2). Until this
script, the second half was prose: some PRs carried a review in the body, some
in a comment, most in neither, and nothing could tell which. This makes the
review a command with a cache and a machine-checkable record, the way
test-cache.sh made the test run one.

    review         run a review of HEAD's diff and post the record on its PR
    record         post a review that already happened elsewhere (a council,
                   a human, another model) as the record for this diff
    dispose        post dispositions for a FINDINGS record, clearing it
    key            print the diff key for HEAD
    sweep          review one quiet PR, including drafts, without a current review
    ci             (workflow only) set the `review` evidence check on a PR head

The diff key
------------
A record is bound to WHAT is reviewed, not to a commit: the key is the sha256
of `git diff --raw` between the merge base and the head, i.e. the path, mode
and blob id of every changed file. A base merge that does not touch the PR's
files keeps the key, so draft-base-refresh does not void a review; any change
to the PR's own content does. `--raw` carries no hunks, so diff.algorithm,
prefixes and other local config cannot make the laptop and the runner
disagree.

The record
----------
A PR comment starting with the marker below, posted by an account with write
access. The latest record whose key matches the head decides the status:

    CLEAN                          -> success
    FINDINGS(n) with dispositions  -> success
    FINDINGS(n) without            -> action_required (fix, or `dispose`)
    FAILED                         -> neutral warning (UNREVIEWED)
    no matching record             -> neutral warning (run the review)

A failed or expired run is recorded as FAILED, never as clean: absence of
findings from a reviewer that did not finish is not a review.

What the gate does NOT establish
--------------------------------
That the record was honest. Every agent on this repo posts through the
operator's GitHub account, so a comment cannot tell a real review from one an
author wrote about its own diff, and nothing here signs reviewer output. The
gate makes a review impossible to FORGET and visible to check — the record and
its full text sit on the PR — not impossible to fake by an author with write
access. Treat a `record` whose reviewer is the PR's own author as no review.

Cost
----
The CI side reads comments with GITHUB_TOKEN and calls no model. Native Codex is operator opt-in (`git config review.native true`), with a
bounded wait and local fallback. Otherwise the review runs through a CLI the operator already has (`codex`,
`claude`, or `agy` for Antigravity when installed, each on its own subscription login); `record` takes any review text, so a contributor with no model at
all can satisfy the gate with a human review. No metered API is on the
required path (AGENTS.md, execution-cost policy).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MARKER = "unitares-review v1"
STATUS_CONTEXT = "review"
CACHE_DIR = ".review-cache"
DEFAULT_BASE = "origin/master"
DEFAULT_BUDGET_S = 1800  # pipeline skill: clean codex completions ran 1-21 min
PROVIDER_COOLDOWN_S = 3600
UNREVIEWED = 2  # infrastructure unavailable, distinct from actionable findings
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
COMMENT_LIMIT = 60000  # GitHub caps a comment body at 65536 chars
CODEX_BOT = "chatgpt-codex-connector[bot]"
NATIVE_REQUEST = "unitares-native-review v1"
NATIVE_WAIT_S = 600
# Full Codex reviews per PR before the remaining findings are answered without
# another run (conventions §Review workflow, "Round cap"). Each run spends the
# same subscription quota authoring does, and past the third round most
# findings are about text the previous fix added.
ROUND_CAP = 3
# Codex renders severity as an image badge; plain `[P1]` titles occur too.
SEVERE_BADGE_RE = re.compile(r"!\[P[01] Badge\]|\[P[01]\]")
SEVERITY_LABEL_RE = re.compile(r"!\[P[0-3] Badge\]|\[P[0-3]\]")

REVIEWER_NAME_RE = re.compile(r"[A-Za-z0-9_.:@/+-]+")
VERDICT_RE = re.compile(r"\s*\**VERDICT:\s*(CLEAN|FINDINGS\((\d+)\))\**\s*")
RECORD_RE = re.compile(
    r"<!--\s*" + re.escape(MARKER) + r"\s+(?P<attrs>[^>]*?)\s*-->"
)

REVIEW_PROMPT = """\
You are reviewing a pull request to this repository before it is marked ready.
The full diff is in the file {diff_path} (base {base}, head {head}).
Read the diff, then read the surrounding code in this checkout wherever a
change's correctness depends on it. Do not modify any file.

Adversarially look for defects the author may have rationalized: behaviour
that is wrong, a claim in a doc or comment that the code contradicts, a test
that cannot fail, an error path that reports success, a change that breaks a
caller elsewhere in the repo. Cite file:line for every finding and say what
input or state makes it go wrong. Do not report style preferences.
Start every finding with its severity: [P0] or [P1] for a defect that must be
fixed before merge (wrong behaviour, data loss, security), [P2] or [P3] for
the rest. An unlabelled finding is treated as [P1].

The repository's house rules are in AGENTS.md; a violation of one is a finding.

End with exactly one line and nothing after it:
VERDICT: CLEAN
or
VERDICT: FINDINGS(<number of findings>)
"""


# --------------------------------------------------------------------------
# git / gh plumbing


class GhUnavailable(Exception):
    """The `gh` CLI could not be launched, so nothing could be reviewed."""


def _launch(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run, except that a missing `gh` binary raises GhUnavailable.

    Every PR read and record post goes through gh. Without it nothing was
    reviewed, which is infrastructure unavailable (exit 2), not findings
    (exit 1). Only the launch of the gh executable itself is converted, so a
    missing input file stays a FileNotFoundError even if it is named "gh".
    """
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        if cmd[0] == "gh" and exc.filename == "gh":
            raise GhUnavailable("the `gh` CLI is not installed or not on PATH") from exc
        raise


def _run(cmd: list[str], *, check: bool = True, cwd: str | None = None) -> str:
    proc = _launch(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"review_gate: {' '.join(cmd[:3])}… failed: {proc.stderr.strip()}")
    return proc.stdout


def git(*args: str, check: bool = True) -> str:
    return _run(["git", *args], check=check)


def gh_json(*args: str):
    return json.loads(_run(["gh", *args]))


class ClosedPullRequest(Exception):
    """Stop local review work once its PR has been closed or merged."""


def require_open(pr: int, info: dict | None = None) -> None:
    info = info if info is not None else gh_json("pr", "view", str(pr), "--json", "state")
    if info["state"] != "OPEN":
        raise ClosedPullRequest(f"PR #{pr} is {info['state'].lower()}; review work has stopped")


def diff_key(base: str, head: str) -> str:
    mb = git("merge-base", base, head).strip()
    # Bytes, not text: -z keeps path bytes verbatim, and a non-UTF-8 file
    # name must hash, not crash (a crash leaves the PR pending forever).
    raw = subprocess.run(
        ["git", "diff", "--raw", "--no-renames", "--full-index", "--no-abbrev",
         "--no-ext-diff", "-z", mb, head],
        capture_output=True, check=True).stdout
    return hashlib.sha256(raw).hexdigest()


def diff_text(base: str, head: str) -> str:
    # Reviewer input only; undecodable path bytes become U+FFFD, not a crash.
    out = subprocess.run(["git", "diff", "--no-color", "--no-ext-diff", f"{base}...{head}"],
                         capture_output=True, check=True).stdout
    return out.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# records


@dataclass
class Record:
    key: str
    verdict: str  # CLEAN | FINDINGS | FAILED
    findings: int
    disposed: bool
    reviewer: str
    url: str = ""
    text: str = ""
    created_at: str = ""

    def status(self) -> tuple[str, str]:
        if self.verdict == "CLEAN":
            return "success", f"clean ({self.reviewer})"
        if self.verdict == "FINDINGS" and self.disposed:
            return "success", f"{self.findings} finding(s) disposed ({self.reviewer})"
        if self.verdict == "FINDINGS":
            return "pending", f"{self.findings} finding(s) need fixes or dispositions"
        return "pending", f"UNREVIEWED: {self.reviewer} unavailable; author must retry or hand off explicitly"


def render_marker(r: Record) -> str:
    return (f"<!-- {MARKER} key={r.key} verdict={r.verdict} findings={r.findings} "
            f"disposed={int(r.disposed)} reviewer={r.reviewer} -->")


def parse_record(body: str) -> Record | None:
    m = RECORD_RE.search(body or "")
    if not m:
        return None
    attrs = dict(kv.split("=", 1) for kv in m.group("attrs").split() if "=" in kv)
    try:
        return Record(
            key=attrs["key"],
            verdict=attrs["verdict"],
            findings=int(attrs.get("findings", "0")),
            disposed=attrs.get("disposed") == "1",
            reviewer=attrs.get("reviewer", "unknown"),
        )
    except (KeyError, ValueError):
        return None


_NATIVE_DISPOSITION_RE = re.compile(
    r"dispositions for FINDINGS\((\d+)\) — (\S*#pullrequestreview-\d+)")


def _cited_native_review(rec: Record) -> str | None:
    """The native review URL a disposition's heading cites, when the cited
    count matches (``post_record`` writes that heading in `dispose`)."""
    m = _NATIVE_DISPOSITION_RE.search(getattr(rec, "text", "") or "")
    return m.group(2) if m and int(m.group(1)) == rec.findings else None


def latest_matching(comments: list[dict], key: str,
                    native: list[Record] = ()) -> Record | None:
    """The record that decides `key`'s status. Comments arrive oldest first.

    Normally the latest trusted record. But findings on a diff stay open until
    they are disposed or the diff changes: a later CLEAN or FAILED on the SAME
    diff — a re-run that came back quieter or crashed, or a `record` — does not
    clear or hide them, or re-rolling the reviewer would drop a finding silently.
    """
    records = []
    for c in comments:
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        body = c.get("body", "")
        rec = parse_record(body)
        if rec and rec.key == key and rec.verdict in {"CLEAN", "FINDINGS", "FAILED"}:
            if rec.disposed and not dispositions_complete(body, rec.findings):
                rec.disposed = False
            rec.url = c.get("html_url", "")
            rec.text = body
            rec.created_at = c.get("created_at", "")
            records.append(rec)
    records.extend(rec for rec in native if rec.key == key)
    found, completed, open_findings = None, None, []
    for rec in sorted(records, key=lambda r: timestamp(r.created_at)):
        found = rec
        if rec.verdict != "FAILED":
            completed = rec
        if rec.verdict == "FINDINGS":
            # A disposition answers ONE findings record, not every earlier one.
            cited = _cited_native_review(rec) if rec.disposed else None
            if not rec.disposed:
                open_findings.append(rec)
            elif cited:
                # A disposition of a native review answers THAT review, found
                # by URL, never another open finding with the same count.
                match = [i for i, o in enumerate(open_findings) if o.url == cited]
                if match:
                    open_findings.pop(match[-1])
                # Otherwise the review is no longer visible: native evidence
                # is bound to the reviewed head commit, so a base merge that
                # moves the head (keeping this diff key) drops it. The
                # disposition names it, so it stays disposed and consumes
                # nothing else.
            elif open_findings and open_findings[-1].findings == rec.findings:
                open_findings.pop()
            else:
                rec.disposed = False
                open_findings.append(rec)
    # Open findings decide the status whatever came after them on this diff —
    # a quieter re-run, a FAILED re-run — so they stay visible and disposable.
    return open_findings[-1] if open_findings else completed or found


def pr_comments(repo: str, pr: int) -> list[dict]:
    return api_pages(f"repos/{repo}/issues/{pr}/comments")


def api_pages(endpoint: str) -> list[dict]:
    pages = gh_json("api", "--paginate", "--slurp", endpoint)
    return [c for page in pages for c in page]


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def is_codex_bot(item: dict) -> bool:
    user = item.get("user") or {}
    return user.get("login") == CODEX_BOT and user.get("type") == "Bot"


def reviewed_head(short: str, head: str) -> bool:
    # Native clean comments use ten hex digits; activity summaries use seven.
    # Require an unambiguous object resolution, not merely a matching prefix.
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", short) and head.startswith(short)
                and git("rev-parse", "--verify", f"{short}^{{commit}}", check=False).strip() == head)


@dataclass
class NativeReview:
    records: list[Record]
    running: bool = False
    unavailable_reason: str = ""
    completed: bool = False
    rounds: CodexRounds | None = None


@dataclass
class CodexRounds:
    """Completed Codex reviews on the PR, across every head it has had."""
    count: int = 0
    last_head: str = ""          # as Codex printed it; may be abbreviated
    last_findings: list[dict] = field(default_factory=list)  # inline comments
    answered_since: bool = False  # a disposition or fix verification answered the last round

    @property
    def last_severe(self) -> bool:
        # Unlabelled counts as severe: a local review can report data loss in
        # prose, and the cap must never route that fix around a full run.
        return any(SEVERE_BADGE_RE.search(c.get("body") or "")
                   or not SEVERITY_LABEL_RE.search(c.get("body") or "") for c in self.last_findings)

    def capped(self) -> bool:
        """Past the cap, a findings round is answered without another Codex run.

        A clean last round is not a fix loop: a push after it is new work and
        gets a full review. Neither is a P0/P1: its fix always gets one.
        """
        # Once the round is answered (disposed, or fixes verified once), a
        # push may be new work, and new work gets a full review (PR #2401).
        return (self.count >= ROUND_CAP and bool(self.last_findings)
                and not self.last_severe and not self.answered_since)


def codex_rounds(comments: list[dict], reviews: list[dict], inline: list[dict],
                 events: list[dict] = ()) -> CodexRounds:
    """Count completed native Codex runs by the distinct commits they name.

    Local fallback records are not counted: they name no commit, no start
    time and no per-finding severity, so each attempt to count them opened a
    stale-base or severity gap (PR #2401 rounds 4-7). The cap still stops a
    capped PR from starting a local run, and the fallback only runs when
    native review is unavailable.

    Native findings arrive as a submitted review; a clean result as a comment
    naming the commit or an activity row marked Completed. Heads are compared
    on seven hex digits, the shortest form Codex prints. After any base change
    nothing counts and the cap never applies: native_records discards the same
    evidence, because a native run does not name the base it reviewed.
    """
    runs: dict[str, tuple[float, str, list[dict]]] = {}
    retargeted = any(e.get("event") in {"base_ref_changed", "base_ref_force_pushed"} for e in events)

    def seen(commit: str, when: str, findings: list[dict]) -> None:
        t = timestamp(when)
        if retargeted:
            return
        run = commit[:7]
        prior = runs.get(run)
        # One run leaves several artifacts (review, activity row) seconds
        # apart, in any order: keep its findings whichever arrives first.
        if prior is None:
            runs[run] = (t, commit, findings)
        else:
            runs[run] = (max(t, prior[0]), commit if t >= prior[0] else prior[1],
                         findings or prior[2])

    for c in comments:
        if not is_codex_bot(c):
            continue
        body = c.get("body") or ""
        commit = re.search(r"^\*\*Reviewed commit:\*\*\s*`([0-9a-f]+)`", body, re.M)
        if commit:
            seen(commit[1], c.get("updated_at") or c.get("created_at", ""), [])
        if "<!-- codex-pull-request-review-summary -->" in body:
            for line in body.splitlines():
                fields = line.split("|")
                if len(fields) >= 5 and "**Code Review**" in fields[1] and "**Completed**" in fields[2]:
                    row = re.search(r"`([0-9a-f]+)`", fields[3])
                    done = re.search(r'<relative-time datetime="([^"]+)"', fields[2])
                    if row:
                        seen(row[1], done[1] if done else c.get("updated_at", ""), [])
    for review in reviews:
        if not is_codex_bot(review) or review.get("state") == "PENDING" or not review.get("commit_id"):
            continue
        posted = [c for c in inline if c.get("pull_request_review_id") == review["id"]
                  and is_codex_bot(c)]
        findings = [c for c in posted if not c.get("in_reply_to_id")]
        # A reply filed inside an earlier thread is conversation, not a run
        # (the same rule native_records applies).
        replies_only = posted and not findings and not (review.get("body") or "").strip()
        if not replies_only:
            seen(review["commit_id"], review.get("submitted_at", ""), findings)
    answered_at = 0.0
    for c in comments:
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        rec = parse_record(c.get("body", ""))
        if rec and (rec.disposed or rec.reviewer.startswith("fix-verify:")):
            answered_at = max(answered_at, timestamp(c.get("created_at", "")))
            continue  # an answer to a round, never a run of its own
    if not runs:
        return CodexRounds()
    last_at, last, findings = max(runs.values(), key=lambda r: r[0])
    return CodexRounds(len(runs), last, findings, answered_since=answered_at > last_at)


def native_records(comments: list[dict], reviews: list[dict], inline: list[dict],
                   events: list[dict], key: str, head: str,
                   reactions: list[dict] | None = None) -> NativeReview:
    """Normalize observed Codex artifacts; a reaction alone is not evidence.

    Native completion is accepted only with an explicit reviewed commit. The
    artifact does not identify the reviewed base, so retargeted PRs use the
    local diff-bound fallback, including reviews racing with a retarget.
    Completed activity alone says nothing about whether findings were posted.
    """
    if any(e.get("event") in {"base_ref_changed", "base_ref_force_pushed"} for e in events):
        return NativeReview([], unavailable_reason="PR base changed; native evidence does not name the reviewed base")
    result = NativeReview([])
    completions = []
    for c in comments:
        if not is_codex_bot(c):
            continue
        body = c.get("body") or ""
        when = c.get("updated_at") or c.get("created_at", "")
        if not when:
            continue
        commit = re.search(r"^\*\*Reviewed commit:\*\*\s*`([0-9a-f]+)`", body, re.M)
        if (re.match(r"^Codex Review: Didn['’]t find any major issues\.", body)
                and commit and reviewed_head(commit[1], head)):
            result.records.append(Record(key, "CLEAN", 0, False, "codex-native",
                                         c.get("html_url", ""), body, when))
        if "<!-- codex-pull-request-review-summary -->" in body:
            for line in body.splitlines():
                fields = line.split("|")
                if len(fields) < 5 or "**Code Review**" not in fields[1]:
                    continue
                commit = re.search(r"`([0-9a-f]+)`", fields[3])
                if commit and reviewed_head(commit[1], head):
                    result.running |= any(word in fields[2] for word in ("**Running**", "**Queued**"))
                    if "**Completed**" in fields[2]:
                        result.completed = True
                        completed = re.search(r'<relative-time datetime="([^"]+)"', fields[2])
                        if completed:
                            completions.append((completed[1], c))
    # Native automatic review can finish clean with only its activity row and
    # a PR thumbs-up. Require both, with a fresh reaction AFTER that exact
    # head's completion time; an old approval must never bless a new push.
    if not result.running:
        for completed, comment in completions:
            for reaction in reactions or []:
                # GitHub's reactions endpoint reports this app as type User,
                # while comments report Bot. Bind its immutable account ID to
                # the already-verified summary author instead of trusting type.
                reactor = reaction.get("user") or {}
                author_id = comment["user"].get("id")
                if (not author_id or reactor.get("id") != author_id
                        or reactor.get("login") != CODEX_BOT or reaction.get("content") != "+1"):
                    continue
                try:
                    fresh = timestamp(reaction.get("created_at", "")) >= timestamp(completed) > 0
                except (ValueError, TypeError):
                    continue
                if fresh:
                    text = (f"Codex completed code review for `{head}` at {completed}; "
                            f"its clean reaction was posted at {reaction['created_at']}.\n\n"
                            + comment["body"])
                    result.records.append(Record(key, "CLEAN", 0, False, "codex-native",
                                                 comment.get("html_url", ""), text,
                                                 reaction["created_at"]))
                    break
    for review in reviews:
        when = review.get("submitted_at") or ""
        if (not is_codex_bot(review) or review.get("commit_id") != head
                or review.get("state") == "PENDING" or not when):
            continue
        findings = [c for c in inline if c.get("pull_request_review_id") == review["id"]
                    and is_codex_bot(c)]
        # GitHub files a bot's answer inside an existing thread as a new review
        # with an empty body, bound to the current head. That is conversation on
        # an earlier finding, not a review of this diff; the formal re-review of
        # the head decides it. Counting it blocked #2369 on Codex's own "no
        # blocking findings" reply. A review with any top-level comment, a body,
        # or no surviving comments at all still counts below.
        if (findings and all(c.get("in_reply_to_id") for c in findings)
                and not (review.get("body") or "").strip()):
            continue
        clean = review.get("state") == "APPROVED" and not findings
        # A submitted/dismissed review with no explicit approval stays visible,
        # including when someone deleted its inline comments. Never infer clean.
        text = "\n\n".join(f"{i}. {c.get('path')}:{c.get('line') or c.get('original_line')} "
                            f"{c.get('body', '')}\n{c.get('html_url', '')}"
                            for i, c in enumerate(findings, 1)) or review.get("body", "")
        result.records.append(Record(key, "CLEAN" if clean else "FINDINGS",
                                     0 if clean else max(1, len(findings)), False,
                                     "codex-native", review.get("html_url", ""), text, when))
    return result


def read_native(repo: str, pr: int, key: str, head: str, comments: list[dict]) -> NativeReview:
    reviews = api_pages(f"repos/{repo}/pulls/{pr}/reviews")
    inline = api_pages(f"repos/{repo}/pulls/{pr}/comments") if reviews else []
    events = api_pages(f"repos/{repo}/issues/{pr}/events")
    summary = any(is_codex_bot(c) and "<!-- codex-pull-request-review-summary -->"
                  in (c.get("body") or "") for c in comments)
    reactions = api_pages(f"repos/{repo}/issues/{pr}/reactions") if summary else []
    snapshot = native_records(comments, reviews, inline, events, key, head, reactions)
    snapshot.rounds = codex_rounds(comments, reviews, inline, events)
    return snapshot


PROVIDERS_FILE = Path(__file__).resolve().with_name("review_providers.json")
KNOWN_PROVIDERS = {"codex", "claude", "antigravity"}
# Cross-family preference when the author's family is unknown (a human branch).
PROVIDER_ORDER = ("codex", "antigravity", "claude")
# Reviewers that need an extra operator-installed CLI count only when it is on
# PATH; codex and claude keep their behaviour (a missing CLI is an UNREVIEWED
# launch failure with a cooldown, not a silent skip).
OPTIONAL_CLI = {"antigravity": "agy"}
# Antigravity reviews an inlined prompt from an empty workspace, so the whole
# prompt rides in ONE argv element: Linux caps that at 128 KiB (MAX_ARG_STRLEN)
# and macOS caps all of argv+env at 1 MiB. Bytes, not characters.
ANTIGRAVITY_PROMPT_LIMIT = 120_000

# agy gets an ALLOWLISTED environment, never the caller's: the prompt carries
# untrusted text (a PR diff, a paused agent's thesis), and an injected "print
# your environment" must find no UNITARES_*/GitHub token to echo. Kept: what a
# CLI needs to find its home, locale, proxy and agy's OWN optional Google
# credentials. Its subscription login lives in the system keyring, not env.
AGY_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
)


def agy_env() -> dict[str, str]:
    return {k: os.environ[k] for k in AGY_ENV_ALLOWLIST if k in os.environ}


ANTIGRAVITY_PROMPT = """\
You are reviewing a pull request to a repository you cannot see: your working
directory is deliberately empty, so do not try to read or list files. The full
diff (base {base}, head {head}) and the complete post-change text of every
changed file are below; judge the change from them alone.

Adversarially look for defects the author may have rationalized: behaviour
that is wrong, a claim in a doc or comment that the code contradicts, a test
that cannot fail, an error path that reports success. Cite file:line for every
finding and say what input or state makes it go wrong. Do not report style
preferences. If the material below is not enough to judge something, say so
rather than assuming it is fine.

End with exactly one line and nothing after it:
VERDICT: CLEAN
or
VERDICT: FINDINGS(<number of findings>)

===== DIFF =====
{diff}
{files}"""


def disabled_providers() -> dict[str, str]:
    """Providers the repo marks unavailable, name -> reason (tracked file).

    One committed switch every session reads, so an outage stops every
    checkout from routing to the dead provider, instead of each session
    failing once per hour of per-machine cooldown.

    An entry may be an object with a "reason", a reason string, or `true`;
    `false`/null leave the provider enabled, and a plain list of names works
    too. A missing file disables nothing. A file that exists but cannot be
    parsed disables nothing AND warns, so a bad hand edit is never silent.
    """
    try:
        raw = PROVIDERS_FILE.read_text()
    except OSError:
        return {}
    def warn(what: str) -> None:
        print(f"[review] WARNING: {PROVIDERS_FILE.name}: {what}", file=sys.stderr)

    try:
        data = json.loads(raw)
        unknown_keys = set(data) - {"_doc", "disabled"}
        if unknown_keys or "disabled" not in data:
            warn(f"expected a 'disabled' key (found {sorted(data)}); a misspelled key "
                 "disables nothing")
        entries = data.get("disabled") or {}
        if isinstance(entries, list):
            entries = {name: True for name in entries}
        out = {}
        for name, info in entries.items():
            key = str(name).strip().lower()  # "Codex" must still disable codex
            if key not in KNOWN_PROVIDERS:
                warn(f"unknown provider {name!r} (known: {', '.join(sorted(KNOWN_PROVIDERS))})")
            if isinstance(info, dict):
                out[key] = str(info.get("reason") or "disabled")
            elif isinstance(info, str):
                out[key] = info or "disabled"
            elif info:
                out[key] = "disabled"
        return out
    except (ValueError, AttributeError, TypeError) as exc:
        print(f"[review] WARNING: {PROVIDERS_FILE.name} is malformed ({exc}); "
              "treating no provider as disabled", file=sys.stderr)
        return {}


def native_enabled() -> bool:
    # Operator opt-in: no cloud/model service is required by a default install.
    # Native review is Codex, so a repo-wide Codex outage switches it off too.
    if "codex" in disabled_providers():
        return False
    return git("config", "--bool", "review.native", check=False).strip() == "true"


def current_record(repo: str, pr: int, key: str, head: str,
                   comments: list[dict]) -> Record | None:
    records = read_native(repo, pr, key, head, comments).records
    return latest_matching(comments, key, records)


def join_native(args, repo: str, pr: int, key: str, head: str) -> Record | None:
    """Join cloud review; request it once for drafts missed by automatic review.

    The caller holds the shared diff lock. A durable head+diff request marker
    also prevents later sweeps from repeatedly mentioning the bot after an
    outage. Unknown formats/absence/timeout fall back; none mean clean.
    """
    started = time.monotonic()
    # Keep at least half of a short budget for the local reviewers. Native
    # absence/stalls must not spend the fallback's entire allowance.
    deadline = started + min(NATIVE_WAIT_S, args.budget / 2)
    request_marker = f"<!-- {NATIVE_REQUEST} head={head} key={key} -->"
    requested = False
    print("[review] joining native Codex review (bounded wait; local fallback available)", flush=True)
    while time.monotonic() < deadline:
        info = gh_json("pr", "view", str(pr), "--json", "headRefOid,state")
        require_open(pr, info)
        if info["headRefOid"] != head:
            return Record(key, "FAILED", 0, False, "PR head changed; rerun review.sh")
        comments = pr_comments(repo, pr)
        snapshot = read_native(repo, pr, key, head, comments)
        if snapshot.unavailable_reason:
            print(f"[review] {snapshot.unavailable_reason}; using local fallback")
            return None
        existing = latest_matching(comments, key, snapshot.records)
        if existing and existing.verdict != "FAILED":
            return existing
        requests = [c for c in comments if c.get("author_association") in TRUSTED_ASSOCIATIONS
                    and request_marker in (c.get("body") or "")]
        requested |= bool(requests)
        # A stuck activity row must not grant a new ten-minute wait on every
        # sweep. The request's age bounds this attempt even while it says running.
        if requests and all(
                time.time() - timestamp(c.get("created_at", "")) >= NATIVE_WAIT_S for c in requests):
            break
        if not snapshot.running and not snapshot.completed and not requested and time.monotonic() - started >= 30:
            body = (f"@codex review\n\nReview the current draft diff at `{head}`. "
                    "The author owns fixes and readiness.\n\n" + request_marker + "\n")
            _launch(["gh", "pr", "comment", str(pr), "--body-file", "-"],
                    input=body, text=True, capture_output=True, check=True)
            requested = True
            print("[review] requested native review for this head and diff", flush=True)
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    print("[review] native review did not produce completed evidence in time; using local fallback")
    return None


def dispositions_complete(text: str, n: int) -> bool:
    """One numbered, non-empty entry per finding: `1. ...` through `n. ...`.

    A disposed record clears the gate, so an empty or partial dispositions
    body must not: that would drop findings silently, which the delivery
    contract forbids. Checked on the CI side too, not just when posting.
    """
    numbered = {int(m) for m in re.findall(r"^\s*#?(\d+)[.):]\s+\S", text or "", re.M)}
    return n > 0 and all(k in numbered for k in range(1, n + 1))


def parse_verdict(text: str) -> tuple[str, int] | None:
    # The verdict must be the LAST non-empty line: "VERDICT: CLEAN" followed
    # by "actually, one more thing…" is not a clean review.
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    m = VERDICT_RE.fullmatch(lines[-1]) if lines else None
    if m is None:
        return None
    word, n = m.groups()
    # FINDINGS(0) is a clean review; as FINDINGS it could never be disposed.
    return ("CLEAN", 0) if word == "CLEAN" or int(n) == 0 else ("FINDINGS", int(n))


# --------------------------------------------------------------------------
# local side


def repo_slug() -> str:
    return gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]


def current_pr() -> dict | None:
    proc = _launch(["gh", "pr", "view", "--json",
                    "number,headRefOid,headRefName,baseRefName,state"],
                   text=True, capture_output=True)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def optional_cli_installed(provider: str) -> bool:
    return provider not in OPTIONAL_CLI or shutil.which(OPTIONAL_CLI[provider]) is not None


def reviewer_candidates(branch: str) -> list[str]:
    """Usable reviewers for a branch, best first.

    Prefer a model family other than the author's (the branch prefix), but
    independence is a fresh reviewer context, not a provider name, so the
    author's own family stays last as a valid fallback. Disabled providers
    are dropped, and an optional CLI only counts when installed.
    """
    author = branch.split("/", 1)[0]
    disabled = disabled_providers()
    usable = [p for p in PROVIDER_ORDER if p not in disabled and optional_cli_installed(p)]
    return [p for p in usable if p != author] + [p for p in usable if p == author]


def default_reviewer(branch: str) -> str:
    # A quota outage must not prohibit the available reviewer.
    candidates = reviewer_candidates(branch)
    return candidates[0] if candidates else ("claude" if branch.startswith("codex/") else "codex")


def provider_state_path(reviewer: str) -> Path:
    common = Path(git("rev-parse", "--git-common-dir").strip()).resolve()
    return common / "review-gate" / f"{reviewer}-availability.json"


def provider_cooldown(reviewer: str) -> str | None:
    try:
        state = json.loads(provider_state_path(reviewer).read_text())
        if float(state["retry_after"]) > time.time():
            return str(state["reason"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def remember_unavailable(reviewer: str, text: str, note: str) -> None:
    """Back off account/startup failures across diffs and worktrees.

    Only classified infrastructure errors impose an hour's cooldown. An
    incomplete/model-generated answer isn't proof the provider is unavailable.
    """
    message = (text + "\n" + note).lower()
    reasons = {
        "quota": ("weekly limit", "usage limit", "rate limit", "rate_limit", "quota",
                  "resource_exhausted"),
        "authentication": ("not logged in", "authentication failed", "unauthorized", "login required"),
        "startup": ("could not start",),
    }
    reason = next((name for name, matches in reasons.items()
                   if any(term in message for term in matches)), None)
    if reason is None:
        return
    path = provider_state_path(reviewer)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic per-provider file; concurrent reviews of other diffs cannot
    # clobber another provider's cooldown or observe partially written JSON.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump({"retry_after": time.time() + PROVIDER_COOLDOWN_S, "reason": reason}, f)
    os.replace(f.name, path)


def _changed_blobs(base: str) -> list[tuple[str, str]]:
    """(path, committed text) for each changed regular file at HEAD.

    Everything comes from git objects, never the working tree: paths from
    `git diff --name-only` (not from diff text, where an added line can forge
    a `+++ b/` header), content from `git show HEAD:<path>` (a symlink's
    target is never followed, uncommitted edits never leak in). Symlinks,
    submodules and any path that is not clean and relative are skipped, so
    nothing outside the repository can be read into a prompt that leaves the
    machine.
    """
    out = subprocess.run(["git", "diff", "--name-only", "-z", "--no-renames",
                          "--diff-filter=d", f"{base}...HEAD"],
                         capture_output=True, check=False).stdout
    blobs = []
    for raw in out.split(b"\0"):
        path = raw.decode("utf-8", "replace")
        parts = Path(path).parts
        if not path or Path(path).is_absolute() or ".." in parts:
            continue
        ls = subprocess.run(["git", "ls-tree", "-z", "HEAD", "--", path],
                            capture_output=True, check=False).stdout.decode("utf-8", "replace")
        if not ls.startswith("100"):  # 100644/100755 only: no 120000 symlink, 160000 submodule
            continue
        show = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, check=False)
        if show.returncode == 0:
            blobs.append((path, show.stdout.decode("utf-8", "replace")))
    return blobs


def antigravity_prompt(diff: str, base: str, head: str) -> str | None:
    """A self-contained review prompt: the diff plus each changed file's full
    committed text. None when even the diff alone would not fit, so the caller
    fails over instead of reviewing part of the change."""
    def size(s: str) -> int:
        return len(s.encode("utf-8"))

    skeleton = ANTIGRAVITY_PROMPT.format(base=base, head=head, diff=diff, files="")
    budget = ANTIGRAVITY_PROMPT_LIMIT - size(skeleton)
    if budget < 0:
        return None
    files = ""
    for path, body in _changed_blobs(base):
        block = f"\n===== FILE {path} (post-change) =====\n{body}"
        if size(files) + size(block) > budget:
            note = f"\n===== FILE {path} omitted: prompt size limit =====\n"
            if size(files) + size(note) <= budget:
                files += note
            continue
        files += block
    return ANTIGRAVITY_PROMPT.format(base=base, head=head, diff=diff, files=files)


def _antigravity_text(stdout: str) -> str:
    """agy -p --output-format json prints one object; SUCCESS carries the
    answer in "response". Anything else is returned raw for classification."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        return stdout
    if isinstance(data, dict) and data.get("status") == "SUCCESS" and isinstance(data.get("response"), str):
        return data["response"]
    return stdout


def run_reviewer(reviewer: str, prompt: str, out_dir: Path, budget_s: int) -> tuple[str, str]:
    """Return (final text, status note). Never raises on reviewer failure."""
    last = (out_dir / "last-message.txt").resolve()
    # A previous run's output must never stand in for this run's: a --fresh
    # claude run would otherwise post the old codex verdict.
    last.unlink(missing_ok=True)
    if reviewer == "codex":
        cmd = ["codex", "exec", "--sandbox", "read-only", "-C", os.getcwd(),
               "--output-last-message", str(last), prompt]
    elif reviewer == "antigravity":
        # The prompt is already self-contained (antigravity_prompt). An EMPTY workspace, not the checkout: a PR can carry .agents/ hooks,
        # rules and project permissions that agy would load from its cwd.
        # --mode plan and --sandbox are defence in depth, not the boundary.
        cmd = ["agy", "-p", prompt, "--mode", "plan", "--sandbox", "--output-format", "json"]
    elif reviewer == "claude":
        cmd = ["claude", "-p", prompt,
               "--allowedTools", "Read", "Grep", "Glob",
               "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
    else:
        raise SystemExit(f"review_gate: unknown reviewer {reviewer!r}")

    log = out_dir / "reviewer.log"
    isolated = reviewer == "antigravity"
    workspace = tempfile.TemporaryDirectory(prefix="review-agy-") if isolated else None
    if workspace and any((d / m).exists() for d in Path(workspace.name).resolve().parents
                         for m in (".git", ".agents")):
        # agy may discover project config by walking up from its cwd.
        workspace.cleanup()
        return ("temporary workspace sits under a .git/.agents directory; set TMPDIR "
                "to a plain directory", "skipped: workspace not isolated")
    with open(log, "w") as fh:
        # antigravity: stdout is the JSON answer, kept apart from stderr.
        out = open(last, "w") if isolated else fh
        try:
            # stdin=DEVNULL: codex blocks reading an open non-TTY stdin.
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=fh if isolated else subprocess.STDOUT,
                                        cwd=workspace.name if workspace else None,
                                        env=agy_env() if isolated else None,
                                        start_new_session=True)
            except OSError as exc:  # reviewer CLI missing or not executable
                return str(exc), f"could not start {reviewer}: {exc.__class__.__name__}"
            try:
                rc = proc.wait(timeout=budget_s)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return log.read_text(errors="replace"), f"killed at the {budget_s}s budget"
        finally:
            if out is not fh:
                out.close()
            if workspace:
                workspace.cleanup()
    text = last.read_text(errors="replace") if last.exists() else ""
    if isolated:
        text = _antigravity_text(text)
    # An empty answer must surface the log, where auth/quota errors land.
    text = text if text.strip() else log.read_text(errors="replace")
    return text, ("exit 0" if rc == 0 else f"exit {rc}")


def render_body(rec: Record, heading: str, text: str) -> str:
    """The exact comment body a record is posted as (and `--emit` prints)."""
    body = f"{render_marker(rec)}\n### Review record — {heading}\n\n"
    body += f"Diff key `{rec.key[:12]}` · reviewer `{rec.reviewer}`\n\n"
    if len(text) > COMMENT_LIMIT:
        text = text[:COMMENT_LIMIT] + "\n\n… (truncated; full text in the local .review-cache)"
    body += text.strip() + "\n"
    # Evidence is not a new bot command. Native footers include example
    # mentions that dispatch cloud tasks when copied by the author's account.
    return re.sub(r"@codex\b", "Codex", body, flags=re.I)


def post_record(pr: int, rec: Record, heading: str, text: str) -> None:
    require_open(pr)  # a local reviewer may have finished after the merge
    _launch(["gh", "pr", "comment", str(pr), "--body-file", "-"],
            input=render_body(rec, heading, text), text=True, check=True, capture_output=True)


def _resolve_offline(args) -> str:
    """Diff key for HEAD without gh, for `--emit`. Refuses an unpushed HEAD.

    CI keys the PR head against its base, so the emitted record is only valid
    if HEAD is exactly what `<remote>/<current branch>` holds, read live with
    ls-remote. `@{upstream}` is deliberately not consulted: it may track the
    base or be stale. The caller posts the
    body through whatever GitHub client it has (an MCP connector, the REST
    API); the gate then reads it like any other record.
    """
    base = args.base or DEFAULT_BASE
    remote, _, branch = base.partition("/")
    git("fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{base}", check=False)
    head = git("rev-parse", "HEAD").strip()
    # Compare against the remote branch of the same name, read live: a
    # stale tracking ref, or one tracking the base, would pass a head CI never sees.
    mine = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    # ls-remote exits 2 when the branch does not exist, anything else nonzero
    # on a transport failure; neither may fall back to a local tracking ref.
    probe = _launch(["git", "ls-remote", "--exit-code", remote, f"refs/heads/{mine}"],
                    text=True, capture_output=True)
    if probe.returncode not in (0, 2):
        raise SystemExit(f"review_gate: --emit could not read {remote}/{mine} "
                         f"({probe.stderr.strip()[:200]}); refusing to trust a local ref")
    pushed = probe.stdout.split()[0] if probe.returncode == 0 and probe.stdout.strip() else ""
    if pushed != head:
        raise SystemExit(f"review_gate: --emit needs HEAD pushed: {remote}/{mine} is "
                         f"{pushed[:12] or 'missing'}, HEAD is {head[:12]}. Push first, or the "
                         "record would describe a diff CI never sees")
    print(f"[review] keyed against {base}; this must be the PR's base branch "
          "(pass --base origin/<base> otherwise)", file=sys.stderr)
    return diff_key(base, head)


def _resolve(args) -> tuple[int, str, str, str]:
    """PR number, repo, key, reviewer label for HEAD. Refuses a stale push."""
    info = current_pr() if args.pr is None else gh_json(
        "pr", "view", str(args.pr), "--json", "number,headRefOid,headRefName,baseRefName,state")
    if info is None:
        raise SystemExit("review_gate: no PR for this branch — ship it first (ship.sh opens one)")
    require_open(info["number"], info)
    # Key against the PR's own base, as CI does — not a fixed master.
    args.base = args.base or f"origin/{info['baseRefName']}"
    remote, _, branch = args.base.partition("/")
    git("fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{args.base}",
        check=False)
    head = git("rev-parse", "HEAD").strip()
    key = diff_key(args.base, head)
    pushed = info["headRefOid"]
    # ship.sh starts the review right after `git push`, and GitHub's API can
    # report the previous head for a few seconds. Wait for it rather than
    # refuse: a refusal here means the reflexive review silently never runs.
    for _ in range(12):
        if pushed == head:
            break
        time.sleep(5)
        pushed = gh_json("pr", "view", str(info["number"]), "--json", "headRefOid")["headRefOid"]
    if pushed != head:
        known = subprocess.run(["git", "cat-file", "-e", f"{pushed}^{{commit}}"],
                               capture_output=True).returncode == 0
        if not known or diff_key(args.base, pushed) != key:
            raise SystemExit("review_gate: local HEAD differs from the PR head — push first, "
                             "or the record would describe a diff CI never sees")
    return info["number"], repo_slug(), key, info["headRefName"]


def completed_review_exit(repo: str, pr: int, key: str, head: str, result: int) -> int:
    """A completed review must still describe the PR we are handing back."""
    if result:
        return result
    # Even different diffs of the same PR may be reviewed concurrently. Fetch
    # both tips into private refs so another worktree cannot move our snapshot.
    snapshot = f"refs/review-gate/handoff/{pr}/{uuid.uuid4().hex}"
    base_ref, head_ref = f"{snapshot}/base", f"{snapshot}/head"
    try:
        info = gh_json("pr", "view", str(pr), "--json", "baseRefName,state")
        require_open(pr, info)
        git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin",
            f"+refs/heads/{info['baseRefName']}:{base_ref}",
            f"+refs/pull/{pr}/head:{head_ref}")
        # The record is diff-bound: message amendments and base-only merges
        # remain valid. Use the fetched head, not an API SHA from before a push.
        current = diff_key(base_ref, head_ref) == key
    except SystemExit as exc:
        print(f"[review] UNREVIEWED: cannot confirm the current PR diff: {exc}; retry review.sh")
        return UNREVIEWED
    finally:
        git("update-ref", "-d", head_ref, check=False)
        git("update-ref", "-d", base_ref, check=False)
    if not current:
        print("[review] UNREVIEWED: the PR head or base diff changed during review; push/join the current diff again")
        return UNREVIEWED
    return 0


def finish_record(repo: str, pr: int, key: str, head: str, rec: Record,
                  comments: list[dict]) -> int:
    result = UNREVIEWED if rec.verdict == "FAILED" else 0 if rec.status()[0] == "success" else 1
    if result == 0 and rec.verdict == "CLEAN" and rec.reviewer == "codex-native":
        # Reactions have no workflow event. A durable comment both re-runs CI
        # when the clean reaction arrives late and preserves diff equivalence.
        recorded = any(c.get("author_association") in TRUSTED_ASSOCIATIONS
                       and (r := parse_record(c.get("body", "")))
                       and r.key == key and r.verdict == "CLEAN" for c in comments)
        if not recorded:
            post_record(pr, Record(key, "CLEAN", 0, False, "codex-native"),
                        f"CLEAN — native review joined: {rec.url}", rec.text)
    # The receipt's network write can race a push too. Validate after it so
    # publishing evidence for an earlier diff cannot finish the current one.
    return completed_review_exit(repo, pr, key, head, result)


class review_lock:
    """One review per diff on this machine, across every worktree.

    Lives in the git common dir, so ship.sh's background review in an agent's
    worktree and the scheduled sweep see the same lock. A kernel flock, not a
    pid file: the OS releases it when the holder exits or dies, so there is no
    stale state to judge and no window where two processes both "take over".
    The file itself is never deleted.
    """

    def __init__(self, key: str):
        common = Path(git("rev-parse", "--git-common-dir").strip()).resolve()
        self.path = common / "review-gate" / f"{key}.lock"
        self.held = False
        self._fd = None

    def _try(self) -> int | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None

    def holder_alive(self) -> bool:
        if self.held:
            return True
        fd = self._try()
        if fd is None:
            return True
        os.close(fd)  # closing releases the probe's lock
        return False

    def __enter__(self):
        self._fd = self._try()
        self.held = self._fd is not None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self.held = False


def cmd_review(args) -> int:
    pr, repo, key, branch = _resolve(args)
    reviewer = args.reviewer or default_reviewer(branch)
    args.branch = branch  # review_with_fallback picks the next candidate by author family
    head = git("rev-parse", "HEAD").strip()
    deadline = time.monotonic() + args.budget
    joined = False
    while True:
        with review_lock(key) as lock:
            if lock.held:
                require_open(pr)
                # Re-read AFTER acquiring: a ship/sweep review may have posted
                # while we waited. Joining it must return its actual result.
                comments = pr_comments(repo, pr)
                native = native_enabled()
                try:
                    existing = current_record(repo, pr, key, head, comments)
                except SystemExit as exc:
                    print(f"[review] UNREVIEWED: review evidence is incomplete: {exc}. "
                          "Retry review.sh when GitHub evidence is readable; existing findings remain open.")
                    return UNREVIEWED
                # --fresh can re-review a clean result; it cannot hide findings.
                if (args.fresh and not joined and existing
                        and not (existing.verdict == "FINDINGS" and not existing.disposed)):
                    existing = None
                if existing and (joined or existing.verdict != "FAILED"):
                    state, desc = existing.status()
                    print(f"[review] already recorded for this diff: {desc}\n{existing.url}")
                    print(existing.text)
                    if existing.verdict == "FAILED":
                        return UNREVIEWED
                    return finish_record(repo, pr, key, head, existing, comments)
                # The cap binds the local fallback too: it spends the same quota.
                # An explicit --reviewer is the author choosing to spend a round.
                if not args.reviewer:
                    rounds = pr_rounds(repo, pr, key, head, comments)
                    if rounds.capped():
                        return capped_review(args, repo, pr, key, head, rounds)
                args.failed_providers = {p for p in KNOWN_PROVIDERS
                                         if failed_runs(comments, key, p) >= SWEEP_MAX_FAILED}
                if native and not args.reviewer and not args.fresh:
                    native_start = time.monotonic()
                    try:
                        rec = join_native(args, repo, pr, key, head)
                    except SystemExit as exc:
                        print(f"[review] UNREVIEWED: review evidence is incomplete: {exc}; retry review.sh")
                        return UNREVIEWED
                    except subprocess.SubprocessError as exc:
                        print(f"[review] WARNING: native review unavailable: {exc}")
                        rec = None
                    if rec:
                        print(f"[review] {rec.status()[1]}\n{rec.url}\n{rec.text}")
                        return finish_record(repo, pr, key, head, rec, pr_comments(repo, pr))
                    args.budget = max(0, args.budget - int(time.monotonic() - native_start))
                result = review_with_fallback(args, pr, key, reviewer)
                # A cloud review can finish while the local fallback runs.
                # Return those findings too, instead of letting the local
                # CLEAN hide them from the working author until a later CI run.
                try:
                    latest = current_record(repo, pr, key, head, pr_comments(repo, pr))
                except SystemExit as exc:
                    print(f"[review] UNREVIEWED: completion evidence is incomplete: {exc}; retry review.sh")
                    return UNREVIEWED
                if latest and latest.verdict == "FINDINGS" and not latest.disposed:
                    print(f"[review] {latest.status()[1]}\n{latest.url}\n{latest.text}")
                    return 1
                return completed_review_exit(repo, pr, key, head, result)
        if not joined:
            print("[review] joining the review already running for this diff…", flush=True)
            joined = True
        if time.monotonic() >= deadline:
            print("[review] still running; no completed review joined. "
                  "Run scripts/dev/review.sh again before marking ready.")
            return UNREVIEWED
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def pr_rounds(repo: str, pr: int, key: str, head: str, comments: list[dict]) -> CodexRounds:
    return read_native(repo, pr, key, head, comments).rounds or CodexRounds()


VERIFY_PROMPT = """\
A code reviewer raised this finding on a pull request:

<finding>
{finding}
</finding>

The author says this commit fixes it:

<diff>
{diff}
</diff>

Does this diff actually address the specific problem in the finding? Judge only this finding. A change that touches the same code but fixes a different problem does NOT address it.
Answer with exactly one word on the last line: ADDRESSED or NOT_ADDRESSED."""
VERIFY_DIFF_LIMIT = 40000  # characters; keeps gemma4 inside a 16k-token context
VERIFY_TIMEOUT_S = 300


def finding_text(comment: dict) -> str:
    body = re.sub(r"\*\*<sub><sub>!\[P\d Badge\][^\n]*?</sub></sub>\s*", "**", comment.get("body") or "")
    body = body.split("\nUseful?")[0]
    return f"{comment.get('path')}:{comment.get('line') or comment.get('original_line')}\n{body.strip()}"


def ask_verifier(verifier: str, prompt: str) -> str:
    """One chat completion from the configured verifier, via curl.

    `ollama:<model>` uses the local server's native /api/chat (its /v1 shim
    silently clamps prompts to 8K, cutting the front). `hf:<model>` is the
    metered Hugging Face router, so it is opt-in only, never the default.
    """
    backend, _, model = verifier.partition(":")
    if backend == "ollama":
        url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/chat"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "options": {"temperature": 0, "num_ctx": 16384}}
        headers = []
    elif backend == "hf":
        url = "https://router.huggingface.co/v1/chat/completions"
        token = os.environ.get("HF_TOKEN", "")
        token_file = Path.home() / ".cache" / "huggingface" / "token"
        if not token and token_file.exists():
            token = token_file.read_text().strip()
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "max_tokens": 800}
        headers = [f"Authorization: Bearer {token}"]
    else:
        raise ValueError(f"unknown verifier backend {backend!r} (use ollama:<model> or hf:<model>)")
    # Headers (the HF token) go through stdin and the body through a private
    # file, so no secret is in argv for other local users to read.
    with tempfile.NamedTemporaryFile("w", suffix=".json") as body:
        json.dump(payload, body)
        body.flush()
        try:
            proc = subprocess.run(["curl", "-sS", "--fail-with-body", "--max-time", str(VERIFY_TIMEOUT_S),
                                   "-H", "Content-Type: application/json", "-H", "@-",
                                   "--data-binary", f"@{body.name}", url],
                                  input="\n".join(headers) + "\n", text=True, capture_output=True)
        except OSError as exc:  # no curl: an outage (UNREVIEWED), never findings
            raise RuntimeError(f"{verifier} unavailable: cannot run curl: {exc}") from exc
    if proc.returncode:
        raise RuntimeError(f"{verifier} unavailable: {(proc.stderr or proc.stdout).strip()[:200]}")
    try:
        reply = json.loads(proc.stdout)
        text = reply["message"]["content"] if backend == "ollama" else reply["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{verifier} returned an unexpected response: {proc.stdout[:200]}") from exc
    if not isinstance(text, str):
        raise RuntimeError(f"{verifier} returned no text")
    return text


def verify_fix(verifier: str, finding: str, diff: str) -> bool:
    answer = re.findall(r"NOT_ADDRESSED|ADDRESSED", ask_verifier(
        verifier, VERIFY_PROMPT.format(finding=finding, diff=diff[:VERIFY_DIFF_LIMIT])))
    if not answer:
        raise RuntimeError(f"{verifier} gave no ADDRESSED/NOT_ADDRESSED verdict")
    return answer[-1] == "ADDRESSED"


def capped_review(args, repo: str, pr: int, key: str, head: str, rounds: CodexRounds) -> int:
    """Answer the last round's findings without spending another Codex run.

    Reached only when this diff has no record, i.e. fixes were pushed after
    the last round. A configured verifier checks each finding against those
    fix commits and posts a diff-bound record naming itself; it does not
    review the new lines for new problems, and the record says so.
    """
    n = len(rounds.last_findings)
    print(f"[review] review round cap reached ({rounds.count} of {ROUND_CAP}, "
          f"last round {n} finding(s), no P0/P1): not requesting another run.")
    verifier = git("config", "review.verifier", check=False).strip()
    last = git("rev-parse", "--verify", "--quiet", f"{rounds.last_head}^{{commit}}", check=False).strip()
    if not verifier or not last:
        why = ("no verifier configured (git config review.verifier ollama:gemma4:latest)"
               if not verifier else f"last reviewed commit {rounds.last_head} is not in this clone")
        print(f"[review] UNREVIEWED: {why}. Past the cap, answer findings with "
              "`review.sh dispose` on the reviewed diff instead of pushing fixes, or spend "
              "a round deliberately with `review.sh --reviewer codex`.")
        return UNREVIEWED
    results = []
    try:
        for comment in rounds.last_findings:
            diff = git("diff", last, head, "--", comment.get("path", ""), check=False)
            diff = diff or git("diff", last, head, check=False)
            results.append((comment, verify_fix(verifier, finding_text(comment), diff)))
    except (RuntimeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"[review] UNREVIEWED: fix verification failed: {exc}")
        return UNREVIEWED
    open_ = [c for c, ok in results if not ok]
    done = [c for c, ok in results if ok]
    lines = [f"Review round cap reached ({rounds.count} of {ROUND_CAP}). {verifier} checked each "
             f"finding from the last Codex round (`{last[:10]}`) against the fixes pushed since "
             f"(`{last[:10]}..{head[:10]}`). It checks the fixes only; it did not review the "
             "new lines for new problems.", ""]
    lines += [f"{i}. NOT ADDRESSED: {finding_text(c)}\n   {c.get('html_url', '')}"
              for i, c in enumerate(open_, 1)]
    lines += [f"- addressed: {finding_text(c).splitlines()[0]} {c.get('html_url', '')}" for c in done]
    lines.append("")
    lines.append("VERDICT: CLEAN" if not open_ else f"VERDICT: FINDINGS({len(open_)})")
    rec = Record(key, "FINDINGS" if open_ else "CLEAN", len(open_), False, f"fix-verify:{verifier}")
    post_record(pr, rec, f"{rec.verdict if not open_ else f'FINDINGS({len(open_)})'} "
                "(fix verification past the round cap)", "\n".join(lines))
    print("\n".join(lines))
    return finish_record(repo, pr, key, head, rec, pr_comments(repo, pr))


def review_with_fallback(args, pr: int, key: str, preferred: str) -> int:
    """At most two independent attempts, sharing one wall-clock budget.

    Findings stop routing: trying another model must never erase a review we
    dislike. An explicit --reviewer retries that provider despite cooldown.
    """
    others = [p for p in reviewer_candidates(getattr(args, "branch", "") or "") if p != preferred]
    providers = [preferred] + others[:1]
    deadline = time.monotonic() + args.budget
    available = []
    disabled = disabled_providers()
    for provider in providers:
        if provider in disabled and getattr(args, "reviewer", None) != provider:
            print(f"[review] skipping {provider}: disabled repo-wide "
                  f"({disabled[provider]}); see {PROVIDERS_FILE.name}")
            continue
        if (provider in getattr(args, "failed_providers", set())
                and getattr(args, "reviewer", None) != provider):
            print(f"[review] {provider} exhausted retries for this diff; "
                  f"use --reviewer {provider} to explicitly retry")
            continue
        reason = provider_cooldown(provider)
        if reason and getattr(args, "reviewer", None) != provider:
            print(f"[review] skipping {provider}: {reason} cooldown; "
                  f"use --reviewer {provider} to retry after restoring access")
        else:
            available.append(provider)
    for i, provider in enumerate(available):
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break
        attempt = argparse.Namespace(**vars(args))
        attempt.budget = max(1, remaining // (len(available) - i))
        result = _review_locked(attempt, pr, key, provider)
        if result != UNREVIEWED:
            return result
        print(f"[review] {provider} did not complete; checking remaining reviewers", flush=True)
    print("[review] UNREVIEWED: no reviewer completed. Keep the PR draft and report "
          "this blocker and next action; retry scripts/dev/review.sh or record "
          "an independent code review with record <file> --reviewer-name <who> --independent.")
    return UNREVIEWED


def _review_locked(args, pr: int, key: str, reviewer: str) -> int:
    require_open(pr)
    out_dir = Path(CACHE_DIR) / key / reviewer
    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = (out_dir / "diff.txt").resolve()
    diff_path.write_text(diff_text(args.base, "HEAD"))
    prompt = REVIEW_PROMPT.format(diff_path=diff_path, base=args.base,
                                  head=git("rev-parse", "--short", "HEAD").strip())
    print(f"[review] {reviewer} reviewing PR #{pr} (key {key[:12]}, budget {args.budget}s)…",
          flush=True)
    t0 = time.monotonic()
    if reviewer == "antigravity":
        # It runs in an empty workspace, so it gets the material inline.
        prompt = antigravity_prompt(diff_path.read_text(errors="replace"), args.base,
                                    git("rev-parse", "--short", "HEAD").strip())
    if prompt is None:
        text, note = ("diff too large for the antigravity reviewer's inlined prompt",
                      "skipped: prompt over the size limit")
    else:
        text, note = run_reviewer(reviewer, prompt, out_dir, args.budget)
    minutes = (time.monotonic() - t0) / 60
    parsed = parse_verdict(text) if note == "exit 0" else None
    if parsed is None:
        remember_unavailable(reviewer, text, note)
        rec = Record(key, "FAILED", 0, False, reviewer)
        heading = f"FAILED ({note}, no VERDICT line)" if note == "exit 0" else f"FAILED ({note})"
    else:
        verdict, n = parsed
        provider_state_path(reviewer).unlink(missing_ok=True)
        rec = Record(key, verdict, n, False, reviewer)
        heading = "CLEAN" if verdict == "CLEAN" else f"FINDINGS({n})"
    heading += f" · {minutes:.1f} min"
    (out_dir / "review.txt").write_text(text)
    post_record(pr, rec, heading, text)
    print(f"[review] full result: {out_dir / 'review.txt'}")
    if parsed is not None:
        print(text.strip())  # the working agent needs the findings, not just a count
    else:
        print(text.strip()[-2000:])  # expose quota/auth/startup failures to the author
        print("[review] independent code reviews can also be recorded with: "
              "scripts/dev/review.sh record <file> --reviewer-name <who> --independent")
    state, desc = rec.status()
    print(f"[review] {desc}")
    if rec.verdict == "FINDINGS":
        print("[review] fix and push (the next run reviews the new diff), or record "
              "dispositions: scripts/dev/review.sh dispose <file>")
    return UNREVIEWED if parsed is None else (0 if state == "success" else 1)


def cmd_record(args) -> int:
    if not args.independent:
        raise SystemExit("review_gate: record requires --independent to attest that a "
                         "separate reviewer examined this diff. Consult advice alone is not a code review.")
    # Without --emit, gh is resolved first: a machine without it is UNREVIEWED
    # (exit 2) before any input is read.
    resolved = None if args.emit else _resolve(args)
    text = Path(args.file).read_text()
    parsed = parse_verdict(text)
    if parsed is None:
        raise SystemExit("review_gate: the review text needs a final "
                         "'VERDICT: CLEAN' or 'VERDICT: FINDINGS(n)' line")
    verdict, n = parsed
    if not REVIEWER_NAME_RE.fullmatch(args.reviewer_name):
        raise SystemExit("review_gate: --reviewer-name must match [A-Za-z0-9_.:@/+-]+ "
                         "(it is a field in the record marker; whitespace or '>' would break it)")
    heading = (f"{verdict if verdict == 'CLEAN' else f'FINDINGS({n})'} "
               f"(recorded, reviewed by {args.reviewer_name})")
    if args.emit:
        rec = Record(_resolve_offline(args), verdict, n, False, args.reviewer_name)
        sys.stdout.write(render_body(rec, heading, text))
        print(f"[review] emitted, not posted: post the body above verbatim as a PR comment "
              f"({rec.status()[1]})", file=sys.stderr)
        return 0
    pr, repo, key, branch = resolved
    rec = Record(key, verdict, n, False, args.reviewer_name)
    post_record(pr, rec, heading, text)
    print(f"[review] recorded: {rec.status()[1]}")
    return 0


def cmd_dispose(args) -> int:
    if getattr(args, "emit", False):
        return _dispose_emit(args)
    pr, repo, key, _ = _resolve(args)
    prior = current_record(repo, pr, key, git("rev-parse", "HEAD").strip(), pr_comments(repo, pr))
    if prior is None or prior.verdict != "FINDINGS" or prior.disposed:
        raise SystemExit("review_gate: no open FINDINGS record for this diff to dispose")
    text = Path(args.file).read_text()
    if not dispositions_complete(text, prior.findings):
        raise SystemExit(f"review_gate: dispositions need a numbered entry for each of the "
                         f"{prior.findings} finding(s) — `1. <fixed in …|rebutted: why>` …")
    rec = Record(key, "FINDINGS", prior.findings, True, prior.reviewer)
    post_record(pr, rec, f"dispositions for FINDINGS({prior.findings}) — {prior.url}", text)
    print(f"[review] {rec.status()[1]}")
    return 0


def _dispose_emit(args) -> int:
    """`dispose --emit`: the disposition body, without gh.

    Offline there is no way to read the open FINDINGS record, so the caller
    names it: its finding count, its reviewer and its URL, all as shown on the
    PR. CI checks only the diff key, the count, and a native-review URL
    (`_cited_native_review`); a wrong count or key leaves the findings open.
    Any other cite, and the reviewer, are NOT verified: CI answers the latest
    open FINDINGS record with the same count, and the check shows whatever
    reviewer was passed. Copy both from the open record exactly.
    """
    missing = [f for f in ("findings", "reviewer", "cites") if getattr(args, f, None) in (None, "")]
    if missing:
        raise SystemExit("review_gate: dispose --emit needs --findings N, --reviewer NAME and "
                         "--cites URL of the open FINDINGS record (missing: " + ", ".join(missing) + ")")
    if args.findings < 1:
        raise SystemExit("review_gate: --findings must be at least 1")
    if not REVIEWER_NAME_RE.fullmatch(args.reviewer):
        raise SystemExit("review_gate: --reviewer must match [A-Za-z0-9_.:@/+-]+ "
                         "(the open record's reviewer, as its marker shows it)")
    if re.search(r"\s", args.cites):
        raise SystemExit("review_gate: --cites must be a single URL")
    text = Path(args.file).read_text()
    if not dispositions_complete(text, args.findings):
        raise SystemExit(f"review_gate: dispositions need a numbered entry for each of the "
                         f"{args.findings} finding(s) — `1. <fixed in …|rebutted: why>` …")
    rec = Record(_resolve_offline(args), "FINDINGS", args.findings, True, args.reviewer)
    sys.stdout.write(render_body(rec, f"dispositions for FINDINGS({args.findings}) — {args.cites}", text))
    print(f"[review] emitted, not posted: post the body above verbatim as a PR comment "
          f"({rec.status()[1]})", file=sys.stderr)
    return 0


BOT_AUTHORS = {"app/dependabot", "dependabot", "dependabot[bot]"}
SWEEP_MAX_FAILED = 3  # a FAILED review is retried, but not forever
SWEEP_HOLD_LABEL = "no-auto-review"


def failed_runs(comments: list[dict], key: str, reviewer: str | None = None) -> int:
    n = 0
    for c in comments:
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        rec = parse_record(c.get("body", ""))
        n += bool(rec and rec.key == key and rec.verdict == "FAILED"
                  and (reviewer is None or rec.reviewer == reviewer))
    return n


def sweep_candidates(prs: list[dict], owner: str, now: float, quiet_s: int) -> list[dict]:
    """Open PRs the sweep may review, oldest-updated first.

    - Drafts are included: review is how their owners reach readiness. Reading
      a diff never authorizes editing the branch, marking ready, or merging.
    - `no-auto-review` explicitly holds automatic review of unfinished work.
    - Only the owner's account and dependabot: a stranger's PR gets a human
      first, not a model running over its code on this machine.
    - Nothing updated in the last `quiet_s`: the pusher's own review may be
      about to start, and the lock only sees reviews already running.
    """
    from datetime import datetime

    out = []
    for p in prs:
        login = (p.get("author") or {}).get("login", "")
        if SWEEP_HOLD_LABEL in {label["name"] for label in p.get("labels", [])}:
            continue
        if login.lower() != owner.lower() and login not in BOT_AUTHORS:
            continue
        updated = datetime.fromisoformat(p["updatedAt"].replace("Z", "+00:00")).timestamp()
        if now - updated < quiet_s:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p["updatedAt"])


def cmd_sweep(args) -> int:
    """Review at most ONE PR per run: bounded cost, and launchd never overlaps
    a job with itself. Runs THIS script (the caller's trusted checkout) with
    its cwd in `--worktree`, detached at the PR head — the PR's own copy of
    this script is never executed."""
    repo = repo_slug()
    prs = gh_json("pr", "list", "--state", "open", "--limit", "100", "--json",
                  "number,isDraft,author,headRefOid,headRefName,baseRefName,updatedAt,labels")
    candidates = 0
    for p in sweep_candidates(prs, repo.split("/")[0], time.time(), args.quiet_minutes * 60):
        n, head, base = p["number"], p["headRefOid"], p["baseRefName"]
        git("fetch", "--quiet", "--no-tags", "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
            f"+refs/pull/{n}/head:refs/review-gate/pr-{n}")
        if git("rev-parse", f"refs/review-gate/pr-{n}").strip() != head:
            continue  # pushed since the listing; next run
        key = diff_key(f"origin/{base}", head)
        comments = pr_comments(repo, n)
        try:
            rec = current_record(repo, n, key, head, comments)
        except SystemExit as exc:
            print(f"[sweep] WARNING: native evidence unavailable: {exc}")
            continue  # cannot decide which findings remain open from partial evidence
        if rec is not None and not (rec.verdict == "FAILED" and any(
                failed_runs(comments, key, provider) < SWEEP_MAX_FAILED
                for provider in ("claude", "codex"))):
            if rec.status()[0] != "success":
                print(f"[sweep] PR #{n}: author follow-up needed — {rec.status()[1]} {rec.url}")
            elif not args.dry_run and rec.verdict == "CLEAN" and rec.reviewer == "codex-native":
                with review_lock(key) as lock:
                    if lock.held:
                        comments = pr_comments(repo, n)
                        rec = current_record(repo, n, key, head, comments)
                        if rec is not None:
                            finish_record(repo, n, key, head, rec, comments)
            continue  # report findings/retry exhaustion rather than silently skipping
        if review_lock(key).holder_alive():
            continue
        print(f"[sweep] PR #{n} ({p['headRefName']}) has no review for {key[:12]}")
        candidates += 1
        if args.dry_run:
            continue
        wt = Path(args.worktree).expanduser()
        if wt.exists():
            _run(["git", "-C", str(wt), "checkout", "--quiet", "--detach", head])
        else:
            git("worktree", "add", "--quiet", "--detach", str(wt), head)
        return subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--pr", str(n), "review"], cwd=wt).returncode
    print(f"[sweep] {candidates} review candidate(s)" if candidates else
          "[sweep] no reviews to start")
    return 0


def cmd_key(args) -> int:
    print(diff_key(args.base or DEFAULT_BASE, "HEAD"))
    return 0


def review_check(rec: Record | None) -> tuple[str, str]:
    if rec is None:
        return "neutral", "UNREVIEWED: author should run scripts/dev/review.sh (drafts included)"
    if rec.verdict == "FAILED":
        return "neutral", rec.status()[1]
    if rec.verdict == "FINDINGS" and not rec.disposed:
        return "action_required", rec.status()[1]
    return "success", rec.status()[1]


def post_check(repo: str, pr: int, head: str, conclusion: str, description: str,
               url: str) -> None:
    """Use neutral for outages; commit statuses cannot represent a warning."""
    external_id = f"unitares-review:{pr}:{head}"
    pages = gh_json("api", "--paginate", "--slurp",
                    f"repos/{repo}/commits/{head}/check-runs?check_name={STATUS_CONTEXT}&filter=all")
    existing = [c for page in pages for c in page.get("check_runs", [])
                if c.get("external_id") == external_id
                and (c.get("app") or {}).get("slug") == "github-actions"]
    payload = {"name": STATUS_CONTEXT, "head_sha": head, "external_id": external_id,
               "status": "completed", "conclusion": conclusion,
               "output": {"title": description, "summary": f"{description}\n\n{url}"}}
    if url:
        payload["details_url"] = url
    endpoint = f"repos/{repo}/check-runs"
    method = "POST"
    if existing:
        endpoint += f"/{existing[0]['id']}"
        method = "PATCH"
        payload.pop("head_sha")
    _launch(["gh", "api", "--method", method, endpoint, "--input", "-"],
            input=json.dumps(payload), text=True, capture_output=True, check=True)


# --------------------------------------------------------------------------
# CI side: never executes PR code. The workflow checks out the BASE branch
# (this script) and fetches the PR head only as git objects to hash.


def round_note(rounds: CodexRounds | None) -> str:
    if not rounds or not rounds.count:
        return ""
    note = f" · review round {rounds.count} of {ROUND_CAP}"
    return note + (" (cap reached)" if rounds.count >= ROUND_CAP else "")


def cmd_ci(args) -> int:
    repo, pr = args.repo, args.pr
    info = gh_json("api", f"repos/{repo}/pulls/{pr}")
    if info.get("state") != "open":
        print(f"PR #{pr} is not open; nothing to gate")
        return 0
    head = info["head"]["sha"]
    base_ref = info["base"]["ref"]
    git("fetch", "--quiet", "--no-tags", "origin",
        f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
        f"+refs/pull/{pr}/head:refs/review-gate/head")
    key = diff_key(f"origin/{base_ref}", head)
    comments = pr_comments(repo, pr)
    try:
        snapshot = read_native(repo, pr, key, head, comments)
        rec = latest_matching(comments, key, snapshot.records)
    except SystemExit as exc:
        print(f"::warning::Review evidence incomplete: {exc}. Existing review check preserved; "
              "retry this workflow when GitHub evidence is readable.")
        # Do not overwrite an existing action_required with success/neutral
        # derived from only the issue-comment portion of the review history.
        return 0
    if rec is None:
        url = f"https://github.com/{repo}/blob/{base_ref}/docs/operations/github-workflow-conventions.md#review-workflow"
    else:
        url = rec.url
    conclusion, desc = review_check(rec)
    desc += round_note(snapshot.rounds)
    print(f"PR #{pr} head {head[:12]} key {key[:12]}: {conclusion} — {desc}")
    if conclusion == "neutral":
        print(f"::warning::{desc}")
    if args.post_status:
        post_check(repo, pr, head, conclusion, desc, url)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default=None,
                   help="base ref to key against (default: the PR's base branch on origin)")
    p.add_argument("--pr", type=int, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review HEAD's diff and post the record")
    r.add_argument("--reviewer", choices=sorted(KNOWN_PROVIDERS))
    r.add_argument("--budget", type=int, default=DEFAULT_BUDGET_S)
    r.add_argument("--fresh", action="store_true", help="ignore an existing record")

    rc = sub.add_parser("record", help="post an externally performed review")
    rc.add_argument("file")
    rc.add_argument("--reviewer-name", required=True)
    rc.add_argument("--independent", action="store_true",
                    help="attest this is a separate review of the current diff, not the author's self-check")
    # SUPPRESS keeps the top-level --base when this one is not given.
    rc.add_argument("--base", default=argparse.SUPPRESS,
                    help="base ref for --emit's key (default origin/master); must be the PR's base")
    rc.add_argument("--emit", action="store_true",
                    help="print the record body instead of posting it (no gh needed); "
                         "post it verbatim with any GitHub client")

    d = sub.add_parser("dispose", help="post dispositions for a FINDINGS record")
    d.add_argument("file")
    d.add_argument("--base", default=argparse.SUPPRESS,
                   help="base ref for --emit's key (default origin/master); must be the PR's base")
    d.add_argument("--emit", action="store_true",
                   help="print the disposition body instead of posting it (no gh needed); "
                        "needs --findings, --reviewer and --cites from the open record")
    d.add_argument("--findings", type=int, help="with --emit: the open record's finding count")
    d.add_argument("--reviewer", help="with --emit: the open record's reviewer")
    d.add_argument("--cites", help="with --emit: the open record's URL (comment or native review)")

    sub.add_parser("key", help="print the diff key for HEAD")

    sw = sub.add_parser("sweep", help="review one quiet PR, including drafts (scheduled)")
    sw.add_argument("--worktree", required=True,
                    help="worktree to check PR heads out into (created if missing)")
    sw.add_argument("--quiet-minutes", type=int, default=15)
    sw.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("ci", help="workflow: set the review evidence check on a PR head")
    c.add_argument("--repo", required=True)
    c.add_argument("--post-check", "--post-status", dest="post_status", action="store_true",
                   help="publish a review check (legacy --post-status spelling is accepted)")

    args = p.parse_args(argv)
    if args.cmd == "ci" and args.pr is None:
        p.error("ci needs --pr")
    try:
        return {"review": cmd_review, "record": cmd_record, "dispose": cmd_dispose,
                "key": cmd_key, "ci": cmd_ci, "sweep": cmd_sweep}[args.cmd](args)
    except ClosedPullRequest as exc:
        print(f"[review] {exc}")
        return 0
    except GhUnavailable as exc:
        # Nothing was reviewed: infrastructure unavailable (exit 2), not
        # findings needing author action (exit 1), which is what the uncaught
        # FileNotFoundError traceback used to report.
        print(f"[review] UNREVIEWED: {exc}, so review evidence can be neither "
              "read nor posted from here. Run review.sh where gh is available, "
              "request native review (`@codex review` on the PR), or run an "
              "independent review and render its record with `review.sh record "
              "FILE --reviewer-name NAME --independent --emit`, then post the "
              "printed body verbatim through your GitHub connector "
              "(docs/operations/github-workflow-conventions.md, "
              "#recording-a-review-without-gh).")
        return UNREVIEWED


if __name__ == "__main__":
    sys.exit(main())
