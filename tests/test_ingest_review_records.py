"""Tests for the review-history knowledge-graph producer
(scripts/dev/ingest_review_records.py).

Exercised end-to-end as a subprocess against a stub `gh` on PATH and a
loopback HTTP server standing in for the governance REST endpoint, the same
harness shape as tests/test_ingest_ci_findings.py and for the same reason: the
positive cases assert on the REQUEST BODY the stub received, because an exit 0
with no store is the failure this producer exists to remove.

The review and disposition comments are built with review_gate's own
render_marker, so a change to the gate's record format breaks these tests
rather than silently leaving the producer reading a format nobody writes.
"""

from __future__ import annotations

import importlib.util
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
PRODUCER = REPO_ROOT / "scripts" / "dev" / "ingest_review_records.py"
REPO = "example/repo"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


producer = _load("ingest_review_records_under_test", PRODUCER)
gate = producer.gate

IDENTITY_REFUSAL = {
    "status": "identity_required",
    "tool": "knowledge",
    "hint": "Bind first with onboard(force_new=true).",
    "next_step": "onboard(force_new=true)",
    "rollout_flag": "STRICT_IDENTITY_REQUIRED",
}

STUB_GH = """#!/usr/bin/env bash
d="$STUB_DATA"
printf '%s\\n' "$*" >> "$d/calls.log"
case "$*" in
  "pr list"*) cat "$d/pr_list.json" 2>/dev/null || echo "[]" ;;
  "pr view "*) n=$(echo "$*" | awk '{print $3}'); cat "$d/pr_$n.json" ;;
  "api repos/"*"/comments"*)
    n=$(echo "$*" | sed -E 's#.*issues/([0-9]+)/comments.*#\\1#')
    python3 -c "import json,sys; [print(json.dumps(c)) for c in json.load(open(sys.argv[1]))]" "$d/comments_$n.json" ;;
  *) echo "stub gh: unhandled: $*" >&2; exit 64 ;;
esac
"""


class StubGovernance:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.rows: list[dict] = []
        self.keep_status = True
        self.store_refusal: dict | None = None

    def bodies(self, action: str) -> list[dict]:
        return [r["arguments"] for r in self.requests if r["arguments"].get("action") == action]

    def reply(self, arguments: dict) -> dict:
        action = arguments.get("action")
        if action == "search":
            wanted = set(arguments.get("tags") or [])
            rows = [r for r in self.rows if wanted & set(r.get("tags") or [])]
            return {"discoveries": rows, "count": len(rows)}
        if action == "store":
            if self.store_refusal is not None:
                return dict(self.store_refusal)
            discovery_id = f"disc-{len(self.rows) + 1}"
            self.rows.append({
                "discovery_id": discovery_id,
                "summary": arguments.get("summary"),
                "details": arguments.get("details"),
                "tags": list(arguments.get("tags") or []),
                "status": arguments.get("status") or "open",
            })
            return {"discovery_id": discovery_id, "message": "stored"}
        if action == "update":
            for row in self.rows:
                if row["discovery_id"] == arguments.get("discovery_id") and self.keep_status:
                    row["status"] = arguments.get("status") or row["status"]
                    row["resolved_at"] = "2026-09-26T00:00:00Z"
            return {"success": True, "message": "updated"}
        if action == "get":
            for row in self.rows:
                if row["discovery_id"] == arguments.get("discovery_id"):
                    return {"discovery": dict(row)}
            return {"success": False, "error": "not found"}
        return {"success": False, "error": f"stub: unhandled action {action!r}"}


def _handler(stub: StubGovernance):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            stub.requests.append({"name": body.get("name"), "arguments": body.get("arguments") or {}})
            payload = json.dumps({"success": True, "result": stub.reply(body.get("arguments") or {})}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    return Handler


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "data"
    data.mkdir()
    stub = StubGovernance()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environ = os.environ.copy()
    environ["PATH"] = f"{bin_dir}:{environ['PATH']}"
    environ["STUB_DATA"] = str(data)
    environ["GOV_REST_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1/tools/call"
    environ.pop("UNITARES_MCP_BEARER_TOKEN", None)
    environ.pop("GITHUB_REPOSITORY", None)
    try:
        yield environ, data, stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- fixtures in the gate's own format ---------------------------------------

_ids = iter(range(1000, 100000))


def review(pr: int, key: str, verdict: str, n: int, reviewer: str = "codex",
           text: str = "finding text", association: str = "OWNER") -> dict:
    rec = gate.Record(key=key, verdict=verdict, findings=n, disposed=False, reviewer=reviewer)
    cid = next(_ids)
    return {
        "html_url": f"https://github.com/{REPO}/pull/{pr}#issuecomment-{cid}",
        "created_at": "2026-09-01T00:00:00Z",
        "author_association": association,
        "body": gate.render_marker(rec) + f"\n### Review record — {verdict}\n\n{text}",
    }


def dispose(pr: int, answered: dict, key: str, n: int, items: list[str],
            reviewer: str = "codex") -> dict:
    rec = gate.Record(key=key, verdict="FINDINGS", findings=n, disposed=True, reviewer=reviewer)
    cid = next(_ids)
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))
    return {
        "html_url": f"https://github.com/{REPO}/pull/{pr}#issuecomment-{cid}",
        "created_at": "2026-09-01T01:00:00Z",
        "author_association": "OWNER",
        "body": (gate.render_marker(rec)
                 + f"\n### Review record — dispositions for FINDINGS({n}) — {answered['html_url']}\n\n"
                 + lines + "\n\n---\n_Generated_"),
    }


def pull(number: int, state: str = "MERGED") -> dict:
    return {"number": number, "title": f"change {number}", "url": f"https://github.com/{REPO}/pull/{number}",
            "state": state, "author": {"login": "someone"}, "mergedAt": None, "closedAt": None}


def seed(data: Path, pulls: list[dict], comments: dict[int, list[dict]]) -> None:
    (data / "pr_list.json").write_text(json.dumps(pulls))
    for p in pulls:
        (data / f"pr_{p['number']}.json").write_text(json.dumps(p))
    for number, items in comments.items():
        (data / f"comments_{number}.json").write_text(json.dumps(items))


def run(environ: dict, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(PRODUCER), "--repo", REPO, *flags],
                          env=environ, capture_output=True, text=True, timeout=60)


KEY_A = "a" * 64
KEY_B = "b" * 64


def one_disposed_pr(data: Path, number: int = 7) -> None:
    first = review(number, KEY_A, "FINDINGS", 2)
    seed(data, [pull(number)], {number: [
        first,
        dispose(number, first, KEY_A, 2, ["Fixed in abc123: the guard now checks.",
                                          "Rebutted: the caller already validates."]),
    ]})


# --- the store happens --------------------------------------------------------


def test_history_is_stored_resolved_under_apply(env):
    environ, data, stub = env
    one_disposed_pr(data)
    proc = run(environ, "--apply")
    assert proc.returncode == 0, proc.stderr
    [store] = stub.bodies("store")
    assert store["discovery_type"] == "observation"
    # closed by an update, which stamps resolved_at, not by a status on the store
    assert "status" not in store
    [update] = stub.bodies("update")
    assert update["status"] == "resolved" and update["discovery_id"] == "disc-1"
    assert "Historical record" in update["resolution_notes"]
    assert store["severity"] == "low"
    details = store["details"]
    assert details.splitlines()[0] == f"review-record: {REPO}#7"
    assert "1. [accepted] Fixed in abc123" in details
    assert "2. [rebutted] Rebutted: the caller already validates." in details
    assert "self-attested" in details and "not an outcome_event" in details
    assert "agent-identity: not recorded" in details
    assert set(store["tags"]) >= {"review-record", "review-gate", "review-pr-7", "reviewer-codex"}
    assert "1 accepted" in store["summary"] and "1 rebutted" in store["summary"]
    assert stub.bodies("get"), "the stored status must be read back"


def test_only_the_knowledge_tool_is_ever_called(env):
    # Dispositions are self-attested: nothing may reach outcome_event, the
    # anchor tiers, or the registered EISV read.
    environ, data, stub = env
    one_disposed_pr(data)
    assert run(environ, "--apply").returncode == 0
    assert stub.requests
    assert {r["name"] for r in stub.requests} == {"knowledge"}


def test_search_precedes_the_store_and_a_second_run_is_idempotent(env):
    environ, data, stub = env
    one_disposed_pr(data)
    assert run(environ, "--apply").returncode == 0
    actions = [r["arguments"]["action"] for r in stub.requests]
    assert actions == ["search", "store", "update", "get"]
    stub.requests.clear()
    proc = run(environ, "--apply")
    assert proc.returncode == 0
    assert not stub.bodies("store")
    assert "already-recorded" in proc.stdout


def test_default_run_writes_nothing(env):
    environ, data, stub = env
    one_disposed_pr(data)
    proc = run(environ)
    assert proc.returncode == 0
    assert not stub.bodies("store")
    assert "would-store" in proc.stdout


def test_a_row_sharing_the_tag_without_the_marker_does_not_suppress(env):
    environ, data, stub = env
    one_disposed_pr(data, number=24)
    stub.rows.append({"discovery_id": "x", "tags": ["review-pr-24"],
                      "details": f"review-record: {REPO}#2475\nsomeone else's note"})
    assert run(environ, "--apply").returncode == 0
    assert len(stub.bodies("store")) == 1


# --- what is and is not read ---------------------------------------------------


def test_open_pull_request_is_skipped(env):
    environ, data, stub = env
    first = review(9, KEY_A, "FINDINGS", 1)
    seed(data, [pull(9, state="OPEN")], {9: [first]})
    proc = run(environ, "--apply")
    assert proc.returncode == 0
    assert not stub.bodies("store")
    assert "skipped-open" in proc.stdout


def test_untrusted_comment_is_not_a_record(env):
    environ, data, stub = env
    forged = review(8, KEY_A, "FINDINGS", 3, association="NONE")
    seed(data, [pull(8)], {8: [forged]})
    proc = run(environ, "--apply")
    assert proc.returncode == 0
    assert not stub.bodies("store")
    assert "no-reviews=1" in proc.stdout


def test_disposition_citing_another_record_does_not_apply(env):
    environ, data, stub = env
    first = review(5, KEY_A, "FINDINGS", 1)
    other = review(5, KEY_A, "FINDINGS", 1)
    seed(data, [pull(5)], {5: [first, dispose(5, other, KEY_A, 1, ["Fixed in abc."])]})
    assert run(environ, "--apply").returncode == 0
    [store] = stub.bodies("store")
    assert "round 1: FINDINGS(1)" in store["details"]
    # the first record's finding stays without a disposition
    assert "[undisposed]" in store["details"]


def test_superseded_and_undisposed_are_told_apart(env):
    environ, data, stub = env
    seed(data, [pull(6)], {6: [
        review(6, KEY_A, "FINDINGS", 1),
        review(6, KEY_B, "FINDINGS", 1),
    ]})
    assert run(environ, "--apply").returncode == 0
    details = stub.bodies("store")[0]["details"]
    round1, round2 = details.split("round 2:")
    assert "[superseded]" in round1
    assert "[undisposed]" in round2


def test_clean_rounds_stay_in_the_history(env):
    environ, data, stub = env
    seed(data, [pull(4)], {4: [review(4, KEY_A, "FINDINGS", 1), review(4, KEY_B, "CLEAN", 0)]})
    assert run(environ, "--apply").returncode == 0
    details = stub.bodies("store")[0]["details"]
    assert "round 2: CLEAN · reviewer codex" in details


def test_details_stay_within_the_bound(env):
    environ, data, stub = env
    huge = "x" * 20000
    seed(data, [pull(3)], {3: [review(3, f"{i:064x}", "FINDINGS", 1, text=huge) for i in range(30)]})
    assert run(environ, "--apply").returncode == 0
    details = stub.bodies("store")[0]["details"]
    assert len(details) <= producer.MAX_DETAILS_CHARS
    assert "round 30:" in details, "the round list must survive trimming"


# --- failing loud --------------------------------------------------------------


def test_a_status_the_server_did_not_keep_fails_the_run(env):
    environ, data, stub = env
    stub.keep_status = False
    one_disposed_pr(data)
    proc = run(environ, "--apply")
    assert proc.returncode == 1
    assert "would read as open work" in proc.stderr
    assert "stored=0" in proc.stdout


def test_client_session_id_rides_on_every_call(env):
    environ, data, stub = env
    one_disposed_pr(data)
    assert run(environ, "--apply", "--client-session-id", "sess-123").returncode == 0
    assert stub.requests
    assert all(r["arguments"].get("client_session_id") == "sess-123" for r in stub.requests)


def test_identity_refusal_on_store_fails_the_run(env):
    environ, data, stub = env
    stub.store_refusal = IDENTITY_REFUSAL
    one_disposed_pr(data)
    proc = run(environ, "--apply")
    assert proc.returncode == 1
    assert "identity write gate" in proc.stderr


# --- the disposition reading ---------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Accepted, not rebutted, and deferred rather than fixed in this PR.", "accepted"),
    ("rebutted, fixed in #2414: the two refusal routes named here", "rebutted"),
    ("Deferred (accepted). The docstring's latency parity is wrong", "accepted"),
    ("[P2] Accepted and fixed in the doc", "accepted"),
    ("Fixed in the next commit: the vacuous equality", "accepted"),
    ("Rebutted: `offset` is read by the details handler", "rebutted"),
    ("Kept deliberately, with the comment corrected", "unclassified"),
])
def test_classify_disposition(text, expected):
    assert producer.classify_disposition(text) == expected


def test_disposition_items_join_continuation_lines():
    body = "heading\n\n1. Fixed in abc:\n   the second line.\n2. Rebutted: no.\n\n---\n_Generated_"
    assert producer.parse_disposition_items(body, 2) == {
        1: "Fixed in abc: the second line.",
        2: "Rebutted: no.",
    }
