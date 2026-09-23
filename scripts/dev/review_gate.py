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
    ci             (workflow only) set the `review` commit status on a PR head

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
    FINDINGS(n) without            -> pending   (fix, or `dispose`)
    FAILED                         -> pending   (UNREVIEWED: reviewer unavailable)
    no matching record             -> pending   (run the review)

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
The CI side reads comments with GITHUB_TOKEN and calls no model. The review
itself runs locally through a CLI the operator already has (`codex`,
`claude`); `record` takes any review text, so a contributor with no model at
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
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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

The repository's house rules are in AGENTS.md; a violation of one is a finding.

End with exactly one line and nothing after it:
VERDICT: CLEAN
or
VERDICT: FINDINGS(<number of findings>)
"""


# --------------------------------------------------------------------------
# git / gh plumbing


def _run(cmd: list[str], *, check: bool = True, cwd: str | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"review_gate: {' '.join(cmd[:3])}… failed: {proc.stderr.strip()}")
    return proc.stdout


def git(*args: str, check: bool = True) -> str:
    return _run(["git", *args], check=check)


def gh_json(*args: str):
    return json.loads(_run(["gh", *args]))


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


def latest_matching(comments: list[dict], key: str) -> Record | None:
    """The record that decides `key`'s status. Comments arrive oldest first.

    Normally the latest trusted record. But findings on a diff stay open until
    they are disposed or the diff changes: a later CLEAN or FAILED on the SAME
    diff — a re-run that came back quieter or crashed, or a `record` — does not
    clear or hide them, or re-rolling the reviewer would drop a finding silently.
    """
    found, open_findings = None, []
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
            found = rec
            if rec.verdict == "FINDINGS":
                # A disposition answers ONE findings record — the most recent
                # open one, with the same count — never every earlier one.
                if not rec.disposed:
                    open_findings.append(rec)
                elif open_findings and open_findings[-1].findings == rec.findings:
                    open_findings.pop()
                else:
                    rec.disposed = False  # answers nothing that is open
                    open_findings.append(rec)
    # Open findings decide the status whatever came after them on this diff —
    # a quieter re-run, a FAILED re-run — so they stay visible and disposable.
    return open_findings[-1] if open_findings else found


def pr_comments(repo: str, pr: int) -> list[dict]:
    pages = gh_json("api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments")
    return [c for page in pages for c in page]


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
    proc = subprocess.run(["gh", "pr", "view", "--json",
                           "number,headRefOid,headRefName,baseRefName"],
                          text=True, capture_output=True)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def default_reviewer(branch: str) -> str:
    # Prefer diversity, but independence is a fresh reviewer context, not a
    # provider name. A quota outage must not prohibit the available reviewer.
    return "claude" if branch.startswith("codex/") else "codex"


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
        "quota": ("weekly limit", "usage limit", "rate limit", "rate_limit", "quota"),
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


def run_reviewer(reviewer: str, prompt: str, out_dir: Path, budget_s: int) -> tuple[str, str]:
    """Return (final text, status note). Never raises on reviewer failure."""
    last = (out_dir / "last-message.txt").resolve()
    # A previous run's output must never stand in for this run's: a --fresh
    # claude run would otherwise post the old codex verdict.
    last.unlink(missing_ok=True)
    if reviewer == "codex":
        cmd = ["codex", "exec", "--sandbox", "read-only", "-C", os.getcwd(),
               "--output-last-message", str(last), prompt]
    elif reviewer == "claude":
        cmd = ["claude", "-p", prompt,
               "--allowedTools", "Read", "Grep", "Glob",
               "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
    else:
        raise SystemExit(f"review_gate: unknown reviewer {reviewer!r}")

    log = out_dir / "reviewer.log"
    with open(log, "w") as fh:
        # stdin=DEVNULL: codex blocks reading an open non-TTY stdin.
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:  # reviewer CLI missing or not executable
            return str(exc), f"could not start {reviewer}: {exc.__class__.__name__}"
        try:
            rc = proc.wait(timeout=budget_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return log.read_text(errors="replace"), f"killed at the {budget_s}s budget"
    text = last.read_text(errors="replace") if last.exists() else log.read_text(errors="replace")
    return text, ("exit 0" if rc == 0 else f"exit {rc}")


def post_record(pr: int, rec: Record, heading: str, text: str) -> None:
    body = f"{render_marker(rec)}\n### Review record — {heading}\n\n"
    body += f"Diff key `{rec.key[:12]}` · reviewer `{rec.reviewer}`\n\n"
    if len(text) > COMMENT_LIMIT:
        text = text[:COMMENT_LIMIT] + "\n\n… (truncated; full text in the local .review-cache)"
    body += text.strip() + "\n"
    subprocess.run(["gh", "pr", "comment", str(pr), "--body-file", "-"],
                   input=body, text=True, check=True, capture_output=True)


def _resolve(args) -> tuple[int, str, str, str]:
    """PR number, repo, key, reviewer label for HEAD. Refuses a stale push."""
    info = current_pr() if args.pr is None else gh_json(
        "pr", "view", str(args.pr), "--json", "number,headRefOid,headRefName,baseRefName")
    if info is None:
        raise SystemExit("review_gate: no PR for this branch — ship it first (ship.sh opens one)")
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
    deadline = time.monotonic() + args.budget
    joined = False
    while True:
        with review_lock(key) as lock:
            if lock.held:
                # Re-read AFTER acquiring: a ship/sweep review may have posted
                # while we waited. Joining it must return its actual result.
                comments = pr_comments(repo, pr)
                existing = (None if args.fresh and not joined else
                            latest_matching(comments, key))
                if existing and (joined or existing.verdict != "FAILED"):
                    state, desc = existing.status()
                    print(f"[review] already recorded for this diff: {desc}\n{existing.url}")
                    print(existing.text)
                    if existing.verdict == "FAILED":
                        return UNREVIEWED
                    return 0 if state == "success" else 1
                args.failed_providers = {p for p in ("claude", "codex")
                                         if failed_runs(comments, key, p) >= SWEEP_MAX_FAILED}
                return review_with_fallback(args, pr, key, reviewer)
        if not joined:
            print("[review] joining the review already running for this diff…", flush=True)
            joined = True
        if time.monotonic() >= deadline:
            print("[review] still running; no completed review joined. "
                  "Run scripts/dev/review.sh again before marking ready.")
            return UNREVIEWED
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def review_with_fallback(args, pr: int, key: str, preferred: str) -> int:
    """At most two independent attempts, sharing one wall-clock budget.

    Findings stop routing: trying another model must never erase a review we
    dislike. An explicit --reviewer retries that provider despite cooldown.
    """
    providers = [preferred, "codex" if preferred == "claude" else "claude"]
    deadline = time.monotonic() + args.budget
    available = []
    for provider in providers:
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
          "an independent code review with record <file> --reviewer-name <who>.")
    return UNREVIEWED


def _review_locked(args, pr: int, key: str, reviewer: str) -> int:
    out_dir = Path(CACHE_DIR) / key / reviewer
    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = (out_dir / "diff.txt").resolve()
    diff_path.write_text(diff_text(args.base, "HEAD"))
    prompt = REVIEW_PROMPT.format(diff_path=diff_path, base=args.base,
                                  head=git("rev-parse", "--short", "HEAD").strip())
    print(f"[review] {reviewer} reviewing PR #{pr} (key {key[:12]}, budget {args.budget}s)…",
          flush=True)
    t0 = time.monotonic()
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
              "scripts/dev/review.sh record <file> --reviewer-name <who>")
    state, desc = rec.status()
    print(f"[review] {desc}")
    if rec.verdict == "FINDINGS":
        print("[review] fix and push (the next run reviews the new diff), or record "
              "dispositions: scripts/dev/review.sh dispose <file>")
    return UNREVIEWED if parsed is None else (0 if state == "success" else 1)


def cmd_record(args) -> int:
    pr, repo, key, branch = _resolve(args)
    if not args.independent:
        raise SystemExit("review_gate: record requires --independent to attest that a "
                         "separate reviewer examined this diff. Consult advice alone is not a code review.")
    text = Path(args.file).read_text()
    parsed = parse_verdict(text)
    if parsed is None:
        raise SystemExit("review_gate: the review text needs a final "
                         "'VERDICT: CLEAN' or 'VERDICT: FINDINGS(n)' line")
    verdict, n = parsed
    rec = Record(key, verdict, n, False, args.reviewer_name)
    post_record(pr, rec, f"{verdict if verdict == 'CLEAN' else f'FINDINGS({n})'} "
                f"(recorded, reviewed by {args.reviewer_name})", text)
    print(f"[review] recorded: {rec.status()[1]}")
    return 0


def cmd_dispose(args) -> int:
    pr, repo, key, _ = _resolve(args)
    prior = latest_matching(pr_comments(repo, pr), key)
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
        rec = latest_matching(comments, key)
        if rec is not None and not (rec.verdict == "FAILED" and any(
                failed_runs(comments, key, provider) < SWEEP_MAX_FAILED
                for provider in ("claude", "codex"))):
            if rec.status()[0] != "success":
                print(f"[sweep] PR #{n}: author follow-up needed — {rec.status()[1]} {rec.url}")
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


# --------------------------------------------------------------------------
# CI side: never executes PR code. The workflow checks out the BASE branch
# (this script) and fetches the PR head only as git objects to hash.


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
    rec = latest_matching(pr_comments(repo, pr), key)
    if rec is None:
        state, desc = "pending", "author: run scripts/dev/review.sh to start/join review (drafts included)"
        url = f"https://github.com/{repo}/blob/{base_ref}/docs/operations/github-workflow-conventions.md#review-workflow"
    else:
        (state, desc), url = rec.status(), rec.url
    print(f"PR #{pr} head {head[:12]} key {key[:12]}: {state} — {desc}")
    if args.post_status:
        fields = ["-f", f"state={state}", "-f", f"context={STATUS_CONTEXT}",
                  "-f", f"description={desc[:140]}"]
        if url:
            fields += ["-f", f"target_url={url}"]
        _run(["gh", "api", "--method", "POST", f"repos/{repo}/statuses/{head}", *fields])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default=None,
                   help="base ref to key against (default: the PR's base branch on origin)")
    p.add_argument("--pr", type=int, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review HEAD's diff and post the record")
    r.add_argument("--reviewer", choices=["codex", "claude"])
    r.add_argument("--budget", type=int, default=DEFAULT_BUDGET_S)
    r.add_argument("--fresh", action="store_true", help="ignore an existing record")

    rc = sub.add_parser("record", help="post an externally performed review")
    rc.add_argument("file")
    rc.add_argument("--reviewer-name", required=True)
    rc.add_argument("--independent", action="store_true",
                    help="attest this is a separate review of the current diff, not the author's self-check")

    d = sub.add_parser("dispose", help="post dispositions for a FINDINGS record")
    d.add_argument("file")

    sub.add_parser("key", help="print the diff key for HEAD")

    sw = sub.add_parser("sweep", help="review one quiet PR, including drafts (scheduled)")
    sw.add_argument("--worktree", required=True,
                    help="worktree to check PR heads out into (created if missing)")
    sw.add_argument("--quiet-minutes", type=int, default=15)
    sw.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("ci", help="workflow: set the review status on a PR head")
    c.add_argument("--repo", required=True)
    c.add_argument("--post-status", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "ci" and args.pr is None:
        p.error("ci needs --pr")
    return {"review": cmd_review, "record": cmd_record, "dispose": cmd_dispose,
            "key": cmd_key, "ci": cmd_ci, "sweep": cmd_sweep}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
