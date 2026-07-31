"""Tests for the dogfood friction issue surfacer.

The behaviour that matters is dedup: a recurring friction must update its
existing issue, never spawn a second one. Before this existed, 19 findings
accumulated with rising recurrence_count and no issues filed since June.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "dogfood_issue_surfacer.py"
_spec = importlib.util.spec_from_file_location("dogfood_issue_surfacer", MODULE_PATH)
dis = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(dis)


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
    io, calls = make_io([finding()])
    dis.Surfacer(io=io, apply=True).run()
    create = [a for a in calls if a[:2] == ["issue", "create"]]
    assert len(create) == 1
    assert "dogfood friction:" in create[0][create[0].index("--title") + 1]


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
    dis.Surfacer(io=io, apply=True).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)
    assert not any(a[:2] == ["issue", "comment"] for a in calls)


def test_comments_when_recurred_with_new_change_token():
    """Content moved => the friction changed shape; update, don't duplicate."""
    f = finding(change_token="NEWTOKEN")
    prior = {"number": 42, "state": "OPEN", "title": "x",
             "body": dis.FP_MARKER.format(f["fingerprint"]) + "\n"
                     + dis.CT_MARKER.format("OLDTOKEN")}
    io, calls = make_io([f], issues=[prior])
    dis.Surfacer(io=io, apply=True).run()
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
    dis.Surfacer(io=io, apply=True).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_ignores_findings_not_routed_to_issue_surface():
    io, calls = make_io([finding(routes=["kg_note"])])
    dis.Surfacer(io=io, apply=True).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_skips_finding_with_no_fingerprint():
    """No fingerprint means no dedup key — filing it would guarantee a
    duplicate on the next run."""
    io, calls = make_io([finding(fingerprint="")])
    dis.Surfacer(io=io, apply=True).run()
    assert not any(a[:2] == ["issue", "create"] for a in calls)


def test_body_carries_repro_and_markers():
    f = finding(repro_command="curl localhost:8767/api/events", evidence_uri="file:///tmp/x.json")
    body = dis.render_body(f)
    assert "curl localhost:8767/api/events" in body
    assert "file:///tmp/x.json" in body
    assert dis.FP_MARKER.format(f["fingerprint"]) in body
    assert dis.CT_MARKER.format(f["change_token"]) in body
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
