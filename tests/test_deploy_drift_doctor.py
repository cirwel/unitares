"""Tests for the deploy-drift doctor.

Pins the behaviour that motivated it: a merged fix sitting dark because the
live checkout was never pulled (unitares-pi-plugin #8, 2026-07-28), and the
follow-on state where the bytes are on disk but the process still holds the
old module.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "deploy_drift_doctor.py"
_spec = importlib.util.spec_from_file_location("deploy_drift_doctor", MODULE_PATH)
ddd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ddd)


def make_io(*, behind: int = 0, head_epoch: float | None = None,
            started: float | None = None, log_subjects: str = "abc1234 fix: a thing",
            changed_files: str = "src/thing.py"):
    """IO seam returning a scripted git/process state."""
    def git(path, *args):
        if args[:1] == ("rev-list",):
            return f"0\t{behind}"
        if args[:2] == ("log", "-1"):
            return str(head_epoch) if head_epoch is not None else ""
        if any(a.startswith("--since=") for a in args):
            return changed_files
        if args[:1] == ("log",):
            return log_subjects
        return ""
    return {
        "git": git,
        "fetch": lambda path: None,
        "process_start_epoch": lambda label: started,
        "post_finding": lambda payload: posted.append(payload),
        "post_outcome": lambda args: outcomes.append(args),
    }


posted: list = []
outcomes: list = []


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    posted.clear()
    outcomes.clear()
    monkeypatch.setattr(ddd, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ddd, "DEFAULT_SURFACES", [
        ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
    ])
    yield


def test_behind_origin_is_detected(tmp_path):
    """The #8 case: merged commits the live checkout never pulled."""
    d = ddd.diagnose(ddd.Surface("live-thing", str(tmp_path), "main", None),
                     make_io(behind=2))
    assert len(d) == 1
    assert d[0].condition == "behind_origin"
    assert d[0].behind == 2
    assert "not pulled" in d[0].detail


def test_restart_pending_is_detected(tmp_path):
    """Bytes on disk, old module in memory — what `git pull` alone leaves."""
    now = time.time()
    d = ddd.diagnose(ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
                     make_io(behind=0, head_epoch=now, started=now - 3600))
    assert len(d) == 1
    assert d[0].condition == "restart_pending"


def test_conditions_are_distinguished_not_merged(tmp_path):
    """Both true at once => two findings. Their fixes differ (pull vs restart),
    so collapsing them into one 'out of date' would mis-route the operator."""
    now = time.time()
    d = ddd.diagnose(ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
                     make_io(behind=3, head_epoch=now, started=now - 60))
    assert {x.condition for x in d} == {"behind_origin", "restart_pending"}


def test_pinned_deploy_worktree_exempt_from_behind(tmp_path):
    """Lagging origin is deliberate for the pinned deploy worktree, so it must
    not raise behind_origin — but restart lag is never deliberate."""
    now = time.time()
    s = ddd.Surface("pinned", str(tmp_path), "master", "com.example.svc", check_behind=False)
    d = ddd.diagnose(s, make_io(behind=5, head_epoch=now, started=now - 600))
    assert [x.condition for x in d] == ["restart_pending"]


def test_in_sync_yields_nothing(tmp_path):
    now = time.time()
    d = ddd.diagnose(ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
                     make_io(behind=0, head_epoch=now - 7200, started=now - 3600))
    assert d == []


def test_finding_posted_then_suppressed_by_cooldown(tmp_path):
    """A surface left un-deployed must not re-alert every cycle."""
    doc = ddd.Doctor(io=make_io(behind=1))
    doc.run()
    assert len(posted) == 1
    assert posted[0]["event_type"] == ddd.FINDING_KIND
    assert posted[0]["severity"] == "critical"

    ddd.Doctor(io=make_io(behind=1)).run()
    assert len(posted) == 1, "second cycle should be cooldown-suppressed"


def test_resolution_closes_finding_when_drift_clears(tmp_path):
    """An open finding nobody closes is how a detector decays into noise."""
    ddd.Doctor(io=make_io(behind=1)).run()
    doc2 = ddd.Doctor(io=make_io(behind=0))
    doc2.run()
    assert doc2.state.get("open") == {}


def test_no_outcome_event_without_baselined_identity(tmp_path, monkeypatch):
    """An outcome row carrying no EISV would add noise to the label-breadth
    problem it is meant to help, so emit nothing until an identity is set."""
    monkeypatch.delenv("DEPLOY_DRIFT_DOCTOR_UUID", raising=False)
    ddd.Doctor(io=make_io(behind=1)).run()
    ddd.Doctor(io=make_io(behind=0)).run()
    assert outcomes == []


def test_outcome_event_emitted_with_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_DRIFT_DOCTOR_UUID", "1111-2222")
    ddd.Doctor(io=make_io(behind=1)).run()
    ddd.Doctor(io=make_io(behind=0)).run()
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["verification_source"] == "external_signal"
    assert o["outcome_type"] == f"{ddd.FINDING_KIND}_confirmed"
    # Drift that was real and got deployed is a CORRECT call, not a false positive.
    assert o["is_bad"] is False


def test_dry_run_posts_nothing(tmp_path):
    doc = ddd.Doctor(io=make_io(behind=1), dry_run=True)
    doc.run()
    assert posted == []
    assert not Path(ddd.STATE_FILE).exists()


def test_doctor_never_heals():
    """Contract item 2. Pulling a live checkout and restarting governance-mcp
    is a production deploy on Lumen's check-in path — never automated."""
    src = MODULE_PATH.read_text()
    for forbidden in ("git\", \"pull", "'pull'", "kickstart", "launchctl load",
                      "merge --ff-only"):
        assert forbidden not in src, f"doctor must not deploy: found {forbidden!r}"


@pytest.mark.parametrize("etime,expected", [
    ("05:03", 303),
    ("01:00:00", 3600),
    ("2-03:00:00", 2 * 86400 + 3 * 3600),
])
def test_parse_etime(etime, expected):
    assert ddd._parse_etime(etime) == expected


def test_restart_pending_ignores_non_code_changes(tmp_path):
    """A pull carrying only markdown/config changes nothing in memory, so
    telling the operator to restart a production server would be wrong-class
    advice. Real case: the 2026-07-28 governance-plugin pull was 9 files,
    zero Python."""
    now = time.time()
    d = ddd.diagnose(
        ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
        make_io(behind=0, head_epoch=now, started=now - 600,
                changed_files=".github/dependabot.yml skills/a/SKILL.md "
                              "skills/SKILLS_MANIFEST.sha256"),
    )
    assert d == []


def test_restart_pending_fires_on_code_among_docs(tmp_path):
    """Mixed pull still needs a restart — one .py is enough."""
    now = time.time()
    d = ddd.diagnose(
        ddd.Surface("live-thing", str(tmp_path), "main", "com.example.svc"),
        make_io(behind=0, head_epoch=now, started=now - 600,
                changed_files="README.md src/handlers.py docs/x.md"),
    )
    assert [x.condition for x in d] == ["restart_pending"]
    assert "src/handlers.py" in d[0].detail
