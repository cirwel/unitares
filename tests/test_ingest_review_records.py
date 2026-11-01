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
        self.search_override: dict | None = None

    def bodies(self, action: str) -> list[dict]:
        return [r["arguments"] for r in self.requests if r["arguments"].get("action") == action]

    def reply(self, arguments: dict) -> dict:
        action = arguments.get("action")
        if action == "search":
            if self.search_override is not None:
                return self.search_override
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
    runner = bin_dir / "unitares-github"
    runner.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
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
    environ["UNITARES_CLIENT_SESSION_ID"] = "test-import-session"
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
    assert "agent-identity: not inferred" in details
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


def test_current_app_reviews_use_the_gates_api_provenance():
    app_id, actor_id = next(iter(gate.TRUSTED_REVIEW_APPS.items()))
    comment = review(7, KEY_A, "CLEAN", 0, association="NONE")
    comment.update(performed_via_github_app={"id": app_id},
                   user={"id": actor_id, "type": "Bot"})
    entries = producer.review_records([comment])
    assert len(entries) == 1
    details = producer.build_details(REPO, pull(7), entries)
    assert f"github-app={app_id}" in details and f"actor={actor_id}" in details
    # A login or a body claiming the App is not API provenance.
    comment["performed_via_github_app"] = None
    comment["user"]["login"] = "unitares-agents[bot]"
    assert producer.review_records([comment]) == []


def test_interrupted_resolution_is_repaired_without_a_second_store(env):
    environ, data, stub = env
    one_disposed_pr(data)
    stub.keep_status = False
    assert run(environ, "--apply").returncode == 1
    assert len(stub.rows) == 1 and stub.rows[0]["status"] == "open"
    stub.keep_status = True
    stub.requests.clear()
    result = run(environ, "--apply")
    assert result.returncode == 0, result.stderr
    assert "repaired=1" in result.stdout
    assert not stub.bodies("store")
    assert stub.rows[0]["status"] == "resolved"


def test_dry_run_reports_partial_resolution_without_writing(env):
    environ, data, stub = env
    one_disposed_pr(data)
    stub.keep_status = False
    assert run(environ, "--apply").returncode == 1
    stub.requests.clear()
    result = run(environ)
    assert result.returncode == 0
    assert "would-repair=1" in result.stdout
    assert not stub.bodies("store") and not stub.bodies("update")


def test_edited_or_late_review_requires_explicit_reconciliation(env):
    environ, data, stub = env
    one_disposed_pr(data)
    assert run(environ, "--apply").returncode == 0
    source = data / "comments_7.json"
    comments = json.loads(source.read_text())
    comments[0]["body"] += "\nA post-close correction."
    source.write_text(json.dumps(comments))
    stub.requests.clear()
    result = run(environ, "--apply")
    assert result.returncode == 1 and "reconcile it explicitly" in result.stderr
    assert not stub.bodies("store") and not stub.bodies("update")


def test_apply_requires_an_explicit_process_binding_before_any_io(env):
    environ, _, stub = env
    environ.pop("UNITARES_CLIENT_SESSION_ID")
    result = run(environ, "--apply")
    assert result.returncode == 2 and "requires --client-session-id" in result.stderr
    assert stub.requests == []


@pytest.mark.parametrize("reply", [{"count": 0}, {"discoveries": [], "count": 51}])
def test_missing_or_truncated_search_evidence_never_authorizes_a_store(env, reply):
    environ, data, stub = env
    one_disposed_pr(data)
    stub.search_override = reply
    result = run(environ, "--apply")
    assert result.returncode == 1
    assert not stub.bodies("store")


def test_rest_reads_request_the_full_canonical_payload(env):
    environ, data, stub = env
    one_disposed_pr(data)
    result = run(environ, "--apply")
    assert result.returncode == 0, result.stderr
    assert stub.bodies("search")[0]["response_mode"] == "full"
    assert stub.bodies("get")[0]["response_mode"] == "full"


def test_failed_review_does_not_resolve_an_earlier_finding():
    entries = producer.review_records([
        review(7, KEY_A, "FINDINGS", 1), review(7, KEY_B, "FAILED", 0),
    ])
    assert producer.finding_class(entries, 0, 1) == "undisposed"


@pytest.mark.parametrize("verdict,count", [("CLEAN", -1), ("FINDINGS", 1000000), ("OTHER", 0)])
def test_invalid_marker_cannot_expand_an_unbounded_finding_list(verdict, count):
    with pytest.raises(producer.IngestError, match="invalid verdict or finding count"):
        producer.review_records([review(7, KEY_A, verdict, count)])


def test_oversized_round_metadata_fails_before_silently_truncating_history(monkeypatch):
    entries = producer.review_records([review(7, KEY_A, "FINDINGS", 5)])
    monkeypatch.setattr(producer, "MAX_DETAILS_CHARS", 500)
    with pytest.raises(producer.IngestError, match="metadata exceeds"):
        producer.build_store_arguments(REPO, pull(7), entries)


def test_unknown_pr_state_is_not_treated_as_closed(env):
    environ, data, stub = env
    seed(data, [pull(7, "UNKNOWN")], {})
    result = run(environ, "--apply")
    assert result.returncode == 1 and "unknown state" in result.stderr
    assert not stub.requests


def test_github_access_never_falls_back_to_a_human_login(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda _: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(producer.IngestError, match="refusing the ambient human login"):
        producer._gh(["gh", "pr", "list"], "list", REPO)


def test_github_runner_is_read_only_and_scoped_to_the_requested_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(producer.shutil, "which", lambda _: "/runner/unitares-github")

    def run_stub(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(producer.subprocess, "run", run_stub)
    assert producer._gh(["gh", "pr", "list"], "list", REPO) == "[]"
    assert calls == [["/runner/unitares-github", "--repo", REPO, "--read-only", "--", "gh", "pr", "list"]]


@pytest.mark.parametrize("text,expected", [
    ("Not fixed — rebutted: the caller validates.", "unclassified"),
    ("Not accepted; this needs investigation.", "unclassified"),
    ("Never agreed to this change.", "unclassified"),
    ("Wasn't fixed; rebutted because it is already guarded.", "unclassified"),
    ("Not yet fixed, but accepted for follow-up.", "unclassified"),
    ("Not rebutted; accepted and deferred.", "unclassified"),
    ("Don't accept: the guard already exists — rebutted.", "unclassified"),
    ("Can't accept this; rebutted.", "unclassified"),
    ("Cannot be confirmed; rebutted.", "unclassified"),
    ("Will not be fixed.", "unclassified"),
])
def test_negated_disposition_words_are_not_counted_as_acceptance(text, expected):
    assert producer.classify_disposition(text) == expected


def test_snapshot_ignores_incidental_github_metadata_but_tracks_identity_and_content():
    comment = review(7, KEY_A, "CLEAN", 0)
    comment.update(user={"id": 42, "type": "Bot", "login": "old-name"},
                   performed_via_github_app={"id": 101, "updated_at": "old", "permissions": {}})
    entries = producer.review_records([comment])
    source_pull = pull(7)
    source_pull["author"].update(id="stable-id", name="Old display name")
    original = producer.source_fingerprint(source_pull, entries)
    source_pull["author"].update(name="New name", login="renamed-login")
    source_pull["new_api_field"] = "added by GitHub"
    comment["author_association"] = "NONE"
    comment["user"].update(login="new-name", avatar_url="new-avatar")
    comment["performed_via_github_app"].update(updated_at="new", permissions={"issues": "write"})
    assert producer.source_fingerprint(source_pull, entries) == original
    comment["user"]["id"] = 43
    assert producer.source_fingerprint(source_pull, entries) != original
    comment["user"]["id"] = 42
    comment["body"] += " amended"
    assert producer.source_fingerprint(source_pull, entries) != original


def test_stored_disposition_excerpts_are_not_claimed_verbatim():
    comment = review(7, KEY_A, "FINDINGS", 1)
    entries = producer.review_records([
        comment, dispose(7, comment, KEY_A, 1, ["Accepted: " + "long " * 200]),
    ])
    details = producer.build_details(REPO, pull(7), entries)
    assert "whitespace-normalized excerpt" in details and "[trimmed]" in details
    assert "verbatim" not in details


@pytest.mark.parametrize("prefix", ["1.", "1)", "1:", "#1.", "#1)", "#1:", "**1.**", "**#1:**"])
def test_gate_supported_numbering_keeps_the_disposition_in_the_stored_record(env, prefix):
    environ, data, stub = env
    first = review(7, KEY_A, "FINDINGS", 1)
    answer = dispose(7, first, KEY_A, 1, ["Rebutted: the caller validates."])
    answer["body"] = answer["body"].replace("\n1. Rebutted:", f"\n{prefix} Rebutted:")
    seed(data, [pull(7)], {7: [first, answer]})
    result = run(environ, "--apply")
    assert result.returncode == 0, result.stderr
    [stored] = stub.bodies("store")
    assert "1. [rebutted] Rebutted: the caller validates." in stored["details"]
    assert "1 rebutted" in stored["summary"]
    assert "[undisposed]" not in stored["details"]


@pytest.mark.parametrize("prefix", ["1.", "1)", "1:", "#1.", "#1)", "#1:"])
@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\n\n", "\r\n", "\u00a0", "\u2003"])
def test_disposition_layout_matrix_matches_the_gates_acceptance(prefix, separator):
    body = f"\u00a0{prefix}{separator}Rebutted: the caller validates."
    assert gate.dispositions_complete(body, 1)
    assert producer.parse_disposition_items(body, 1) == {1: "Rebutted: the caller validates."}


def test_next_line_disposition_is_retained_by_the_actual_store(env):
    environ, data, stub = env
    first = review(7, KEY_A, "FINDINGS", 1)
    answer = dispose(7, first, KEY_A, 1, ["Rebutted: the caller validates."])
    answer["body"] = answer["body"].replace("\n1. Rebutted:", "\n1.\n\nRebutted:")
    assert gate.dispositions_complete(answer["body"], 1)
    seed(data, [pull(7)], {7: [first, answer]})
    result = run(environ, "--apply")
    assert result.returncode == 0, result.stderr
    assert "1. [rebutted] Rebutted: the caller validates." in stub.bodies("store")[0]["details"]


@pytest.mark.parametrize("wrapped", ["2.5s in the default config.", "3:00 on the clock.", "2)without whitespace", "2:no whitespace"])
def test_numeric_continuation_is_not_a_new_disposition(wrapped):
    body = f"1. Rebutted: the value is\n{wrapped}\n\n2. Fixed in abc123.\n3. Accepted for follow-up."
    assert gate.dispositions_complete(body, 3)
    assert producer.parse_disposition_items(body, 3) == {
        1: f"Rebutted: the value is {wrapped}",
        2: "Fixed in abc123.",
        3: "Accepted for follow-up.",
    }


def test_decimal_continuation_cannot_drop_the_authors_real_second_disposition(env):
    environ, data, stub = env
    first = review(7, KEY_A, "FINDINGS", 2)
    answer = dispose(7, first, KEY_A, 2, [
        "Rebutted: the timeout is\n2.5s in the default config.\n", "Fixed in abc123.",
    ])
    assert gate.dispositions_complete(answer["body"], 2)
    seed(data, [pull(7)], {7: [first, answer]})
    result = run(environ, "--apply")
    assert result.returncode == 0, result.stderr
    [stored] = stub.bodies("store")
    assert "1. [rebutted] Rebutted: the timeout is 2.5s in the default config." in stored["details"]
    assert "2. [accepted] Fixed in abc123." in stored["details"]
    assert "1 accepted" in stored["summary"] and "1 rebutted" in stored["summary"]
