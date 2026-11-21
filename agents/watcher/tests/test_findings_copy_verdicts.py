"""Tests for cross-worktree Watcher verdict propagation (#3340).

Covers:
1. ``_copy_group_key`` extraction and delegation from ``_group_copies``.
2. ``update_finding_status`` propagation of ``dismissed`` verdicts to
   open/surfaced sibling copies in other worktrees as ``dup`` with
   ``duplicate_of`` (while ``confirmed`` stays branch-local so fixing one
   branch never closes unfixed sibling worktrees), emitting only a single
   governance resolution event and reporting the closed worktree copy count.
3. ``persist_findings`` carry-over of current-revision ``status=="dismissed"``
   AND ``resolution_reason=="fp"`` (and ONLY ``"fp"``) to brand-new worktree
   copies, writing them as ``dismissed``/``dup``/``duplicate_of`` while
   omitting them from ``fresh`` and suppressing high-severity event emission.
4. ``rule_version`` compatibility across both propagation and ``fp`` carry-over.
5. ``calibration.precision_by_pattern_and_class`` and ``routes._watcher_summary_from_rows``
   excluding propagated ``dup`` copies from resolution counts.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agents.watcher import findings as watcher_findings
from agents.watcher.calibration import (
    PRECISION_REASONS_TRUE_NEGATIVE,
    precision_by_pattern_and_class,
)
from agents.watcher.findings import (
    Finding,
    _copy_group_key,
    _group_copies,
    persist_findings,
    update_finding_status,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repo_with_worktrees(root: Path, *names: str) -> dict[str, Path]:
    """Create a git repo at ``root/main`` plus linked worktrees ``root/wt/<name>``."""
    main = root / "main"
    main.mkdir(parents=True)
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "--no-verify", "-m", "init", cwd=main)
    trees: dict[str, Path] = {"main": main}
    for name in names:
        wt_path = root / "wt" / name
        _git("worktree", "add", "-q", "-b", name, str(wt_path), cwd=main)
        trees[name] = wt_path
    for tree in trees.values():
        for rel in ("pkg/mod.py", "pkg/other.py"):
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("try:\n    work()\nexcept Exception:\n    pass\n")
    return {name: tree.resolve() for name, tree in trees.items()}


@pytest.fixture
def isolated_findings_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "watcher-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    findings_file = state_dir / "findings.jsonl"
    dedup_file = state_dir / "dedup.json"
    monkeypatch.setattr(watcher_findings, "STATE_DIR", state_dir)
    monkeypatch.setattr(watcher_findings, "FINDINGS_FILE", findings_file)
    monkeypatch.setattr(watcher_findings, "DEDUP_FILE", dedup_file)
    return state_dir


def _make_row(
    fp: str,
    file_path: Path | str,
    *,
    pattern: str = "P006",
    line: int = 3,
    severity: str = "medium",
    status: str = "open",
    line_content_hash: str = "c0ffee000000",
    detected_at: str = "2026-11-20T12:00:00Z",
    resolution_reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "pattern": pattern,
        "file": str(file_path),
        "line": line,
        "hint": "bare except",
        "severity": severity,
        "detected_at": detected_at,
        "model_used": "gemma4:latest",
        "line_content_hash": line_content_hash,
        "fingerprint": fp,
        "status": status,
        "violation_class": "ENT",
        "line_content": "except Exception:",
        "path_gone": False,
        "rule_version": 1,
    }
    if resolution_reason is not None:
        row["resolution_reason"] = resolution_reason
    row.update(extra)
    return row


def _make_finding(
    file_path: Path | str,
    *,
    pattern: str = "P006",
    line: int = 3,
    severity: str = "medium",
    line_content_hash: str = "c0ffee000000",
    detected_at: str = "2026-11-21T00:00:00Z",
) -> Finding:
    return Finding(
        pattern=pattern,
        file=str(file_path),
        line=line,
        hint="bare except",
        severity=severity,
        detected_at=detected_at,
        model_used="gemma4:latest",
        line_content_hash=line_content_hash,
        violation_class="ENT",
        line_content="except Exception:",
    )


def _write_rows(state_dir: Path, rows: list[dict[str, Any]]) -> None:
    (state_dir / "findings.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )


def _read_rows(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "findings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. _copy_group_key and _group_copies
# ---------------------------------------------------------------------------


class TestCopyGroupKey:
    def test_matches_across_worktrees_and_accepts_finding_or_dict(self, tmp_path: Path):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        cache: dict[str, Any] = {}
        row_main = _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3)
        finding_wt = _make_finding(trees["feat-a"] / "pkg/mod.py", line=19)

        key_main = _copy_group_key(row_main, cache)
        key_wt = _copy_group_key(finding_wt, cache)

        assert key_main is not None
        assert key_main == key_wt
        assert key_main[1:] == ("pkg/mod.py", "P006", "c0ffee000000", "medium")
        assert Path(key_main[0]).is_dir()

    def test_differs_across_repositories(self, tmp_path: Path):
        repo_a = _repo_with_worktrees(tmp_path / "repo-a")
        repo_b = _repo_with_worktrees(tmp_path / "repo-b")
        cache: dict[str, Any] = {}
        key_a = _copy_group_key(
            _make_row("aaaa000000000001", repo_a["main"] / "pkg/mod.py"), cache
        )
        key_b = _copy_group_key(
            _make_row("bbbb000000000002", repo_b["main"] / "pkg/mod.py"), cache
        )
        assert key_a is not None and key_b is not None
        assert key_a != key_b
        assert key_a[0] != key_b[0]

    def test_returns_none_for_r000_missing_hash_or_unresolvable_paths(self, tmp_path: Path):
        trees = _repo_with_worktrees(tmp_path)
        non_git_file = tmp_path / "outside" / "pkg" / "mod.py"
        non_git_file.parent.mkdir(parents=True)
        non_git_file.write_text("pass\n")

        assert _copy_group_key(
            _make_row("r000000000000001", trees["main"] / "pkg/mod.py", pattern="R000"),
            {},
        ) is None
        assert _copy_group_key(
            _make_row("h000000000000001", trees["main"] / "pkg/mod.py", line_content_hash=""),
            {},
        ) is None
        assert _copy_group_key(
            _make_row("o000000000000001", non_git_file),
            {},
        ) is None
        assert _copy_group_key(
            _make_row("rel0000000000001", "pkg/mod.py"),
            {},
        ) is None
        assert _copy_group_key(
            _make_row("gone000000000001", tmp_path / "deleted-wt" / "pkg/mod.py"),
            {},
        ) is None

    def test_group_copies_delegates_to_copy_group_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        rows = [
            _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3),
            _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=9),
        ]
        calls: list[str] = []
        real_copy_group_key = watcher_findings._copy_group_key

        def spy(finding, cache):
            calls.append(finding["fingerprint"])
            return real_copy_group_key(finding, cache)

        monkeypatch.setattr(watcher_findings, "_copy_group_key", spy)
        groups = _group_copies(rows, {})
        assert len(groups) == 1
        assert [r["fingerprint"] for r in groups[0]] == [
            "aaaa000000000001",
            "bbbb000000000002",
        ]
        assert calls == ["aaaa000000000001", "bbbb000000000002"]


# ---------------------------------------------------------------------------
# 2. update_finding_status cross-worktree propagation
# ---------------------------------------------------------------------------


class TestUpdateFindingStatusPropagation:
    def test_dismiss_propagates_to_open_and_surfaced_siblings_only(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        trees = _repo_with_worktrees(tmp_path / "repo-a", "feat-a", "feat-b", "feat-c")
        other_repo = _repo_with_worktrees(tmp_path / "repo-b")

        rows = [
            _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", status="open"),
            _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=12, status="surfaced"),
            _make_row("cccc000000000003", trees["feat-b"] / "pkg/mod.py", line=42, status="open"),
            # Must NOT be touched: already aged_out
            _make_row("dddd000000000004", trees["feat-c"] / "pkg/mod.py", status="aged_out"),
            # Must NOT be touched: different severity
            _make_row("eeee000000000005", trees["feat-a"] / "pkg/mod.py", line=30, severity="high"),
            # Must NOT be touched: different content hash
            _make_row(
                "ffff000000000006",
                trees["feat-a"] / "pkg/mod.py",
                line=31,
                line_content_hash="deadbeef0000",
            ),
            # Must NOT be touched: different git repository
            _make_row("1111000000000007", other_repo["main"] / "pkg/mod.py", status="open"),
        ]
        _write_rows(isolated_findings_state, rows)

        posted: list[tuple[str, str, str | None, str | None]] = []
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event",
            lambda finding, status, resolver, reason=None: posted.append(
                (finding["fingerprint"], status, resolver, reason)
            ),
        )

        sink: list[dict[str, Any]] = []
        rc = update_finding_status(
            "aaaa0000",
            "dismissed",
            resolver_agent_id="resolver-uuid-1",
            reason="fp",
            updated_finding_sink=sink,
        )
        assert rc == 0

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        target = by_fp["aaaa000000000001"]
        assert target["status"] == "dismissed"
        assert target["resolved_by"] == "resolver-uuid-1"
        assert target["resolution_reason"] == "fp"
        assert "duplicate_of" not in target
        assert target["dismissed_at"].endswith("Z")

        for sib_fp in ("bbbb000000000002", "cccc000000000003"):
            sib = by_fp[sib_fp]
            assert sib["status"] == "dismissed"
            assert sib["dismissed_at"] == target["dismissed_at"]
            assert sib["resolved_by"] == "resolver-uuid-1"
            assert sib["resolution_reason"] == "dup"
            assert sib["duplicate_of"] == "aaaa000000000001"

        assert by_fp["dddd000000000004"]["status"] == "aged_out"
        assert "duplicate_of" not in by_fp["dddd000000000004"]
        assert by_fp["eeee000000000005"]["status"] == "open"
        assert by_fp["ffff000000000006"]["status"] == "open"
        assert by_fp["1111000000000007"]["status"] == "open"

        # Exactly ONE governance resolution event and ONE sink item, for target_fp
        assert posted == [("aaaa000000000001", "dismissed", "resolver-uuid-1", "fp")]
        assert [item["fingerprint"] for item in sink] == ["aaaa000000000001"]

        stdout = capsys.readouterr().out
        assert stdout.startswith("ok: aaaa000000000001 → dismissed ")
        assert "2 worktree copies" in stdout

        # Calibration excludes "dup" siblings from TN count
        assert PRECISION_REASONS_TRUE_NEGATIVE == frozenset({"fp"})
        stats = precision_by_pattern_and_class(
            list(by_fp.values()),
            now=datetime.now(timezone.utc),
            min_weighted_n=0.5,
        )
        assert stats[("P006", "app")].weighted_dismissed == pytest.approx(1.0, rel=0.05)

    @pytest.mark.parametrize("reason", ["fixed", "fixed now", None])
    def test_confirm_stays_branch_local_and_leaves_sibling_worktrees_open(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        reason: str | None,
    ):
        """Confirming/fixing a bug in one branch must NOT close unfixed copies
        in sibling worktrees."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=8, status="surfaced"),
            ],
        )

        posted: list[str] = []
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event",
            lambda finding, status, resolver, reason=None: posted.append(finding["fingerprint"]),
        )

        rc = update_finding_status(
            "aaaa0000",
            "confirmed",
            resolver_agent_id="resolver-uuid-2",
            reason=reason,
        )
        assert rc == 0

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        target = by_fp["aaaa000000000001"]
        sib = by_fp["bbbb000000000002"]

        assert target["status"] == "confirmed"
        assert "duplicate_of" not in target
        if reason is not None:
            assert target["resolution_reason"] == reason

        assert sib["status"] == "surfaced"
        assert "confirmed_at" not in sib
        assert "duplicate_of" not in sib
        assert "resolution_reason" not in sib

        assert posted == ["aaaa000000000001"]

        stdout = capsys.readouterr().out
        assert "ok: aaaa000000000001 → confirmed " in stdout
        assert "worktree cop" not in stdout

        stats = precision_by_pattern_and_class(
            list(by_fp.values()),
            now=datetime.now(timezone.utc),
            min_weighted_n=0.5,
        )
        assert stats[("P006", "app")].weighted_confirmed == pytest.approx(1.0, rel=0.05)

    def test_aged_out_status_does_not_propagate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", status="open"),
            ],
        )
        rc = update_finding_status("aaaa0000", "aged_out")
        assert rc == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == "aged_out"
        assert by_fp["bbbb000000000002"]["status"] == "open"

    def test_r000_and_non_git_findings_do_not_propagate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        non_git_a = tmp_path / "outside-a" / "pkg" / "mod.py"
        non_git_b = tmp_path / "outside-b" / "pkg" / "mod.py"
        for path in (non_git_a, non_git_b):
            path.parent.mkdir(parents=True)
            path.write_text("pass\n")

        _write_rows(
            isolated_findings_state,
            [
                _make_row("r111000000000001", trees["main"] / "pkg/mod.py", pattern="R000"),
                _make_row("r222000000000002", trees["feat-a"] / "pkg/mod.py", pattern="R000"),
                _make_row("n111000000000001", non_git_a),
                _make_row("n222000000000002", non_git_b),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert update_finding_status("r1110000", "dismissed", reason="fp") == 0
        assert update_finding_status("n1110000", "dismissed", reason="fp") == 0

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["r222000000000002"]["status"] == "open"
        assert by_fp["n222000000000002"]["status"] == "open"


# ---------------------------------------------------------------------------
# 3. persist_findings fp carry-over
# ---------------------------------------------------------------------------


class TestPersistFindingsFpCarryOver:
    def test_carries_forward_fp_dismissal_to_brand_new_worktree_copy(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    severity="high",
                    status="dismissed",
                    resolution_reason="fp",
                    dismissed_at="2026-11-20T18:00:00Z",
                )
            ],
        )

        emitted: list[dict[str, Any]] = []
        monkeypatch.setattr(
            watcher_findings,
            "post_finding",
            lambda **kwargs: emitted.append(kwargs) or True,
        )

        new_copy = _make_finding(
            trees["feat-a"] / "pkg/mod.py",
            line=15,
            severity="high",
        )
        fresh = persist_findings([new_copy])

        assert fresh == []
        assert emitted == []  # suppressed: never mirror an auto-dismissed fp dup

        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 2
        appended = rows[1]
        assert appended["fingerprint"] == new_copy.fingerprint
        assert appended["status"] == "dismissed"
        assert appended["dismissed_at"].endswith("Z")
        assert appended["resolution_reason"] == "dup"
        assert appended["duplicate_of"] == prior_fp

        # Dedup was recorded, so a second scan of feat-a does not append again
        assert persist_findings([new_copy]) == []
        assert len(_read_rows(isolated_findings_state)) == 2

        # Calibration counts only the original fp dismissal, not the dup copy
        stats = precision_by_pattern_and_class(
            rows,
            now=datetime.now(timezone.utc),
            min_weighted_n=0.5,
        )
        assert stats[("P006", "app")].weighted_dismissed == pytest.approx(1.0, rel=0.05)

    @pytest.mark.parametrize(
        ("prior_status", "prior_reason"),
        [
            ("dismissed", "wont_fix"),
            ("dismissed", "out_of_scope"),
            ("dismissed", "unclear"),
            ("dismissed", "stale"),
            ("dismissed", "dup"),
            ("dismissed", "not fixing right now"),
            ("dismissed", None),
            ("confirmed", "fp"),
            ("confirmed", None),
            ("aged_out", "fp"),
        ],
    )
    def test_never_auto_carries_non_fp_or_non_dismissed_verdicts(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        prior_status: str,
        prior_reason: str | None,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    status=prior_status,
                    resolution_reason=prior_reason,
                )
            ],
        )

        new_copy = _make_finding(trees["feat-a"] / "pkg/mod.py", line=7)
        fresh = persist_findings([new_copy])

        assert [f.fingerprint for f in fresh] == [new_copy.fingerprint]
        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 2
        appended = rows[1]
        assert appended["status"] == "open"
        assert "dismissed_at" not in appended
        assert "resolution_reason" not in appended
        assert "duplicate_of" not in appended

    def test_mixed_batch_auto_dismisses_matching_fp_and_keeps_unrelated_fresh(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        other_repo = _repo_with_worktrees(tmp_path / "other-repo")
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    severity="high",
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        emitted: list[str] = []
        monkeypatch.setattr(
            watcher_findings,
            "post_finding",
            lambda **kwargs: emitted.append(kwargs["fingerprint"]) or True,
        )

        matching_copy = _make_finding(trees["feat-a"] / "pkg/mod.py", line=10, severity="high")
        other_repo_copy = _make_finding(
            other_repo["main"] / "pkg/mod.py", line=10, severity="high"
        )
        r000_finding = _make_finding(
            trees["feat-a"] / "pkg/mod.py",
            line=11,
            pattern="R000",
            severity="high",
        )

        fresh = persist_findings([matching_copy, other_repo_copy, r000_finding])
        assert [f.fingerprint for f in fresh] == [
            other_repo_copy.fingerprint,
            r000_finding.fingerprint,
        ]
        assert emitted == [other_repo_copy.fingerprint, r000_finding.fingerprint]

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[matching_copy.fingerprint]["status"] == "dismissed"
        assert by_fp[matching_copy.fingerprint]["resolution_reason"] == "dup"
        assert by_fp[matching_copy.fingerprint]["duplicate_of"] == prior_fp
        assert by_fp[other_repo_copy.fingerprint]["status"] == "open"
        assert by_fp[r000_finding.fingerprint]["status"] == "open"

    def test_expired_dedup_for_same_fp_row_does_not_create_self_duplicate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path)
        same_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    same_finding.fingerprint,
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        fresh = persist_findings([same_finding])
        assert fresh == []
        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 1
        assert rows[0]["resolution_reason"] == "fp"
        assert "duplicate_of" not in rows[0]

    def test_expired_dedup_on_propagated_dup_copy_does_not_resurface_or_duplicate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        """After FINDINGS_TTL_DAYS expires a sibling worktree's dedup entry,
        rescanning the propagated dismissed/dup copy must refresh dedup without
        re-opening or appending a colliding duplicate-fingerprint row — whether
        or not the original fp row is still present."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        wt_copy = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert persist_findings([wt_copy]) == []
        assert len(_read_rows(isolated_findings_state)) == 2

        # Simulate dedup TTL expiry by clearing dedup.json.
        (isolated_findings_state / "dedup.json").write_text("{}")

        assert persist_findings([wt_copy]) == []
        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 2
        assert [r["fingerprint"] for r in rows].count(wt_copy.fingerprint) == 1
        assert rows[1]["status"] == "dismissed"
        assert rows[1]["resolution_reason"] == "dup"
        assert rows[1]["duplicate_of"] == prior_fp
        assert rows[1]["duplicate_of_reason"] == "fp"
        dedup_after = json.loads((isolated_findings_state / "dedup.json").read_text())
        assert wt_copy.fingerprint in dedup_after

        # Even if the original fp row was compacted away, the remaining
        # propagated dup row still prevents resurfacing on dedup expiry.
        _write_rows(isolated_findings_state, [rows[1]])
        (isolated_findings_state / "dedup.json").write_text("{}")
        assert persist_findings([wt_copy]) == []
        rows_after_compact = _read_rows(isolated_findings_state)
        assert len(rows_after_compact) == 1
        assert rows_after_compact[0]["status"] == "dismissed"

    @pytest.mark.parametrize("reason", ["stale", "unclear", "wont_fix", None])
    def test_expired_dedup_on_non_fp_propagated_copy_resurfaces_like_target(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reason: str | None,
    ):
        """Copies propagated from non-fp dismissals (stale/unclear/wont_fix/None)
        must NOT stay suppressed after dedup TTL expiry — they resurface on
        rescan consistently with their target."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(
                main_finding.fingerprint, "dismissed", reason=reason
            )
            == 0
        )

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[wt_finding.fingerprint]["status"] == "dismissed"
        assert by_fp[wt_finding.fingerprint]["resolution_reason"] == "dup"
        assert by_fp[wt_finding.fingerprint]["duplicate_of"] == main_finding.fingerprint
        if reason is not None:
            assert by_fp[wt_finding.fingerprint]["duplicate_of_reason"] == reason
        else:
            assert "duplicate_of_reason" not in by_fp[wt_finding.fingerprint]

        # Simulate dedup TTL expiry: both target and its non-fp dup copy resurface
        (isolated_findings_state / "dedup.json").write_text("{}")
        resurfaced = persist_findings([main_finding, wt_finding])
        assert [f.fingerprint for f in resurfaced] == [
            main_finding.fingerprint,
            wt_finding.fingerprint,
        ]

    @pytest.mark.parametrize(
        ("corrected_status", "corrected_reason"),
        [
            ("confirmed", "actually a real bug"),
            ("dismissed", "stale"),
        ],
    )
    def test_reclassifying_original_fp_unsuppresses_dup_copy_after_dedup_expiry(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        corrected_status: str,
        corrected_reason: str,
    ):
        """If an operator first dismisses a finding as fp (propagating a dup
        copy with duplicate_of_reason=='fp') and later reclassifies the
        original (--resolve or --dismiss --reason stale), the original's live
        verdict takes precedence over the stale snapshot on the copy."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="fp")
            == 0
        )
        # Operator corrects the original verdict
        assert (
            update_finding_status(
                main_finding.fingerprint,
                corrected_status,
                reason=corrected_reason,
            )
            == 0
        )

        # After dedup TTL expiry, the copy resurfaces just like its target
        (isolated_findings_state / "dedup.json").write_text("{}")
        resurfaced = persist_findings([wt_finding])
        assert [f.fingerprint for f in resurfaced] == [wt_finding.fingerprint]

    @pytest.mark.parametrize(
        ("corrected_status", "corrected_reason", "expected_dup_reason"),
        [
            ("confirmed", "fixed", None),
            ("dismissed", "stale", "stale"),
        ],
    )
    def test_reclassifying_original_syncs_duplicate_of_reason_before_compaction(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        corrected_status: str,
        corrected_reason: str | None,
        expected_dup_reason: str | None,
    ):
        """Reclassifying the original finding must synchronize duplicate_of_reason
        on existing dup copies so compacting the original cannot revive a stale
        'fp' snapshot."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="fp")
            == 0
        )
        assert (
            update_finding_status(
                main_finding.fingerprint,
                corrected_status,
                reason=corrected_reason,
            )
            == 0
        )

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        wt_row = by_fp[wt_finding.fingerprint]
        assert wt_row.get("duplicate_of_reason") == expected_dup_reason

        # Compact the original away and expire dedup -> copy must NOT stay suppressed
        _write_rows(isolated_findings_state, [wt_row])
        (isolated_findings_state / "dedup.json").write_text("{}")
        resurfaced = persist_findings([wt_finding])
        assert [f.fingerprint for f in resurfaced] == [wt_finding.fingerprint]

    def test_redismissing_fp_without_reason_preserves_duplicate_of_reason_on_copies(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Re-dismissing an existing fp original with reason=None preserves
        resolution_reason='fp' on the target and must preserve
        duplicate_of_reason='fp' on its sibling copies (regardless of row order)."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        # Put sibling copy BEFORE target in findings.jsonl to verify order independence
        assert len(persist_findings([wt_finding, main_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="fp")
            == 0
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason=None)
            == 0
        )

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[main_finding.fingerprint]["resolution_reason"] == "fp"
        wt_row = by_fp[wt_finding.fingerprint]
        assert wt_row["duplicate_of"] == main_finding.fingerprint
        assert wt_row["duplicate_of_reason"] == "fp"

        # Compact original away and expire dedup -> copy stays suppressed as fp
        _write_rows(isolated_findings_state, [wt_row])
        (isolated_findings_state / "dedup.json").write_text("{}")
        assert persist_findings([wt_finding]) == []

    def test_resurfaced_copy_can_be_resolved_without_ambiguity(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """After dedup expiry resurfaces a non-fp copy as a new open row sharing
        the same fingerprint as the historical dismissed/dup row, resolving by
        fingerprint or unique prefix succeeds and updates only the live row."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a")
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="stale")
            == 0
        )

        # Dedup expires -> wt_finding resurfaces as a second open row
        (isolated_findings_state / "dedup.json").write_text("{}")
        assert [f.fingerprint for f in persist_findings([wt_finding])] == [
            wt_finding.fingerprint
        ]
        rows_before = _read_rows(isolated_findings_state)
        wt_rows_before = [
            r for r in rows_before if r["fingerprint"] == wt_finding.fingerprint
        ]
        assert len(wt_rows_before) == 2

        # Resolving by 8-char prefix must succeed (not fail as ambiguous)
        rc = update_finding_status(
            wt_finding.fingerprint[:8],
            "confirmed",
            resolver_agent_id="resolver-uuid-3",
            reason="fixed now",
        )
        assert rc == 0

        wt_rows_after = [
            r
            for r in _read_rows(isolated_findings_state)
            if r["fingerprint"] == wt_finding.fingerprint
        ]
        assert len(wt_rows_after) == 2
        # Historical dismissed/dup row preserved, live resurfaced row confirmed
        assert wt_rows_after[0]["status"] == "dismissed"
        assert wt_rows_after[0]["duplicate_of"] == main_finding.fingerprint
        assert wt_rows_after[1]["status"] == "confirmed"
        assert wt_rows_after[1]["resolution_reason"] == "fixed now"
        assert "duplicate_of" not in wt_rows_after[1]

        summary = _watcher_summary_from_rows(
            wt_rows_after, now=datetime.now(timezone.utc), window_days=3
        )
        assert summary["patterns"][0]["confirmed"] == 1
        assert summary["patterns"][0]["dismissed"] == 0

    def test_closed_occurrence_in_source_or_sibling_worktree_blocks_propagation(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A closed (confirmed/dismissed) occurrence in the same worktree/file
        must count toward multi-occurrence ambiguity just like an open one."""
        trees = _repo_with_worktrees(tmp_path, "feat-a", "feat-b")
        _write_rows(
            isolated_findings_state,
            [
                # Source worktree has a previously confirmed line 3 AND open line 40
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3, status="confirmed"),
                _make_row("aaaa000000000002", trees["main"] / "pkg/mod.py", line=40, status="open"),
                # feat-a has a single open copy -> must NOT be closed (source ambiguous)
                _make_row("bbbb000000000003", trees["feat-a"] / "pkg/mod.py", line=12, status="open"),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert update_finding_status("aaaa000000000002", "dismissed", reason="fp") == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["bbbb000000000003"]["status"] == "open"

        # Sibling side: source has 1 row, feat-b has closed line 3 + open line 40
        _write_rows(
            isolated_findings_state,
            [
                _make_row("cccc000000000001", trees["main"] / "pkg/mod.py", line=3, status="open"),
                _make_row("dddd000000000002", trees["feat-b"] / "pkg/mod.py", line=3, status="confirmed"),
                _make_row("dddd000000000003", trees["feat-b"] / "pkg/mod.py", line=40, status="open"),
            ],
        )
        assert update_finding_status("cccc000000000001", "dismissed", reason="fp") == 0
        by_fp2 = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp2["dddd000000000003"]["status"] == "open"

    def test_explicit_verdict_on_propagated_copy_clears_dup_metadata_and_counts(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Directly resolving or dismissing a previously propagated dup copy
        clears duplicate_of/duplicate_of_reason (and stale resolution_reason='dup'
        when no reason is passed) so calibration and summary count it cleanly."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a")
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="fp")
            == 0
        )

        # Operator directly confirms the propagated copy with no explicit reason
        assert update_finding_status(wt_finding.fingerprint, "confirmed") == 0

        rows = _read_rows(isolated_findings_state)
        by_fp = {r["fingerprint"]: r for r in rows}
        wt_row = by_fp[wt_finding.fingerprint]
        assert wt_row["status"] == "confirmed"
        assert "duplicate_of" not in wt_row
        assert "duplicate_of_reason" not in wt_row
        assert "resolution_reason" not in wt_row
        assert "dismissed_at" not in wt_row

        now = datetime.now(timezone.utc)
        stats = precision_by_pattern_and_class(rows, now=now, min_weighted_n=0.5)
        assert stats[("P006", "app")].weighted_confirmed == pytest.approx(1.0, rel=0.05)
        assert stats[("P006", "app")].weighted_dismissed == pytest.approx(1.0, rel=0.05)

        summary = _watcher_summary_from_rows(rows, now=now, window_days=3)
        assert summary["patterns"][0]["confirmed"] == 1
        assert summary["patterns"][0]["dismissed"] == 1
        assert summary["patterns"][0]["dismissed_fp"] == 1
        by_day = {d["day"]: d for d in summary["timeline"]}
        today = now.date().isoformat()
        assert by_day[today]["confirmed"] == 1
        assert by_day[today]["dismissed"] == 1

    @pytest.mark.parametrize("new_status", ["confirmed", "dismissed"])
    def test_multiple_active_rows_with_same_fingerprint_all_close_and_count_once(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        new_status: str,
    ):
        """If the same fingerprint has >1 active (open/surfaced) row (e.g.
        rescanned after 14d dedup TTL before 21d aging), resolving or
        dismissing it closes ALL active rows for that fingerprint while
        counting the verdict only once in calibration and summary."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path)
        fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    fp,
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="surfaced",
                    detected_at="2026-11-05T00:00:00Z",
                ),
                _make_row(
                    fp,
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="open",
                    detected_at="2026-11-20T00:00:00Z",
                ),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        reason = "fp" if new_status == "dismissed" else "fixed"
        assert (
            update_finding_status(
                fp,
                new_status,
                resolver_agent_id="resolver-uuid-4",
                reason=reason,
            )
            == 0
        )

        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 2
        assert all(r["status"] == new_status for r in rows)
        # Older active row closed as dup pointing to target_fp; latest row is primary
        assert rows[0]["resolution_reason"] == "dup"
        assert rows[0]["duplicate_of"] == fp
        assert rows[1]["resolution_reason"] == reason
        assert "duplicate_of" not in rows[1]

        now = datetime.now(timezone.utc)
        stats = precision_by_pattern_and_class(rows, now=now, min_weighted_n=0.5)
        summary = _watcher_summary_from_rows(rows, now=now, window_days=3)
        by_day = {d["day"]: d for d in summary["timeline"]}
        today = now.date().isoformat()
        if new_status == "confirmed":
            assert stats[("P006", "app")].weighted_confirmed == pytest.approx(1.0, rel=0.05)
            assert summary["patterns"][0]["confirmed"] == 1
            assert by_day[today]["confirmed"] == 1
        else:
            assert stats[("P006", "app")].weighted_dismissed == pytest.approx(1.0, rel=0.05)
            assert summary["patterns"][0]["dismissed"] == 1
            assert summary["patterns"][0]["dismissed_fp"] == 1
            assert by_day[today]["dismissed"] == 1
        assert summary["patterns"][0]["surfaced"] == 0

    def test_propagating_to_sibling_with_multiple_active_rows_closes_all_of_them(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        """If a sibling worktree has two active rows sharing the same sibling_fp
        (e.g. surfaced + open after a 14d dedup expiry), propagating a verdict
        must close ALL active rows for sibling_fp so 0 active rows remain."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a")
        target_fp = "aaaa000000000001"
        sibling_fp = "bbbb000000000002"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(target_fp, trees["main"] / "pkg/mod.py", line=3, status="open"),
                _make_row(
                    sibling_fp,
                    trees["feat-a"] / "pkg/mod.py",
                    line=15,
                    status="surfaced",
                    detected_at="2026-11-05T00:00:00Z",
                ),
                _make_row(
                    sibling_fp,
                    trees["feat-a"] / "pkg/mod.py",
                    line=15,
                    status="open",
                    detected_at="2026-11-20T00:00:00Z",
                ),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert (
            update_finding_status(
                target_fp, "dismissed", resolver_agent_id="resolver-uuid-5", reason="fp"
            )
            == 0
        )

        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 3
        active = [r for r in rows if r["status"] in ("open", "surfaced")]
        assert active == []

        sib_rows = [r for r in rows if r["fingerprint"] == sibling_fp]
        assert len(sib_rows) == 2
        for sib in sib_rows:
            assert sib["status"] == "dismissed"
            assert sib["resolution_reason"] == "dup"
            assert sib["duplicate_of"] == target_fp
            assert sib["duplicate_of_reason"] == "fp"
            assert sib["resolved_by"] == "resolver-uuid-5"

        assert "1 worktree copy" in capsys.readouterr().out

        now = datetime.now(timezone.utc)
        stats = precision_by_pattern_and_class(rows, now=now, min_weighted_n=0.5)
        assert stats[("P006", "app")].weighted_dismissed == pytest.approx(1.0, rel=0.05)
        summary = _watcher_summary_from_rows(rows, now=now, window_days=3)
        assert summary["patterns"][0]["dismissed"] == 1
        assert summary["patterns"][0]["dismissed_fp"] == 1
        assert summary["patterns"][0]["surfaced"] == 0

    @pytest.mark.parametrize("legacy_rule_version", [0, None])
    def test_obsolete_rule_version_fp_does_not_carry_over_or_suppress_current_revision(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        legacy_rule_version: int | None,
    ):
        """A P006 fp dismissal from rev 0 (or missing rule_version) must neither
        carry over across worktrees nor suppress a rev-1 P006 finding after
        dedup expiry."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        rev1_same_wt = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        assert rev1_same_wt.rule_version == 1

        legacy_row = _make_row(
            rev1_same_wt.fingerprint,
            trees["main"] / "pkg/mod.py",
            line=3,
            status="dismissed",
            resolution_reason="fp",
        )
        if legacy_rule_version is None:
            legacy_row.pop("rule_version", None)
        else:
            legacy_row["rule_version"] = legacy_rule_version
        _write_rows(isolated_findings_state, [legacy_row])

        # Cross-worktree rev-1 P006 finding must NOT be auto-dismissed
        rev1_other_wt = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert rev1_other_wt.rule_version == 1
        fresh_other = persist_findings([rev1_other_wt])
        assert [f.fingerprint for f in fresh_other] == [rev1_other_wt.fingerprint]

        # Same-fingerprint rev-1 P006 finding after dedup expiry must NOT be suppressed
        (isolated_findings_state / "dedup.json").write_text("{}")
        fresh_same = persist_findings([rev1_same_wt])
        assert [f.fingerprint for f in fresh_same] == [rev1_same_wt.fingerprint]

    @pytest.mark.parametrize("legacy_rule_version", [0, None])
    def test_update_finding_status_on_obsolete_rule_version_does_not_close_current_sibling(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy_rule_version: int | None,
    ):
        """Dismissing an obsolete rev-0 (or missing rule_version) P006 finding
        must NOT close a rev-1 P006 sibling copy in another worktree."""
        trees = _repo_with_worktrees(tmp_path, "feat-a")
        rev0_row = _make_row(
            "aaaa000000000001",
            trees["main"] / "pkg/mod.py",
            line=3,
            status="open",
        )
        if legacy_rule_version is None:
            rev0_row.pop("rule_version", None)
        else:
            rev0_row["rule_version"] = legacy_rule_version

        rev1_row = _make_row(
            "bbbb000000000002",
            trees["feat-a"] / "pkg/mod.py",
            line=15,
            status="open",
            rule_version=1,
        )
        _write_rows(isolated_findings_state, [rev0_row, rev1_row])
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert update_finding_status("aaaa0000", "dismissed", reason="fp") == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == "dismissed"
        assert by_fp["bbbb000000000002"]["status"] == "open"
        assert "duplicate_of" not in by_fp["bbbb000000000002"]

    def test_persist_without_matching_fp_spawns_no_git_subprocesses(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        trees = _repo_with_worktrees(tmp_path)
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/other.py",
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        git_calls: list[Any] = []

        def _fail_git(*args: Any, **kwargs: Any):
            git_calls.append(args)
            raise AssertionError("unexpected git subprocess")

        monkeypatch.setattr(subprocess, "run", _fail_git)

        new_finding = _make_finding(trees["main"] / "pkg/mod.py", line=3)
        fresh = persist_findings([new_finding])
        assert [f.fingerprint for f in fresh] == [new_finding.fingerprint]
        assert git_calls == []

    def test_same_worktree_second_occurrence_is_not_auto_dismissed(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path)
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        same_wt_line_40 = _make_finding(trees["main"] / "pkg/mod.py", line=40)
        fresh = persist_findings([same_wt_line_40])

        assert [f.fingerprint for f in fresh] == [same_wt_line_40.fingerprint]
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[same_wt_line_40.fingerprint]["status"] == "open"
        assert "duplicate_of" not in by_fp[same_wt_line_40.fingerprint]

    def test_ambiguous_multi_occurrence_in_target_or_source_is_not_auto_dismissed(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a", "feat-b")
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    line=3,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        # Target worktree scans two identical lines in one batch -> ambiguous.
        a_line_3 = _make_finding(trees["feat-a"] / "pkg/mod.py", line=3)
        a_line_40 = _make_finding(trees["feat-a"] / "pkg/mod.py", line=40)
        fresh_a = persist_findings([a_line_3, a_line_40])
        assert [f.fingerprint for f in fresh_a] == [
            a_line_3.fingerprint,
            a_line_40.fingerprint,
        ]

        # Single occurrence in feat-b auto-dismisses, then a later second line
        # in feat-b stays open because feat-b already has an occurrence.
        b_line_3 = _make_finding(trees["feat-b"] / "pkg/mod.py", line=3)
        assert persist_findings([b_line_3]) == []
        b_line_40 = _make_finding(trees["feat-b"] / "pkg/mod.py", line=40)
        fresh_b2 = persist_findings([b_line_40])
        assert [f.fingerprint for f in fresh_b2] == [b_line_40.fingerprint]

        # Source worktree with another open occurrence -> do not auto-carry.
        trees_c = _repo_with_worktrees(tmp_path / "repo-c", "feat-c")
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "cccc000000000001",
                    trees_c["main"] / "pkg/mod.py",
                    line=3,
                    status="dismissed",
                    resolution_reason="fp",
                ),
                _make_row(
                    "cccc000000000002",
                    trees_c["main"] / "pkg/mod.py",
                    line=40,
                    status="open",
                ),
            ],
        )
        c_copy = _make_finding(trees_c["feat-c"] / "pkg/mod.py", line=12)
        fresh_c = persist_findings([c_copy])
        assert [f.fingerprint for f in fresh_c] == [c_copy.fingerprint]


# ---------------------------------------------------------------------------
# 4. Regression tests for review findings (#3343 P1/P2/P3)
# ---------------------------------------------------------------------------


class TestSameWorktreeAndCommitScanAndRoutes:
    @pytest.mark.parametrize("new_status", ["dismissed", "confirmed"])
    def test_same_worktree_identical_lines_do_not_close_each_other(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        new_status: str,
    ):
        trees = _repo_with_worktrees(tmp_path)
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3, status="open"),
                _make_row("bbbb000000000002", trees["main"] / "pkg/mod.py", line=40, status="open"),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        rc = update_finding_status(
            "aaaa0000",
            new_status,
            resolver_agent_id="resolver-uuid-1",
            reason="fp" if new_status == "dismissed" else "fixed",
        )
        assert rc == 0

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == new_status
        assert by_fp["bbbb000000000002"]["status"] == "open"
        assert "duplicate_of" not in by_fp["bbbb000000000002"]
        assert "worktree cop" not in capsys.readouterr().out

    def test_ambiguous_multi_occurrence_sibling_worktree_is_not_closed(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a", "feat-b")
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3, status="open"),
                # feat-a has two identical lines: ambiguous, do not close either
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=3, status="open"),
                _make_row("cccc000000000003", trees["feat-a"] / "pkg/mod.py", line=40, status="open"),
                # feat-b has a single copy: unambiguous, close it
                _make_row("dddd000000000004", trees["feat-b"] / "pkg/mod.py", line=12, status="open"),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert update_finding_status("aaaa0000", "dismissed", reason="fp") == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == "dismissed"
        assert by_fp["bbbb000000000002"]["status"] == "open"
        assert by_fp["cccc000000000003"]["status"] == "open"
        assert by_fp["dddd000000000004"]["status"] == "dismissed"
        assert by_fp["dddd000000000004"]["duplicate_of"] == "aaaa000000000001"
        assert "1 worktree copy" in capsys.readouterr().out

    def test_scan_commits_leaves_sibling_worktree_copies_open(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import agents.watcher.agent as watcher_agent

        trees = _repo_with_worktrees(tmp_path, "feat-a")
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=3, status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=3, status="open"),
            ],
        )
        monkeypatch.setattr(
            watcher_agent, "FINDINGS_FILE", isolated_findings_state / "findings.jsonl"
        )
        monkeypatch.setattr(watcher_agent, "_post_resolution_event", lambda *a, **kw: None)

        real_run = subprocess.run
        fake_log = f"{'deadbeef' * 5}\x00fix(mod): handle error\x00fixes aaaa000000000001\x1e"

        def _selective_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "log":
                return subprocess.CompletedProcess(cmd, 0, stdout=fake_log, stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(watcher_agent.subprocess, "run", _selective_run)

        resolved = watcher_agent.scan_commits(since="14 days ago")
        assert resolved == 1

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == "confirmed"
        assert by_fp["bbbb000000000002"]["status"] == "open"
        assert "duplicate_of" not in by_fp["bbbb000000000002"]

    def test_watcher_summary_excludes_propagated_dup_from_resolutions(self):
        from agents.watcher.routes import _watcher_summary_from_rows

        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        ts = "2026-11-21T10:00:00Z"
        rows = [
            _make_row(
                "aaaa000000000001",
                "/repo/main/pkg/mod.py",
                status="confirmed",
                detected_at=ts,
                confirmed_at=ts,
            ),
            _make_row(
                "bbbb000000000002",
                "/repo/wt-a/pkg/mod.py",
                status="confirmed",
                detected_at=ts,
                confirmed_at=ts,
                resolution_reason="dup",
                duplicate_of="aaaa000000000001",
            ),
            _make_row(
                "cccc000000000003",
                "/repo/wt-b/pkg/mod.py",
                status="confirmed",
                detected_at=ts,
                confirmed_at=ts,
                resolution_reason="dup",
                duplicate_of="aaaa000000000001",
            ),
            _make_row(
                "dddd000000000004",
                "/repo/main/pkg/other.py",
                status="dismissed",
                detected_at=ts,
                dismissed_at=ts,
                resolution_reason="fp",
            ),
            _make_row(
                "eeee000000000005",
                "/repo/wt-a/pkg/other.py",
                status="dismissed",
                detected_at=ts,
                dismissed_at=ts,
                resolution_reason="dup",
                duplicate_of="dddd000000000004",
            ),
            _make_row(
                "ffff000000000006",
                "/repo/wt-b/pkg/other.py",
                status="dismissed",
                detected_at=ts,
                dismissed_at=ts,
                resolution_reason="dup",
                duplicate_of="dddd000000000004",
            ),
            _make_row(
                "9999000000000007",
                "/repo/main/pkg/open.py",
                status="open",
                detected_at=ts,
            ),
        ]

        summary = _watcher_summary_from_rows(rows, now=now, window_days=3)
        # Raw row totals remain accurate in total / by_status
        assert summary["total"] == 7
        assert summary["by_status"] == {"confirmed": 3, "dismissed": 3, "open": 1}

        # Pattern resolution metrics count each unique verdict once
        assert len(summary["patterns"]) == 1
        pat = summary["patterns"][0]
        assert pat["pattern"] == "P006"
        assert pat["confirmed"] == 1
        assert pat["dismissed"] == 1
        assert pat["dismissed_fp"] == 1
        assert pat["surfaced"] == 1
        assert pat["other"] == 0
        assert pat["dismiss_ratio"] == pytest.approx(0.5)

        # Daily resolution timeline counts each unique verdict once
        by_day = {d["day"]: d for d in summary["timeline"]}
        assert by_day["2026-11-21"]["confirmed"] == 1
        assert by_day["2026-11-21"]["dismissed"] == 1

