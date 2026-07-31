"""Tests for the dogfood friction issue surfacer.

The behaviour that matters is dedup: a recurring friction must update its
existing issue, never spawn a second one. Before this existed, 19 findings
accumulated with rising recurrence_count and no issues filed since June.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "dogfood_issue_surfacer.py"
_spec = importlib.util.spec_from_file_location("dogfood_issue_surfacer", MODULE_PATH)
dis = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(dis)

CHECKPOINT = "2030-01-02T03:04:05Z"


def finding(**kw):
    base = {
        "surface": "Dashboard/API docs-to-command exactness",
        "attempted_action": "GET /api/events?event_type=x",
        "expected": "honor or reject the alias",
        "observed": "silently ignored, returned unfiltered success",
        "proposed_action": "alias it or 400 it",
        "severity": "medium",
        "reproducible": True,
        "fingerprint": "9028fa1ed06f9938",
        "change_token": "11d7bebcd75ff4b6",
        "routes": ["issue_surface", "kg_note"],
        "recurrence_count": 2,
        "event_timestamp": CHECKPOINT,
    }
    base.update(kw)
    return base


def make_io(findings, issues=None):
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(issues or [])
        return "https://github.com/CIRWEL/unitares/issues/999"

    return {"fetch_findings": lambda base, limit: findings, "gh": gh}, calls


def test_dry_run_creates_nothing():
    io, calls = make_io([finding()])
    dis.Surfacer(io=io, apply=False).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_creates_issue_when_new():
    f = finding()
    io, calls = make_io([f])
    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()
    create = [a for a in calls if a[:2] == ["issue", "create"]]
    assert len(create) == 1
    assert "dogfood friction:" in create[0][create[0].index("--title") + 1]


def test_label_rejection_retry_preserves_the_issue_body():
    """Missing repository labels must not turn the safe retry into an invalid call."""
    f = finding()
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "create"] and "--label" in args:
            raise RuntimeError("label not found")
        return "https://github.com/CIRWEL/unitares/issues/999"

    dis.Surfacer(
        io={"fetch_findings": lambda base, limit: [f], "gh": gh},
        apply=True,
        fingerprints={f["fingerprint"]},
    ).run()

    creates = [args for args in calls if args[:2] == ["issue", "create"]]
    assert len(creates) == 2
    retry = creates[-1]
    assert "--label" not in retry
    assert retry[retry.index("--body") + 1] == dis.render_body(f)


def test_fingerprint_selection_applies_only_reviewed_findings():
    reviewed = finding(fingerprint="reviewed-fp", surface="reviewed surface")
    unreviewed = finding(fingerprint="unreviewed-fp", surface="unreviewed surface")
    io, calls = make_io([reviewed, unreviewed])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={"reviewed-fp"},
    ).run()

    creates = [args for args in calls if args[:2] == ["issue", "create"]]
    assert len(creates) == 1
    body = creates[0][creates[0].index("--body") + 1]
    assert dis.FP_MARKER.format("reviewed-fp") in body
    assert dis.FP_MARKER.format("unreviewed-fp") not in body


def test_dedupes_on_fingerprint():
    """Same fingerprint + same change_token => already tracked, do nothing."""
    f = finding()
    prior = {"number": 42, "state": "OPEN", "title": "x",
             "body": dis.FP_MARKER.format(f["fingerprint"]) + "\n"
                     + dis.CT_MARKER.format(f["change_token"])}
    io, calls = make_io([f], issues=[prior])
    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)
    assert not any(a[:2] == ["issue", "comment"] for a in calls)


def test_comments_when_recurred_with_new_change_token():
    """Content moved => the friction changed shape; update, don't duplicate."""
    f = finding(change_token="NEWTOKEN")
    prior = {"number": 42, "state": "OPEN", "title": "x",
             "body": dis.FP_MARKER.format(f["fingerprint"]) + "\n"
                     + dis.CT_MARKER.format("OLDTOKEN")}
    io, calls = make_io([f], issues=[prior])
    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()
    comments = [a for a in calls if a[:2] == ["issue", "comment"]]
    assert len(comments) == 1
    assert "42" in comments[0]
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_closed_issue_still_dedupes():
    """A closed issue means a human judged it. Re-filing relitigates that."""
    f = finding()
    prior = {"number": 7, "state": "CLOSED", "title": "x",
             "body": dis.FP_MARKER.format(f["fingerprint"]) + "\n"
                     + dis.CT_MARKER.format(f["change_token"])}
    io, calls = make_io([f], issues=[prior])
    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_ignores_findings_not_routed_to_issue_surface():
    io, calls = make_io([finding(routes=["kg_note"])])
    dis.Surfacer(io=io, apply=False).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_skips_finding_with_no_fingerprint():
    """No fingerprint means no dedup key — filing it would guarantee a
    duplicate on the next run."""
    io, calls = make_io([finding(fingerprint="")])
    dis.Surfacer(io=io, apply=True, not_before=CHECKPOINT).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_body_carries_repro_and_markers():
    f = finding(repro_command="curl localhost:8767/api/events", evidence_uri="file:///tmp/x.json")
    body = dis.render_body(f)
    assert "curl localhost:8767/api/events" in body
    assert "file:///tmp/x.json" in body
    assert dis.FP_MARKER.format(f["fingerprint"]) in body
    assert dis.CT_MARKER.format(f["change_token"]) in body
    assert dis.SEM_MARKER.format(dis.semantic_key(f)) in body
    assert "recurrence_count**: 2" in body


def test_body_flags_ambiguous_and_policy_question():
    """The probe's own uncertainty must reach the human triaging it."""
    body = dis.render_body(finding(ambiguous=True, policy_question=True))
    assert "ambiguous" in body
    assert "policy_question" in body


def test_recurrence_of_one_is_not_advertised():
    assert "recurrence_count**:" not in dis.render_body(finding(recurrence_count=1))


@pytest.mark.parametrize("length,expect_ellipsis", [(40, False), (200, True)])
def test_title_bounded(length, expect_ellipsis):
    t = dis.render_title(finding(surface="s" * length))
    assert len(t) <= 110
    assert ("..." in t) is expect_ellipsis


def test_does_not_close_issues():
    """Triage stays human — the surfacer files and updates, never resolves."""
    src = MODULE_PATH.read_text()
    assert "issue\", \"close" not in src
    assert "'close'" not in src


def test_apply_without_review_or_checkpoint_fails_before_any_io():
    """An unattended actuator must declare its forward-only boundary."""
    calls = []

    def fetch_findings(base, limit):
        calls.append(("fetch", base, limit))
        return [finding()]

    def gh(args):
        calls.append(("gh", args))
        return "[]"

    with pytest.raises(RuntimeError, match="--not-before|--fingerprint"):
        dis.Surfacer(
            io={"fetch_findings": fetch_findings, "gh": gh},
            apply=True,
        ).run()

    assert calls == []


def test_bulk_apply_only_actuates_findings_at_or_after_checkpoint():
    historical = finding(
        fingerprint="historical-fp",
        surface="historical surface",
        event_timestamp="2030-01-02T03:04:04Z",
    )
    boundary = finding(
        fingerprint="boundary-fp",
        surface="boundary surface",
        event_timestamp=CHECKPOINT,
    )
    io, calls = make_io([historical, boundary])

    dis.Surfacer(io=io, apply=True, not_before=CHECKPOINT).run()

    creates = [args for args in calls if args[:2] == ["issue", "create"]]
    assert len(creates) == 1
    body = creates[0][creates[0].index("--body") + 1]
    assert dis.FP_MARKER.format("boundary-fp") in body
    assert dis.FP_MARKER.format("historical-fp") not in body


def test_bulk_apply_skips_finding_without_a_parseable_event_timestamp(capsys):
    io, calls = make_io([finding(event_timestamp=None)])

    dis.Surfacer(io=io, apply=True, not_before=CHECKPOINT).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)
    assert "timestamp" in capsys.readouterr().out.lower()


def test_reviewed_fingerprint_can_apply_historical_finding_without_checkpoint():
    reviewed = finding(
        fingerprint="reviewed-historical-fp",
        event_timestamp="2000-01-01T00:00:00Z",
    )
    io, calls = make_io([reviewed])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={"reviewed-historical-fp"},
    ).run()

    assert len([args for args in calls if args[:2] == ["issue", "create"]]) == 1


def test_not_before_requires_an_explicit_utc_offset():
    with pytest.raises(ValueError, match="UTC offset"):
        dis.Surfacer(not_before="2030-01-02T03:04:05")


def test_semantic_key_is_structural_normalized_and_conservative():
    base = finding()
    formatting_variant = finding(
        surface="  DASHBOARD/API DOCS-TO-COMMAND EXACTNESS ",
        attempted_action="GET   /api/events?event_type=x",
        expected="HONOR OR REJECT THE ALIAS",
        observed="silently ignored,   returned unfiltered success",
    )
    meaning_changed = finding(observed="request was rejected with a typed error")

    assert dis.semantic_key(base) == dis.semantic_key(formatting_variant)
    assert dis.semantic_key(base) != dis.semantic_key(meaning_changed)
    assert dis.semantic_key(finding(expected="")) is None


def test_semantic_marker_dedupes_a_different_fingerprint():
    f = finding(fingerprint="new-fingerprint")
    marker = dis.SEM_MARKER.format(dis.semantic_key(f))
    prior = {
        "number": 42,
        "state": "OPEN",
        "title": "x",
        "body": marker + "\n" + dis.CT_MARKER.format(f["change_token"]),
        "comments": [],
    }
    io, calls = make_io([f], issues=[prior])

    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)
    assert not any(args[:2] == ["issue", "comment"] for args in calls)


def test_semantic_marker_in_closed_issue_comment_is_deduped():
    f = finding(fingerprint="new-fingerprint")
    marker = dis.SEM_MARKER.format(dis.semantic_key(f))
    prior = {
        "number": 7,
        "state": "CLOSED",
        "title": "human-triaged",
        "body": "No surfacer marker remains in the edited body.",
        "comments": [
            {"body": marker + "\n" + dis.CT_MARKER.format(f["change_token"])}
        ],
    }
    io, calls = make_io([f], issues=[prior])

    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)
    issue_lists = [args for args in calls if args[:2] == ["issue", "list"]]
    assert issue_lists
    assert all(args[args.index("--state") + 1] == "all" for args in issue_lists)
    assert all("comments" in args[args.index("--json") + 1] for args in issue_lists)


def test_semantic_recurrence_comment_carries_all_machine_markers():
    f = finding(fingerprint="new-fingerprint", change_token="new-token")
    marker = dis.SEM_MARKER.format(dis.semantic_key(f))
    prior = {
        "number": 42,
        "state": "OPEN",
        "title": "x",
        "body": marker + "\n" + dis.CT_MARKER.format("old-token"),
        "comments": [],
    }
    io, calls = make_io([f], issues=[prior])

    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()

    comments = [args for args in calls if args[:2] == ["issue", "comment"]]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert dis.FP_MARKER.format(f["fingerprint"]) in body
    assert dis.CT_MARKER.format(f["change_token"]) in body
    assert marker in body


def test_ambiguous_semantic_matches_are_flagged_without_mutation(capsys):
    f = finding(fingerprint="new-fingerprint")
    marker = dis.SEM_MARKER.format(dis.semantic_key(f))
    issues = [
        {"number": 41, "state": "OPEN", "title": "a", "body": marker, "comments": []},
        {"number": 42, "state": "CLOSED", "title": "b", "body": marker, "comments": []},
    ]
    io, calls = make_io([f], issues=issues)

    dis.Surfacer(io=io, apply=True, fingerprints={f["fingerprint"]}).run()

    assert not any(args[:2] in (["issue", "create"], ["issue", "comment"]) for args in calls)
    assert "ambiguous" in capsys.readouterr().out.lower()


def test_unreadable_existing_issue_index_fails_closed():
    f = finding()
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "not-json"
        return "unexpected mutation"

    with pytest.raises(RuntimeError, match="existing issue index"):
        dis.Surfacer(
            io={"fetch_findings": lambda base, limit: [f], "gh": gh},
            apply=True,
            fingerprints={f["fingerprint"]},
        ).run()

    assert not any(args[:2] in (["issue", "create"], ["issue", "comment"]) for args in calls)


def test_semantic_duplicates_in_one_batch_create_one_issue():
    first = finding(fingerprint="first-fingerprint")
    second = finding(
        fingerprint="second-fingerprint",
        surface=first["surface"].upper(),
        expected=first["expected"].upper(),
    )
    io, calls = make_io([first, second])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={first["fingerprint"], second["fingerprint"]},
    ).run()

    assert len([args for args in calls if args[:2] == ["issue", "create"]]) == 1


def test_fetch_findings_attaches_the_durable_event_timestamp(monkeypatch):
    captured = {}

    def run(args, **kwargs):
        captured["sql"] = args[-1]
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps({"fingerprint": "fp", "event_timestamp": CHECKPOINT}) + "\n",
        )

    monkeypatch.setattr(dis, "_resolve_psql", lambda: "psql")
    monkeypatch.setattr(dis.subprocess, "run", run)

    rows = dis.io_fetch_findings("unused", 5)

    assert rows[0]["event_timestamp"] == CHECKPOINT
    assert "jsonb_build_object('event_timestamp', ts)" in captured["sql"]


def test_fetch_findings_rejects_malformed_partial_rows(monkeypatch):
    monkeypatch.setattr(dis, "_resolve_psql", lambda: "psql")
    monkeypatch.setattr(
        dis.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout='{"fingerprint":"valid"}\nnot-json\n',
        ),
    )

    with pytest.raises(RuntimeError, match="malformed JSON"):
        dis.io_fetch_findings("unused", 5)


def test_successful_bulk_apply_persists_and_reuses_checkpoint(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    f = finding(event_timestamp="2030-01-02T03:04:06Z")
    io, _ = make_io([f])

    dis.Surfacer(
        io=io,
        apply=True,
        not_before=CHECKPOINT,
        checkpoint_path=checkpoint_path,
    ).run()

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "repo": dis.DEFAULT_REPO,
        "event_type": dis.EVENT_TYPE,
        "event_timestamp": "2030-01-02T03:04:06Z",
    }
    dis.Surfacer(
        io=io,
        apply=True,
        checkpoint_path=checkpoint_path,
    ).run()


def test_failed_apply_does_not_create_or_advance_checkpoint(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "[]"
        raise RuntimeError("HTTP 401 authentication failed")

    with pytest.raises(RuntimeError, match="401"):
        dis.Surfacer(
            io={"fetch_findings": lambda base, limit: [finding()], "gh": gh},
            apply=True,
            not_before=CHECKPOINT,
            checkpoint_path=checkpoint_path,
        ).run()

    assert not checkpoint_path.exists()
    assert len([args for args in calls if args[:2] == ["issue", "create"]]) == 1


def test_corrupt_or_symlinked_checkpoint_fails_before_remote_io(tmp_path):
    calls = []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not-json", encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(corrupt)

    for path in (corrupt, linked):
        with pytest.raises(dis.UnsafeApplyError, match="checkpoint"):
            dis.Surfacer(
                io={
                    "fetch_findings": lambda base, limit: calls.append("fetch"),
                    "gh": lambda args: calls.append("gh"),
                },
                apply=True,
                checkpoint_path=path,
            ).run()

    assert calls == []


def test_bulk_apply_refuses_saturated_fetch_without_checkpoint_advance(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    io, calls = make_io([finding()])

    with pytest.raises(dis.UnsafeApplyError, match="--limit"):
        dis.Surfacer(
            io=io,
            apply=True,
            not_before=CHECKPOINT,
            checkpoint_path=checkpoint_path,
            limit=1,
        ).run()

    assert not checkpoint_path.exists()
    assert not calls


def test_existing_issue_index_refuses_search_cap_truncation():
    f = finding()
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"number": index} for index in range(1000)])
        return "unexpected mutation"

    with pytest.raises(RuntimeError, match="search cap"):
        dis.Surfacer(
            io={"fetch_findings": lambda base, limit: [f], "gh": gh},
            apply=True,
            fingerprints={f["fingerprint"]},
        ).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)


def test_markerless_legacy_issue_dedupes_observed_text_change():
    old = finding(fingerprint="old-fp", observed="old diagnostic count was 3")
    new = finding(fingerprint="new-fp", observed="new diagnostic count is 4")
    prior = {
        "number": 42,
        "state": "CLOSED",
        "title": dis.render_title(old),
        "body": "\n".join(
            line
            for line in dis.render_body(old).splitlines()
            if not line.startswith("<!-- dogfood-")
        ),
        "comments": [],
    }
    io, calls = make_io([new], issues=[prior])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={new["fingerprint"]},
    ).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)


def test_non_label_create_failure_is_not_retried():
    f = finding()
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "[]"
        raise RuntimeError("HTTP 401 authentication failed")

    with pytest.raises(RuntimeError, match="401"):
        dis.Surfacer(
            io={"fetch_findings": lambda base, limit: [f], "gh": gh},
            apply=True,
            fingerprints={f["fingerprint"]},
        ).run()

    assert len([args for args in calls if args[:2] == ["issue", "create"]]) == 1


def test_create_requires_a_canonical_issue_url():
    f = finding()
    io, calls = make_io([f])
    io["gh"] = lambda args: (
        "[]" if args[:2] == ["issue", "list"] else "issue created"
    )

    with pytest.raises(RuntimeError, match="invalid issue URL"):
        dis.Surfacer(
            io=io,
            apply=True,
            fingerprints={f["fingerprint"]},
        ).run()


def test_fingerprint_and_timestamp_selectors_intersect():
    f = finding(event_timestamp="2000-01-01T00:00:00Z")
    io, calls = make_io([f])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={f["fingerprint"]},
        not_before=CHECKPOINT,
    ).run()

    assert not any(args[:2] == ["issue", "create"] for args in calls)


def test_connected_batch_collapse_is_order_independent_and_newest_wins():
    first = finding(
        fingerprint="shared-fp",
        surface="identity-a",
        event_timestamp="2030-01-02T03:04:05Z",
    )
    bridge = finding(
        fingerprint="shared-fp",
        surface="identity-b",
        event_timestamp="2030-01-02T03:04:06Z",
    )
    newest = finding(
        fingerprint="other-fp",
        surface="identity-b",
        event_timestamp="2030-01-02T03:04:07Z",
    )

    forward = dis.Surfacer._collapse_batch([first, bridge, newest])
    reverse = dis.Surfacer._collapse_batch([newest, bridge, first])

    assert [row["fingerprint"] for row in forward] == ["other-fp"]
    assert forward == reverse


def test_explicitly_reviewed_fingerprint_can_lack_semantic_fields():
    reviewed = finding(expected="", observed="", fingerprint="reviewed-incomplete")
    io, calls = make_io([reviewed])

    dis.Surfacer(
        io=io,
        apply=True,
        fingerprints={reviewed["fingerprint"]},
    ).run()

    assert len([args for args in calls if args[:2] == ["issue", "create"]]) == 1
