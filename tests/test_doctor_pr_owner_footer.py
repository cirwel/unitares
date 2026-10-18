"""The owner footer is how a later session finds who owns an open agent PR.

Local sessions get ``UNITARES agent: <uuid>`` stamped by the App runner. Hosted
cloud sessions type it by hand, and in October 2026 the record showed both
failure shapes: agent PRs with no footer (#2765, #2767-#2769) and a footer
naming an identity from another session. This check reads open agent PRs and
resolves each footer against ``core.identities``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"

KNOWN = "6359a70d-8849-4608-a82d-435db1520fe8"
UNMINTED = "00000000-0000-4000-8000-000000000000"


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


def _wire(monkeypatch, doctor, prs, known=(KNOWN,), runner="/bin/runner"):
    monkeypatch.setattr(doctor, "_github_runner", lambda: runner)
    monkeypatch.setattr(doctor, "_open_agent_prs", lambda *_a: prs)
    queries: list[str] = []

    def rows(_db, sql, **_k):
        queries.append(sql)
        return None if known is None else [[u] for u in known]

    monkeypatch.setattr(doctor, "_psql_rows", rows)
    return queries


def _run(doctor):
    return doctor.check_pr_owner_footer("postgres://x", REPO_ROOT)


def test_skips_without_the_app_runner(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [], runner=None)
    assert _run(doctor).status == doctor.Status.SKIP


def test_unreadable_pr_list_is_a_warning_not_a_pass(monkeypatch, doctor):
    _wire(monkeypatch, doctor, None)
    result = _run(doctor)
    assert result.status == doctor.Status.WARN
    assert "not a pass" in result.detail


def test_every_footer_resolving_passes(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [
        {"number": 1, "headRefName": "claude/a", "body": f"x\n\nUNITARES agent: {KNOWN}"},
    ])
    result = _run(doctor)
    assert result.status == doctor.Status.PASS
    assert "#1" in result.detail


def test_missing_footer_warns(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [
        {"number": 7, "headRefName": "codex/b", "body": "no owner line"},
    ])
    result = _run(doctor)
    assert result.status == doctor.Status.WARN
    assert "no footer: #7" in result.detail


def test_template_text_is_not_a_footer(monkeypatch, doctor):
    """#2808's body quoted the rule as `UNITARES agent: <uuid>`."""
    _wire(monkeypatch, doctor, [
        {"number": 8, "headRefName": "claude/c", "body": "End with `UNITARES agent: <uuid>`."},
    ])
    assert "no footer: #8" in _run(doctor).detail


def test_unminted_uuid_warns(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [
        {"number": 9, "headRefName": "claude/d", "body": f"UNITARES agent: {UNMINTED}"},
    ])
    result = _run(doctor)
    assert result.status == doctor.Status.WARN
    assert f"{UNMINTED} (#9)" in result.detail


def test_unreadable_identities_is_a_warning_not_a_pass(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [
        {"number": 1, "headRefName": "claude/a", "body": f"UNITARES agent: {KNOWN}"},
    ], known=None)
    result = _run(doctor)
    assert result.status == doctor.Status.WARN
    assert "not a pass" in result.detail


def test_uuids_reach_sql_only_after_the_strict_pattern(monkeypatch, doctor):
    queries = _wire(monkeypatch, doctor, [
        {"number": 1, "headRefName": "claude/a",
         "body": "UNITARES agent: 6359a70d-8849-4608-a82d-435db1520fe8'); drop"},
    ])
    _run(doctor)
    assert queries and "drop" not in queries[0]


def test_no_open_agent_prs_passes(monkeypatch, doctor):
    _wire(monkeypatch, doctor, [])
    assert _run(doctor).status == doctor.Status.PASS


def test_only_agent_branches_are_read(monkeypatch, doctor):
    payload = [
        {"number": 1, "headRefName": "dependabot/pip/x", "body": ""},
        {"number": 2, "headRefName": "auto/plugin-skill-sync", "body": ""},
        {"number": 3, "headRefName": "claude/e", "body": ""},
    ]

    class Done:
        def __init__(self, stdout):
            self.stdout = stdout

    calls = iter([Done("https://github.com/o/r.git\n"),
                  Done(__import__("json").dumps(payload))])
    monkeypatch.setattr(doctor.subprocess, "run", lambda *_a, **_k: next(calls))
    prs = doctor._open_agent_prs("/bin/runner", REPO_ROOT)
    assert [p["number"] for p in prs] == [3]


def test_registered_as_an_operator_check(doctor):
    """Registration is the wiring: doctor_findings escalates every operator check."""
    names = {c.name: c.mode for c in doctor.build_checks(REPO_ROOT, "postgres://x")}
    assert names.get("pr_owner_footer") == "operator"
