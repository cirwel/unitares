"""Tests for the read-only Ground Crew collector and renderers."""

from __future__ import annotations

from pathlib import Path

from scripts.ops.ground_crew import (
    CommandResult,
    collect_repo_state,
    parse_cron_failures,
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
