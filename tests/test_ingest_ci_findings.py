"""Tests for the CI-finding knowledge-graph producer (scripts/dev/ingest_ci_findings.py).

The producer is exercised end-to-end as a subprocess against a stub `gh`
binary on PATH and a real loopback HTTP server standing in for the
governance REST endpoint, because its whole job is orchestrating those two
calls and deciding between them. Following the convention of
tests/test_merge_loss_guards.py: the POSITIVE cases matter most, because a
producer whose only tested property is "does not double-write" can be a
silent no-op and pass - and a bridge that writes nothing is exactly the
state this script exists to leave behind. So every positive case asserts on
the REQUEST BODY the stub received, not merely on the exit code: an exit 0
with no store request is the failure, not the success.

The NEGATIVE cases assert against the server's real refusal shapes, not a
convenient one. src/http_routes/tools.py::_build_http_tool_response wraps any
handler result as `{"result": <parsed>, "success": true}`, so a tool-level
error and the #425 typed identity refusal both arrive inside an HTTP 200 whose
outer envelope says success. An earlier version of this file modelled a refusal
as an outer `success: false`, a shape the server never produces, and the
producer consequently reported a refused write as a completed store.

The request/response SHAPES are reproduced from
agents/sdk/src/unitares_sdk/sync_client.py::_rest_call, the knowledge
handler's search serializer, and src/mcp_handlers/identity_bootstrap.py, not
observed against a live server; no live governance server was reachable when
these were written.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCER = REPO_ROOT / "scripts" / "dev" / "ingest_ci_findings.py"

MARKER = "<!-- finding-fingerprint: {} -->"
FP_ORPHAN = "orphan-push-guard claude/dead-branch"
FP_CONTENT = "merge-content-check pr-1610"
FP_STRANDED = "stranded-work-audit"

# The #425 typed refusal, field for field from
# src/mcp_handlers/identity_bootstrap.py::strict_identity_refusal_payload.
IDENTITY_REFUSAL = {
    "status": "identity_required",
    "tool": "knowledge",
    "tool_class": "required",
    "hint": "Bind first with onboard(force_new=true).",
    "next_step": "onboard(force_new=true)",
    "safe_options": [],
    "do_not": [],
    "ontology_ref": "docs/ontology/identity.md#operational-contract",
    "rollout_flag": "STRICT_IDENTITY_REQUIRED",
}

STUB_GH = """#!/usr/bin/env bash
d="$STUB_DATA"
printf '%s\\n' "$*" >> "$d/calls.log"
case "$*" in
  "issue list"*)
    if [ -f "$d/issue_list.fail" ]; then
      echo "stub gh: HTTP 401: Bad credentials" >&2
      exit 1
    fi
    cat "$d/issue_list.json" 2>/dev/null || echo "[]" ;;
  *) echo "stub gh: unhandled: $*" >&2; exit 64 ;;
esac
"""


class StubGovernance:
    """Scriptable stand-in for POST /v1/tools/call.

    Holds the recorded requests plus the knobs a test needs:

    * ``rows``    - discoveries the search returns. A store appends its own
      row here (``echo_stores``), so a second run finds what the first wrote
      and idempotency is exercised through the real code path rather than a
      hand-seeded reply.
    * ``fts_blind`` - text probes return nothing while tag-filtered probes
      still match, simulating the full-text tokenizer mangling a slug full of
      ``/`` and ``-``.
    * ``search_payload`` - replace the whole search result, for the shapes a
      serializer change or a refusal would produce.
    * ``store_ok`` / ``store_error`` / ``fail_store_on`` - make a store fail
      the way the server really reports it: inside ``result``.
    * ``store_refusal`` - answer a store with a success-shaped payload.
    * ``store_omit_discovery_id`` - answer a store with no id.
    * ``http_error`` - answer every request with ``(status, payload)``.
    * ``unavailable_once`` - answer the first request with the §3.2
      typed-unavailable 503.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.rows: list[dict] = []
        self.echo_stores = True
        self.fts_blind = False
        self.search_payload: dict | None = None
        self.store_ok = True
        self.store_error = "knowledge store refused: simulated"
        self.fail_store_on: int | None = None
        self.store_refusal: dict | None = None
        self.store_omit_discovery_id = False
        self.http_error: tuple[int, dict] | None = None
        self.unavailable_once = False
        self._served_503 = 0
        self._store_calls = 0

    # --- views over the recorded traffic ---

    def bodies(self, action: str) -> list[dict]:
        return [
            r["body"]["arguments"]
            for r in self.requests
            if r["body"].get("arguments", {}).get("action") == action
        ]

    @property
    def searches(self) -> list[dict]:
        return self.bodies("search")

    @property
    def stores(self) -> list[dict]:
        return self.bodies("store")

    def reset_traffic(self) -> None:
        self.requests.clear()

    # --- reply construction ---

    def reply(self, arguments: dict) -> tuple[int, dict]:
        action = arguments.get("action")
        if action == "search":
            if self.search_payload is not None:
                return 200, {"success": True, "result": dict(self.search_payload)}
            return 200, {"success": True, "result": self._search_result(arguments)}
        if action == "store":
            return self._store_result(arguments)
        return 200, {"success": False, "error": f"stub: unhandled action {action!r}"}

    def _store_result(self, arguments: dict) -> tuple[int, dict]:
        self._store_calls += 1
        if self.store_refusal is not None:
            return 200, {"success": True, "result": dict(self.store_refusal)}
        failing = not self.store_ok or self._store_calls == self.fail_store_on
        if failing:
            # The shape the server ACTUALLY produces for a tool-level failure:
            # an HTTP 200 whose outer envelope is a success, carrying
            # error_response()'s {"success": false, "error": ...} in `result`.
            return 200, {
                "success": True,
                "result": {"success": False, "error": self.store_error},
            }
        discovery_id = f"disc-{len(self.rows) + 1}"
        if self.echo_stores:
            self.rows.append(
                {
                    "discovery_id": discovery_id,
                    "summary": arguments.get("summary"),
                    "details": arguments.get("details"),
                    "tags": list(arguments.get("tags") or []),
                }
            )
        result: dict = {"message": "stored"}
        if not self.store_omit_discovery_id:
            result["discovery_id"] = discovery_id
        return 200, {"success": True, "result": result}

    def _search_result(self, arguments: dict) -> dict:
        wanted_tags = [str(t) for t in (arguments.get("tags") or [])]
        if wanted_tags:
            rows = [r for r in self.rows if set(r.get("tags") or []) & set(wanted_tags)]
        elif self.fts_blind:
            rows = []
        else:
            # Return everything: the producer's own exactness is what must
            # decide, so the stub never pre-filters a text query for it.
            rows = list(self.rows)
        return {"discoveries": rows, "count": len(rows), "search_mode_used": "fts"}


def _make_handler(stub: StubGovernance):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _send(self, status: int, envelope: dict) -> None:
            payload = json.dumps(envelope).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            body = json.loads(raw)
            stub.requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )

            if stub.unavailable_once and stub._served_503 == 0:
                stub._served_503 += 1
                payload = json.dumps(
                    {
                        "ok": False,
                        "error": "governance_temporarily_unavailable",
                        "retry_after_seconds": 0,
                    }
                ).encode()
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if stub.http_error is not None:
                status, envelope = stub.http_error
                self._send(status, envelope)
                return

            status, envelope = stub.reply(body.get("arguments") or {})
            self._send(status, envelope)

        def log_message(self, *args) -> None:
            pass

    return Handler


@pytest.fixture
def producer_env(tmp_path):
    """PATH-front stub gh + isolated data dir + a live stub governance server."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    gh_path = stub_dir / "gh"
    gh_path.write_text(STUB_GH)
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)

    data = tmp_path / "data"
    data.mkdir()

    stub = StubGovernance()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data)
    env["GOV_REST_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1/tools/call"
    env.pop("UNITARES_MCP_BEARER_TOKEN", None)
    env.pop("GITHUB_REPOSITORY", None)

    try:
        yield env, data, stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def issue(
    number: int,
    fingerprint: str | None,
    *,
    title: str = "guard finding",
    state: str = "OPEN",
    body: str | None = None,
    comments: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("ci-finding",),
) -> dict:
    """One issue in the shape `gh issue list --json <GH_JSON_FIELDS>` returns."""
    marker = MARKER.format(fingerprint) + "\n" if fingerprint else ""
    default_body = f"{marker}Branch claude/dead-branch has 1 unlanded commit."
    return {
        "number": number,
        "title": title,
        "body": default_body if body is None else body,
        "url": f"https://github.com/example/repo/issues/{number}",
        "state": state,
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-02T00:00:00Z",
        "labels": [{"name": name} for name in labels],
        "comments": [
            {
                "body": text,
                "url": (
                    f"https://github.com/example/repo/issues/{number}"
                    f"#issuecomment-{index}"
                ),
            }
            for index, text in enumerate(comments, start=1)
        ],
    }


def recorded_row(fingerprint: str, issue_number: int, *, discovery_id: str) -> dict:
    """A graph row in the shape this producer itself writes."""
    return {
        "discovery_id": discovery_id,
        "summary": f"CI finding [x] issue #{issue_number}: seeded",
        "details": (
            f"finding-fingerprint: {fingerprint}\ngithub-issue: #{issue_number}\n"
        ),
        "tags": ["ci-finding", f"issue-{issue_number}"],
    }


def seed_issues(data: Path, issues: list[dict]) -> None:
    (data / "issue_list.json").write_text(json.dumps(issues))


def run_producer(
    env: dict, *flags: str, repo: str = "example/repo"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(PRODUCER), "--repo", repo, *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def gh_calls(data: Path) -> str:
    log = data / "calls.log"
    return log.read_text() if log.exists() else ""


# --- the store happens (the case that matters most) -------------------------


def test_marked_finding_is_stored_under_apply(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN, labels=("ci-finding", "severity:high"))])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "storing" in proc.stdout
    assert "stored=1" in proc.stdout

    assert len(stub.stores) == 1, "an --apply run with a new finding must WRITE"
    stored = stub.stores[0]
    assert stored["discovery_type"] == "bug_found"
    assert stored["severity"] == "medium"
    # the dedup key is the PAIR, and both halves must be recoverable
    assert f"finding-fingerprint: {FP_ORPHAN}" in stored["details"]
    assert "github-issue: #55" in stored["details"]
    assert "https://github.com/example/repo/issues/55" in stored["details"]
    assert "orphan-push-guard" in stored["summary"]
    assert "55" in stored["summary"]
    # the guard's own severity claim lives in the labels, recorded as text
    assert "github-labels: ci-finding, severity:high" in stored["details"]
    assert "github-title: guard finding" in stored["details"]
    assert "finding-origin: issue body" in stored["details"]

    # reachable from more than one vocabulary
    tags = stored["tags"]
    assert "ci-finding" in tags
    assert "github" in tags
    assert "orphan-push-guard" in tags
    assert "orphan-push-guard-claude-dead-branch" in tags
    assert "issue-55" in tags
    assert "orphan" in tags and "push" in tags, "class words must come from the slug"
    assert len(set(tags)) == len(tags), "tags must be deduplicated"


def test_search_precedes_every_store(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr

    actions = [r["body"]["arguments"].get("action") for r in stub.requests]
    assert actions[0] == "search", "search-before-write is the KG discipline"
    assert "store" in actions
    assert actions.index("search") < actions.index("store")

    probe = stub.searches[0]
    assert probe["query"] == FP_ORPHAN
    assert probe["search_mode"] == "fts"
    assert probe["include_archived"] is True
    assert probe["include_cold"] is True

    assert all(r["body"]["name"] == "knowledge" for r in stub.requests)
    assert all(r["path"].endswith("/v1/tools/call") for r in stub.requests)
    assert all(
        r["headers"].get("Content-Type") == "application/json" for r in stub.requests
    )


def test_marker_in_a_comment_is_ingested(producer_env):
    # Issue #2168's recorded shape: the BODY carries the orphan-push-guard
    # marker and the only COMMENT carries the stranded-work-audit one
    # (capture-stage-2168-artifacts-arm.md, F27). A body-only reader records
    # one of the two findings and silently loses the other, on the very
    # incident this producer exists to repair.
    env, data, stub = producer_env
    seed_issues(
        data,
        [
            issue(
                2168,
                FP_ORPHAN,
                comments=(
                    f"{MARKER.format(FP_STRANDED)}\n"
                    "The weekly stranded-work audit found branch work requiring "
                    "a decision.",
                ),
            )
        ],
    )

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores) == 2, "both markers on one issue are separate findings"

    by_fingerprint = {
        FP_ORPHAN
        if f"finding-fingerprint: {FP_ORPHAN}" in s["details"]
        else FP_STRANDED: s
        for s in stub.stores
    }
    assert set(by_fingerprint) == {FP_ORPHAN, FP_STRANDED}
    assert "finding-origin: issue body" in by_fingerprint[FP_ORPHAN]["details"]

    from_comment = by_fingerprint[FP_STRANDED]
    assert "finding-origin: comment 1" in from_comment["details"]
    assert "weekly stranded-work audit" in from_comment["details"]
    assert "stranded-work-audit" in from_comment["tags"]
    assert "issue-2168" in from_comment["tags"]


def test_recurrence_under_a_new_issue_is_stored_as_a_linked_followup(producer_env):
    # The guards refile a fingerprint as a NEW issue once the previous one is
    # closed. Keying dedup on the fingerprint alone reported that recurrence as
    # already-recorded and wrote nothing, so the record could not see it.
    env, data, stub = producer_env
    seed_issues(data, [issue(99, FP_ORPHAN)])
    stub.rows = [recorded_row(FP_ORPHAN, 55, discovery_id="disc-earlier")]

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "already-recorded=0" in proc.stdout
    assert "followup" in proc.stdout
    assert len(stub.stores) == 1, "a recurrence under a new issue must be RECORDED"

    stored = stub.stores[0]
    assert stored["response_to"] == {
        "discovery_id": "disc-earlier",
        "response_type": "follow_up",
    }
    assert "github-issue: #99" in stored["details"]


# --- the store does NOT happen (the cases that keep it honest) --------------


def test_default_run_is_a_dry_run(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env)
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout
    assert "would-store" in proc.stdout
    assert "would-store=1" in proc.stdout
    assert "--apply" in proc.stdout  # tells the reader how to write

    assert stub.searches, "a dry run still has to search to know what it would do"
    assert stub.stores == [], "the default run must write nothing"


def test_second_apply_run_is_idempotent(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    first = run_producer(env, "--apply")
    assert first.returncode == 0, first.stderr
    assert len(stub.stores) == 1

    stub.reset_traffic()
    second = run_producer(env, "--apply")
    assert second.returncode == 0, second.stderr
    assert "already-recorded" in second.stdout
    assert "already-recorded=1" in second.stdout
    assert stub.stores == [], "the row the first run wrote must suppress the second"


def test_unrelated_search_rows_do_not_suppress_the_store(producer_env):
    # A row for a DIFFERENT finding, and one whose fingerprint merely has this
    # one as a prefix. Neither carries this fingerprint, so the store must run.
    env, data, stub = producer_env
    seed_issues(data, [issue(77, "merge-content-check pr-16")])
    stub.rows = [
        recorded_row(FP_ORPHAN, 55, discovery_id="disc-a"),
        recorded_row(FP_CONTENT, 90, discovery_id="disc-b"),
        {
            "discovery_id": "disc-c",
            "summary": "unrelated note about merge behaviour",
            "details": "",
            "tags": ["merge"],
        },
    ]

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores) == 1, "neighbours in the result set are not a match"
    assert "finding-fingerprint: merge-content-check pr-16" in stub.stores[0]["details"]
    assert "response_to" not in stub.stores[0], "an unrelated row is not a parent"


def test_prose_mention_of_a_guard_slug_does_not_suppress_the_store(producer_env):
    # Three of the four live emitters use a CONSTANT, word-shaped fingerprint
    # with no variable part, so a bare substring test let any pre-existing note
    # whose prose contained that phrase count as the finding already being
    # recorded - permanently suppressing that guard. Only the labelled forms
    # count now.
    env, data, stub = producer_env
    seed_issues(data, [issue(300, FP_STRANDED)])
    stub.rows = [
        {
            "discovery_id": "disc-prose",
            "summary": "the stranded-work-audit workflow times out on large sets",
            "details": (
                "Investigated 2026-08. The stranded-work-audit job exceeded its "
                "step budget."
            ),
            "tags": ["ci", "workflow"],
        }
    ]

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "already-recorded=0" in proc.stdout, "prose is not a recorded finding"
    assert len(stub.stores) == 1, "prose that names a guard is not that guard's record"
    assert "response_to" not in stub.stores[0]


def test_fenced_marker_is_not_a_finding(producer_env):
    # A hand-filed question that quotes a marker inside a code fence used to
    # take over the real finding's dedup key: whichever issue gh returned first
    # won it, so the record held the question and the guard finding never
    # landed.
    env, data, stub = producer_env
    quoted = issue(
        90,
        None,
        title="how does dedup work?",
        body=(
            "I am trying to understand the dedup contract. The guards write:\n"
            "```\n" + MARKER.format(FP_ORPHAN) + "\n```\n"
            "Is that per branch?\n"
        ),
    )
    seed_issues(data, [quoted, issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "skipped-no-marker  #90" in proc.stdout
    assert "skipped-no-marker=1" in proc.stdout
    assert len(stub.stores) == 1, "the real guard finding must still land"
    assert "github-issue: #55" in stub.stores[0]["details"]


def test_exact_tag_probe_backstops_a_blind_text_search(producer_env):
    # The full-text tokenizer mangles slugs carrying `/` and `-`. When the text
    # probe comes back empty, the exact tag filter must still find the row -
    # otherwise every run duplicates every finding.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.fts_blind = True
    stub.rows = [
        {
            "discovery_id": "disc-1",
            "summary": "CI finding [orphan-push-guard] issue #55: guard finding",
            "details": "(details elided by the server)",
            "tags": [
                "orphan-push-guard-claude-dead-branch",
                "issue-55",
                "ci-finding",
            ],
        }
    ]

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "already-recorded" in proc.stdout
    assert stub.stores == []
    assert len(stub.searches) == 2, (
        "the tag probe runs only after the text probe misses"
    )
    assert stub.searches[1]["tags"] == ["orphan-push-guard-claude-dead-branch"]


def test_unmarked_issue_is_skipped_and_counted(producer_env):
    env, data, stub = producer_env
    seed_issues(
        data, [issue(41, None, title="hand-filed, no marker"), issue(55, FP_ORPHAN)]
    )

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "skipped-no-marker" in proc.stdout
    assert "skipped-no-marker=1" in proc.stdout
    assert "#41" in proc.stdout, "a skip must be visible, not silent"
    assert "counted, not dropped" in proc.stdout
    assert len(stub.stores) == 1, "the marked sibling is still ingested"


def test_closed_issue_is_reported_and_not_stored(producer_env):
    # knowledge(action='store') carries no status field, so a closed finding
    # would enter the graph as an OPEN entry with no resolution condition -
    # unfinished work to every later sweep. `--state all` audits the board.
    env, data, stub = producer_env
    seed_issues(
        data,
        [issue(30, FP_CONTENT, state="CLOSED"), issue(55, FP_ORPHAN)],
    )

    proc = run_producer(env, "--apply", "--state", "all")
    assert proc.returncode == 0, proc.stderr
    assert "skipped-closed     #30" in proc.stdout
    assert "skipped-closed=1" in proc.stdout
    assert "cannot land resolved" in proc.stdout
    assert len(stub.stores) == 1, "only the open sibling is stored"
    assert "github-issue: #55" in stub.stores[0]["details"]


def test_marker_regex_tolerates_surrounding_whitespace(producer_env):
    env, data, stub = producer_env
    marked = issue(55, None)
    marked["body"] = f"  <!--   finding-fingerprint:   {FP_ORPHAN}   -->\nbody"
    seed_issues(data, [marked])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores) == 1
    assert f"finding-fingerprint: {FP_ORPHAN}" in stub.stores[0]["details"]


# --- fail loud --------------------------------------------------------------


def test_gh_failure_exits_nonzero(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    (data / "issue_list.fail").touch()

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "Bad credentials" in proc.stderr, "the underlying message must survive"
    assert stub.requests == [], "a failed read must not write anything"


def test_missing_gh_exits_nonzero(producer_env, tmp_path):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    # A PATH carrying an interpreter and nothing else: gh is unreachable.
    only_python = tmp_path / "only-python"
    only_python.mkdir()
    (only_python / "python3").symlink_to(sys.executable)
    env = {**env, "PATH": str(only_python)}

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "gh CLI not found" in proc.stderr
    assert stub.requests == []


def test_tool_level_error_inside_a_success_envelope_exits_nonzero(producer_env):
    # The shape the server really produces: HTTP 200, outer success true, the
    # failure inside `result`. Checking the outer envelope alone reported this
    # refused write as `stored=1` and exited 0.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.store_ok = False
    stub.store_error = "SESSION_ERROR: agent_id required for knowledge writes"

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0, (
        "a bridge that reports success while writing nothing is the bug"
    )
    assert "agent_id required for knowledge writes" in proc.stderr
    assert "stored=1" not in proc.stdout
    assert "stored=0" in proc.stdout, "the tally must not claim a refused write"


def test_identity_refusal_payload_exits_nonzero(producer_env):
    # strict_identity_refusal_payload is documented as "a structured
    # success-shape, not an error": it carries no `error` key and no
    # `success: false`. This producer never onboards, so on a deployment with
    # the strict gate armed the refusal is the EXPECTED answer to a write.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.store_refusal = IDENTITY_REFUSAL

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "identity_required" in proc.stderr
    assert "onboard(force_new=true)" in proc.stderr
    assert "stored=1" not in proc.stdout


def test_refused_search_is_not_read_as_not_recorded(producer_env):
    # A refusal on the READ side is the more dangerous half: treated as "no
    # rows", it would duplicate every finding on every run.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.search_payload = IDENTITY_REFUSAL

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "identity_required" in proc.stderr
    assert stub.stores == [], "a refused search must never authorize a write"


def test_store_without_discovery_id_exits_nonzero(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.store_omit_discovery_id = True

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "no discovery_id" in proc.stderr
    assert "stored=1" not in proc.stdout


def test_unrecognized_search_shape_exits_nonzero(producer_env):
    # A serializer change that renames the rows key must not silently become
    # "nothing is recorded", which is a duplicate write on every run.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.search_payload = {
        "success": True,
        "message": "Found 1 discovery(ies)",
        "items": [recorded_row(FP_ORPHAN, 55, discovery_id="disc-1")],
    }

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "no recognizable result rows" in proc.stderr
    assert stub.stores == []


def test_partial_run_reports_the_tally_before_failing(producer_env):
    # Exit 1 does not mean "verified nothing": writes already committed in the
    # run must be reported, or a re-run has no statement of what landed.
    env, data, stub = producer_env
    seed_issues(
        data,
        [
            issue(1, "guard-a claude/one"),
            issue(2, "guard-b claude/two"),
            issue(3, "guard-c claude/three"),
        ],
    )
    stub.fail_store_on = 2
    stub.store_error = "storage unavailable"

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "storage unavailable" in proc.stderr
    assert "stored=1" in proc.stdout, "the one write that landed must be reported"
    assert "stopped after storing 1 finding(s)" in proc.stderr
    assert "issue(s) not examined" in proc.stderr
    assert len(stub.stores) == 2, "issue #3 was never reached"


def test_http_error_body_message_survives(producer_env):
    # src/http_routes/tools.py returns 400/500 with {"error", "error_type"} in
    # the body. Reporting only "HTTP 400 Bad Request" discards the reason.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.http_error = (
        400,
        {
            "success": False,
            "error": "Invalid severity 'urgent'. Valid: ['critical', 'high', ...]",
            "error_type": "ValidationError",
        },
    )

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "HTTP 400" in proc.stderr
    assert "Invalid severity 'urgent'" in proc.stderr


def test_transport_failure_exits_nonzero(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    # An unbound loopback port: nothing is listening.
    env = {**env, "GOV_REST_URL": "http://127.0.0.1:1/v1/tools/call"}

    proc = run_producer(env, "--apply")
    assert proc.returncode != 0
    assert "failed" in proc.stderr.lower()
    assert stub.requests == []


def test_limit_ceiling_is_reported_as_degraded(producer_env):
    # gh truncates at --limit without saying so, so at the ceiling the producer
    # cannot tell a full board from a clipped one. Degraded is never silent:
    # the tally still prints, and the run does not claim to have completed.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply", "--limit", "1")
    assert proc.returncode != 0
    assert "--limit ceiling" in proc.stderr
    assert "higher --limit" in proc.stderr
    assert "stored=1" in proc.stdout, "what landed is still reported"
    assert len(stub.stores) == 1


def test_empty_finding_list_is_a_clean_zero(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "stored=0" in proc.stdout
    assert stub.requests == []
    assert "issue list" in gh_calls(data)


# --- envelope contract ------------------------------------------------------


def test_severity_is_never_high_or_critical(producer_env):
    # knowledge(action='store') refuses high/critical without supporting
    # conditions, and an automated bridge has no standing to assert impact.
    env, data, stub = producer_env
    seed_issues(
        data,
        [
            issue(55, FP_ORPHAN, title="orphan push"),
            issue(56, FP_CONTENT, title="content missing"),
            issue(57, "automerge-disarm-detector", title="disarmed"),
            issue(58, FP_STRANDED, title="stranded"),
        ],
    )

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores) == 4
    assert {s["severity"] for s in stub.stores} <= {"low", "medium"}
    # class tags stay derived per finding, never a shared hardcoded list
    by_slug = {s["summary"]: s["tags"] for s in stub.stores}
    assert any("automerge" in tags and "disarm" in tags for tags in by_slug.values())
    assert any("stranded" in tags and "work" in tags for tags in by_slug.values())


def test_bearer_header_is_sent_only_when_configured(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert all("Authorization" not in r["headers"] for r in stub.requests)

    stub.reset_traffic()
    stub.rows.clear()
    seed_issues(data, [issue(56, FP_CONTENT)])
    proc = run_producer({**env, "UNITARES_MCP_BEARER_TOKEN": "tok-123"}, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert stub.requests
    assert all(
        r["headers"].get("Authorization") == "Bearer tok-123" for r in stub.requests
    )


def test_typed_unavailable_503_is_retried_once(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])
    stub.unavailable_once = True

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "HTTP 503" in proc.stdout
    assert len(stub.searches) >= 1
    assert len(stub.stores) == 1, "the retry has to actually complete the work"


def test_long_issue_body_is_trimmed_to_the_named_bound(producer_env):
    env, data, stub = producer_env
    big = issue(55, FP_ORPHAN)
    big["body"] = MARKER.format(FP_ORPHAN) + "\n" + ("x" * 20000)
    seed_issues(data, [big])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    details = stub.stores[0]["details"]
    assert "x" * 100 in details
    assert len(details) < 6000, (
        "the issue is the full record; the discovery points at it"
    )
    assert "trimmed at 4000 chars" in details
    assert "https://github.com/example/repo/issues/55" in details


def test_summary_stays_within_the_store_summary_bound(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN, title="t" * 400)])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores[0]["summary"]) <= 200
    assert stub.stores[0]["summary"].endswith("...")
    # the untruncated title is still recoverable from the record
    assert "github-title: " + "t" * 400 in stub.stores[0]["details"]


def test_no_ephemeral_tag_is_derivable_from_any_guard_slug(producer_env):
    # An EPHEMERAL_TAG would let the lifecycle pass archive the record the
    # bridge exists to create (7 days; bug_found is not a PERMANENT_TYPE). The
    # invariant has to hold for slugs nobody has written yet, so these are
    # adversarial rather than the live vocabulary: a guard named for what it
    # detects mints `test`, `scratch` or `demo` from its own name.
    env, data, stub = producer_env
    seed_issues(
        data,
        [
            issue(1, "flaky-test-detector pr-1"),
            issue(2, "scratch-branch-guard main"),
            issue(3, "demo-merge-check claude/x"),
        ],
    )

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.stores) == 3
    for stored in stub.stores:
        forbidden = {"ephemeral", "temp", "scratch", "test", "demo"}
        assert not (set(stored["tags"]) & forbidden), stored["tags"]
    # the non-colliding class words still land, so the filter is not a blanket
    all_tags = {tag for stored in stub.stores for tag in stored["tags"]}
    assert {"flaky", "detector", "branch", "guard", "merge", "check"} <= all_tags


def test_a_forbidden_fingerprint_tag_skips_the_tag_probe(producer_env):
    # Pathological but reachable: a one-word fingerprint that normalizes to an
    # EPHEMERAL_TAG. derive_tags drops it, so probing the graph for it would
    # ask a question no record of ours can answer; the labelled text arm still
    # carries dedup.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, "demo")])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert len(stub.searches) == 1, "no tag probe for a tag that is never minted"
    assert "demo" not in stub.stores[0]["tags"]
    assert "finding-fingerprint: demo" in stub.stores[0]["details"]


def test_tag_normalizer_mirrors_the_servers_spelling_fold(producer_env):
    # src/knowledge_graph.py::normalize_tags ends with the SPELLING_VARIANTS
    # fold (postgresql -> postgres), applied on every write AND every
    # tag-filtered search. A local mirror that stopped one step short would
    # compare a tag the server never stored.
    env, data, stub = producer_env
    seed_issues(data, [issue(55, "postgresql")])

    proc = run_producer(env, "--apply")
    assert proc.returncode == 0, proc.stderr
    tags = stub.stores[0]["tags"]
    assert "postgres" in tags
    assert "postgresql" not in tags
    assert stub.searches[1]["tags"] == ["postgres"]


# --- CLI surface ------------------------------------------------------------


def test_apply_and_dry_run_together_are_rejected(producer_env):
    env, data, _ = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply", "--dry-run")
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stderr


def test_malformed_repo_is_rejected_before_any_call(producer_env):
    env, data, stub = producer_env
    seed_issues(data, [issue(55, FP_ORPHAN)])

    proc = run_producer(env, "--apply", repo="not-a-repo")
    assert proc.returncode != 0
    assert "owner/name" in proc.stderr
    assert gh_calls(data) == ""
    assert stub.requests == []


def test_repo_defaults_from_github_repository_env(producer_env):
    env, data, _ = producer_env
    seed_issues(data, [])

    proc = subprocess.run(
        ["python3", str(PRODUCER)],
        env={**env, "GITHUB_REPOSITORY": "example/from-env"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "-R example/from-env" in gh_calls(data)


def test_state_all_is_passed_through(producer_env):
    env, data, _ = producer_env
    seed_issues(data, [])

    proc = run_producer(env, "--state", "all")
    assert proc.returncode == 0, proc.stderr
    assert "--state all" in gh_calls(data)
    assert "--label ci-finding" in gh_calls(data)


def test_comment_bodies_are_requested_from_gh(producer_env):
    # The recurrence channel is comments, so the read has to ask for them.
    env, data, _ = producer_env
    seed_issues(data, [])

    proc = run_producer(env)
    assert proc.returncode == 0, proc.stderr
    calls = gh_calls(data)
    assert "comments" in calls
    assert "updatedAt" in calls
    assert "labels" in calls
    assert "--limit 200" in calls, (
        "no narrower than merge_loss_common.find_open_finding"
    )
