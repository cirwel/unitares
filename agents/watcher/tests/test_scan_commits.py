"""Tests for ``scan_commits`` — the commit-trail → finding-resolution loop.

The contract: walk ``git log --since=<since>``, extract fingerprint-shaped
hex strings from each commit message, and resolve unique-prefix matches
against the findings store with the commit subject as resolution_reason.

Edge cases that need to stay locked down:
  - Revert commits don't auto-undo a prior fix.
  - Dismissed findings stay dismissed (no auto-resurrect of false positives).
  - Findings that already have a resolution_reason are not re-resolved
    (idempotent across repeated scans).
  - Ambiguous prefixes (matches >1 fingerprint) are skipped silently.
  - git log failures degrade to 0 — safe to call from cron.
"""

from __future__ import annotations

import contextlib
import json

import pytest

import agents.watcher.agent as watcher_module


@pytest.fixture(autouse=True)
def _isolate_findings_file(tmp_path, monkeypatch):
    """Per-test FINDINGS_FILE redirect. The autouse fixture in test_agent.py
    isn't visible across test files, so without this each test would inherit
    the previous test's findings.jsonl path and seed-data leaks across tests."""
    from agents.watcher import findings as watcher_findings

    tmp_state = tmp_path / "watcher-state"
    tmp_state.mkdir()
    findings_file = tmp_state / "findings.jsonl"

    monkeypatch.setattr(watcher_findings, "STATE_DIR", tmp_state)
    monkeypatch.setattr(watcher_findings, "FINDINGS_FILE", findings_file)
    monkeypatch.setattr(watcher_module, "FINDINGS_FILE", findings_file)
    # post_finding goes over the network — stub it out so resolution events
    # don't fail (or worse, leak) during scan_commits' update_finding_status calls.
    monkeypatch.setattr(watcher_findings, "post_finding", lambda **kw: True)
    monkeypatch.setattr(watcher_module, "post_finding", lambda **kw: True)
    yield


def _seed(findings_file, *findings: dict) -> None:
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    findings_file.write_text("\n".join(json.dumps(f) for f in findings) + "\n")


def _read_findings(findings_file) -> list[dict]:
    return [json.loads(line) for line in findings_file.read_text().splitlines() if line.strip()]


def _git_log_record(sha: str, subject: str, body: str = "") -> str:
    """Format one record the way ``git log --format=%H%x00%s%x00%b%x1e`` does."""
    return f"{sha}\x00{subject}\x00{body}\x1e"


@pytest.fixture
def make_subprocess_run(monkeypatch):
    """Patch subprocess.run inside agent.py to return a canned git log payload."""

    def _install(stdout: str, returncode: int = 0, stderr: str = "") -> None:
        class _Result:
            def __init__(self):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def _fake(*args, **kwargs):
            return _Result()

        monkeypatch.setattr(watcher_module.subprocess, "run", _fake)

    return _install


def _seed_finding(fingerprint: str, status: str = "confirmed", **extra) -> dict:
    return {
        "fingerprint": fingerprint,
        "pattern": extra.get("pattern", "P011"),
        "file": extra.get("file", "/tmp/x.py"),
        "line": extra.get("line", 1),
        "hint": extra.get("hint", "h"),
        "severity": extra.get("severity", "high"),
        "violation_class": extra.get("violation_class", "INT"),
        "status": status,
        **{k: v for k, v in extra.items() if k not in ("pattern", "file", "line", "hint", "severity", "violation_class")},
    }


class TestScanCommitsResolves:
    def test_resolves_fingerprint_referenced_in_commit_body(self, make_subprocess_run):
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(
            _git_log_record("deadbeef" * 5, "fix(thing): does the thing", f"resolves {fp}")
        )

        n = watcher_module.scan_commits(since="14 days ago")
        assert n == 1
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert len(rows) == 1
        assert rows[0]["status"] == "confirmed"
        assert rows[0].get("resolution_reason", "").startswith("referenced in deadbeef:")
        assert "fix(thing): does the thing" in rows[0]["resolution_reason"]

    def test_resolves_short_8_char_prefix(self, make_subprocess_run):
        """8-char prefix is the floor — short SHAs and short fingerprints
        often co-exist in commit bodies."""
        fp = "1234567890abcdef"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(_git_log_record("aaaaaaaa" * 5, "fix: thing", "fp 12345678"))
        assert watcher_module.scan_commits() == 1

    def test_resolves_full_16_char(self, make_subprocess_run):
        fp = "1234567890abcdef"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(_git_log_record("aaaaaaaa" * 5, "fix", fp))
        assert watcher_module.scan_commits() == 1


class TestScanCommitsSkips:
    def test_skips_revert_commits(self, make_subprocess_run):
        """A revert commit referencing a fingerprint must NOT auto-resolve —
        the fix it referenced was just rolled back."""
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(
            _git_log_record(
                "deadbeef" * 5,
                'Revert "fix(thing): introduced regression"',
                f"This reverts the fix referenced by {fp}",
            )
        )
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert rows[0]["status"] == "open"

    def test_skips_dismissed_findings(self, make_subprocess_run):
        """Dismissed = false positive. A coincidental commit reference must
        not flip it back to confirmed."""
        fp = "abcd1234ef005678"
        _seed(
            watcher_module.FINDINGS_FILE,
            _seed_finding(fp, status="dismissed", resolution_reason="fp"),
        )
        make_subprocess_run(_git_log_record("d" * 40, "fix something", fp))
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert rows[0]["status"] == "dismissed"

    def test_skips_already_resolved_findings(self, make_subprocess_run):
        """Idempotent: re-running the scanner mustn't churn the resolution_reason
        of findings already linked to a prior commit."""
        fp = "abcd1234ef005678"
        prior = "referenced in baadf00d: prior fix"
        _seed(
            watcher_module.FINDINGS_FILE,
            _seed_finding(fp, status="confirmed", resolution_reason=prior),
        )
        make_subprocess_run(_git_log_record("d" * 40, "newer fix", fp))
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert rows[0]["resolution_reason"] == prior

    def test_skips_confirmed_without_resolution_reason(self, make_subprocess_run):
        """An operator who ran ``--resolve <fp>`` without ``--reason`` left the
        finding ``status=confirmed`` with no ``resolution_reason``. A later
        coincidental fingerprint mention in a commit must not re-stamp
        ``confirmed_at`` or emit a duplicate governance event. Active-queue-only
        gating is what enforces this.
        """
        fp = "abcd1234ef005678"
        original_ts = "2026-01-01T00:00:00Z"
        _seed(
            watcher_module.FINDINGS_FILE,
            _seed_finding(fp, status="confirmed", confirmed_at=original_ts),
        )
        make_subprocess_run(_git_log_record("d" * 40, "fix", fp))
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        # confirmed_at must not have been re-stamped, resolution_reason must
        # still be absent (we didn't auto-fill anything).
        assert rows[0]["confirmed_at"] == original_ts
        assert "resolution_reason" not in rows[0]

    def test_skips_aged_out_findings(self, make_subprocess_run):
        """Same gating principle: aged_out is terminal, don't resurrect."""
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="aged_out"))
        make_subprocess_run(_git_log_record("d" * 40, "fix", fp))
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert rows[0]["status"] == "aged_out"

    def test_skips_ambiguous_prefix(self, make_subprocess_run):
        """An 8-char prefix matching two findings must not be resolved
        — the operator's intent is unclear."""
        fp1 = "abcd12340000aaaa"
        fp2 = "abcd12340000bbbb"
        _seed(
            watcher_module.FINDINGS_FILE,
            _seed_finding(fp1, status="open"),
            _seed_finding(fp2, status="open"),
        )
        make_subprocess_run(_git_log_record("d" * 40, "fix", "abcd1234"))
        assert watcher_module.scan_commits() == 0
        rows = _read_findings(watcher_module.FINDINGS_FILE)
        assert all(r["status"] == "open" for r in rows)

    def test_skips_nonmatching_hex_strings(self, make_subprocess_run):
        """Random hex like a SHA prefix that doesn't match any known
        fingerprint must not raise or update anything."""
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(_git_log_record("d" * 40, "fix", "see commit deadbeef"))
        assert watcher_module.scan_commits() == 0


class TestScanCommitsResilience:
    def test_returns_zero_on_git_failure(self, make_subprocess_run):
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run("", returncode=128, stderr="not a git repository")
        assert watcher_module.scan_commits() == 0

    def test_returns_zero_when_git_missing(self, monkeypatch):
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))

        def _raise(*a, **kw):
            raise FileNotFoundError("git not on PATH")

        monkeypatch.setattr(watcher_module.subprocess, "run", _raise)
        assert watcher_module.scan_commits() == 0

    def test_returns_zero_on_empty_findings(self, make_subprocess_run):
        # File never created — the iterator returns []
        make_subprocess_run(_git_log_record("d" * 40, "fix", "abcd1234ef005678"))
        assert watcher_module.scan_commits() == 0

    def test_dedups_duplicate_fingerprint_in_same_commit(self, make_subprocess_run):
        """One fingerprint mentioned twice in the same commit body should
        result in exactly one resolve, not two events."""
        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        body = f"fixes {fp}\nalso see {fp}"
        make_subprocess_run(_git_log_record("d" * 40, "fix", body))
        assert watcher_module.scan_commits() == 1


class TestScanCommitsLeaseAdvisory:
    """Phase A advisory lease wiring (RFC v0.5 §6.1).

    These tests verify that `scan_commits` routes through the lease plane's
    advisory scope, but DO NOT enforce any lease-related behavior — Phase A
    is telemetry-only. The body of `scan_commits` must run identically
    whether the lease is acquired, contended, or unavailable.
    """

    def test_scan_commits_invokes_lease_advisory_with_expected_surface(
        self, make_subprocess_run, monkeypatch
    ):
        """Surface_id must be stable across runs so Phase A telemetry can be
        correlated. Locking the shape down here so a future refactor that
        renames the surface gets a noisy test failure instead of silent
        telemetry drift."""
        from src.lease_plane import advisory as advisory_module

        captured: dict = {}
        original_scope = advisory_module.lease_advisory_scope

        @contextlib.contextmanager
        def _capturing_scope(*args, **kwargs):
            captured.update(kwargs)
            with original_scope(*args, **kwargs) as ret:
                yield ret

        monkeypatch.setattr(advisory_module, "lease_advisory_scope", _capturing_scope)

        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(_git_log_record("d" * 40, "fix", fp))

        # No bearer token in test env, so the wrapper falls back to disabled
        # client (every acquire → service_unavailable). The body MUST still
        # run end-to-end.
        monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
        result = watcher_module.scan_commits()

        assert result == 1, "Phase A advisory: body must run regardless of lease outcome"

        # surface_kind no longer passed (PR 2.5 — derived server-side from scheme prefix
        # via migration 026's generated column).
        assert "surface_kind" not in captured
        assert captured["surface_id"].startswith("resident:/watcher_scan_commits_")
        assert captured["ttl_s"] == 60
        assert "since=" in captured["intent"]

    def test_scan_commits_runs_when_lease_held_by_other(
        self, make_subprocess_run, monkeypatch
    ):
        """held_by_other MUST NOT block Phase A — body still runs."""
        from src.lease_plane import advisory as advisory_module

        @contextlib.contextmanager
        def _held_by_other_scope(**_kwargs):
            yield "held_by_other", None

        monkeypatch.setattr(
            advisory_module, "lease_advisory_scope", _held_by_other_scope
        )

        fp = "abcd1234ef005678"
        _seed(watcher_module.FINDINGS_FILE, _seed_finding(fp, status="open"))
        make_subprocess_run(_git_log_record("d" * 40, "fix", fp))

        assert watcher_module.scan_commits() == 1
