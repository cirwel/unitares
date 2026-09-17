#!/usr/bin/env python3
"""Carry CI findings from GitHub issues into the UNITARES knowledge graph.

WHAT THIS IS
    An operator-side producer. It reads the issues the merge-loss guards and
    the stranded-work audit file (label ``ci-finding``, each body or comment
    carrying a hidden ``<!-- finding-fingerprint: SLUG -->`` marker) and stores
    each one it does not already find as a knowledge-graph discovery, keyed on
    the pair (fingerprint, issue number).

THE LEVER IT REPAIRS
    Incident #2168 was reconstructed twice, once from git/PR/CI artifacts and
    once from UNITARES records
    (``docs/evaluations/accountability-journey/capture-stage-2168-*.md``). The
    record held no fact about the incident. One of the separately repairable
    causes was that no producer bridged CI or git findings into the record:
    ``scripts/ci/surface_findings.py`` runs repo-to-GitHub, the guards write
    GitHub issues, and nothing carried either into the graph. This script is
    that missing direction, GitHub-to-graph.

WHY PULL-SIDE AND NOT A CI PUSH
    The guard workflows are ``GITHUB_TOKEN``-only by contract, asserted by
    ``tests/test_merge_loss_guards.py``: no metered API, and no extra secret.
    Writing to the governance server from CI would need a server-reachable
    endpoint and a bearer credential in the workflow environment, which
    breaks that contract. So the bridge runs where the credential and the
    server already are, on the operator's machine, on demand.

WHY IT FAILS LOUD
    The CI guards fail *open* on purpose: a broken guard must not block
    delivery. This producer has no delivery to block, and the failure it
    exists to remove is precisely a run that reports success while writing
    nothing. So it refuses to report a store it cannot prove: a gh failure, a
    transport error, a tool-level refusal inside an HTTP 200, a typed identity
    refusal, a search whose result shape it does not recognize, a store that
    returns no ``discovery_id``, and a read truncated at ``--limit`` all end
    the run non-zero, carrying the server's own message where there is one.

WHAT IT DOES NOT DO
    It is not the portable incident bundle, and it is not successor retrieval.
    It adds no MCP tool, no agent, no workflow, and no schema. It reads two
    things (the GitHub issue list, the knowledge graph) and writes one
    (``knowledge(action='store')``).

VERIFICATION STATUS
    The GitHub read path and the request envelopes are covered behaviorally by
    ``tests/test_ingest_ci_findings.py`` against a stub ``gh`` and a stub HTTP
    server, including the server's real refusal shapes. End-to-end
    verification against a live governance server has not been run for this
    change; the envelope is reproduced from
    ``agents/sdk/src/unitares_sdk/sync_client.py::_rest_call`` and the refusal
    shapes from ``src/http_routes/tools.py`` and
    ``src/mcp_handlers/identity_bootstrap.py``, rather than observed against a
    running server.

USAGE
    python3 scripts/dev/ingest_ci_findings.py [--repo owner/name]
        [--state open|all] [--limit N] [--rest-url URL] [--timeout S]
        [--dry-run | --apply]

    Dry run is the default: it prints a verdict per finding and writes
    nothing. ``--apply`` is required to store.

    Exit codes: 0 = the run completed and examined every issue it read;
    1 = the run did not complete, and the tally line printed before the error
    says what had landed by then.

ENVIRONMENT
    GITHUB_REPOSITORY          default for --repo (else the repo default below)
    GOV_REST_URL               default for --rest-url
    UNITARES_MCP_BEARER_TOKEN  bearer token, sent only when set (SDK contract)

Reads GitHub through the ``gh`` CLI only; no metered model API on any path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PRODUCER_NAME = "scripts/dev/ingest_ci_findings.py"

# --- GitHub side ------------------------------------------------------------

# Label and marker are the guards' own contract: see scripts/ci/merge_loss_common.py
# (FINDING_LABEL, fingerprint_marker) and .github/workflows/stranded-work.yml.
FINDING_LABEL = "ci-finding"
MARKER_NAME = "finding-fingerprint"
MARKER_HTML = "<!-- " + MARKER_NAME + ": {} -->"
# Anchored at a line start, because a marker quoted inside prose is not a
# finding. Every real emitter puts it on the first line of the body or comment
# it writes (merge_loss_common.file_or_comment_finding builds "{marker}\n{body}",
# stranded-work.yml echoes the marker first), so nothing legitimate needs a
# free-floating scan -- and a free-floating scan lets a hand-filed question
# that quotes a marker take over the real finding's dedup key.
MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*" + MARKER_NAME + r":\s*(.+?)\s*-->",
    re.MULTILINE,
)
# Fenced regions are quoted text, never an emitter's own marker line.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

GH_JSON_FIELDS = "number,title,body,url,state,createdAt,updatedAt,labels,comments"
# Matches scripts/ci/merge_loss_common.py::find_open_finding, so this producer's
# view of the board is never narrower than the guards' own.
DEFAULT_ISSUE_LIMIT = 200
# Matches scripts/dev/stranded_work_audit.py and scripts/dev/stuck_draft_audit.py,
# which carry the same literal as a plain argparse default.
DEFAULT_REPO = "cirwel/unitares"
REPO_ENV = "GITHUB_REPOSITORY"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OPEN_STATES = frozenset({"", "OPEN"})

# --- Governance side -------------------------------------------------------

KNOWLEDGE_TOOL = "knowledge"
DEFAULT_REST_URL = "http://127.0.0.1:8767/v1/tools/call"
REST_URL_ENV = "GOV_REST_URL"
BEARER_ENV = "UNITARES_MCP_BEARER_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 30.0
GH_TIMEOUT_SECONDS = 60

# Severity is pinned low-side on purpose: knowledge(action='store') refuses
# high/critical without supporting conditions, and an automated bridge has no
# standing to assert impact on the guard's behalf. The guard's own severity
# claim is recorded as text (the issue labels) rather than mapped into this.
STORE_SEVERITY = "medium"
STORE_DISCOVERY_TYPE = "bug_found"
# A recurrence of a fingerprint already in the graph under a different issue is
# linked to it rather than filed as an unrelated note (the linked-correction
# shape CLAUDE.md's KG discipline asks for). "follow_up" is in
# src/knowledge_graph.py::ResponseType.
FOLLOW_UP_RESPONSE_TYPE = "follow_up"

# Wave 3 §3.2 typed-unavailable contract, mirrored from
# agents/sdk/src/unitares_sdk/errors.py.
UNAVAILABLE_ERROR = "governance_temporarily_unavailable"
DEFAULT_RETRY_AFTER_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 30.0

# The one success-SHAPED payload that is not a success. Mirrored from
# src/mcp_handlers/identity_bootstrap.py (IDENTITY_REFUSAL_MARKER, written by
# strict_identity_refusal_payload and by nothing else, and the statuses its
# emission points use). The REST gate returns it raw inside an HTTP 200 with
# success:true and no error key, so a consumer that branches on success alone
# misses it -- and this producer never onboards, so on a deployment with the
# strict gate armed the refusal is the EXPECTED answer to a write.
IDENTITY_REFUSAL_MARKER = "STRICT_IDENTITY_REQUIRED"
IDENTITY_REFUSAL_STATUSES = frozenset(
    {"identity_required", "lineage_declaration_required"}
)

# Bounds on what is copied into the graph. The issue is the full record; the
# discovery is a searchable pointer to it, so the marked text is trimmed rather
# than mirrored. The server's own cap is far higher (MAX_DETAILS_LEN = 64 KiB).
MAX_ISSUE_BODY_CHARS = 4000
MAX_SUMMARY_CHARS = 200
MAX_ERROR_DETAIL_CHARS = 400

SEARCH_LIMIT = 25
# Tag vocabularies every ingested finding carries, so a successor searching by
# provenance ("where did this come from") reaches it without knowing the slug.
STATIC_TAGS = ("ci-finding", "github", "ci")
MIN_CLASS_WORD_LEN = 3
# Mirrors src/knowledge_graph_lifecycle.py::EPHEMERAL_TAGS. A CI finding is a
# durable claim about a defect, so none of these may ever be derived from a
# guard slug: get_lifecycle_policy archives on any one of them after 7 days and
# bug_found is not in PERMANENT_TYPES, so nothing would override it. The
# invariant belongs here rather than in the current slug vocabulary's luck --
# "flaky-test-detector" alone would mint `test`.
_FORBIDDEN_TAGS = frozenset({"ephemeral", "temp", "scratch", "test", "demo"})
# Mirrors src/knowledge_ontology.py::SPELLING_VARIANTS, applied as the last
# step of the server's normalize_tags on every write and tag-filtered search.
# Source of truth is that map; this copy exists so the tag computed here is the
# tag the server stores. The text arm of the dedup check does not depend on it.
_SPELLING_VARIANTS = {"postgresql": "postgres"}

_TAG_SPLIT_RE = re.compile(r"[^0-9a-z]+")
# Characters that can extend a slug. A bare substring test would let the
# fingerprint "merge-content-check pr-16" match a stored
# "merge-content-check pr-1610" and silently skip a real finding.
_SLUG_CHAR = r"[0-9A-Za-z_/-]"


class IngestError(Exception):
    """A failure that must end the run non-zero, carrying the cause's message."""


# --- fingerprint and tag handling ------------------------------------------


def strip_fenced(text: str) -> str:
    """Blank out lines inside paired code fences, keeping line structure.

    A marker inside a fence is a quotation -- a docs excerpt, a question about
    the dedup contract -- not an emitter's own claim. Treating one as a finding
    lets an unrelated issue take over a real finding's dedup key.
    """
    out: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def parse_fingerprints(text: str) -> list[str]:
    """Every fingerprint slug a body or comment declares, in order, deduped."""
    seen: set[str] = set()
    slugs: list[str] = []
    for match in MARKER_RE.finditer(strip_fenced(text)):
        slug = " ".join(match.group(1).split())
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def _comment_bodies(issue: dict) -> list[tuple[str, str]]:
    """(origin label, text) for each comment gh returned on this issue."""
    comments = issue.get("comments")
    if not isinstance(comments, list):
        return []
    out: list[tuple[str, str]] = []
    for index, comment in enumerate(comments, start=1):
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        url = (comment.get("url") or "").strip()
        out.append((f"comment {index}" + (f" ({url})" if url else ""), body))
    return out


def finding_sources(issue: dict) -> list[dict]:
    """Every fingerprint this issue carries, with the text that carried it.

    The body is not the only channel. ``scripts/ci/merge_loss_common.py::
    file_or_comment_finding`` comments the marker plus the new body onto an
    existing open issue, and ``.github/workflows/stranded-work.yml`` does the
    same every week, so a recurrence -- and sometimes a DIFFERENT guard's
    finding entirely -- lands in a comment while the body stays frozen at the
    first detection. Issue #2168 is the recorded case: its body carries the
    ``orphan-push-guard`` marker and its only comment carries the
    ``stranded-work-audit`` one (capture-stage-2168-artifacts-arm.md, F27).
    Reading the body alone would miss the second finding on the very incident
    this producer exists to repair.
    """
    channels: list[tuple[str, str]] = [("issue body", issue.get("body") or "")]
    channels.extend(_comment_bodies(issue))

    seen: set[str] = set()
    findings: list[dict] = []
    for origin, text in channels:
        for fingerprint in parse_fingerprints(text):
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            findings.append(
                {"fingerprint": fingerprint, "origin": origin, "text": text}
            )
    return findings


def normalize_tag(value: str) -> str:
    """Lowercase, fold punctuation runs to one hyphen, strip edges, fold variants.

    Mirrors src/knowledge_graph.py::normalize_tags, including the final
    spelling-variant fold, so the tag this script computes locally is the tag
    the server stores -- which is what makes the exact tag comparison in
    row_carries_fingerprint meaningful. If that map ever grows past the local
    copy, the text arm of the dedup check still holds; only the tag backstop
    degrades.
    """
    folded = _TAG_SPLIT_RE.sub("-", str(value).strip().lower()).strip("-")
    return _SPELLING_VARIANTS.get(folded, folded)


def guard_slug_of(fingerprint: str) -> str:
    """The guard name portion of a fingerprint ("orphan-push-guard claude/x")."""
    tokens = fingerprint.split()
    return normalize_tag(tokens[0]) if tokens else ""


def issue_tag_of(issue_number: Any) -> str:
    return normalize_tag(f"issue-{issue_number}")


def derive_tags(fingerprint: str, issue_number: Any) -> list[str]:
    """Tags for one finding, derived from the fingerprint -- never hand-listed.

    Four vocabularies, so a successor can arrive from any of them: the exact
    fingerprint (half the dedup key), the guard slug, the guard's individual
    class words ("orphan", "push", "merge", "stranded"), and the provenance
    tags including the issue number (the other half).

    No candidate in _FORBIDDEN_TAGS survives, whatever the slug is named.
    """
    guard = guard_slug_of(fingerprint)
    candidates = [normalize_tag(fingerprint), guard]
    candidates.extend(normalize_tag(token) for token in fingerprint.split()[1:])
    candidates.extend(
        word
        for word in guard.split("-")
        if len(word) >= MIN_CLASS_WORD_LEN and not word.isdigit()
    )
    candidates.extend(STATIC_TAGS)
    if issue_number is not None:
        candidates.append(issue_tag_of(issue_number))

    seen: set[str] = set()
    tags: list[str] = []
    for tag in candidates:
        if tag and tag not in seen and tag not in _FORBIDDEN_TAGS:
            seen.add(tag)
            tags.append(tag)
    return tags


def _boundary_matcher(literal: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<!" + _SLUG_CHAR + r")" + re.escape(literal) + r"(?!" + _SLUG_CHAR + r")"
    )


def _fingerprint_matcher(fingerprint: str) -> re.Pattern[str]:
    """Match only the LABELLED forms of this fingerprint, never the bare slug.

    A bare boundary-anchored slug is not enough. Three of the four live
    emitters use a constant, word-shaped fingerprint with no variable part
    (``stranded-work-audit``, ``sdk-release-sync``, ``server-release-sync``),
    so any pre-existing note whose prose happens to contain that phrase would
    count as the finding already being recorded -- and the full-text probe on
    those tokens is what retrieves exactly such notes. Both accepted forms
    below are strings only this producer (the ``finding-fingerprint:`` line it
    writes into details) or a guard (the HTML marker) emits.
    """
    return re.compile(
        "(?:"
        + _boundary_matcher(f"{MARKER_NAME}: {fingerprint}").pattern
        + "|"
        + _boundary_matcher(MARKER_HTML.format(fingerprint)).pattern
        + ")"
    )


def _issue_ref_matcher(issue_number: Any) -> re.Pattern[str]:
    return _boundary_matcher(f"github-issue: #{issue_number}")


# Fields of a returned search row that may carry the fingerprint. details is
# only present when the server includes it; details_preview is the summary-mode
# fallback, so both are checked.
FINGERPRINT_TEXT_FIELDS = ("summary", "details", "details_preview")


def _row_tags(row: dict) -> list[str]:
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return [normalize_tag(tag) for tag in tags] if isinstance(tags, list) else []


def row_carries_fingerprint(row: Any, fingerprint: str, fingerprint_tag: str) -> bool:
    """True only when this row actually carries this fingerprint.

    Presence of search results is never enough: a full-text query on a slug
    returns neighbours, and treating those as a hit would make the producer
    skip real findings. A row counts when it carries the exact fingerprint
    tag, or a labelled occurrence of the fingerprint in a text field.
    """
    if not isinstance(row, dict):
        return False
    if fingerprint_tag and fingerprint_tag in _row_tags(row):
        return True
    matcher = _fingerprint_matcher(fingerprint)
    return any(
        isinstance(row.get(field), str) and matcher.search(row[field])
        for field in FINGERPRINT_TEXT_FIELDS
    )


def row_carries_issue(row: Any, issue_number: Any) -> bool:
    """True when this row was written from this same GitHub issue.

    The dedup key is the PAIR (fingerprint, issue number), not the fingerprint
    alone. The guards refile a fingerprint as a new issue once the previous one
    is closed, and a run that keyed on the fingerprint alone reported that
    recurrence as already-recorded and wrote nothing.
    """
    if not isinstance(row, dict) or issue_number is None:
        return False
    if issue_tag_of(issue_number) in _row_tags(row):
        return True
    matcher = _issue_ref_matcher(issue_number)
    return any(
        isinstance(row.get(field), str) and matcher.search(row[field])
        for field in FINGERPRINT_TEXT_FIELDS
    )


def row_discovery_id(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("discovery_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --- GitHub read -----------------------------------------------------------


def gh_issues(repo: str, state: str, limit: int) -> list[dict]:
    """The open (or all) ci-finding issues, via the gh CLI. Raises on failure."""
    argv = [
        "gh",
        "issue",
        "list",
        "-R",
        repo,
        "--label",
        FINDING_LABEL,
        "--state",
        state,
        "--json",
        GH_JSON_FIELDS,
        "--limit",
        str(limit),
    ]
    try:
        proc = subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise IngestError(
            "gh CLI not found on PATH — this producer reads GitHub through gh"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestError(
            f"gh issue list timed out after {GH_TIMEOUT_SECONDS}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "no output"
        raise IngestError(
            f"gh issue list failed (exit {exc.returncode}): {detail}"
        ) from exc

    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise IngestError(
            f"gh issue list returned non-JSON output: {(proc.stdout or '')[:200]!r}"
        ) from exc
    if not isinstance(issues, list):
        raise IngestError("gh issue list did not return a JSON array")
    return [issue for issue in issues if isinstance(issue, dict)]


# --- governance REST -------------------------------------------------------


def _bound_delay(value: Any) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _read_error_body(exc: urllib.error.HTTPError) -> dict | None:
    """The error response body, parsed once (``exc.read()`` is single-use)."""
    try:
        raw = exc.read()
    except (OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after_seconds(exc: urllib.error.HTTPError, body: dict | None) -> float:
    """Server-suggested delay from a 503: header first, then the typed body."""
    header = exc.headers.get("Retry-After") if exc.headers else None
    bounded = _bound_delay(header)
    if bounded is not None:
        return bounded
    if isinstance(body, dict) and body.get("error") == UNAVAILABLE_ERROR:
        bounded = _bound_delay(body.get("retry_after_seconds"))
        if bounded is not None:
            return bounded
    return DEFAULT_RETRY_AFTER_SECONDS


def _unwrap_result(result: Any, tool: str) -> dict:
    """Normalize the MCP result shapes the REST endpoint can return."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return {"text": result, "raw": True}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    if isinstance(result, dict):
        if result.get("isError"):
            content = result.get("content") or []
            text = " ".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ).strip()
            raise IngestError(f"{tool} returned error: {text or 'unknown error'}")
        content = result.get("content")
        if isinstance(content, list) and content:
            merged: dict = {}
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        parsed = json.loads(item["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        merged.update(parsed)
            return merged or result
        return result

    raise IngestError(f"{tool}: unexpected result type {type(result).__name__}")


def _check_tool_payload(payload: dict, tool: str) -> dict:
    """Refuse a tool-level failure that arrived inside a successful envelope.

    src/http_routes/tools.py::_build_http_tool_response wraps ANY handler
    result as ``{"result": <parsed>, "success": true}``, and
    src/mcp_handlers/error_handling.py::error_response puts
    ``{"success": false, "error": ...}`` in that parsed body. So the outer
    envelope's success key says only that a handler ran. Checking it alone
    reported a refused write as a completed store.
    """
    if payload.get("success") is False:
        detail = payload.get("error") or payload.get("error_code") or "unknown error"
        raise IngestError(f"{tool} refused: {detail}")
    if (
        payload.get("rollout_flag") == IDENTITY_REFUSAL_MARKER
        or payload.get("status") in IDENTITY_REFUSAL_STATUSES
    ):
        status = payload.get("status") or "identity_required"
        hint = str(payload.get("hint") or "").strip()
        next_step = str(payload.get("next_step") or "").strip()
        raise IngestError(
            f"{tool} refused by the identity write gate (status={status})"
            + (f": {hint}" if hint else "")
            + (f" next_step={next_step}" if next_step else "")
        )
    return payload


def rest_call(rest_url: str, tool: str, arguments: dict, timeout: float) -> dict:
    """POST to /v1/tools/call and return the tool result.

    Reproduces agents/sdk/src/unitares_sdk/sync_client.py::_rest_call with the
    stdlib alone (the SDK needs pydantic and httpx): the same body envelope,
    the same bearer-only-when-configured header contract, and the same
    retry-once on an HTTP 503 typed-unavailable response. It also reproduces
    the layer the SDK gets from validating its reply into a typed model, which
    is where a tool-level refusal is caught.
    """
    payload = json.dumps({"name": tool, "arguments": arguments}).encode()
    data: dict | None = None

    for attempt in range(2):
        headers = {"Content-Type": "application/json"}
        bearer = os.environ.get(BEARER_ENV) or None
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            rest_url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode()
            data = json.loads(raw)
            break
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            if exc.code == 503:
                delay = _retry_after_seconds(exc, body)
                if attempt == 0:
                    print(
                        f"    governance unavailable (HTTP 503); retrying in {delay:.1f}s",
                        flush=True,
                    )
                    time.sleep(delay)
                    continue
                raise IngestError(
                    f"{tool}: governance temporarily unavailable "
                    f"(retry_after_seconds={delay})"
                ) from exc
            detail = str(body.get("error") or "").strip() if body else ""
            raise IngestError(
                f"POST {rest_url} failed: HTTP {exc.code} {exc.reason}"
                + (f": {detail[:MAX_ERROR_DETAIL_CHARS]}" if detail else "")
            ) from exc
        except urllib.error.URLError as exc:
            raise IngestError(f"POST {rest_url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise IngestError(f"{tool} timed out after {timeout}s") from exc
        except json.JSONDecodeError as exc:
            raise IngestError(f"{tool}: response body was not JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise IngestError(f"{tool}: response envelope was not a JSON object")
    if not data.get("success", False):
        raise IngestError(f"{tool} failed: {data.get('error') or 'unknown error'}")
    result = data.get("result")
    if result is None:
        raise IngestError(f"{tool}: response carried no result")
    return _check_tool_payload(_unwrap_result(result, tool), tool)


# --- search and store ------------------------------------------------------


def _search_rows(result: dict) -> list:
    """The result rows, or a loud failure if the shape is not recognized.

    An unrecognized shape must never read as "no existing record": that turns
    a response-shape change into a duplicate write on every run, which is the
    opposite of the idempotency this producer promises. A genuine zero is a
    recognized key holding an empty list.
    """
    for key in ("discoveries", "results"):
        rows = result.get(key)
        if isinstance(rows, list):
            return rows
    raise IngestError(
        "knowledge search returned no recognizable result rows "
        f"(keys: {sorted(str(key) for key in result)}) — refusing to treat "
        "that as 'not recorded'"
    )


def find_existing(
    rest_url: str, fingerprint: str, issue_number: Any, timeout: float
) -> tuple[bool, str | None]:
    """Search before write. ``(exact, related_discovery_id)``.

    ``exact`` means the graph already holds this fingerprint FOR THIS ISSUE, so
    there is nothing to write. ``related_discovery_id`` is set when it holds
    the fingerprint under a different issue, which is a recurrence: the write
    still happens, linked to that entry rather than filed as an unrelated note.

    Two probes, because the full-text tokenizer mangles the characters these
    slugs are made of -- branch names carry ``/`` and markers carry ``:``, the
    same reason scripts/ci/merge_loss_common.py::find_open_finding matches
    issue bodies in-process instead of through GitHub search. The text probe is
    the normal path; the exact tag filter is the backstop, run only when the
    text probe produced no exact match, so a mangled query cannot turn into a
    duplicate write.
    """
    fingerprint_tag = normalize_tag(fingerprint)
    probes: list[dict] = [
        {
            "action": "search",
            "query": fingerprint,
            "search_mode": "fts",
            "include_archived": True,
            "include_cold": True,
            "include_details": True,
            "limit": SEARCH_LIMIT,
        }
    ]
    # A forbidden tag is never minted by derive_tags, so probing for one would
    # ask the graph a question no record of ours can answer.
    if fingerprint_tag and fingerprint_tag not in _FORBIDDEN_TAGS:
        probes.append(
            {
                "action": "search",
                "tags": [fingerprint_tag],
                "include_archived": True,
                "include_cold": True,
                "include_details": True,
                "limit": SEARCH_LIMIT,
            }
        )

    related: str | None = None
    for probe in probes:
        result = rest_call(rest_url, KNOWLEDGE_TOOL, probe, timeout)
        for row in _search_rows(result):
            if not row_carries_fingerprint(row, fingerprint, fingerprint_tag):
                continue
            if row_carries_issue(row, issue_number):
                return True, None
            related = related or row_discovery_id(row)
    return False, related


def build_summary(issue: dict, fingerprint: str) -> str:
    guard = guard_slug_of(fingerprint) or "ci-guard"
    title = (issue.get("title") or "").strip() or "(untitled issue)"
    summary = f"CI finding [{guard}] issue #{issue.get('number')}: {title}"
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    return summary


def _issue_labels(issue: dict) -> str:
    """The label names gh reported, comma-joined, or a named absence.

    Recorded as text because the upstream producer declares severity and source
    there: .github/workflows/surface-findings.yml labels its issues
    ``severity:<s>`` and ``source:<s>``. Recording the claim is fidelity;
    mapping it into the stored severity would be the escalation this bridge
    has no standing to make.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return "(none reported)"
    names = [
        str(label.get("name")).strip()
        for label in labels
        if isinstance(label, dict) and label.get("name")
    ]
    names.extend(str(label).strip() for label in labels if isinstance(label, str))
    return ", ".join(name for name in names if name) or "(none reported)"


def build_details(issue: dict, finding: dict) -> str:
    fingerprint = finding["fingerprint"]
    origin = finding["origin"]
    text = (finding.get("text") or "").strip()
    truncated = len(text) > MAX_ISSUE_BODY_CHARS
    if truncated:
        text = text[:MAX_ISSUE_BODY_CHARS].rstrip()

    url = (issue.get("url") or "").strip()
    lines = [
        f"{MARKER_NAME}: {fingerprint}",
        f"guard: {guard_slug_of(fingerprint) or 'unknown'}",
        f"github-issue: #{issue.get('number')}",
        f"github-url: {url or '(none reported)'}",
        f"github-title: {(issue.get('title') or '').strip() or '(untitled issue)'}",
        f"github-labels: {_issue_labels(issue)}",
        f"issue-state: {issue.get('state') or 'unknown'}",
        f"issue-created: {issue.get('createdAt') or 'unknown'}",
        f"issue-updated: {issue.get('updatedAt') or 'unknown'}",
        f"finding-origin: {origin}",
        f"ingested-by: {PRODUCER_NAME} (operator-side pull of the '{FINDING_LABEL}' label)",
        "",
        f"--- {origin} (first {MAX_ISSUE_BODY_CHARS} chars) ---"
        if truncated
        else f"--- {origin} ---",
        text or "(empty)",
    ]
    if truncated:
        lines.append(
            f"--- trimmed at {MAX_ISSUE_BODY_CHARS} chars; the issue URL above is the full record ---"
        )
    return "\n".join(lines)


def build_store_arguments(
    issue: dict, finding: dict, related_id: str | None = None
) -> dict:
    arguments = {
        "action": "store",
        "discovery_type": STORE_DISCOVERY_TYPE,
        "severity": STORE_SEVERITY,
        "summary": build_summary(issue, finding["fingerprint"]),
        "details": build_details(issue, finding),
        "tags": derive_tags(finding["fingerprint"], issue.get("number")),
    }
    if related_id:
        arguments["response_to"] = {
            "discovery_id": related_id,
            "response_type": FOLLOW_UP_RESPONSE_TYPE,
        }
    return arguments


# --- run -------------------------------------------------------------------


def _is_open(issue: dict) -> bool:
    return str(issue.get("state") or "").strip().upper() in OPEN_STATES


def _store_one(
    args: argparse.Namespace, issue: dict, finding: dict, related: str | None
) -> str:
    """Write one finding and return the discovery_id the server reported.

    A store with no ``discovery_id`` in its reply is not a store this producer
    will report as one: the whole point of the lever is that the record can be
    trusted to hold what the run claims it holds.
    """
    result = rest_call(
        args.rest_url,
        KNOWLEDGE_TOOL,
        build_store_arguments(issue, finding, related),
        args.timeout,
    )
    discovery_id = result.get("discovery_id")
    if not isinstance(discovery_id, str) or not discovery_id.strip():
        raise IngestError(
            f"knowledge store returned no discovery_id for #{issue.get('number')} "
            f"[{finding['fingerprint']}] — nothing was recorded"
        )
    return discovery_id.strip()


def _run(args: argparse.Namespace) -> int:
    issues = gh_issues(args.repo, args.state, args.limit)
    mode = "APPLY (writing)" if args.apply else "DRY RUN (writing nothing)"
    print(f"{PRODUCER_NAME}: {mode}")
    print(
        f"{len(issues)} issue(s) labelled '{FINDING_LABEL}' in {args.repo} "
        f"(state={args.state}, limit={args.limit})"
    )

    stored = would_store = already = skipped = closed = 0
    examined = 0

    try:
        for issue in issues:
            number = issue.get("number")
            title = (issue.get("title") or "").strip()

            if not _is_open(issue):
                closed += 1
                examined += 1
                print(
                    f"  skipped-closed     #{number} {title} "
                    f"— issue is {issue.get('state')}; a store cannot land resolved, "
                    "so backfilling it would add an entry nothing can close"
                )
                continue

            findings = finding_sources(issue)
            if not findings:
                skipped += 1
                examined += 1
                print(
                    f"  skipped-no-marker  #{number} {title} "
                    f"— no '{MARKER_NAME}' marker on a line of the body or any comment"
                )
                continue

            for finding in findings:
                fingerprint = finding["fingerprint"]
                exact, related = find_existing(
                    args.rest_url, fingerprint, number, args.timeout
                )
                if exact:
                    already += 1
                    print(f"  already-recorded   #{number} [{fingerprint}]")
                    continue

                kind = "followup" if related else "new"
                if not args.apply:
                    would_store += 1
                    print(
                        f"  would-store        #{number} [{fingerprint}] "
                        f"({kind}, from {finding['origin']})"
                    )
                    continue

                discovery_id = _store_one(args, issue, finding, related)
                stored += 1
                print(
                    f"  storing            #{number} [{fingerprint}] "
                    f"({kind}, from {finding['origin']}) -> {discovery_id}"
                )
            examined += 1
    except IngestError as exc:
        raise IngestError(
            f"{exc} — stopped after storing {stored} finding(s); "
            f"{len(issues) - examined} issue(s) not examined"
        ) from exc
    finally:
        print(
            f"tally: stored={stored} would-store={would_store} "
            f"already-recorded={already} skipped-no-marker={skipped} "
            f"skipped-closed={closed}"
        )

    if skipped:
        print(
            f"note: {skipped} issue(s) carry the '{FINDING_LABEL}' label with no "
            f"'{MARKER_NAME}' marker — they were counted, not dropped, and are "
            "unreachable by fingerprint until their producer emits one."
        )
    if closed:
        print(
            f"note: {closed} non-open issue(s) were read and not stored. "
            "knowledge(action='store') has no status field, so a closed finding "
            "would enter the record as an open entry only a later "
            "knowledge(action='update') could close."
        )
    if would_store:
        print(
            f"note: re-run with --apply to write the {would_store} pending finding(s)."
        )
    if len(issues) >= args.limit:
        # Degraded is never silent: the same rule scripts/ci/merge_loss_common.py
        # states for the guards. gh truncates at --limit without saying so, so
        # at the ceiling the producer cannot tell a full board from a clipped
        # one, and a clean exit here would be instrumentation failing toward
        # "healthy".
        print(
            f"error: gh returned {len(issues)} issue(s) at the --limit ceiling "
            f"({args.limit}); any finding beyond it was NOT examined. Re-run with "
            "a higher --limit. The tally above covers only what was read.",
            file=sys.stderr,
        )
        return 1
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Carry ci-finding GitHub issues into the UNITARES knowledge graph.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get(REPO_ENV) or DEFAULT_REPO,
        help=f"owner/name to read findings from (env {REPO_ENV}; default {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--state",
        choices=("open", "all"),
        default="open",
        help="issue state to read (default: open; non-open issues are reported, not stored)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_ISSUE_LIMIT,
        help=f"maximum issues to read (default: {DEFAULT_ISSUE_LIMIT})",
    )
    parser.add_argument(
        "--rest-url",
        default=os.environ.get(REST_URL_ENV) or DEFAULT_REST_URL,
        help=f"governance REST endpoint (env {REST_URL_ENV}; default {DEFAULT_REST_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually store new findings (default is a dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly request the default: report, write nothing",
    )
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if not REPO_RE.match(args.repo or ""):
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
        # Fail loud: this is not a CI guard, it has no delivery to block, and a
        # run that reports success while writing nothing is the failure mode
        # this whole lever exists to remove.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
