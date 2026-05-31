"""Tests for the read-only Ground Crew collector and renderers."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.ops import ground_crew
from scripts.ops.ground_crew import (
    CommandResult,
    RepoState,
    audit_claims,
    collect_repo_state,
    main,
    parse_cron_failures,
    render_evidence_audit,
    render_handoff_pack,
    render_onboard_brief,
    render_pulse,
)


def test_collect_repo_state_marks_clean_repo_and_upstream_counts() -> None:
    """Repo collection should summarize git state without mutating the repo."""
    calls: list[tuple[tuple[str, ...], Path | None]] = []

    def fake_runner(args: list[str], cwd: Path | None = None, timeout: int = 20) -> CommandResult:
        calls.append((tuple(args), cwd))
        command = tuple(args)
        if command == ("git", "branch", "--show-current"):
            return CommandResult(args, 0, "master\n", "")
        if command == ("git", "status", "--short"):
            return CommandResult(args, 0, "", "")
        if command == ("git", "log", "-1", "--oneline"):
            return CommandResult(args, 0, "abc123 test commit\n", "")
        if command == ("git", "rev-list", "--left-right", "--count", "HEAD...origin/master"):
            return CommandResult(args, 0, "0\t2\n", "")
        raise AssertionError(f"unexpected command: {command}")

    state = collect_repo_state(Path("/repo"), runner=fake_runner)

    assert state.path == Path("/repo")
    assert state.branch == "master"
    assert state.clean is True
    assert state.status_short == ""
    assert state.head == "abc123 test commit"
    assert state.upstream_counts == "0\t2"
    assert all(cwd == Path("/repo") for _args, cwd in calls)


def test_collect_repo_state_marks_status_failure_as_unknown_not_dirty() -> None:
    """Failed git status collection should not masquerade as a dirty tree."""

    def fake_runner(args: list[str], cwd: Path | None = None, timeout: int = 20) -> CommandResult:
        command = tuple(args)
        if command == ("git", "branch", "--show-current"):
            return CommandResult(args, 0, "master\n", "")
        if command == ("git", "status", "--short"):
            return CommandResult(args, 128, "", "fatal: not a git repository\n")
        if command == ("git", "log", "-1", "--oneline"):
            return CommandResult(args, 0, "abc123 test commit\n", "")
        if command == ("git", "rev-list", "--left-right", "--count", "HEAD...origin/master"):
            return CommandResult(args, 0, "0\t0\n", "")
        raise AssertionError(f"unexpected command: {command}")

    state = collect_repo_state(Path("/not-a-repo"), runner=fake_runner)
    output = render_onboard_brief(
        task="debug repo",
        repo_state=state,
        hermes_version="Hermes Agent v0.15.1",
        mcp_list="",
        cron_text="Name: job\nLast run: now ok\n",
    )

    assert state.clean is None
    assert state.status_short == ""
    assert "working tree status unknown" in output
    assert "dirty" not in output
    assert "Collection warning:" in output



def test_parse_cron_failures_extracts_named_failed_jobs() -> None:
    """Cron parsing should surface failed jobs but ignore ok jobs."""
    cron_text = """
  ea2e3655cee4 [active]
    Name:      UNITARES dogfood pulse
    Last run:  2026-05-30T04:06:15-06:00  error: RuntimeError: Codex auth is missing access_token

  5139fb8f9079 [active]
    Name:      UNITARES ablation watchdog
    Last run:  2026-05-30T07:59:16-06:00  ok
"""

    failures = parse_cron_failures(cron_text)

    assert len(failures) == 1
    assert failures[0].name == "UNITARES dogfood pulse"
    assert failures[0].error == "RuntimeError: Codex auth is missing access_token"


def test_render_pulse_is_silent_when_no_failures() -> None:
    """Quiet pulse should suppress routine green state."""
    assert render_pulse(cron_text="Name: job\nLast run: now  ok\n").strip() == "[SILENT]"


def test_render_pulse_surfaces_cron_failure_with_signal_evidence_next() -> None:
    """Quiet pulse should emit only actionable failures."""
    output = render_pulse(
        cron_text="Name: UNITARES dogfood pulse\nLast run: now error: RuntimeError: missing token\n"
    )

    assert "Signal:" in output
    assert "Evidence:" in output
    assert "Next:" in output
    assert "UNITARES dogfood pulse" in output
    assert "RuntimeError: missing token" in output
    assert "[SILENT]" not in output


def test_render_onboard_brief_marks_missing_cli_as_proposed() -> None:
    """Onboarding brief should be explicit that Ground Crew commands are not yet magic."""
    class RepoLike:
        path = Path("/repo")
        branch = "master"
        clean = True
        status_short = ""
        head = "abc123 test commit"
        upstream_counts = "0\t0"
        errors: tuple[str, ...] = ()

    output = render_onboard_brief(
        task="continue Ground Crew",
        repo_state=RepoLike(),
        hermes_version="Hermes Agent v0.15.1",
        mcp_list="unitares enabled\nanima enabled",
        cron_text="Name: job\nLast run: now ok\n",
    )

    assert "Current situation:" in output
    assert "Relevant surfaces:" in output
    assert "Fresh evidence:" in output
    assert "Stale or unverified context:" in output
    assert "No Ground Crew CLI exists yet" not in output
    assert "Ground Crew collector is available" in output
    assert "continue Ground Crew" in output
    assert "/repo" in output


def test_render_evidence_audit_separates_supported_and_unsupported_claims() -> None:
    """Evidence audit should cite direct support and name remaining gaps."""
    output = render_evidence_audit(
        claims=(
            "Ground Crew pulse returned [SILENT]",
            "PR #552 is merged",
        ),
        evidence=(
            "Command output: Ground Crew pulse returned [SILENT]",
            "pytest tests/test_ground_crew.py -q -> 6 passed",
        ),
    )

    assert "Supported claims:" in output
    assert "Unsupported claims:" in output
    assert "Evidence gaps:" in output
    assert "Risk:" in output
    assert "Repair recommendation:" in output
    assert "Ground Crew pulse returned [SILENT]" in output
    assert "PR #552 is merged" in output
    assert "Collect direct evidence for: PR #552 is merged" in output


def test_audit_claims_does_not_support_blank_or_substring_claims() -> None:
    """Direct matching should not turn blank or substring matches into evidence."""
    result = audit_claims(
        claims=("", "safe"),
        evidence=("operator note: unsafe action was blocked",),
    )

    assert result["supported"] == []
    assert [item["claim"] for item in result["unsupported"]] == ["", "safe"]


def test_render_handoff_pack_includes_repo_state_and_stop_conditions() -> None:
    """Handoff packs should be compact, sectioned, and evidence-bearing."""
    class RepoLike:
        path = Path("/repo")
        branch = "feat/ground-crew"
        clean = False
        status_short = " M scripts/ops/ground_crew.py\n?? tests/test_ground_crew.py"
        head = "abc123 add ground crew phase 2"
        upstream_counts = "1\t0"
        errors: tuple[str, ...] = ()

    output = render_handoff_pack(
        task="Ground Crew audit and handoff",
        repo_state=RepoLike(),
        cron_text="Name: UNITARES dogfood pulse\nLast run: now ok\n",
        surfaces=("file:///repo/scripts/ops/ground_crew.py",),
        tests=("pytest tests/test_ground_crew.py -q -> pending",),
    )

    assert "Task:" in output
    assert "Current state:" in output
    assert "Verified facts:" in output
    assert "Unverified assumptions:" in output
    assert "Changed files / surfaces:" in output
    assert "Lease or collision status:" in output
    assert "Relevant tests:" in output
    assert "Open questions:" in output
    assert "Stop conditions:" in output
    assert "Ground Crew audit and handoff" in output
    assert "feat/ground-crew" in output
    assert "scripts/ops/ground_crew.py" in output
    assert "No lease probe was run" in output


def test_handoff_json_includes_task_context(monkeypatch, capsys) -> None:
    """Machine-readable handoff output should preserve the task context."""
    repo_state = RepoState(
        path=Path("/repo"),
        branch="master",
        clean=True,
        status_short="",
        head="abc123 test commit",
        upstream_counts="0\t0",
    )
    monkeypatch.setattr(ground_crew, "collect_repo_state", lambda _repo: repo_state)
    monkeypatch.setattr(
        ground_crew,
        "collect_hermes_text",
        lambda: {
            "version": "Hermes Agent v0.15.1",
            "mcp_list": "unitares enabled",
            "cron_list": "Name: job\nLast run: now ok\n",
        },
    )

    assert main(["handoff", "--task", "publish Ground Crew", "--repo", "/repo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["task"] == "publish Ground Crew"
    assert payload["repo"]["branch"] == "master"
    assert payload["cron_failures"] == []
