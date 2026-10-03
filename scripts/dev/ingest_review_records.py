#!/usr/bin/env python3
"""Carry review-gate records and their dispositions into the UNITARES knowledge graph.

WHAT THIS IS
    An operator-side producer, the sibling of ``ingest_ci_findings.py``. It
    reads the review records ``scripts/dev/review_gate.py`` posts on pull
    requests (the ``unitares-review v1`` marker comment) together with the
    disposition record that answers each one, and stores each closed pull
    request's review history as one knowledge-graph observation: every round,
    its reviewer and verdict, and for each finding the author's disposition of
    it (accepted, rebutted, unclassified, superseded or undisposed), with the
    disposition text kept verbatim.

THE LEVER IT REPAIRS
    A review record is a claim (a reviewer says this diff has defects), and its
    disposition is what became of the claim (the author fixed it, or argued it
    was not a defect). Both live only in GitHub comments, so reconstructing
    what a review found and what happened to it means re-scraping pull
    requests. That is how the 2026-09-26 reviewer comparison had to be built.
    This producer puts the pair into the record, where a successor can search
    it.

WHAT THE DISPOSITIONS ARE, AND ARE NOT
    A disposition is written by the pull request's authoring agent through
    ``review.sh dispose``. It is that agent's own account of a finding against
    its own work: **self-attested**, not exogenous. This producer therefore
    writes knowledge-graph observations only. It never calls ``outcome_event``,
    so nothing it writes reaches ``audit.outcome_events``, the anchor tiers in
    ``src/grounding/outcome_anchors.py``, calibration, or the registered EISV
    outcome-grounding read
    (``docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md``).
    Every stored entry says so in its details. Promoting any of it into an
    outcome label would need an independent verifier; it is not a new label
    channel as it stands.

IDENTITY
    The review gate records a reviewer *name* (``codex``, ``claude``,
    ``antigravity``, a subagent label) and GitHub records the pull request's
    author login. Neither is a UNITARES identity, and this producer does not
    infer one: both are stored as text, and the entry states that no agent
    identity was recorded. Like ``ingest_ci_findings.py`` it never onboards;
    the write is attributed to whatever identity the REST call resolves, and on
    a deployment with the strict write gate armed the refusal ends the run.

WHICH RECORDS
    One entry per pull request, written once the pull request is merged or
    closed, when its history is final. An open pull request is skipped and
    counted: a later round or disposition would otherwise have to amend a
    stored entry. CLEAN and FAILED rounds stay in the history, because a clean
    verdict is also a claim a later defect can contradict. Native Codex
    reviews (inline review threads) are not read by this version.

    One entry per pull request rather than per record keeps the graph's volume
    to what a successor would search for: a dry run over 30 merged pull
    requests on 2026-09-26 found 115 FINDINGS records, most of them earlier
    rounds of the same change that were superseded rather than disposed.

    Every stored entry lands with ``status='resolved'``: it is a historical
    record, not open work, and must not reappear in sweeps for unfinished
    entries. The producer reads the entry back and fails if the server did not
    keep that status.

WHY IT FAILS LOUD
    Same contract as ``ingest_ci_findings.py``, whose REST envelope, refusal
    checks and search-row handling are imported rather than copied: a gh
    failure, a transport error, a tool-level refusal inside an HTTP 200, an
    identity refusal, an unrecognized search shape, a store with no
    ``discovery_id``, or a status the server did not keep all end the run
    non-zero after printing the tally.

USAGE
    python3 scripts/dev/ingest_review_records.py [--repo owner/name]
        [--state merged|open|closed|all] [--limit N] [--pr N ...]
        [--rest-url URL] [--timeout S] [--dry-run | --apply]

    Dry run is the default. ``--apply`` is required to store. ``--limit`` is
    a window (the N most recent pull requests), not a truncation guard.

ENVIRONMENT
    GITHUB_REPOSITORY          default for --repo
    GOV_REST_URL               default for --rest-url
    UNITARES_MCP_BEARER_TOKEN  bearer token, sent only when set

Reads GitHub through the ``gh`` CLI only; no metered model API on any path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest_ci_findings as ci  # noqa: E402  (sibling module, same directory)
import review_gate as gate  # noqa: E402

PRODUCER_NAME = "scripts/dev/ingest_review_records.py"
IngestError = ci.IngestError

DEFAULT_PR_LIMIT = 50
PR_JSON_FIELDS = "number,title,url,state,author,mergedAt,closedAt"

STORE_DISCOVERY_TYPE = "observation"
# Low on purpose: a record of a review is not an impact claim, and the
# producer has no standing to grade the findings it carries.
STORE_SEVERITY = "low"
STORE_STATUS = "resolved"

RECORD_MARKER_NAME = "review-record"
STATIC_TAGS = ("review-record", "review-gate", "github")
SEARCH_LIMIT = 50
MAX_REVIEW_TEXT_CHARS = 3000
MAX_DISPOSITION_CHARS = 400
MAX_SUMMARY_CHARS = ci.MAX_SUMMARY_CHARS
# Well under the server's MAX_DETAILS_LEN (64 KiB); the round list always fits,
# the quoted review texts are what gets trimmed.
MAX_DETAILS_CHARS = 48_000

# The heading `review.sh dispose` writes, naming the record it answers
# (review_gate.post_record; the native form is review_gate._NATIVE_DISPOSITION_RE).
_DISPOSITION_HEADING_RE = re.compile(r"dispositions for FINDINGS\((\d+)\) — (\S+)")
# A numbered item at the start of a line, optionally bolded: "1. ...", "**2.** ...".
_ITEM_RE = re.compile(r"^[ \t]*\**(\d+)\.\**[ \t]+(.*)$")
_SEVERITY_PREFIX_RE = re.compile(r"^\s*\[P[0-3]\]\s*", re.IGNORECASE)
_ACCEPT_RE = re.compile(r"\b(fixed|accepted|accept|confirmed|agreed)\b", re.IGNORECASE)
_REBUT_RE = re.compile(r"\brebut(?:ted|tal)?\b", re.IGNORECASE)

DISPOSITION_PROVENANCE = (
    "self-attested: written by the pull request's authoring agent through "
    "review.sh dispose. Not exogenous, not an outcome_event, not a label for "
    "the registered EISV outcome-grounding read."
)


# --- GitHub side ------------------------------------------------------------


def _gh(argv: list[str], what: str) -> str:
    try:
        proc = subprocess.run(
            argv, check=True, capture_output=True, text=True,
            timeout=ci.GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise IngestError("gh CLI not found on PATH — this producer reads GitHub through gh") from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"{what} timed out after {ci.GH_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "no output"
        raise IngestError(f"{what} failed (exit {exc.returncode}): {detail}") from exc
    return proc.stdout or ""


def gh_pulls(repo: str, state: str, limit: int) -> list[dict]:
    out = _gh(
        ["gh", "pr", "list", "-R", repo, "--state", state,
         "--json", PR_JSON_FIELDS, "--limit", str(limit)],
        "gh pr list",
    )
    try:
        pulls = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise IngestError(f"gh pr list returned non-JSON output: {out[:200]!r}") from exc
    if not isinstance(pulls, list):
        raise IngestError("gh pr list did not return a JSON array")
    return [pull for pull in pulls if isinstance(pull, dict)]


def gh_pull(repo: str, number: int) -> dict:
    out = _gh(
        ["gh", "pr", "view", str(number), "-R", repo, "--json", PR_JSON_FIELDS],
        f"gh pr view {number}",
    )
    try:
        pull = json.loads(out)
    except json.JSONDecodeError as exc:
        raise IngestError(f"gh pr view {number} returned non-JSON output") from exc
    if not isinstance(pull, dict):
        raise IngestError(f"gh pr view {number} did not return a JSON object")
    return pull


def gh_comments(repo: str, number: int) -> list[dict]:
    """Every issue comment on the pull request, oldest first, one per line."""
    out = _gh(
        ["gh", "api", f"repos/{repo}/issues/{number}/comments", "--paginate",
         "--jq", ".[] | @json"],
        f"gh api comments for #{number}",
    )
    comments = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(f"comment on #{number} was not JSON: {line[:120]!r}") from exc
        if isinstance(item, dict):
            comments.append(item)
    return comments


# --- records and dispositions ----------------------------------------------


def classify_disposition(text: str) -> str:
    """Coarse class of one disposition item: the author's earliest keyword.

    ``accepted`` covers "fixed", "accepted", "deferred (accepted)" and
    "confirmed"; ``rebutted`` is an argued non-defect. The earliest keyword
    wins, so "Accepted, not rebutted" is accepted and "rebutted, fixed in
    #2414" is rebutted, as the author led with it. Anything else is
    ``unclassified`` and keeps its text: this is a reading aid, never a
    judgement the producer makes on the author's behalf.
    """
    lead = _SEVERITY_PREFIX_RE.sub("", text or "")
    rebut = _REBUT_RE.search(lead)
    accept = _ACCEPT_RE.search(lead)
    if rebut and (not accept or rebut.start() < accept.start()):
        return "rebutted"
    if accept:
        return "accepted"
    return "unclassified"


def parse_disposition_items(body: str, count: int) -> dict[int, str]:
    """Item number -> text for items 1..count, continuation lines joined."""
    items: dict[int, list[str]] = {}
    current: int | None = None
    for line in (body or "").splitlines():
        match = _ITEM_RE.match(line)
        if match and 1 <= int(match.group(1)) <= count and int(match.group(1)) not in items:
            current = int(match.group(1))
            items[current] = [match.group(2).strip()]
            continue
        if current is not None:
            if not line.strip() or line.startswith(("---", "_Generated")):
                current = None
                continue
            items[current].append(line.strip())
    return {k: " ".join(v).strip() for k, v in items.items()}


def trusted(comment: dict) -> bool:
    return comment.get("author_association") in gate.TRUSTED_ASSOCIATIONS


def review_records(comments: list[dict]) -> list[dict]:
    """Pair each FINDINGS/CLEAN/FAILED record with the disposition answering it.

    Only trusted comments count, as in the gate itself. A disposition answers
    a record when it carries the same diff key and its heading cites that
    record's comment URL.
    """
    parsed = []
    for comment in comments:
        if not trusted(comment):
            continue
        record = gate.parse_record(comment.get("body") or "")
        if record is None:
            continue
        parsed.append((comment, record))

    dispositions: dict[tuple[str, str], tuple[dict, int]] = {}
    for comment, record in parsed:
        heading = _DISPOSITION_HEADING_RE.search(comment.get("body") or "")
        if heading:
            dispositions[(record.key, heading.group(2))] = (comment, int(heading.group(1)))

    out = []
    for comment, record in parsed:
        body = comment.get("body") or ""
        if _DISPOSITION_HEADING_RE.search(body):
            continue
        url = comment.get("html_url") or ""
        answer = dispositions.get((record.key, url))
        items: dict[int, str] = {}
        if answer is not None and answer[1] == record.findings:
            items = parse_disposition_items(answer[0].get("body") or "", record.findings)
        out.append({
            "record": record,
            "comment": comment,
            "disposition_comment": answer[0] if answer else None,
            "items": items,
        })
    return out


def history_marker(repo: str, pr: int) -> str:
    return f"{RECORD_MARKER_NAME}: {repo}#{pr}"


def pr_tag(pr: int) -> str:
    return f"review-pr-{pr}"


def derive_tags(reviewers: list[str], pr: int) -> list[str]:
    tags = list(STATIC_TAGS)
    for reviewer in reviewers:
        reviewer_tag = ci.normalize_tag(f"reviewer-{reviewer}")
        if reviewer_tag and reviewer_tag not in ci._FORBIDDEN_TAGS and reviewer_tag not in tags:
            tags.append(reviewer_tag)
    tags.append(pr_tag(pr))
    return tags


def _pr_author(pull: dict) -> str:
    author = pull.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "unknown")
    return str(author or "unknown")


def superseded(entries: list[dict], index: int) -> bool:
    """A later round reviewed a different diff, so the author pushed a change
    after this round instead of disposing it. That is what the gate asks for
    when a finding is fixed, but a push is not proof that it fixed this one."""
    key = entries[index]["record"].key
    return any(later["record"].key != key for later in entries[index + 1:])


def finding_class(entries: list[dict], index: int, n: int) -> str:
    text = entries[index]["items"].get(n)
    if text:
        return classify_disposition(text)
    return "superseded" if superseded(entries, index) else "undisposed"


def disposition_tally(entries: list[dict]) -> dict[str, int]:
    tally = {"accepted": 0, "rebutted": 0, "unclassified": 0, "superseded": 0, "undisposed": 0}
    for index, entry in enumerate(entries):
        if entry["record"].verdict != "FINDINGS":
            continue
        for n in range(1, entry["record"].findings + 1):
            tally[finding_class(entries, index, n)] += 1
    return tally


def reviewers_of(entries: list[dict]) -> list[str]:
    seen: list[str] = []
    for entry in entries:
        if entry["record"].reviewer not in seen:
            seen.append(entry["record"].reviewer)
    return seen


def build_summary(pull: dict, entries: list[dict]) -> str:
    t = disposition_tally(entries)
    findings = sum(t.values())
    summary = (
        f"Review history PR #{pull.get('number')}: {len(entries)} round(s) "
        f"({', '.join(reviewers_of(entries))}), {findings} finding(s): "
        + ", ".join(f"{v} {k}" for k, v in t.items() if v)
        if findings else
        f"Review history PR #{pull.get('number')}: {len(entries)} round(s) "
        f"({', '.join(reviewers_of(entries))}), no findings"
    )
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    return summary


def _trim(text: str, bound: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= bound else text[:bound].rstrip() + " …[trimmed]"


def _round_lines(entries: list[dict], position: int) -> list[str]:
    entry = entries[position]
    index = position + 1
    record = entry["record"]
    comment = entry["comment"]
    answer = entry["disposition_comment"]
    verdict = f"FINDINGS({record.findings})" if record.verdict == "FINDINGS" else record.verdict
    lines = [
        f"round {index}: {verdict} · reviewer {record.reviewer} · diff-key {record.key[:12]}",
        f"  review-comment: {comment.get('html_url') or '(none)'} at {comment.get('created_at') or 'unknown'}",
    ]
    if record.verdict == "FINDINGS":
        lines.append(
            "  dispositions-comment: "
            + (f"{answer.get('html_url')} at {answer.get('created_at')}" if answer else "(none recorded)")
        )
        for n in range(1, record.findings + 1):
            text = entry["items"].get(n)
            cls = finding_class(entries, position, n)
            if text:
                lines.append(f"  {n}. [{cls}] {_trim(text, MAX_DISPOSITION_CHARS)}")
            elif cls == "superseded":
                lines.append(f"  {n}. [superseded] no disposition; a later round reviewed a newer diff")
            else:
                lines.append(f"  {n}. [undisposed] no disposition recorded, and no later diff was reviewed")
    return lines


def build_details(repo: str, pull: dict, entries: list[dict]) -> str:
    head = [
        history_marker(repo, pull.get("number")),
        f"github-pr: #{pull.get('number')} {pull.get('url') or ''}".rstrip(),
        f"github-title: {(pull.get('title') or '').strip() or '(untitled)'}",
        f"pr-state: {pull.get('state') or 'unknown'}",
        f"pr-author-github-login: {_pr_author(pull)}",
        "agent-identity: not recorded — the review gate names a reviewer, not a UNITARES identity",
        f"disposition-provenance: {DISPOSITION_PROVENANCE}",
        "disposition class: the author's earliest keyword (accepted/rebutted), kept verbatim; "
        "superseded = no disposition but a later round reviewed a newer diff (a push, not proof of a fix)",
        f"ingested-by: {PRODUCER_NAME}",
        "",
    ]
    rounds: list[str] = []
    for position in range(len(entries)):
        rounds += _round_lines(entries, position)
    texts = ["", f"--- review texts (each trimmed to {MAX_REVIEW_TEXT_CHARS} chars; the comment URLs are the full record) ---"]
    for index, entry in enumerate(entries, 1):
        if entry["record"].verdict == "FINDINGS":
            texts += [f"[round {index}]", _trim(entry["comment"].get("body") or "", MAX_REVIEW_TEXT_CHARS)]
    details = "\n".join(head + rounds)
    budget = MAX_DETAILS_CHARS - len(details)
    rest = "\n".join(texts)
    if len(rest) > budget:
        rest = rest[: max(budget - 60, 0)].rstrip() + "\n…[review texts trimmed at the details bound]"
    return details + "\n" + rest


def build_store_arguments(repo: str, pull: dict, entries: list[dict]) -> dict:
    return {
        "action": "store",
        "discovery_type": STORE_DISCOVERY_TYPE,
        "severity": STORE_SEVERITY,
        "status": STORE_STATUS,
        "summary": build_summary(pull, entries),
        "details": build_details(repo, pull, entries),
        "tags": derive_tags(reviewers_of(entries), pull.get("number")),
    }


# --- governance side --------------------------------------------------------


def already_recorded(rest_url: str, repo: str, pr: int, timeout: float) -> bool:
    """Search before write: is this pull request's history already stored?

    One exact tag probe. The marker line in the details is the identity of an
    entry, so a row that only shares the tag (a note someone else tagged with
    the pull request) never suppresses a store.
    """
    result = ci.rest_call(
        rest_url, ci.KNOWLEDGE_TOOL,
        {"action": "search", "tags": [pr_tag(pr)], "include_archived": True,
         "include_cold": True, "include_details": True, "limit": SEARCH_LIMIT},
        timeout,
    )
    marker = re.compile(rf"^{re.escape(history_marker(repo, pr))}$", re.M)
    for row in ci._search_rows(result):
        if not isinstance(row, dict):
            continue
        for field in ci.FINGERPRINT_TEXT_FIELDS:
            text = row.get(field)
            if isinstance(text, str) and marker.search(text):
                return True
    return False


def store_one(args: argparse.Namespace, pull: dict, entries: list[dict]) -> str:
    number = pull.get("number")
    result = ci.rest_call(
        args.rest_url, ci.KNOWLEDGE_TOOL,
        build_store_arguments(args.repo, pull, entries), args.timeout,
    )
    discovery_id = result.get("discovery_id")
    if not isinstance(discovery_id, str) or not discovery_id.strip():
        raise IngestError(
            f"knowledge store returned no discovery_id for PR #{number} — nothing was recorded"
        )
    discovery_id = discovery_id.strip()
    readback = ci.rest_call(
        args.rest_url, ci.KNOWLEDGE_TOOL,
        {"action": "get", "discovery_id": discovery_id}, args.timeout,
    )
    stored = readback.get("discovery") if isinstance(readback.get("discovery"), dict) else readback
    status = stored.get("status") if isinstance(stored, dict) else None
    if status != STORE_STATUS:
        raise IngestError(
            f"stored {discovery_id} for PR #{number} but the server reports "
            f"status={status!r}, not {STORE_STATUS!r}, so the entry would read as "
            f"open work. Close it with knowledge(action='update', "
            f"discovery_id='{discovery_id}', status='{STORE_STATUS}')"
        )
    return discovery_id


# --- run --------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    if args.pr:
        pulls = [gh_pull(args.repo, n) for n in args.pr]
    else:
        pulls = gh_pulls(args.repo, args.state, args.limit)
    mode = "APPLY (writing)" if args.apply else "DRY RUN (writing nothing)"
    print(f"{PRODUCER_NAME}: {mode}")
    print(f"{len(pulls)} pull request(s) from {args.repo}")

    counts = {"stored": 0, "would-store": 0, "already-recorded": 0,
              "skipped-open": 0, "no-reviews": 0}
    examined = 0
    try:
        for pull in pulls:
            number = pull.get("number")
            if str(pull.get("state") or "").upper() == "OPEN":
                # Its history is not final: a later round or disposition would
                # have to amend a stored entry. It is stored once it closes.
                counts["skipped-open"] += 1
                examined += 1
                print(f"  skipped-open       #{number} — history not final until the PR closes")
                continue
            entries = review_records(gh_comments(args.repo, number))
            if not entries:
                counts["no-reviews"] += 1
                examined += 1
                continue
            t = disposition_tally(entries)
            breakdown = ", ".join(f"{v} {k}" for k, v in t.items() if v) or "no findings"
            label = f"#{number} {len(entries)} round(s), {sum(t.values())} finding(s) ({breakdown})"
            if already_recorded(args.rest_url, args.repo, number, args.timeout):
                counts["already-recorded"] += 1
                print(f"  already-recorded   {label}")
            elif not args.apply:
                counts["would-store"] += 1
                print(f"  would-store        {label}")
            else:
                discovery_id = store_one(args, pull, entries)
                counts["stored"] += 1
                print(f"  storing            {label} -> {discovery_id}")
            examined += 1
    except IngestError as exc:
        raise IngestError(
            f"{exc} — stopped after storing {counts['stored']} history(ies); "
            f"{len(pulls) - examined} pull request(s) not examined"
        ) from exc
    finally:
        print("tally: " + " ".join(f"{k}={v}" for k, v in counts.items()))

    if counts["would-store"]:
        print(f"note: re-run with --apply to write the {counts['would-store']} pending history(ies).")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Carry review-gate records and dispositions into the UNITARES knowledge graph.",
    )
    parser.add_argument("--repo", default=os.environ.get(ci.REPO_ENV) or ci.DEFAULT_REPO,
                        help=f"owner/name (env {ci.REPO_ENV}; default {ci.DEFAULT_REPO})")
    parser.add_argument("--state", choices=("merged", "open", "closed", "all"), default="merged",
                        help="pull request state to read (default: merged)")
    parser.add_argument("--limit", type=int, default=DEFAULT_PR_LIMIT,
                        help=f"the N most recent pull requests to read (default: {DEFAULT_PR_LIMIT})")
    parser.add_argument("--pr", type=int, action="append",
                        help="read this pull request only (repeatable; overrides --state/--limit)")
    parser.add_argument("--rest-url", default=os.environ.get(ci.REST_URL_ENV) or ci.DEFAULT_REST_URL,
                        help=f"governance REST endpoint (env {ci.REST_URL_ENV})")
    parser.add_argument("--timeout", type=float, default=ci.DEFAULT_TIMEOUT_SECONDS,
                        help="per-request timeout in seconds")
    parser.add_argument("--apply", action="store_true", help="actually store (default is a dry run)")
    parser.add_argument("--dry-run", action="store_true", help="explicitly request the default")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if not ci.REPO_RE.match(args.repo or ""):
        parser.error(f"--repo must be owner/name, got {args.repo!r}")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run(args)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
