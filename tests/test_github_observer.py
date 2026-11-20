"""The GitHub outcome observer's reading half and dry run
(agents/github_observer; docs/proposals/active/github-outcome-observer-v0.md).

The JSON under tests/fixtures/github_observer was recorded from the GitHub
REST API on 2026-11-14 and trimmed to the fields the observer reads: an open
pull request with failed and passing CI on several heads and review records
from an owner and from a registered App, and a merged pull request's timeline.
Cases GitHub did not happen to produce that day (a reopen, a re-run, an
untrusted marker) are derived from those recordings in the test that needs
them, so every shape still starts from a real response.
"""

from __future__ import annotations

import copy
import http.client
import importlib.util
import io
import itertools
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agents.github_observer import events as ev
from agents.github_observer import observer as obs
from agents.github_observer.store import FingerprintStore, read_only_fingerprints

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "github_observer"
REPO = "example/repo"
RID = "1110227889"  # the recorded pull requests' base.repo.id
AGENT = "6bccb016-774c-44cf-90a0-612d2117f607"
APP_ID, APP_ACTOR_ID = 5306351, 342195540
HEAD = "7d51a443591c576096ca38b967d7461d07910d57"
SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)

# GitHub's own rendering (body_html) of every Markdown body the footer tests
# use, keyed by the body. See the fixture's ``_source`` for how it was made.
_RENDERED = {
    case["body"]: case
    for case in json.loads((FIXTURES / "rendered_bodies.json").read_text())["cases"]
}


def footer_of(body: str | None) -> str | None:
    """The footer GitHub's rendering of a Markdown body names.

    The observer reads ``body_html``, which only GitHub produces, so a test
    body needs its recorded rendering in rendered_bodies.json.
    """
    if body is None:
        return ev.agent_uuid_from_body_html(None)
    case = _RENDERED.get(body)
    if case is None:
        raise AssertionError(
            f"no GitHub rendering recorded for {body!r}: render it with POST /markdown "
            "(mode=gfm) and add it to tests/fixtures/github_observer/rendered_bodies.json"
        )
    return ev.agent_uuid_from_body_html(case["github_html"])


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def pr_open():
    return load("pr_open.json")


@pytest.fixture
def comments():
    return load("comments.json")


class FakeReader:
    """Serves the recordings through the observer's Reader interface."""

    def __init__(self, pulls=None):
        self._pulls = pulls if pulls is not None else [load("pr_open.json"), load("pr_merged.json")]
        self.failed_job_reads: list[tuple[int, int]] = []

    def pulls(self, repo, since):
        return copy.deepcopy(self._pulls)

    def timeline(self, repo, number):
        return load("timeline_open.json" if number == 3259 else "timeline_merged.json")

    def comments(self, repo, number):
        return load("comments.json") if number == 3259 else []

    def commit_shas(self, repo, number):
        return [c["sha"] for c in load("pr_commits.json")] if number == 3259 else []

    def workflow_runs(self, repo, branch):
        return load("workflow_runs.json")["workflow_runs"]

    def run_attempt(self, repo, run_id, attempt):
        raise AssertionError("no recorded run has an earlier attempt")

    def check_runs(self, repo, sha):
        return load("check_runs.json")["check_runs"]

    def failed_jobs(self, repo, run_id, attempt):
        self.failed_job_reads.append((run_id, attempt))
        path = FIXTURES / f"jobs_{run_id}_{attempt}.json"
        if not path.exists():
            return []
        return [j["name"] for j in json.loads(path.read_text())["jobs"] if j["conclusion"] == "failure"]


def cycle(reader, *, known=frozenset(), trusted_apps=None, writer=None, mark=None):
    out = io.StringIO()
    counts = obs.run_cycle(
        reader,
        [REPO],
        writer or obs.DryRunWriter(out),
        seen=known.__contains__,
        mark=mark or (lambda fingerprint, event_type: None),
        since=SINCE,
        trusted_apps={APP_ID: APP_ACTOR_ID} if trusted_apps is None else trusted_apps,
    )
    return counts, [json.loads(line) for line in out.getvalue().splitlines()]


# --- who a pull request is attributed to ---------------------------------


def test_the_footer_names_the_subject(pr_open):
    assert ev.agent_uuid_from_body_html(pr_open["body_html"]) == AGENT


def test_the_last_footer_wins_and_quoted_or_fenced_ones_do_not_count():
    other = "11111111-2222-3333-4444-555555555555"
    body = "\n".join(
        [
            f"UNITARES agent: {other}",
            "> UNITARES agent: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "```",
            "UNITARES agent: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "```",
            f"UNITARES agent: {AGENT}",
            "```text",
            "UNITARES agent: cccccccc-cccc-cccc-cccc-cccccccccccc",
            "```",
        ]
    )
    assert footer_of(body) == AGENT


@pytest.mark.parametrize(
    "body", [None, "", "no footer here", "UNITARES agent: not-a-uuid", "UNITARES agent: " + "z" * 36]
)
def test_a_body_without_a_valid_footer_names_nobody(body):
    assert footer_of(body) is None


def test_an_unsigned_pull_request_is_counted_and_skipped(pr_open):
    unsigned = dict(pr_open, body="Fixes a thing.", body_html="<p>Fixes a thing.</p>")
    counts, events = cycle(FakeReader(pulls=[unsigned]))
    assert events == []
    assert counts["prs_unsigned"] == 1 and counts["prs_read"] == 0


# --- pull request state ---------------------------------------------------


def test_an_open_pull_request_has_only_its_opening(pr_open):
    events = ev.pr_state_events(REPO, pr_open, load("timeline_open.json"), AGENT)
    assert [(e["type"], e["fingerprint"]) for e in events] == [
        ("github.pr_opened", f"{RID}#3259:opened")
    ]
    assert events[0]["agent_uuid"] == AGENT and events[0]["head_sha_at_observation"] == HEAD
    assert events[0]["pr_author"] == {"association": "CONTRIBUTOR", "id": APP_ACTOR_ID, "type": "Bot"}


def test_a_merge_folds_the_close_github_emits_with_it():
    timeline = load("timeline_merged.json")
    # The recording itself is the API fact this rests on.
    assert [e["event"] for e in timeline][-2:] == ["merged", "closed"]
    events = ev.pr_state_events(REPO, load("pr_merged.json"), timeline, AGENT)
    assert [e["type"] for e in events] == ["github.pr_opened", "github.pr_merged"]
    merged = events[1]
    assert merged["fingerprint"] == f"{RID}#3256:state:36009921873"
    assert merged["merge_commit_sha"].startswith("d95e7e95")


def test_a_close_a_reopen_and_a_second_close_are_three_facts(pr_open):
    timeline = [
        {"id": 1, "event": "closed", "created_at": "2026-11-10T00:00:00Z", "commit_id": None},
        {"id": 2, "event": "reopened", "created_at": "2026-11-11T00:00:00Z", "commit_id": None},
        {"id": 3, "event": "closed", "created_at": "2026-11-12T00:00:00Z", "commit_id": None},
    ]
    events = ev.pr_state_events(REPO, pr_open, timeline, AGENT)
    assert [e["type"] for e in events[1:]] == [
        "github.pr_closed",
        "github.pr_reopened",
        "github.pr_closed",
    ]
    assert len({e["fingerprint"] for e in events}) == 4


def test_a_close_before_a_later_merge_is_kept(pr_open):
    timeline = [
        {"id": 1, "event": "closed", "created_at": "2026-11-10T00:00:00Z", "commit_id": None},
        {"id": 2, "event": "reopened", "created_at": "2026-11-11T00:00:00Z", "commit_id": None},
        {"id": 3, "event": "merged", "created_at": "2026-11-12T00:00:00Z", "commit_id": "abc"},
        {"id": 4, "event": "closed", "created_at": "2026-11-12T00:00:00Z", "commit_id": None},
    ]
    events = ev.pr_state_events(REPO, pr_open, timeline, AGENT)
    assert [e["type"] for e in events[1:]] == [
        "github.pr_closed",
        "github.pr_reopened",
        "github.pr_merged",
    ]


# --- CI -------------------------------------------------------------------


def other_pr(run, number=1):
    """A run GitHub associates with another pull request on a reused branch."""
    return dict(run, pull_requests=[{"number": number, "base": {"repo": {"id": int(RID)}}}])


def ci(pr_open, runs=None, checks=None, **kwargs):
    return ev.ci_events(
        REPO,
        pr_open,
        load("workflow_runs.json")["workflow_runs"] if runs is None else runs,
        load("check_runs.json")["check_runs"] if checks is None else checks,
        AGENT,
        pr_commit_shas=[c["sha"] for c in load("pr_commits.json")],
        **kwargs,
    )


def test_each_completed_run_and_each_other_app_check_is_one_event(pr_open):
    events = ci(pr_open, failed_jobs={(43534989292, 1): ["smoke"]})
    by_fp = {e["fingerprint"]: e for e in events}
    assert len(by_fp) == len(events) == 6
    failed = by_fp[f"{RID}#3259:ci:43534989292:1"]
    assert failed["conclusion"] == "failure" and failed["failed_jobs"] == ["smoke"]
    assert failed["head_sha"].startswith("262a42ab")  # an earlier head of the same PR
    assert by_fp[f"{RID}#3259:ci:43539563127:1"]["conclusion"] == "success"
    codeql = by_fp[f"{RID}#3259:check:132276839439:success"]
    assert codeql["source"] == "check" and codeql["app"] == "github-advanced-security"


def test_the_review_check_and_actions_check_runs_are_not_repeated(pr_open):
    events = ci(pr_open)
    assert all(e["workflow"] != "review" for e in events)
    # Three of the four recorded check runs belong to the Actions app, whose
    # workflow runs already report them.
    assert [e["workflow"] for e in events if e["source"] == "check"] == ["CodeQL"]


def test_a_run_still_in_progress_is_not_reported(pr_open):
    runs = load("workflow_runs.json")["workflow_runs"]
    runs[0] = dict(runs[0], status="in_progress", conclusion=None)
    assert f"{RID}#3259:ci:{runs[0]['id']}:1" not in {e["fingerprint"] for e in ci(pr_open, runs)}


class RerunReader(FakeReader):
    """The API's real shape for a re-run: the run list carries ONE entry per
    run id, at its latest attempt; an earlier attempt is a separate read."""

    def __init__(self):
        super().__init__(pulls=[load("pr_open.json")])
        self.attempt_reads: list[tuple[int, int]] = []

    def workflow_runs(self, repo, branch):
        runs = super().workflow_runs(repo, branch)
        runs[2] = dict(runs[2], run_attempt=2, conclusion="success")
        return runs

    def run_attempt(self, repo, run_id, attempt):
        self.attempt_reads.append((run_id, attempt))
        first = load("workflow_runs.json")["workflow_runs"][2]
        assert (first["id"], first["conclusion"]) == (run_id, "failure")
        return dict(first, run_attempt=attempt)


def test_the_failed_first_attempt_of_a_rerun_is_reported_with_the_passing_one():
    reader = RerunReader()
    _, events = cycle(reader)
    by_fp = {e["fingerprint"]: e for e in events}
    assert by_fp[f"{RID}#3259:ci:43534989292:1"]["conclusion"] == "failure"
    assert by_fp[f"{RID}#3259:ci:43534989292:1"]["failed_jobs"] == ["smoke"]
    assert by_fp[f"{RID}#3259:ci:43534989292:2"]["conclusion"] == "success"
    assert reader.attempt_reads == [(43534989292, 1)]


def test_an_attempt_already_written_is_not_read_again():
    reader = RerunReader()
    cycle(reader, known=frozenset({f"{RID}#3259:ci:43534989292:1"}))
    assert reader.attempt_reads == []
    assert (43534989292, 1) not in reader.failed_job_reads


def test_a_run_waiting_for_approval_is_not_a_result(pr_open):
    runs = load("workflow_runs.json")["workflow_runs"]
    runs[0] = dict(runs[0], conclusion="action_required")
    assert f"{RID}#3259:ci:{runs[0]['id']}:1" not in {e["fingerprint"] for e in ci(pr_open, runs)}


def test_the_current_head_counts_even_when_the_commit_list_is_cut_short(pr_open):
    events = ev.ci_events(
        REPO,
        pr_open,
        [other_pr(run) for run in load("workflow_runs.json")["workflow_runs"]],
        [],
        AGENT,
        pr_commit_shas=[],
    )
    assert {e["head_sha"] for e in events} == {HEAD}


def test_a_run_on_a_commit_outside_the_pull_request_is_left_out(pr_open):
    runs = load("workflow_runs.json")["workflow_runs"]
    stray = other_pr(dict(runs[0], id=1, head_sha="0" * 40))
    assert f"{RID}#3259:ci:1:1" not in {e["fingerprint"] for e in ci(pr_open, [*runs, stray])}


def test_a_rewritten_check_run_is_keyed_by_its_conclusion(pr_open):
    checks = load("check_runs.json")["check_runs"]
    codeql = next(c for c in checks if c["name"] == "CodeQL")
    rewritten = dict(codeql, conclusion="failure")
    fingerprints = {e["fingerprint"] for e in ci(pr_open, checks=[codeql, rewritten])}
    assert fingerprints >= {
        f"{RID}#3259:check:{codeql['id']}:success",
        f"{RID}#3259:check:{codeql['id']}:failure",
    }


def test_failed_jobs_are_read_only_for_failed_runs_of_this_pull_request(pr_open):
    class WithStray(FakeReader):
        def workflow_runs(self, repo, branch):
            runs = super().workflow_runs(repo, branch)
            # A failed run on the same branch name but a commit of another PR.
            return [*runs, other_pr(dict(runs[2], id=7, head_sha="0" * 40))]

    reader = WithStray(pulls=[pr_open])
    cycle(reader)
    assert sorted(reader.failed_job_reads) == [(43528403669, 1), (43534989292, 1)]


# --- review records -------------------------------------------------------


def test_records_from_an_owner_and_a_registered_app_are_admitted(pr_open, comments):
    events, untrusted = ev.review_events(
        REPO, pr_open, comments, AGENT, trusted_apps={APP_ID: APP_ACTOR_ID}
    )
    assert untrusted == 0
    assert [(e["comment_id"], e["verdict"], e["findings"]) for e in events] == [
        (6854328344, "FINDINGS", 4),
        (6866483548, "FAILED", 0),
        (6876306245, "FINDINGS", 1),
    ]
    last = events[-1]
    assert last["fingerprint"] == f"{RID}#3259:review:6876306245"
    assert last["head_sha"] == HEAD and last["raw"] is True and last["disposed"] is False
    assert last["reviewer"] == "codex:gpt-6.1-sol-independent"


def test_an_app_the_deployment_did_not_register_is_not_admitted(pr_open, comments):
    events, untrusted = ev.review_events(REPO, pr_open, comments, AGENT, trusted_apps={})
    assert [e["comment_id"] for e in events] == [6854328344]
    assert untrusted == 2


def test_a_pasted_marker_from_an_outsider_is_ignored(pr_open, comments):
    owner = next(c for c in comments if c["author_association"] == "OWNER")
    pasted = dict(owner, id=99, author_association="NONE", performed_via_github_app=None)
    # A body or login claiming an App changes nothing: only GitHub's own
    # provenance fields are read.
    claiming = dict(pasted, id=100, user={"id": APP_ACTOR_ID, "type": "User", "login": "x[bot]"})
    events, untrusted = ev.review_events(
        REPO, pr_open, [pasted, claiming], AGENT, trusted_apps={APP_ID: APP_ACTOR_ID}
    )
    assert events == [] and untrusted == 2


def test_a_comment_without_a_marker_is_not_a_record(pr_open, comments):
    plain = next(c for c in comments if "unitares-review v1" not in c["body"])
    assert ev.review_events(REPO, pr_open, [plain], AGENT, trusted_apps={}) == ([], 0)


def test_a_legacy_record_has_no_head_and_a_disposition_is_flagged():
    legacy = "<!-- unitares-review v1 key=abc verdict=CLEAN findings=0 disposed=1 reviewer=codex -->"
    assert ev.parse_review_marker(legacy) == {
        "key": "abc",
        "verdict": "CLEAN",
        "findings": 0,
        "disposed": True,
        "reviewer": "codex",
        "head_sha": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        "<!-- unitares-review v1 key=abc verdict=MAYBE findings=0 -->",
        "<!-- unitares-review v1 verdict=CLEAN findings=0 -->",
        "<!-- unitares-review v1 key=abc verdict=CLEAN findings=many -->",
        "<!-- unitares-review-waiver v1 key=abc -->",
    ],
)
def test_a_marker_the_gate_does_not_write_is_not_a_record(body):
    assert ev.parse_review_marker(body) is None


def test_the_trusted_associations_match_the_review_gate():
    script = REPO_ROOT / "scripts" / "dev" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("review_gate_for_observer_parity", script)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    try:
        spec.loader.exec_module(gate)
        assert ev.TRUSTED_ASSOCIATIONS == gate.TRUSTED_ASSOCIATIONS
        assert ev.REVIEW_MARKER == gate.MARKER
        for comment in load("comments.json"):
            assert ev.trusted_review_comment(
                comment, gate.TRUSTED_REVIEW_APPS
            ) == gate.trusted_review_comment(comment)
    finally:
        sys.modules.pop(spec.name, None)


# --- a whole poll ---------------------------------------------------------


def test_a_poll_reports_every_fact_once_and_only_github_types():
    counts, events = cycle(FakeReader())
    assert counts["prs_read"] == 2 and counts["prs_failed"] == 0
    assert counts["events_new"] == len(events) == 12
    assert {e["type"] for e in events} <= ev.EVENT_TYPES
    assert len({e["fingerprint"] for e in events}) == len(events)
    assert all(e["agent_uuid"] == AGENT and e["repo"] == REPO for e in events)
    assert all(e["fingerprint"].startswith(RID + "#") for e in events)
    # Nothing an event carries is a judgement about the agent.
    forbidden = {"severity", "risk", "verdict_for_agent", "score", "penalty"}
    assert not any(forbidden & set(e) for e in events)


def test_a_second_poll_with_a_durable_store_reports_nothing(tmp_path):
    path = tmp_path / "state" / "emitted.sqlite3"
    store = FingerprintStore(path)
    first, _ = cycle(FakeReader(), known=frozenset(), mark=store.add)
    store.close()

    reopened = FingerprintStore(path)  # an observer restart
    assert reopened.count() == first["events_new"] == 12
    second, events = cycle(FakeReader(), known=read_only_fingerprints(path), mark=reopened.add)
    assert events == [] and second["events_new"] == 0 and second["events_known"] == 12
    assert reopened.add("x", "github.pr_opened") is True
    assert reopened.add("x", "github.pr_opened") is False


def test_a_write_that_fails_is_not_marked_and_is_tried_again():
    class Refusing:
        def write(self, event):
            return False

    marked: list[str] = []
    counts, _ = cycle(FakeReader(), writer=Refusing(), mark=lambda fp, t: marked.append(fp))
    assert marked == [] and counts["writes_failed"] == 12 and counts["events_new"] == 0


def test_a_pull_request_that_cannot_be_read_does_not_stop_the_others(pr_open):
    class Flaky(FakeReader):
        def comments(self, repo, number):
            if number == 3259:
                raise obs.GitHubError("GitHub returned HTTP 502 for a read")
            return super().comments(repo, number)

    counts, events = cycle(Flaky())
    assert counts["prs_failed"] == 1 and counts["prs_read"] == 1
    assert {e["pr"] for e in events} == {3256}


# --- the dry run and the client ------------------------------------------


def test_the_dry_run_writes_nothing_and_live_mode_requires_operator_token(tmp_path, monkeypatch, capsys):
    state = tmp_path / "emitted.sqlite3"
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_OBSERVER_TRUSTED_APPS", raising=False)
    monkeypatch.delenv("UNITARES_OPERATOR_TOKEN", raising=False)
    monkeypatch.setattr(obs, "GitHubReader", lambda client: FakeReader())

    args = ["--repo", REPO, "--state-path", str(state), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}"]
    assert obs.main([*args, "--dry-run"]) == 0
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert len(events) == 12
    assert json.loads(captured.err)["events_new"] == 12
    assert not state.exists()  # a dry run does not create the store

    assert obs.main(args) == 2
    assert "UNITARES_OPERATOR_TOKEN" in capsys.readouterr().err


def test_the_dry_run_leaves_out_what_the_store_already_holds(tmp_path, monkeypatch, capsys):
    state = tmp_path / "emitted.sqlite3"
    store = FingerprintStore(state)
    store.add(f"{RID}#3259:opened", "github.pr_opened")
    store.close()
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(obs, "GitHubReader", lambda client: FakeReader())
    args = ["--repo", REPO, "--state-path", str(state), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}"]
    assert obs.main([*args, "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert f"{RID}#3259:opened" not in captured.out
    assert json.loads(captured.err)["events_known"] == 1
    assert read_only_fingerprints(state) == {f"{RID}#3259:opened"}


def test_main_needs_a_token_and_a_well_formed_repo(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert obs.main(["--repo", REPO, "--dry-run"]) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        obs.main(["--repo", "not a repo", "--dry-run"])


def test_trusted_apps_are_pairs_of_integers():
    assert obs.parse_trusted_apps(["1:2", "3:4, 5:6", ""]) == {1: 2, 3: 4, 5: 6}
    with pytest.raises(ValueError):
        obs.parse_trusted_apps(["1"])


class _Response:
    def __init__(self, body, link=None):
        self._body, self.headers = json.dumps(body).encode(), {"Link": link} if link else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_client_only_sends_gets_follows_pages_and_stays_on_its_host():
    seen: list[tuple[str, str]] = []
    pages = {
        "https://api.example/repos/o/r/pulls?per_page=100&state=open": _Response(
            [{"number": 1}], '<https://api.example/repos/o/r/pulls?page=2>; rel="next"'
        ),
        "https://api.example/repos/o/r/pulls?page=2": _Response(
            [{"number": 2}], '<https://elsewhere.example/steal>; rel="next"'
        ),
    }

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url))
        assert request.get_header("Authorization") == "Bearer secret"
        # ``full`` is what serves body_html, which the footer is read from.
        assert request.get_header("Accept") == "application/vnd.github.full+json"
        return pages[request.full_url]

    client = obs.GitHubClient("secret", "https://api.example", opener=opener)
    with pytest.raises(obs.GitHubError) as refused:
        client.get_all("/repos/o/r/pulls", {"state": "open"})
    assert "secret" not in str(refused.value)
    assert [method for method, _ in seen] == ["GET", "GET"]
    assert not [name for name in dir(client) if name.startswith(("post", "put", "patch", "delete"))]


def test_paging_stops_once_a_sorted_page_is_out_of_range():
    calls: list[str] = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if "state=open" in request.full_url:
            return _Response([])
        return _Response(
            [{"updated_at": "2026-10-01T00:00:00Z"}],
            '<https://api.example/repos/o/r/pulls?page=2>; rel="next"',
        )

    reader = obs.GitHubReader(obs.GitHubClient("t", "https://api.example", opener=opener))
    # The closed list is sorted newest first, so a page that ends before the
    # window means every later page is older still.
    assert reader.pulls("o/r", SINCE) == []
    assert len([url for url in calls if "state=closed" in url]) == 1


# --- findings of the 2026-11-14 independent review -------------------------


@pytest.mark.parametrize(
    "hidden",
    [
        "<!--\nUNITARES agent: {other}\n-->",
        "<!-- UNITARES agent: {other} -->",
        "<!--\nUNITARES agent: {other}",  # never closed: hidden to the end
        "> quoted paragraph\nUNITARES agent: {other}",  # lazy continuation
        "    UNITARES agent: {other}",  # indented code
        "text\u2028UNITARES agent: {other}",  # not a line break on GitHub
        "UNITARES agent: ----{compact}",  # not the 8-4-4-4-12 form
    ],
)
def test_a_footer_the_rendered_body_does_not_show_names_nobody(hidden):
    other = "11111111-2222-3333-4444-555555555555"
    body = f"UNITARES agent: {AGENT}\n\n" + hidden.format(other=other, compact=other.replace("-", ""))
    assert footer_of(body) == AGENT


def test_a_footer_after_a_closed_comment_or_quote_counts_again():
    body = f"<!-- note -->\n> quote\n\nUNITARES agent: {AGENT.upper()}\n"
    assert footer_of(body) == AGENT


def test_a_footer_typed_by_someone_the_deployment_does_not_trust_is_skipped(pr_open):
    # The recorded pull requests were opened by a registered App's bot. With no
    # App registered they are a stranger's, whatever the footer says.
    counts, events = cycle(FakeReader(), trusted_apps={})
    assert events == [] and counts["prs_untrusted_author"] == 2 and counts["prs_read"] == 0
    # An owner's pull request needs no registration.
    owner = dict(pr_open, author_association="OWNER", user={"id": 1, "type": "User", "login": "o"})
    counts, events = cycle(FakeReader(pulls=[owner]), trusted_apps={})
    assert counts["prs_read"] == 1 and events
    # A stranger's bot, or a user whose id equals a registered bot's, is not.
    for user in ({"id": 9, "type": "Bot"}, {"id": APP_ACTOR_ID, "type": "User"}, None):
        stranger = dict(pr_open, author_association="NONE", user=user)
        assert not ev.trusted_pr_author(stranger, {APP_ID: APP_ACTOR_ID})


def test_the_merge_close_is_folded_whatever_its_timestamp(pr_open):
    merged_pr = dict(pr_open, merged_at="2026-11-12T00:00:01Z")
    early_close = [
        {"id": 3, "event": "closed", "created_at": "2026-11-12T00:00:00Z", "commit_id": None},
        {"id": 4, "event": "merged", "created_at": "2026-11-12T00:00:01Z", "commit_id": "abc"},
    ]
    events = ev.pr_state_events(REPO, merged_pr, early_close, AGENT)
    assert [e["type"] for e in events] == ["github.pr_opened", "github.pr_merged"]
    # The merge is known from the pull request before its timeline event shows.
    events = ev.pr_state_events(REPO, merged_pr, early_close[:1], AGENT)
    assert [e["type"] for e in events] == ["github.pr_opened"]


def test_marker_attributes_are_tokenised_as_the_gate_does():
    script = REPO_ROOT / "scripts" / "dev" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("review_gate_for_observer_tokens", script)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    try:
        spec.loader.exec_module(gate)
        assert ev._RECORD_RE.pattern == gate.RECORD_RE.pattern
        for attrs in (
            "key=abc 9verdict=CLEAN findings=0",
            "key=abc verdict=CLEAN findings=0",
            "xkey=abc verdict=CLEAN",
            "key=abc verdict=FINDINGS findings=2 disposed=1 reviewer=a:b head=deadbeef",
            "key=abc verdict=CLEAN findings=x",
        ):
            body = f"<!-- unitares-review v1 {attrs} -->"
            ours, theirs = ev.parse_review_marker(body), gate.parse_record(body)
            assert (ours is None) == (theirs is None), attrs
            if ours is not None:
                assert (ours["key"], ours["verdict"], ours["findings"], ours["disposed"]) == (
                    theirs.key,
                    theirs.verdict,
                    theirs.findings,
                    theirs.disposed,
                )
    finally:
        sys.modules.pop(spec.name, None)


def test_the_admission_rule_matches_the_gate_over_every_shape():
    script = REPO_ROOT / "scripts" / "dev" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("review_gate_for_observer_matrix", script)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    try:
        spec.loader.exec_module(gate)
        (app_id, actor_id), = gate.TRUSTED_REVIEW_APPS.items()
        associations = ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE", None]
        apps = [None, "app", {}, {"id": app_id}, {"id": str(app_id)}, {"id": True}, {"id": app_id + 1}]
        users = [
            None,
            "user",
            {},
            {"id": actor_id, "type": "Bot"},
            {"id": actor_id, "type": "User"},
            {"id": str(actor_id), "type": "Bot"},
            {"id": actor_id + 1, "type": "Bot"},
            {"id": True, "type": "Bot"},
        ]
        admitted = 0
        for association, app, user in itertools.product(associations, apps, users):
            comment = {
                "author_association": association,
                "performed_via_github_app": app,
                "user": user,
            }
            ours = ev.trusted_review_comment(comment, gate.TRUSTED_REVIEW_APPS)
            assert ours == gate.trusted_review_comment(comment), comment
            admitted += ours
        # Three associations times every app and user shape, plus the one
        # registered pair under each of the other three associations.
        assert admitted == 3 * len(apps) * len(users) + 3
    finally:
        sys.modules.pop(spec.name, None)


def test_the_configured_spelling_of_a_repository_does_not_change_a_fingerprint():
    def fingerprints(name):
        out = io.StringIO()
        obs.run_cycle(
            FakeReader(),
            [name],
            obs.DryRunWriter(out),
            seen=lambda fingerprint: False,
            mark=lambda fingerprint, event_type: None,
            since=SINCE,
            trusted_apps={APP_ID: APP_ACTOR_ID},
        )
        return {json.loads(line)["fingerprint"] for line in out.getvalue().splitlines()}

    assert fingerprints("example/repo") == fingerprints("EXAMPLE/Repo")


def test_a_malformed_pull_request_and_a_failing_repository_are_counted(pr_open):
    no_number = {k: v for k, v in pr_open.items() if k != "number"}
    no_repo_id = dict(pr_open, base={"ref": "master"})
    counts, events = cycle(FakeReader(pulls=[no_number, no_repo_id, pr_open]))
    assert counts["prs_failed"] == 2 and counts["prs_read"] == 1 and len(events) == 10

    class OneRepoDown(FakeReader):
        def pulls(self, repo, since):
            if repo == "down/repo":
                raise obs.GitHubError("GitHub returned HTTP 503 for a read")
            return super().pulls(repo, since)

    out = io.StringIO()
    counts = obs.run_cycle(
        OneRepoDown(),
        ["down/repo", REPO],
        obs.DryRunWriter(out),
        seen=lambda fingerprint: False,
        mark=lambda fingerprint, event_type: None,
        since=SINCE,
        trusted_apps={APP_ID: APP_ACTOR_ID},
    )
    assert counts["repos_failed"] == 1 and counts["prs_read"] == 2


def test_a_pull_request_listed_twice_is_reported_once(pr_open):
    counts, events = cycle(FakeReader(pulls=[pr_open, pr_open]))
    assert len(events) == 10 == counts["events_new"] and counts["events_known"] == 10


def test_a_poll_that_could_not_read_something_exits_nonzero(tmp_path, monkeypatch, capsys):
    class Down(FakeReader):
        def comments(self, repo, number):
            raise obs.GitHubError("GitHub returned HTTP 502 for a read")

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(obs, "GitHubReader", lambda client: Down())
    args = ["--repo", REPO, "--state-path", str(tmp_path / "s"), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}"]
    assert obs.main([*args, "--dry-run"]) == 1
    assert json.loads(capsys.readouterr().err)["prs_failed"] == 2


def test_skip_check_adds_to_the_review_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(obs, "GitHubReader", lambda client: FakeReader())
    args = ["--repo", REPO, "--state-path", str(tmp_path / "s"), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}"]
    assert obs.main([*args, "--dry-run", "--skip-check", "Review Gate"]) == 0
    workflows = {json.loads(line).get("workflow") for line in capsys.readouterr().out.splitlines()}
    assert "Review Gate" not in workflows and "review" not in workflows and "Tests" in workflows


def test_a_file_that_is_not_a_store_is_refused_and_an_odd_path_is_read(tmp_path, monkeypatch, capsys):
    not_a_store = tmp_path / "plain.sqlite3"
    not_a_store.write_text("not a database")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert obs.main(["--repo", REPO, "--state-path", str(not_a_store), "--dry-run"]) == 2
    assert "could not be read" in capsys.readouterr().err

    odd = tmp_path / "a?b#c%20d" / "emitted.sqlite3"
    store = FingerprintStore(odd)
    store.add("x", "github.pr_opened")
    store.close()
    assert read_only_fingerprints(odd) == {"x"}


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_is_refused_so_the_token_never_reaches_another_host():
    received: list[str | None] = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args):
            pass

    elsewhere = _serve(Elsewhere)

    class Api(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/steal")
            self.end_headers()

        def log_message(self, *args):
            pass

    api = _serve(Api)
    try:
        client = obs.GitHubClient("secret", f"http://127.0.0.1:{api.server_port}")
        with pytest.raises(obs.GitHubError) as refused:
            client.get_all("/repos/o/r/pulls")
        assert received == [] and "secret" not in str(refused.value)
    finally:
        api.shutdown()
        elsewhere.shutdown()
        api.server_close()
        elsewhere.server_close()


def test_a_list_longer_than_the_page_cap_is_an_error_not_a_short_answer():
    def opener(request, timeout):
        return _Response([{"n": 1}], '<https://api.example/repos/o/r/issues/1/comments?page=2>; rel="next"')

    client = obs.GitHubClient("t", "https://api.example", opener=opener)
    with pytest.raises(obs.GitHubError, match="partial read"):
        client.get_all("/repos/o/r/issues/1/comments")


def test_a_body_cut_off_mid_read_is_a_github_error():
    class CutOff(_Response):
        def read(self):
            raise http.client.IncompleteRead(b"[")

    client = obs.GitHubClient("t", "https://api.example", opener=lambda request, timeout: CutOff([]))
    with pytest.raises(obs.GitHubError):
        client.get_all("/repos/o/r/pulls")

    def reset(request, timeout):
        raise ConnectionResetError

    with pytest.raises(obs.GitHubError):
        obs.GitHubClient("t", "https://api.example", opener=reset).get_all("/repos/o/r/pulls")


# --- findings of the 2026-11-16 review of #3263 ---------------------------


class ForcePushedReader(FakeReader):
    """The recordings after the agent force-pushed: the PR lists only its
    current head, but GitHub still associates the earlier runs with it."""

    def __init__(self):
        super().__init__(pulls=[load("pr_open.json")])

    def commit_shas(self, repo, number):
        return [HEAD]


def test_ci_results_from_a_head_that_was_force_pushed_away_are_kept():
    reader = ForcePushedReader()
    _, events = cycle(reader)
    by_fp = {e["fingerprint"]: e for e in events}
    replaced = by_fp[f"{RID}#3259:ci:43534989292:1"]
    assert replaced["conclusion"] == "failure" and replaced["failed_jobs"] == ["smoke"]
    assert replaced["head_sha"].startswith("262a42ab")
    assert by_fp[f"{RID}#3259:ci:43528403669:1"]["head_sha"].startswith("fe9a299e")
    assert sorted(reader.failed_job_reads) == [(43528403669, 1), (43534989292, 1)]


def test_an_association_with_the_same_number_in_another_repository_does_not_count(pr_open):
    runs = load("workflow_runs.json")["workflow_runs"]
    elsewhere = dict(
        runs[2], pull_requests=[{"number": 3259, "base": {"repo": {"id": int(RID) + 1}}}]
    )
    bare = dict(runs[2], id=8, pull_requests=[{"number": 3259}])
    events = ev.ci_events(REPO, pr_open, [elsewhere, bare], [], AGENT, pr_commit_shas=[HEAD])
    assert events == []


def test_an_earlier_attempt_on_a_replaced_head_is_still_read():
    class ForcePushedRerun(ForcePushedReader, RerunReader):
        def __init__(self):
            RerunReader.__init__(self)

    reader = ForcePushedRerun()
    _, events = cycle(reader)
    fingerprints = {e["fingerprint"] for e in events}
    assert {f"{RID}#3259:ci:43534989292:1", f"{RID}#3259:ci:43534989292:2"} <= fingerprints
    assert reader.attempt_reads == [(43534989292, 1)]


@pytest.mark.parametrize(
    "hidden",
    [
        # A second comment opened after the first one closes, never closed.
        "<!-- note --> <!-- unclosed\nUNITARES agent: {other}",
        # A comment that closes and reopens on its own closing line.
        "<!--\nnote\n--> <!--\nUNITARES agent: {other}\n-->",
        # Collapsed or preformatted HTML.
        "<details>\n<summary>more</summary>\n\nUNITARES agent: {other}\n\n</details>",
        "<details><summary>x</summary>\n<details>\n</details>\nUNITARES agent: {other}\n</details>",
        "<pre>\nUNITARES agent: {other}\n</pre>",
        "<PRE class=x>\n```\nUNITARES agent: {other}\n</pre>",
        # A comment opened on the line that opens a hiding block.
        "<details><!-- x -->\nUNITARES agent: {other}\n</details>",
        # A quote whose line also holds a comment; the next line continues it.
        "> quoted <!-- x -->\nUNITARES agent: {other}",
        "> quoted `code`\nUNITARES agent: {other}",
    ],
)
def test_a_footer_inside_a_later_comment_or_hiding_block_names_nobody(hidden):
    other = "11111111-2222-3333-4444-555555555555"
    body = f"UNITARES agent: {AGENT}\n\n" + hidden.format(other=other)
    assert footer_of(body) == AGENT


@pytest.mark.parametrize(
    "before",
    [
        "Write `<!--` to open a comment.\n\n",  # inline code opens nothing
        "Use ``a ` <!-- b`` here.\n\n",
        "<details><summary>log</summary>\n\nlong\n\n</details>\n\n",
        "<pre>\nout\n</pre>\n\n",
        "<!-- a --> text <!-- b -->\n\n",
        "A stray backtick ` and <!-- x --> on one line.\n\n",
    ],
)
def test_a_footer_after_inline_code_or_a_closed_block_counts(before):
    assert footer_of(before + f"UNITARES agent: {AGENT}") == AGENT


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_a_footer_indented_less_than_a_code_block_counts(indent):
    assert footer_of(f"Body.\n\n{indent}UNITARES agent: {AGENT}") == AGENT


def test_check_runs_are_read_with_every_run_of_each_name():
    urls: list[str] = []

    def opener(request, timeout):
        urls.append(request.full_url)
        return _Response({"check_runs": []})

    obs.GitHubReader(obs.GitHubClient("t", "https://api.example", opener=opener)).check_runs(
        "o/r", HEAD
    )
    # The default, filter=latest, would hide an earlier failed run of a check
    # that a re-run of the same name passed.
    assert urls == [f"https://api.example/repos/o/r/commits/{HEAD}/check-runs?per_page=100&filter=all"]


# Codex review of #3263 at 0ffb7ed4: literal HTML examples and multi-line code spans.
_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"


def test_indented_code_details_example_does_not_hide_the_footer():
    body = f"Example:\n\n    <details>\n\n---\nUNITARES agent: {_UUID_A}\n"
    assert footer_of(body) == _UUID_A


def test_quoted_pre_example_does_not_hide_the_footer():
    body = f"> <pre>\n\n---\nUNITARES agent: {_UUID_A}\n"
    assert footer_of(body) == _UUID_A


def test_multiline_code_span_footer_does_not_override_the_author():
    body = (
        f"---\nUNITARES agent: {_UUID_A}\n\n"
        f"Example: `text\nUNITARES agent: {_UUID_B}\nmore`\n"
    )
    assert footer_of(body) == _UUID_A


def test_unclosed_backtick_still_reads_a_later_paragraph_footer():
    body = f"A stray ` here\n\n---\nUNITARES agent: {_UUID_A}\n"
    assert footer_of(body) == _UUID_A


def test_code_span_closed_before_footer_line_keeps_footer_visible():
    body = f"See `a\nb` above\n\n---\nUNITARES agent: {_UUID_A}\n"
    assert footer_of(body) == _UUID_A


def test_indented_closing_tag_inside_pre_closes_the_block():
    body = f"<pre>\nlog\n    </pre>\n\n---\nUNITARES agent: {_UUID_A}\n"
    assert footer_of(body) == _UUID_A


def test_escaped_backtick_opens_no_code_span():
    body = f"---\nUNITARES agent: {_UUID_A}\n\nan escaped \\` and <!--\nUNITARES agent: {_UUID_B}\n-->\n"
    assert footer_of(body) == _UUID_A


# Issue #3277: the footer is read from GitHub's own rendering of the body
# (body_html) instead of with a hand-written Markdown line scanner.


def test_an_inline_comment_opener_never_closed_in_its_paragraph_is_text():
    # The 2026-11-14 scanner held this hid the next paragraph. Under CommonMark
    # an inline ``<!--`` must close inside its paragraph; this one does not, so
    # it is literal text, and GitHub shows the later footer (see the
    # rendered_bodies.json fixture).
    body = f"UNITARES agent: {_UUID_A}\n\ntext <!-- a --> b <!-- c\n\nUNITARES agent: {_UUID_B}\n"
    assert footer_of(body) == _UUID_B


@pytest.mark.parametrize(
    "body",
    [
        # Backticks are literal inside an HTML <pre> block, so `</pre>` closes it.
        f"<pre>\nlog `</pre>` tail\n\n---\nUNITARES agent: {_UUID_A}\n",
        f"---\nUNITARES agent: {_UUID_A}\n\n<pre>\nx ` y\nUNITARES agent: {_UUID_B}\n</pre>\n",
        # Backslash escapes count by parity: \\ is a literal backslash, and the
        # backtick after it opens a real code span.
        f"Write \\\\`<!--` literally.\n\n---\nUNITARES agent: {_UUID_A}\n",
        f"---\nUNITARES agent: {_UUID_A}\n\nWrite \\\\`x\nUNITARES agent: {_UUID_B}\nx` here\n",
        f"---\nUNITARES agent: {_UUID_A}\n\nan escaped \\\\\\` and <!--\nUNITARES agent: {_UUID_B}\n-->\n",
    ],
)
def test_pre_backticks_and_backslash_parity_follow_commonmark(body):
    assert footer_of(body) == _UUID_A


@pytest.mark.parametrize(
    "hidden",
    [
        "~~UNITARES agent: {b}~~",  # struck through
        "[link\nUNITARES agent: {b}\n](https://example.com)",  # link text
        "<details/>\n\nUNITARES agent: {b}",  # the slash does not close it
        "<details open>\n<summary>s</summary>\n\nUNITARES agent: {b}\n\n</details>",
        "<textarea>\n\nUNITARES agent: {b}",  # tagfilter: literal text, not a paragraph
    ],
)
def test_a_footer_that_is_not_a_plain_paragraph_line_names_nobody(hidden):
    body = f"---\nUNITARES agent: {_UUID_A}\n\n" + hidden.format(b=_UUID_B)
    assert footer_of(body) == _UUID_A


def test_the_end_of_a_quote_closes_a_details_opened_in_it():
    body = f"UNITARES agent: {_UUID_A}\n\n> <details>\n\nUNITARES agent: {_UUID_B}\n"
    assert footer_of(body) == _UUID_B


def test_a_line_break_ends_a_footer_line():
    body = f"UNITARES agent: {_UUID_A}  \nUNITARES agent: {_UUID_B}<br>more\n"
    assert footer_of(body) == _UUID_B


@pytest.mark.parametrize("case", list(_RENDERED.values()), ids=[str(i) for i in range(len(_RENDERED))])
def test_each_recorded_rendering_names_its_checked_footer(case):
    # ``expected`` was read by hand from GitHub's HTML when it was recorded.
    assert ev.agent_uuid_from_body_html(case["github_html"]) == case["expected"]


@pytest.mark.parametrize(
    "hidden",
    [
        "<del>\n\nUNITARES agent: {b}\n\n</del>",  # a struck-through paragraph
        "<b>\n\nUNITARES agent: {b}\n\n</b>",  # formatted, not a plain line
        # A table is a scope barrier: GitHub keeps the quote open past this
        # </blockquote>, so the paragraph renders inside the quote.
        "<blockquote><table></blockquote>\n\nUNITARES agent: {b}",
        # &#10; is a newline character, not a line break: one rendered line.
        "example&#10;UNITARES agent: {b}&#10;suffix",
        # HTML ends CDATA at the first >, so "suffix]]>" shows on the line.
        "UNITARES agent: {b}<![CDATA[>suffix]]>",
        "UNITARES agent: {b}\u2028x",  # a separator inside the line
    ],
)
def test_a_paragraph_inside_formatting_or_a_split_line_names_nobody(hidden):
    body = f"UNITARES agent: {_UUID_A}\n\n" + hidden.format(b=_UUID_B)
    assert footer_of(body) == _UUID_A


@pytest.mark.parametrize(
    "visible",
    [
        "UNITARES agent: {b}<!-- note -->",  # GitHub strips the comment
        "note <?pi?>\nUNITARES agent: {b}",
        "<div>\n\nUNITARES agent: {b}\n\n</div>",  # a plain container
        "text <del>struck\n\nUNITARES agent: {b}",  # the paragraph end closes <del>
        "<div><table>\n\nUNITARES agent: {b}\n\n</table></div>",  # moved out of the table
        "<blockquote><td></blockquote>\n\nUNITARES agent: {b}",  # a stray <td> is ignored
        # </table> closes the table with its cell, and then the quote can close.
        "<blockquote><table><tr><td>x</table></blockquote>\n\nUNITARES agent: {b}",
        "UNITARES agent: {b}\u2028",  # edge whitespace hides nothing
    ],
)
def test_a_comment_or_a_plain_container_does_not_hide_a_footer(visible):
    body = f"UNITARES agent: {_UUID_A}\n\n" + visible.format(b=_UUID_B)
    assert footer_of(body) == _UUID_B


def test_the_rendered_fixture_covers_every_case_the_old_scanner_got_wrong():
    bodies = set(_RENDERED)
    assert f"<pre>\nlog `</pre>` tail\n\n---\nUNITARES agent: {_UUID_A}\n" in bodies
    assert f"Write \\\\`<!--` literally.\n\n---\nUNITARES agent: {_UUID_A}\n" in bodies


@pytest.mark.parametrize(
    "html",
    [
        f"<p>UNITARES agent: {_UUID_A}</p><!-- x -->",  # GitHub strips comments
        f"<p>UNITARES agent: {_UUID_A}",  # never closed
        f"<div><p>UNITARES agent: {_UUID_A}</div></p>",  # misnested
        f"<details/><p>UNITARES agent: {_UUID_A}</p>",  # self-closing non-void
        f"<p>UNITARES agent: {_UUID_A}</br></p>",
        f"<!DOCTYPE html><p>UNITARES agent: {_UUID_A}</p>",
        f"<p>UNITARES agent: {_UUID_A}</p></span>",  # a stray end tag
    ],
)
def test_html_that_github_did_not_render_is_refused_not_guessed(html):
    from agents.github_observer import footer

    with pytest.raises(footer.UnrenderedHTML):
        ev.agent_uuid_from_body_html(html)


def test_unrendered_or_missing_body_html_fails_the_pull_request(pr_open):
    # Either is a failed read, never an unsigned pull request.
    for broken in (dict(pr_open, body_html="<p>UNITARES agent: x"), {k: v for k, v in pr_open.items() if k != "body_html"}):
        counts, events = cycle(FakeReader(pulls=[broken]))
        assert counts["prs_failed"] == 1 and counts["prs_unsigned"] == 0 and events == []


@pytest.mark.parametrize(
    ("tildes", "expected"),
    [
        # GFM strikes a run of one or two tildes, paired by equal length; a
        # run of three or more is literal (GitHub POST /markdown, 2026-11-17).
        ("text ~~~example\nUNITARES agent: {b}\nexample~~~", _UUID_B),
        ("a ~~~~x\nUNITARES agent: {b}\ny~~~~", _UUID_B),
        ("~~x\nUNITARES agent: {b}\ny~", _UUID_B),  # unequal runs do not pair
        ("~UNITARES agent: {b}~", _UUID_A),
        ("~~UNITARES agent: {b}~~", _UUID_A),
        ("x~y\nUNITARES agent: {b}\nz~w", _UUID_A),  # intraword, across lines
    ],
)
def test_strikethrough_follows_gfm_tilde_rules(tildes, expected):
    body = f"UNITARES agent: {_UUID_A}\n\n" + tildes.format(b=_UUID_B)
    assert footer_of(body) == expected


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        # A block start tag ends the paragraph, so the text before it is a
        # line of its own (GitHub POST /markdown, 2026-11-17).
        ("UNITARES agent: {b}<hr>suffix", _UUID_B),
        ("UNITARES agent: {b}<div>suffix</div>", _UUID_B),
        ("note\nUNITARES agent: {b}<h2>h</h2>", _UUID_B),
        ("prefix<hr>UNITARES agent: {b}", _UUID_A),  # after it: not in a paragraph
        # Formatting open at the block carries on inside it: bold, not plain.
        ("<b>x<div>UNITARES agent: {b}</div></b>", _UUID_A),
        ("<b>x<div>\n\nUNITARES agent: {b}", _UUID_A),
        ("<del>x<div>\n\nUNITARES agent: {b}\n\n</div>", _UUID_B),  # del is not re-opened
    ],
)
def test_a_block_tag_ends_the_paragraph_it_interrupts(after, expected):
    body = f"UNITARES agent: {_UUID_A}\n\n" + after.format(b=_UUID_B)
    assert footer_of(body) == expected


@pytest.mark.parametrize(
    ("unclosed", "expected"),
    [
        # GitHub re-opens these around the next paragraph (bold, code, italic).
        ("text <b>bold", _UUID_A),
        ("text <code>c", _UUID_A),
        ("text <i>x<hr>", _UUID_A),
        # ...and closes these with their paragraph (GitHub POST /markdown, 2026-11-17).
        ('text <a href="https://example.com">l', _UUID_B),
        ("text <span>s", _UUID_B),
        ("text <sub>s", _UUID_B),
    ],
)
def test_formatting_left_open_carries_into_the_next_paragraph(unclosed, expected):
    body = f"UNITARES agent: {_UUID_A}\n\n{unclosed}\n\nUNITARES agent: {_UUID_B}\n"
    assert footer_of(body) == expected


@pytest.mark.parametrize(
    ("body_tail", "expected"),
    [
        # A carried <b> closes at its own end tag inside the quote; the quote
        # stays open around the footer (GitHub POST /markdown, 2026-11-17).
        ("text <b>x\n\n<blockquote>y</b>\n\nUNITARES agent: {b}", _UUID_A),
        # Its end tag ends the carried <b>, so the next line is plain.
        ("text <b>x\n\nx</b>\nUNITARES agent: {b}", _UUID_B),
        # ...even inside a table, where the open stack would block a close.
        ("text <b>x<hr><table></b></table>\n\nUNITARES agent: {b}", _UUID_B),
        # A <b> closed by its container's end tag is re-opened around later text.
        ("<div><b>x</div>\n\nUNITARES agent: {b}", _UUID_A),
        ("<div><del>x</div>\n\nUNITARES agent: {b}", _UUID_B),
    ],
)
def test_active_formatting_is_tracked_apart_from_open_elements(body_tail, expected):
    body = f"UNITARES agent: {_UUID_A}\n\n" + body_tail.format(b=_UUID_B)
    assert footer_of(body) == expected


@pytest.mark.parametrize(
    ("body_tail", "expected"),
    [
        # Formatting left open in a cell or caption ends with it; formatting
        # opened before the table carries past it (GitHub POST /markdown, 2026-11-17).
        ("<table><tr><td><b>x</td></tr></table>\n\nUNITARES agent: {b}", _UUID_B),
        ("<table><caption><i>c</caption><tr><td>y</td></tr></table>\n\nUNITARES agent: {b}", _UUID_B),
        ("text <b>x<table><tr><td>y</td></tr></table>\n\nUNITARES agent: {b}", _UUID_A),
        # A </b> inside the cell cannot end a <b> carried from before the table.
        ("text <b>x<table><tr><td></b>y</td></tr></table>\n\nUNITARES agent: {b}", _UUID_A),
    ],
)
def test_formatting_left_open_in_a_table_cell_stays_in_the_cell(body_tail, expected):
    body = f"UNITARES agent: {_UUID_A}\n\n" + body_tail.format(b=_UUID_B)
    assert footer_of(body) == expected


@pytest.mark.parametrize(
    ("body_tail", "expected"),
    [
        # Prose that reads as an unknown tag: GitHub drops the tag (#3245's
        # `<name>` left a hand-built tree open and its footer unread).
        ("Fixes List<T> handling.", _UUID_B),
        ("Text <bdi>x", _UUID_B),
        ("text <nobr>x\n\nx</nobr>\nUNITARES agent: {b}", _UUID_B),
        # GitHub moves content out of a table and closes open cells.
        ("<table><details>\n\nUNITARES agent: {b}\n\n</details></table>", _UUID_A),
        ("<table><b><tr><td>x</table>\n\nUNITARES agent: {b}", _UUID_A),
        ("<table><tr><td>a<td>b</td>\n\nUNITARES agent: {b}\n\n</table>", _UUID_B),
    ],
)
def test_github_resolves_unknown_tags_and_tables_before_the_footer_is_read(body_tail, expected):
    tail = body_tail.format(b=_UUID_B)
    if "UNITARES agent" not in tail:
        tail += f"\n\nUNITARES agent: {_UUID_B}"
    assert footer_of(f"UNITARES agent: {_UUID_A}\n\n{tail}\n") == expected




@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"UNITARES agent: {_UUID_B}<img src=x>", _UUID_A),  # a void element on the line
        (f"UNITARES agent: {_UUID_B}<sub></sub>", _UUID_A),  # an empty element on the line
        (f"UNITARES agent: {_UUID_B}", _UUID_B),
    ],
)
def test_any_element_on_the_footer_line_makes_it_not_plain(line, expected):
    html = f"<p>UNITARES agent: {_UUID_A}</p>\n<p>{line}</p>"
    assert ev.agent_uuid_from_body_html(html) == expected


# --- live writer & main() live mode ---------------------------------------


def test_unitares_event_writer_posts_with_operator_header_and_no_redirect():
    import io as _io
    import urllib.error

    seen_headers: list[dict] = []
    seen_urls: list[str] = []

    class _CreatedResponse:
        status = 201

        def __init__(self, body=b'{"success": true, "event_id": "evt-1", "fingerprint": "1#1:opened"}'):
            self._body = body

        def read(self, n=-1):
            return self._body if n < 0 else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def ok_opener(request, timeout=None):
        seen_urls.append(request.full_url)
        seen_headers.append(dict(request.header_items()))
        assert request.get_method() == "POST"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["producer"] == "github_observer"
        return _CreatedResponse()

    writer = obs.UnitaresEventWriter("http://127.0.0.1:8767/", "secret-op", opener=ok_opener)
    event = {"type": "github.pr_opened", "fingerprint": "1#1:opened", "agent_uuid": AGENT}
    assert writer.write(event) == "written"
    assert seen_urls == ["http://127.0.0.1:8767/v1/external/events"]
    lowered = {k.lower(): v for k, v in seen_headers[0].items()}
    assert lowered["x-unitares-operator"] == "secret-op"
    assert "authorization" not in lowered

    for code, expected in ((409, "duplicate"), (404, "unknown_agent"), (403, "failed"), (500, "failed")):
        def err_opener(request, timeout=None, _code=code):
            raise urllib.error.HTTPError(request.full_url, _code, "err", {}, _io.BytesIO(b"secret"))

        w = obs.UnitaresEventWriter("http://127.0.0.1:8767", "secret-op", opener=err_opener)
        assert w.write(event) == expected

    def net_err_opener(request, timeout=None):
        raise TimeoutError("timed out")

    assert obs.UnitaresEventWriter("http://127.0.0.1:8767", "secret-op", opener=net_err_opener).write(event) == "failed"

    # Non-JSON 200 (e.g. HTML proxy fallback) or incomplete ack must fail rather than mark handled.
    class _FakeHttp200:
        status = 200

        def __init__(self, body: bytes):
            self._body = body

        def read(self, n=-1):
            return self._body if n < 0 else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for bad_body in (
        b"<html>proxy fallback</html>",
        b'{"success": false, "event_id": "evt-1", "fingerprint": "1#1:opened"}',
        b'{"success": true, "fingerprint": "1#1:opened"}',
        b'{"success": true, "event_id": "evt-1", "fingerprint": "wrong-fp"}',
    ):
        assert obs.UnitaresEventWriter(
            "http://127.0.0.1:8767",
            "secret-op",
            opener=lambda req, timeout=None, _b=bad_body: _FakeHttp200(_b),
        ).write(event) == "failed"

    # Real _DEFAULT_OPENER must refuse redirects instead of forwarding X-Unitares-Operator.
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/evil")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        real_writer = obs.UnitaresEventWriter(f"http://127.0.0.1:{server.server_port}", "secret-op")
        assert real_writer.write(event) == "failed"
    finally:
        server.shutdown()
        server.server_close()


def test_run_cycle_counts_duplicate_and_unknown_agent_separately():
    class MixedWriter:
        def __init__(self):
            self.n = 0

        def write(self, event):
            self.n += 1
            if self.n == 1:
                return "duplicate"
            if self.n == 2:
                return "unknown_agent"
            if self.n == 3:
                return "failed"
            return "written"

    marked: list[str] = []
    counts, _ = cycle(
        FakeReader(),
        writer=MixedWriter(),
        mark=lambda fp, t: marked.append(fp),
    )
    assert counts["events_known"] == 1
    assert counts["events_unknown_agent"] == 1
    assert counts["writes_failed"] == 1
    assert counts["events_new"] == 9
    assert len(marked) == 10  # 1 duplicate + 9 written; unknown_agent & failed not marked


def test_main_live_mode_persists_fingerprints_and_tracks_failures(tmp_path, monkeypatch, capsys):
    state = tmp_path / "live.sqlite3"
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("UNITARES_OPERATOR_TOKEN", "op-secret")
    monkeypatch.delenv("UNITARES_URL", raising=False)
    monkeypatch.setattr(obs, "GitHubReader", lambda client: FakeReader())

    posted: list[dict] = []

    class FakeWriter:
        def __init__(self, base_url, token):
            assert base_url == "http://127.0.0.1:8767"
            assert token == "op-secret"

        def write(self, event):
            posted.append(event)
            return "written"

    monkeypatch.setattr(obs, "UnitaresEventWriter", FakeWriter)
    args = ["--repo", REPO, "--state-path", str(state), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}"]
    assert obs.main(args) == 0
    summary = json.loads(capsys.readouterr().err)
    assert summary["dry_run"] is False and summary["events_new"] == 12
    assert len(posted) == 12
    assert len(read_only_fingerprints(state)) == 12

    # Second run sees all 12 in durable store and writes nothing.
    posted.clear()
    assert obs.main(args) == 0
    summary2 = json.loads(capsys.readouterr().err)
    assert summary2["events_new"] == 0 and summary2["events_known"] == 12
    assert posted == []

    # Invalid --unitares-url exits 2; write failure exits 1.
    with pytest.raises(SystemExit):
        obs.main([*args, "--unitares-url", "ftp://bad.example"])

    class FailingWriter:
        def __init__(self, base_url, token):
            assert base_url == "https://gov.example"

        def write(self, event):
            return "failed"

    monkeypatch.setattr(obs, "UnitaresEventWriter", FailingWriter)
    assert obs.main([*args, "--state-path", str(tmp_path / "fail.sqlite3"), "--unitares-url", "https://gov.example"]) == 1


def test_permanent_refusals_classify_rejected_and_dry_run_ignores_unitares_url(tmp_path, monkeypatch, capsys):
    import io as _io
    import urllib.error

    event = {"type": "github.pr_opened", "fingerprint": "1#2:opened", "agent_uuid": AGENT}
    for code, body_bytes, expected in (
        (400, b'{"error": "bad payload"}', "rejected"),
        (413, b'{"error": "too big"}', "rejected"),
        (422, b'{"error": "observed_at outside accepted retention window"}', "rejected"),
        (422, b'{"error": "observed_at in the future"}', "future_timestamp"),
        (403, b'{"error": "untrusted pr_author"}', "untrusted_author"),
        (403, b'{"error": "operator credential required"}', "failed"),
        (401, b'{"error": "unauthorized"}', "failed"),
    ):
        def opener(request, timeout=None, _code=code, _body=body_bytes):
            raise urllib.error.HTTPError(request.full_url, _code, "err", {}, _io.BytesIO(_body))

        assert obs.UnitaresEventWriter("http://127.0.0.1:8767", "op", opener=opener).write(event) == expected

    class UntrustedAuthorWriter:
        def __init__(self, base_url, token):
            pass

        def write(self, event):
            return "untrusted_author"

    class FutureTimestampWriter:
        def __init__(self, base_url, token):
            pass

        def write(self, event):
            return "future_timestamp"

    class UnknownAgentWriter:
        def __init__(self, base_url, token):
            pass

        def write(self, event):
            return "unknown_agent"

    class RejectingWriter:
        def __init__(self, base_url, token):
            pass

        def write(self, event):
            return "rejected"

    state = tmp_path / "rejected.sqlite3"
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("UNITARES_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("UNITARES_URL", "not-a-valid-url")
    monkeypatch.setattr(obs, "GitHubReader", lambda client: FakeReader())

    # --dry-run must ignore invalid UNITARES_URL and return 0.
    assert obs.main(["--repo", REPO, "--state-path", str(state), "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}", "--dry-run"]) == 0
    _ = capsys.readouterr()

    # 403 untrusted pr_author keeps fingerprints unmarked and exits 1 to surface config mismatch.
    monkeypatch.setattr(obs, "UnitaresEventWriter", UntrustedAuthorWriter)
    assert obs.main([
        "--repo", REPO,
        "--state-path", str(state),
        "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}",
        "--unitares-url", "http://127.0.0.1:8767",
    ]) == 1
    untrusted_summary = json.loads(capsys.readouterr().err)
    assert untrusted_summary["events_untrusted_author"] == 12
    assert untrusted_summary["writes_failed"] == 0
    assert len(read_only_fingerprints(state)) == 0

    # Transient future_timestamp (clock skew) keeps fingerprints unmarked and exits 1.
    monkeypatch.setattr(obs, "UnitaresEventWriter", FutureTimestampWriter)
    assert obs.main([
        "--repo", REPO,
        "--state-path", str(state),
        "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}",
        "--unitares-url", "http://127.0.0.1:8767",
    ]) == 1
    future_summary = json.loads(capsys.readouterr().err)
    assert future_summary["events_future"] == 12
    assert future_summary["writes_failed"] == 0
    assert len(read_only_fingerprints(state)) == 0

    # Unknown agent_uuid leaves fingerprints unmarked and exits 1 to expose bad subject attribution.
    monkeypatch.setattr(obs, "UnitaresEventWriter", UnknownAgentWriter)
    assert obs.main([
        "--repo", REPO,
        "--state-path", str(state),
        "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}",
        "--unitares-url", "http://127.0.0.1:8767",
    ]) == 1
    unknown_summary = json.loads(capsys.readouterr().err)
    assert unknown_summary["events_unknown_agent"] == 12
    assert unknown_summary["writes_failed"] == 0
    assert len(read_only_fingerprints(state)) == 0

    # Permanent 400/413/stale-422 refusals mark fingerprints once and exit 0.
    monkeypatch.setattr(obs, "UnitaresEventWriter", RejectingWriter)
    assert obs.main([
        "--repo", REPO,
        "--state-path", str(state),
        "--trusted-app", f"{APP_ID}:{APP_ACTOR_ID}",
        "--unitares-url", "http://127.0.0.1:8767",
    ]) == 0
    summary = json.loads(capsys.readouterr().err)
    assert summary["events_rejected"] == 12
    assert summary["writes_failed"] == 0
    assert len(read_only_fingerprints(state)) == 12

