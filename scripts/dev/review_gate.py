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
    FAILED                         -> failure   (the reviewer did not finish)
    no matching record             -> pending   (run the review)

A failed or expired run is recorded as FAILED, never as clean: absence of
findings from a reviewer that did not finish is not a review.

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
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MARKER = "unitares-review v1"
STATUS_CONTEXT = "review"
CACHE_DIR = ".review-cache"
DEFAULT_BASE = "origin/master"
DEFAULT_BUDGET_S = 1800  # pipeline skill: clean codex completions ran 1-21 min
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
COMMENT_LIMIT = 60000  # GitHub caps a comment body at 65536 chars

VERDICT_RE = re.compile(r"^\s*\**VERDICT:\s*(CLEAN|FINDINGS\((\d+)\))\**\s*$", re.M)
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
    raw = git("-c", "core.quotePath=true", "diff", "--raw", "--no-renames",
              "--full-index", "--no-abbrev", "--no-ext-diff", "-z", mb, head)
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


def diff_text(base: str, head: str) -> str:
    return git("diff", "--no-color", "--no-ext-diff", f"{base}...{head}")


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

    def status(self) -> tuple[str, str]:
        if self.verdict == "CLEAN":
            return "success", f"clean ({self.reviewer})"
        if self.verdict == "FINDINGS" and self.disposed:
            return "success", f"{self.findings} finding(s) disposed ({self.reviewer})"
        if self.verdict == "FINDINGS":
            return "pending", f"{self.findings} finding(s) need fixes or dispositions"
        return "failure", f"review did not finish ({self.reviewer}); re-run it"


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
    """Latest trusted record for `key`. Comments arrive oldest first."""
    found = None
    for c in comments:
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        body = c.get("body", "")
        rec = parse_record(body)
        if rec and rec.key == key and rec.verdict in {"CLEAN", "FINDINGS", "FAILED"}:
            if rec.disposed and not dispositions_complete(body, rec.findings):
                rec.disposed = False
            rec.url = c.get("html_url", "")
            found = rec
    return found


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
    matches = VERDICT_RE.findall(text or "")
    if not matches:
        return None
    word, n = matches[-1]
    return ("CLEAN", 0) if word == "CLEAN" else ("FINDINGS", int(n))


# --------------------------------------------------------------------------
# local side


def repo_slug() -> str:
    return gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]


def current_pr() -> dict | None:
    proc = subprocess.run(["gh", "pr", "view", "--json", "number,headRefOid,headRefName"],
                          text=True, capture_output=True)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def default_reviewer(branch: str) -> str:
    # Heterogeneous by construction: a model does not review its own work.
    return "claude" if branch.startswith("codex/") else "codex"


def run_reviewer(reviewer: str, prompt: str, out_dir: Path, budget_s: int) -> tuple[str, str]:
    """Return (final text, status note). Never raises on reviewer failure."""
    last = (out_dir / "last-message.txt").resolve()
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
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
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
    remote, _, branch = args.base.partition("/")
    git("fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{args.base}",
        check=False)
    head = git("rev-parse", "HEAD").strip()
    key = diff_key(args.base, head)
    info = current_pr() if args.pr is None else gh_json(
        "pr", "view", str(args.pr), "--json", "number,headRefOid,headRefName")
    if info is None:
        raise SystemExit("review_gate: no PR for this branch — ship it first (ship.sh opens one)")
    pushed = info["headRefOid"]
    if pushed != head:
        known = subprocess.run(["git", "cat-file", "-e", f"{pushed}^{{commit}}"],
                               capture_output=True).returncode == 0
        if not known or diff_key(args.base, pushed) != key:
            raise SystemExit("review_gate: local HEAD differs from the PR head — push first, "
                             "or the record would describe a diff CI never sees")
    return info["number"], repo_slug(), key, info["headRefName"]


def cmd_review(args) -> int:
    pr, repo, key, branch = _resolve(args)
    reviewer = args.reviewer or default_reviewer(branch)
    existing = None if args.fresh else latest_matching(pr_comments(repo, pr), key)
    if existing and existing.verdict != "FAILED":
        state, desc = existing.status()
        print(f"[review] already recorded for this diff: {desc}\n{existing.url}")
        return 0 if state == "success" else 1

    out_dir = Path(CACHE_DIR) / key
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
        rec = Record(key, "FAILED", 0, False, reviewer)
        heading = f"FAILED ({note}, no VERDICT line)" if note == "exit 0" else f"FAILED ({note})"
    else:
        verdict, n = parsed
        rec = Record(key, verdict, n, False, reviewer)
        heading = "CLEAN" if verdict == "CLEAN" else f"FINDINGS({n})"
    heading += f" · {minutes:.1f} min"
    (out_dir / "review.txt").write_text(text)
    post_record(pr, rec, heading, text)
    state, desc = rec.status()
    print(f"[review] {desc}")
    if rec.verdict == "FINDINGS":
        print("[review] fix and push (the next run reviews the new diff), or record "
              "dispositions: scripts/dev/review.sh dispose <file>")
    return 0 if state == "success" else 1


def cmd_record(args) -> int:
    pr, repo, key, _ = _resolve(args)
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
    if prior is None or prior.verdict != "FINDINGS":
        raise SystemExit("review_gate: no FINDINGS record for this diff to dispose")
    text = Path(args.file).read_text()
    if not dispositions_complete(text, prior.findings):
        raise SystemExit(f"review_gate: dispositions need a numbered entry for each of the "
                         f"{prior.findings} finding(s) — `1. <fixed in …|rebutted: why>` …")
    rec = Record(key, "FINDINGS", prior.findings, True, prior.reviewer)
    post_record(pr, rec, f"dispositions for FINDINGS({prior.findings}) — {prior.url}", text)
    print(f"[review] {rec.status()[1]}")
    return 0


def cmd_key(args) -> int:
    print(diff_key(args.base, "HEAD"))
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
        state, desc, url = "pending", "no review for this diff yet — run scripts/dev/review.sh", ""
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
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--pr", type=int, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review HEAD's diff and post the record")
    r.add_argument("--reviewer", choices=["codex", "claude"])
    r.add_argument("--budget", type=int, default=DEFAULT_BUDGET_S)
    r.add_argument("--fresh", action="store_true", help="ignore an existing record")

    rc = sub.add_parser("record", help="post an externally performed review")
    rc.add_argument("file")
    rc.add_argument("--reviewer-name", required=True)

    d = sub.add_parser("dispose", help="post dispositions for a FINDINGS record")
    d.add_argument("file")

    sub.add_parser("key", help="print the diff key for HEAD")

    c = sub.add_parser("ci", help="workflow: set the review status on a PR head")
    c.add_argument("--repo", required=True)
    c.add_argument("--post-status", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "ci" and args.pr is None:
        p.error("ci needs --pr")
    return {"review": cmd_review, "record": cmd_record, "dispose": cmd_dispose,
            "key": cmd_key, "ci": cmd_ci}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
