"""Tests for cross-worktree Watcher verdict propagation (#3340).

Covers:
1. ``_copy_group_key`` extraction and delegation from ``_group_copies``.
2. ``update_finding_status`` propagation of false-positive
   (``status=="dismissed"`` AND ``effective_reason=="fp"``) verdicts to
   open/surfaced sibling copies in other worktrees as ``dup`` with
   ``duplicate_of`` and ``duplicate_of_reason="fp"`` when they share the same
   ``rule_version`` and identical unique ``±5``-line source context
   (``context_hash``). Non-``fp`` dismissals (``stale``, ``wont_fix``,
   ``out_of_scope``, ``unclear``, ``None``) and ``confirmed`` verdicts stay
   branch-local.
3. ``persist_findings`` stamping ``context_hash`` and carrying forward
   current-revision ``fp`` dismissals to brand-new copies in other worktrees
   (including after the source worktree is pruned) as ``dismissed``/``dup``
   rows omitted from ``fresh`` and event emission.
4. Multi-line context identity and source-file uniqueness failing closed on
   different surrounding contexts or duplicate identical ``±5``-line blocks
   even under partial scans.
5. ``rule_version`` compatibility, expired-dedup handling, duplicate active
   same-fingerprint row closure, explicit override metadata clearing,
   ``calibration.precision_by_pattern_and_class``, and
   ``routes._watcher_summary_from_rows``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agents.watcher import findings as watcher_findings
from agents.watcher._util import hash_line_content
from agents.watcher.calibration import (
    PRECISION_REASONS_TRUE_NEGATIVE,
    precision_by_pattern_and_class,
)
from agents.watcher.findings import (
    Finding,
    _copy_group_key,
    _extract_unique_context_hash,
    _group_copies,
    persist_findings,
    update_finding_status,
)

_DEFAULT_LINE = "    except Exception:"
_DEFAULT_LINE_HASH = hash_line_content(_DEFAULT_LINE)
_DEFAULT_TARGET_LINE = 10


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


def _block_lines(tag: str = "alpha") -> list[str]:
    """11-line block with ``_DEFAULT_LINE`` at offset 5 (exact ±5-line window)."""
    return [
        f"def handle_{tag}(item):",
        f"    prefix_{tag} = prepare(item)",
        f"    logger.debug('{tag}:start')",
        "    try:",
        f"        return run_{tag}(prefix_{tag})",
        _DEFAULT_LINE,
        f"        log_{tag}_failure(prefix_{tag})",
        f"        metrics.inc('{tag}')",
        f"        cleanup_{tag}()",
        f"        return fallback_{tag}()",
        f"    # end {tag}",
    ]


def _write_source_blocks(path: Path, blocks: list[tuple[int, str]]) -> None:
    """Write ``path`` so each ``(target_line, tag)`` places ``_DEFAULT_LINE`` at
    exact 1-based ``target_line`` inside a ±5-line ``_block_lines(tag)`` window.
    """
    lines: list[str] = []
    for target_line, tag in sorted(blocks):
        start_idx = target_line - 6
        assert start_idx >= len(lines), f"overlapping or too-early target_line={target_line}"
        while len(lines) < start_idx:
            lines.append(f"# filler line {len(lines) + 1}")
        lines.extend(_block_lines(tag))
    lines.append("# trailing eof")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _repo_with_worktrees(
    root: Path,
    *names: str,
    line_by_tree: dict[str, int] | None = None,
) -> dict[str, Path]:
    """Create a git repo at ``root/main`` plus linked worktrees ``root/wt/<name>``,
    each holding multi-line ``pkg/mod.py`` and ``pkg/other.py`` matching
    ``_DEFAULT_LINE_HASH`` at the configured line (default 10).
    """
    main = root / "main"
    main.mkdir(parents=True)
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "--no-verify", "-m", "init", cwd=main)
    trees: dict[str, Path] = {"main": main}
    for name in names:
        wt_path = root / "wt" / name
        _git("worktree", "add", "-q", "-b", name, str(wt_path), cwd=main)
        trees[name] = wt_path
    lines_map = line_by_tree or {}
    for name, tree in trees.items():
        target_line = lines_map.get(name, _DEFAULT_TARGET_LINE)
        _write_source_blocks(tree / "pkg/mod.py", [(target_line, "alpha")])
        _write_source_blocks(tree / "pkg/other.py", [(target_line, "other")])
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
    line: int = _DEFAULT_TARGET_LINE,
    severity: str = "medium",
    status: str = "open",
    line_content_hash: str = _DEFAULT_LINE_HASH,
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
        "line_content": _DEFAULT_LINE.strip(),
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
    line: int = _DEFAULT_TARGET_LINE,
    severity: str = "medium",
    line_content_hash: str = _DEFAULT_LINE_HASH,
    detected_at: str = "2026-11-21T00:00:00Z",
    context_hash: str = "",
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
        line_content=_DEFAULT_LINE.strip(),
        context_hash=context_hash,
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 19})
        cache: dict[str, Any] = {}
        row_main = _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10)
        finding_wt = _make_finding(trees["feat-a"] / "pkg/mod.py", line=19)

        key_main = _copy_group_key(row_main, cache)
        key_wt = _copy_group_key(finding_wt, cache)

        assert key_main is not None
        assert key_main == key_wt
        assert key_main[1:] == ("pkg/mod.py", "P006", _DEFAULT_LINE_HASH, "medium")
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
        _write_source_blocks(non_git_file, [(10, "alpha")])

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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        rows = [
            _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10),
            _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=15),
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
# 2. update_finding_status cross-worktree fp propagation
# ---------------------------------------------------------------------------


class TestUpdateFindingStatusPropagation:
    def test_fp_dismiss_propagates_to_open_and_surfaced_siblings_only(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        trees = _repo_with_worktrees(
            tmp_path / "repo-a",
            "feat-a",
            "feat-b",
            "feat-c",
            line_by_tree={"main": 10, "feat-a": 15, "feat-b": 42, "feat-c": 10},
        )
        other_repo = _repo_with_worktrees(tmp_path / "repo-b")

        rows = [
            _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
            _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=15, status="surfaced"),
            _make_row("cccc000000000003", trees["feat-b"] / "pkg/mod.py", line=42, status="open"),
            # Must NOT be touched: already aged_out
            _make_row("dddd000000000004", trees["feat-c"] / "pkg/mod.py", line=10, status="aged_out"),
            # Must NOT be touched: different severity
            _make_row("eeee000000000005", trees["feat-a"] / "pkg/mod.py", line=15, severity="high"),
            # Must NOT be touched: different content hash
            _make_row(
                "ffff000000000006",
                trees["feat-a"] / "pkg/mod.py",
                line=15,
                line_content_hash="deadbeef0000",
            ),
            # Must NOT be touched: different git repository
            _make_row("1111000000000007", other_repo["main"] / "pkg/mod.py", line=10, status="open"),
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
        assert target["context_hash"]

        for sib_fp in ("bbbb000000000002", "cccc000000000003"):
            sib = by_fp[sib_fp]
            assert sib["status"] == "dismissed"
            assert sib["dismissed_at"] == target["dismissed_at"]
            assert sib["resolved_by"] == "resolver-uuid-1"
            assert sib["resolution_reason"] == "dup"
            assert sib["duplicate_of"] == "aaaa000000000001"
            assert sib["duplicate_of_reason"] == "fp"
            assert sib["context_hash"] == target["context_hash"]

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

    @pytest.mark.parametrize(
        "non_fp_reason",
        ["stale", "wont_fix", "out_of_scope", "unclear", "dup", "not now", None],
    )
    def test_non_fp_dismissal_stays_branch_local_and_leaves_siblings_open(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        non_fp_reason: str | None,
    ):
        """Only dismissed + effective_reason=='fp' may propagate across worktrees.
        All non-fp dismissals stay branch-local."""
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=15, status="open"),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert (
            update_finding_status("aaaa0000", "dismissed", reason=non_fp_reason) == 0
        )
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["aaaa000000000001"]["status"] == "dismissed"
        assert by_fp["bbbb000000000002"]["status"] == "open"
        assert "duplicate_of" not in by_fp["bbbb000000000002"]
        assert "worktree cop" not in capsys.readouterr().out

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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=15, status="surfaced"),
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
            _write_source_blocks(path, [(10, "alpha")])

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
# 3. persist_findings fp carry-over and multi-line context identity
# ---------------------------------------------------------------------------


class TestPersistFindingsFpCarryOver:
    def test_carries_forward_fp_dismissal_to_brand_new_worktree_copy(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    line=10,
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
        assert appended["duplicate_of_reason"] == "fp"
        assert appended["context_hash"]

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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    line=10,
                    status=prior_status,
                    resolution_reason=prior_reason,
                )
            ],
        )

        new_copy = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        other_repo = _repo_with_worktrees(tmp_path / "other-repo")
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    line=10,
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

        matching_copy = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15, severity="high")
        other_repo_copy = _make_finding(
            other_repo["main"] / "pkg/mod.py", line=10, severity="high"
        )
        r000_finding = _make_finding(
            trees["feat-a"] / "pkg/mod.py",
            line=16,
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

    def test_expired_dedup_for_same_fp_row_with_matching_context_does_not_duplicate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path)
        mod_path = trees["main"] / "pkg/mod.py"
        same_finding = _make_finding(mod_path, line=10)
        ctx = _extract_unique_context_hash(str(mod_path), 10, _DEFAULT_LINE_HASH, {})
        assert ctx is not None
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    same_finding.fingerprint,
                    mod_path,
                    line=10,
                    status="dismissed",
                    resolution_reason="fp",
                    context_hash=ctx,
                )
            ],
        )

        fresh = persist_findings([same_finding])
        assert fresh == []
        rows = _read_rows(isolated_findings_state)
        assert len(rows) == 1
        assert rows[0]["resolution_reason"] == "fp"
        assert "duplicate_of" not in rows[0]

    def test_expired_dedup_without_context_hash_or_with_changed_context_is_not_suppressed(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        """After dedup expires, a dismissed/fp row lacking context_hash (legacy
        row) OR whose surrounding ±5-line context changed on disk (same line +
        line number) must NOT be silently suppressed."""
        trees = _repo_with_worktrees(tmp_path)
        mod_path = trees["main"] / "pkg/mod.py"
        same_finding = _make_finding(mod_path, line=10)

        # (a) Legacy current-revision dismissed/fp row without context_hash
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    same_finding.fingerprint,
                    mod_path,
                    line=10,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )
        fresh_legacy = persist_findings([same_finding])
        assert [f.fingerprint for f in fresh_legacy] == [same_finding.fingerprint]
        rows_after_legacy = _read_rows(isolated_findings_state)
        assert len(rows_after_legacy) == 2
        assert rows_after_legacy[1]["status"] == "open"
        assert rows_after_legacy[1]["context_hash"]

        # (b) Row WITH context_hash, then surrounding ±5-line context changes on disk
        old_ctx = rows_after_legacy[1]["context_hash"]
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    same_finding.fingerprint,
                    mod_path,
                    line=10,
                    status="dismissed",
                    resolution_reason="fp",
                    context_hash=old_ctx,
                )
            ],
        )
        _write_source_blocks(mod_path, [(10, "rewritten_context")])
        (isolated_findings_state / "dedup.json").write_text("{}")

        fresh_changed = persist_findings([same_finding])
        assert [f.fingerprint for f in fresh_changed] == [same_finding.fingerprint]
        rows_after_change = _read_rows(isolated_findings_state)
        assert len(rows_after_change) == 2
        assert rows_after_change[1]["status"] == "open"
        assert rows_after_change[1]["context_hash"] != old_ctx

    def test_expired_dedup_on_propagated_dup_copy_does_not_resurface_or_duplicate(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        """After FINDINGS_TTL_DAYS expires a sibling worktree's dedup entry,
        rescanning the propagated dismissed/dup copy must refresh dedup without
        re-opening or appending a colliding duplicate-fingerprint row — whether
        or not the original fp row is still present."""
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        prior_fp = "aaaa000000000001"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    prior_fp,
                    trees["main"] / "pkg/mod.py",
                    line=10,
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
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
        """After a propagated dup copy is unsuppressed (original reclassified)
        and resurfaces as a new open row sharing the same fingerprint as the
        historical dismissed/dup row, resolving by prefix succeeds and updates
        only the live row."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
        wt_finding = _make_finding(trees["feat-a"] / "pkg/mod.py", line=15)
        assert len(persist_findings([main_finding, wt_finding])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        # First propagate as fp, then reclassify original to stale so copy can resurface
        assert (
            update_finding_status(main_finding.fingerprint, "dismissed", reason="fp")
            == 0
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
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "beta"), (40, "alpha")])
        _write_source_blocks(trees["feat-a"] / "pkg/mod.py", [(15, "alpha")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="confirmed"),
                _make_row("aaaa000000000002", trees["main"] / "pkg/mod.py", line=40, status="open"),
                _make_row("bbbb000000000003", trees["feat-a"] / "pkg/mod.py", line=15, status="open"),
            ],
        )
        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )

        assert update_finding_status("aaaa000000000002", "dismissed", reason="fp") == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp["bbbb000000000003"]["status"] == "open"

        # Sibling side: source has 1 row, feat-b has closed line 10 + open line 40
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "alpha")])
        _write_source_blocks(trees["feat-b"] / "pkg/mod.py", [(10, "beta"), (40, "alpha")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row("cccc000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
                _make_row("dddd000000000002", trees["feat-b"] / "pkg/mod.py", line=10, status="confirmed"),
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
        clears duplicate_of/duplicate_of_reason, stale resolution_reason='dup',
        and automatic dismissed_at so calibration and summary count it cleanly."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        main_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
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
                    line=10,
                    status="surfaced",
                    detected_at="2026-11-05T00:00:00Z",
                ),
                _make_row(
                    fp,
                    trees["main"] / "pkg/mod.py",
                    line=10,
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
        (e.g. surfaced + open after a 14d dedup expiry), propagating an fp
        dismissal must close ALL active rows for sibling_fp so 0 active rows remain."""
        from agents.watcher.routes import _watcher_summary_from_rows

        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        target_fp = "aaaa000000000001"
        sibling_fp = "bbbb000000000002"
        _write_rows(
            isolated_findings_state,
            [
                _make_row(target_fp, trees["main"] / "pkg/mod.py", line=10, status="open"),
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        rev1_same_wt = _make_finding(trees["main"] / "pkg/mod.py", line=10)
        assert rev1_same_wt.rule_version == 1

        legacy_row = _make_row(
            rev1_same_wt.fingerprint,
            trees["main"] / "pkg/mod.py",
            line=10,
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", line_by_tree={"feat-a": 15})
        rev0_row = _make_row(
            "aaaa000000000001",
            trees["main"] / "pkg/mod.py",
            line=10,
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
                    line=10,
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

        new_finding = _make_finding(trees["main"] / "pkg/mod.py", line=10)
        fresh = persist_findings([new_finding])
        assert [f.fingerprint for f in fresh] == [new_finding.fingerprint]
        assert git_calls == []

    def test_same_worktree_second_occurrence_is_not_auto_dismissed(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
    ):
        trees = _repo_with_worktrees(tmp_path)
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    line=10,
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
        _write_source_blocks(trees["feat-a"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_source_blocks(trees["feat-b"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "aaaa000000000001",
                    trees["main"] / "pkg/mod.py",
                    line=10,
                    status="dismissed",
                    resolution_reason="fp",
                )
            ],
        )

        # Target worktree scans two occurrences in one batch -> ambiguous.
        a_line_10 = _make_finding(trees["feat-a"] / "pkg/mod.py", line=10)
        a_line_40 = _make_finding(trees["feat-a"] / "pkg/mod.py", line=40)
        fresh_a = persist_findings([a_line_10, a_line_40])
        assert [f.fingerprint for f in fresh_a] == [
            a_line_10.fingerprint,
            a_line_40.fingerprint,
        ]

        # Single occurrence in feat-b auto-dismisses, then a later second line
        # in feat-b stays open because feat-b already has an occurrence.
        b_line_10 = _make_finding(trees["feat-b"] / "pkg/mod.py", line=10)
        assert persist_findings([b_line_10]) == []
        b_line_40 = _make_finding(trees["feat-b"] / "pkg/mod.py", line=40)
        fresh_b2 = persist_findings([b_line_40])
        assert [f.fingerprint for f in fresh_b2] == [b_line_40.fingerprint]

        # Source worktree with another open occurrence -> do not auto-carry.
        trees_c = _repo_with_worktrees(tmp_path / "repo-c", "feat-c", line_by_tree={"feat-c": 15})
        _write_source_blocks(trees_c["main"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row(
                    "cccc000000000001",
                    trees_c["main"] / "pkg/mod.py",
                    line=10,
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
        c_copy = _make_finding(trees_c["feat-c"] / "pkg/mod.py", line=15)
        fresh_c = persist_findings([c_copy])
        assert [f.fingerprint for f in fresh_c] == [c_copy.fingerprint]

    def test_identical_line_with_different_surrounding_context_does_not_propagate_or_carry_over(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Two branches with identical single line ('except Exception:') in the
        same repo-relative file but DIFFERENT ±5-line surrounding contexts must
        never propagate or auto-dismiss each other."""
        trees = _repo_with_worktrees(tmp_path, "feat-a", "feat-b")
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "alpha")])
        _write_source_blocks(trees["feat-a"] / "pkg/mod.py", [(10, "beta")])
        _write_source_blocks(trees["feat-b"] / "pkg/mod.py", [(10, "gamma")])

        main_f = _make_finding(trees["main"] / "pkg/mod.py", line=10)
        feat_a_f = _make_finding(trees["feat-a"] / "pkg/mod.py", line=10)
        assert len(persist_findings([main_f, feat_a_f])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert update_finding_status(main_f.fingerprint, "dismissed", reason="fp") == 0

        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[main_f.fingerprint]["status"] == "dismissed"
        assert by_fp[feat_a_f.fingerprint]["status"] == "open"
        assert "duplicate_of" not in by_fp[feat_a_f.fingerprint]

        # persist_findings on feat-b with different surrounding context also stays open
        feat_b_f = _make_finding(trees["feat-b"] / "pkg/mod.py", line=10)
        fresh_b = persist_findings([feat_b_f])
        assert [f.fingerprint for f in fresh_b] == [feat_b_f.fingerprint]

    def test_duplicate_identical_context_blocks_in_one_file_fail_closed_under_partial_scan(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A file containing two identical ±5-line blocks must fail closed
        (context_hash -> None) even when only one of them is present in
        findings.jsonl (partial scan)."""
        trees = _repo_with_worktrees(tmp_path, "feat-dup-target", "feat-dup-source", "feat-clean")
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "alpha")])
        # Two identical ±5-line blocks in feat-dup-target (only line 10 scanned)
        _write_source_blocks(
            trees["feat-dup-target"] / "pkg/mod.py", [(10, "alpha"), (40, "alpha")]
        )
        assert (
            _extract_unique_context_hash(
                str(trees["feat-dup-target"] / "pkg/mod.py"), 10, _DEFAULT_LINE_HASH, {}
            )
            is None
        )

        main_f = _make_finding(trees["main"] / "pkg/mod.py", line=10)
        dup_target_f = _make_finding(trees["feat-dup-target"] / "pkg/mod.py", line=10)
        assert len(persist_findings([main_f, dup_target_f])) == 2

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert update_finding_status(main_f.fingerprint, "dismissed", reason="fp") == 0
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        # Ambiguous target file stays open (not closed by propagation)
        assert by_fp[dup_target_f.fingerprint]["status"] == "open"

        # New scan of an ambiguous target file also does not auto-dismiss
        dup_target_line40 = _make_finding(trees["feat-dup-target"] / "pkg/mod.py", line=40)
        assert [f.fingerprint for f in persist_findings([dup_target_line40])] == [
            dup_target_line40.fingerprint
        ]

        # Ambiguous source file (two identical ±5-line blocks, only 1 row) fails closed too
        _write_source_blocks(
            trees["feat-dup-source"] / "pkg/mod.py", [(10, "delta"), (40, "delta")]
        )
        _write_source_blocks(trees["feat-clean"] / "pkg/mod.py", [(10, "delta")])
        dup_source_f = _make_finding(trees["feat-dup-source"] / "pkg/mod.py", line=10)
        clean_f = _make_finding(trees["feat-clean"] / "pkg/mod.py", line=10)
        assert len(persist_findings([dup_source_f, clean_f])) == 2
        assert update_finding_status(dup_source_f.fingerprint, "dismissed", reason="fp") == 0
        by_fp2 = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp2[clean_f.fingerprint]["status"] == "open"

    def test_fp_auto_carries_after_source_worktree_is_pruned(
        self,
        isolated_findings_state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Identical unique ±5-line context across worktrees propagates fp and
        continues auto-carrying to new worktrees even after the original source
        worktree is removed/pruned from disk."""
        trees = _repo_with_worktrees(
            tmp_path,
            "feat-old",
            "feat-new",
            "feat-diff",
            line_by_tree={"feat-old": 10, "feat-new": 22, "feat-diff": 10},
        )
        _write_source_blocks(trees["feat-diff"] / "pkg/mod.py", [(10, "different")])

        old_f = _make_finding(trees["feat-old"] / "pkg/mod.py", line=10)
        assert [f.fingerprint for f in persist_findings([old_f])] == [old_f.fingerprint]

        monkeypatch.setattr(
            "agents.watcher.agent._post_resolution_event", lambda *a, **kw: None
        )
        assert update_finding_status(old_f.fingerprint, "dismissed", reason="fp") == 0

        # Prune the source worktree completely from disk
        shutil.rmtree(trees["feat-old"])
        _git("worktree", "prune", cwd=trees["main"])
        assert not trees["feat-old"].exists()

        # New worktree with identical unique ±5-line context still auto-dismisses
        new_f = _make_finding(trees["feat-new"] / "pkg/mod.py", line=22)
        assert persist_findings([new_f]) == []
        by_fp = {r["fingerprint"]: r for r in _read_rows(isolated_findings_state)}
        assert by_fp[new_f.fingerprint]["status"] == "dismissed"
        assert by_fp[new_f.fingerprint]["resolution_reason"] == "dup"
        assert by_fp[new_f.fingerprint]["duplicate_of"] == old_f.fingerprint
        assert by_fp[new_f.fingerprint]["duplicate_of_reason"] == "fp"

        # Worktree with different surrounding context still stays open
        diff_f = _make_finding(trees["feat-diff"] / "pkg/mod.py", line=10)
        assert [f.fingerprint for f in persist_findings([diff_f])] == [diff_f.fingerprint]


# ---------------------------------------------------------------------------
# 4. Regression tests for same-worktree, commit-scan, and summary routes
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
        _write_source_blocks(trees["main"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
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
        trees = _repo_with_worktrees(tmp_path, "feat-a", "feat-b", line_by_tree={"feat-b": 15})
        _write_source_blocks(trees["feat-a"] / "pkg/mod.py", [(10, "alpha"), (40, "beta")])
        _write_rows(
            isolated_findings_state,
            [
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
                # feat-a has two occurrences: ambiguous, do not close either
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=10, status="open"),
                _make_row("cccc000000000003", trees["feat-a"] / "pkg/mod.py", line=40, status="open"),
                # feat-b has a single copy: unambiguous, close it
                _make_row("dddd000000000004", trees["feat-b"] / "pkg/mod.py", line=15, status="open"),
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
                _make_row("aaaa000000000001", trees["main"] / "pkg/mod.py", line=10, status="open"),
                _make_row("bbbb000000000002", trees["feat-a"] / "pkg/mod.py", line=10, status="open"),
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
        assert summary["total"] == 7
        assert summary["by_status"] == {"confirmed": 3, "dismissed": 3, "open": 1}

        assert len(summary["patterns"]) == 1
        pat = summary["patterns"][0]
        assert pat["pattern"] == "P006"
        assert pat["confirmed"] == 1
        assert pat["dismissed"] == 1
        assert pat["dismissed_fp"] == 1
        assert pat["surfaced"] == 1
        assert pat["other"] == 0
        assert pat["dismiss_ratio"] == pytest.approx(0.5)

        by_day = {d["day"]: d for d in summary["timeline"]}
        assert by_day["2026-11-21"]["confirmed"] == 1
        assert by_day["2026-11-21"]["dismissed"] == 1
