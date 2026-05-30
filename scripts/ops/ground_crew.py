#!/usr/bin/env python3
"""Read-only Ground Crew collector and briefing renderer.

This is the first deterministic slice of Ground Crew: gather live, local evidence
and render task-scoped operator/agent briefs without writing to UNITARES, KG,
Hermes config, or repository state.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class CommandResult:
    """Captured result from one read-only command invocation."""

    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path | None, int], CommandResult]


@dataclass(frozen=True)
class RepoState:
    """Summary of a git repository's current local state."""

    path: Path
    branch: str = "unknown"
    clean: bool | None = None
    status_short: str = ""
    head: str = "unknown"
    upstream_counts: str = "unknown"
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CronFailure:
    """One failed Hermes cron job extracted from `hermes cron list` output."""

    name: str
    error: str


def run_command(args: list[str], cwd: Path | None = None, timeout: int = 20) -> CommandResult:
    """Run a read-only command and capture output without raising on failure."""
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(args=args, returncode=124, stdout="", stderr=str(exc))
    return CommandResult(
        args=args,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _strip_output(result: CommandResult) -> str:
    """Return stdout stripped of surrounding whitespace."""
    return result.stdout.strip()


def _collect_git_field(
    args: list[str],
    repo_path: Path,
    runner: Runner,
    errors: list[str],
    timeout: int = 20,
) -> str:
    """Run one git command and record an error string if it fails."""
    result = runner(args, repo_path, timeout)
    if result.returncode != 0:
        errors.append(f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
        return "unknown"
    return _strip_output(result)


def collect_repo_state(repo_path: Path, runner: Runner = run_command) -> RepoState:
    """Collect branch, cleanliness, HEAD, and upstream divergence for a repo.

    The function is read-only: it runs git inspection commands only.
    """
    errors: list[str] = []
    branch = _collect_git_field(["git", "branch", "--show-current"], repo_path, runner, errors)
    status_result = runner(["git", "status", "--short"], repo_path, 20)
    if status_result.returncode == 0:
        status_short = _strip_output(status_result)
        clean: bool | None = status_short == ""
    else:
        errors.append(
            "git status --short failed: "
            f"{status_result.stderr.strip() or status_result.stdout.strip()}"
        )
        status_short = ""
        clean = None
    head = _collect_git_field(["git", "log", "-1", "--oneline"], repo_path, runner, errors)
    upstream_counts = _collect_git_field(
        ["git", "rev-list", "--left-right", "--count", "HEAD...origin/master"],
        repo_path,
        runner,
        errors,
    )
    return RepoState(
        path=repo_path,
        branch=branch or "unknown",
        clean=clean,
        status_short=status_short,
        head=head or "unknown",
        upstream_counts=upstream_counts or "unknown",
        errors=tuple(errors),
    )


def collect_hermes_text(runner: Runner = run_command) -> dict[str, str]:
    """Collect small Hermes status surfaces through the CLI when available."""
    if shutil.which("hermes") is None:
        return {
            "version": "hermes command not found",
            "mcp_list": "hermes command not found",
            "cron_list": "hermes command not found",
        }

    version = runner(["hermes", "--version"], None, 30)
    mcp_list = runner(["hermes", "mcp", "list"], None, 60)
    cron_list = runner(["hermes", "cron", "list"], None, 60)
    return {
        "version": _strip_output(version) or version.stderr.strip(),
        "mcp_list": _strip_output(mcp_list) or mcp_list.stderr.strip(),
        "cron_list": _strip_output(cron_list) or cron_list.stderr.strip(),
    }


def parse_cron_failures(cron_text: str) -> list[CronFailure]:
    """Extract named failed jobs from `hermes cron list` text."""
    failures: list[CronFailure] = []
    current_name = "unknown job"
    for raw_line in cron_text.splitlines():
        line = raw_line.strip()
        if line.startswith("Name:"):
            current_name = line.partition("Name:")[2].strip() or "unknown job"
            continue
        if "Last run:" in line and "error:" in line:
            error = line.partition("error:")[2].strip() or "unknown error"
            failures.append(CronFailure(name=current_name, error=error))
    return failures


def _upstream_note(upstream_counts: str) -> str:
    """Render a human note from `git rev-list --left-right --count`."""
    parts = upstream_counts.split()
    if len(parts) != 2:
        return f"upstream divergence unknown ({upstream_counts})"
    ahead, behind = parts
    if ahead == "0" and behind == "0":
        return "in sync with origin/master"
    if ahead == "0":
        return f"behind origin/master by {behind} commit(s)"
    if behind == "0":
        return f"ahead of origin/master by {ahead} commit(s)"
    return f"ahead {ahead} and behind {behind} commit(s) versus origin/master"


def render_pulse(cron_text: str) -> str:
    """Render a quiet-first pulse from currently collected Hermes cron evidence."""
    failures = parse_cron_failures(cron_text)
    if not failures:
        return "[SILENT]\n"

    signal_lines = ["Signal:"]
    evidence_lines = ["Evidence:"]
    for failure in failures:
        signal_lines.append(f"- {failure.name} cron last run failed.")
        evidence_lines.append(f"- {failure.name}: {failure.error}")

    next_lines = [
        "Next:",
        "- Re-run or inspect the failing cron job under the scheduler context; ",
        "  if it still fails, refresh the relevant runtime auth/config and verify the next run.",
    ]
    return "\n".join(signal_lines + [""] + evidence_lines + [""] + next_lines) + "\n"


def render_onboard_brief(
    task: str,
    repo_state: object,
    hermes_version: str,
    mcp_list: str,
    cron_text: str,
) -> str:
    """Render a task-scoped Ground Crew onboarding brief."""
    path = getattr(repo_state, "path", "unknown")
    branch = getattr(repo_state, "branch", "unknown")
    clean = getattr(repo_state, "clean", False)
    status_short = getattr(repo_state, "status_short", "")
    head = getattr(repo_state, "head", "unknown")
    upstream_counts = getattr(repo_state, "upstream_counts", "unknown")
    errors = tuple(getattr(repo_state, "errors", ()))
    failures = parse_cron_failures(cron_text)

    cleanliness = "status unknown"
    if clean is True:
        cleanliness = "clean"
    elif clean is False:
        cleanliness = "dirty"
        if status_short:
            cleanliness = f"dirty ({len(status_short.splitlines())} status line(s))"

    lines = [
        "Current situation:",
        f"- Task: {task}",
        f"- Repo: {path}",
        f"- Branch: {branch}; working tree {cleanliness}; {_upstream_note(upstream_counts)}.",
        f"- HEAD: {head}",
        "- Ground Crew collector is available as a read-only local script; command shapes are still early and bounded.",
        "",
        "Relevant surfaces:",
        f"- Repository root: {path}",
        "- Hermes operational surfaces: version, MCP list, cron list.",
        "- Future implementation surfaces, if approved: scripts/ops/ground_crew.py and tests/test_ground_crew.py.",
        "",
        "Fresh evidence:",
        f"- Hermes version/status probe: {hermes_version.splitlines()[0] if hermes_version else 'unknown'}",
    ]

    if "unitares" in mcp_list.lower():
        lines.append("- Hermes MCP list mentions UNITARES.")
    if "anima" in mcp_list.lower():
        lines.append("- Hermes MCP list mentions Anima/Lumen.")
    if failures:
        for failure in failures:
            lines.append(f"- Cron issue: {failure.name} failed with {failure.error}")
    else:
        lines.append("- No cron failures detected in collected Hermes cron text.")
    for error in errors:
        lines.append(f"- Collection warning: {error}")

    lines.extend([
        "",
        "Stale or unverified context:",
        "- This collector does not mutate services and does not prove runtime health beyond the commands it ran.",
        "- MCP tools injected into an already-running session may stay bound to older endpoints until Hermes reload/restart.",
        "- Treat generated briefs as evidence-indexes, not authority.",
        "",
        "Known footguns:",
        "- Do not turn EISV/coherence/risk into scalar rewards.",
        "- Do not write routine transient event noise into KG.",
        "- Do not claim implementation coverage that was not backed by tests or command output.",
        "",
        "Suggested next smallest step:",
        "- Verify the specific surface you plan to touch, then run the narrowest relevant test before editing.",
    ])
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    """Build the Ground Crew command-line parser."""
    parser = argparse.ArgumentParser(description="Read-only Ground Crew collector")
    subparsers = parser.add_subparsers(dest="command", required=True)

    onboard = subparsers.add_parser("onboard", help="Render a task-scoped onboarding brief")
    onboard.add_argument("--task", required=True, help="Task to orient around")
    onboard.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository path to inspect")
    onboard.add_argument("--json", action="store_true", help="Emit collected data as JSON")

    pulse = subparsers.add_parser("pulse", help="Render quiet-first operational pulse")
    pulse.add_argument("--quiet", action="store_true", help="Accepted for command-shape parity")
    pulse.add_argument("--json", action="store_true", help="Emit parsed failures as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Ground Crew CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "onboard":
        repo_state = collect_repo_state(args.repo)
        hermes = collect_hermes_text()
        if args.json:
            print(json.dumps({"repo": asdict(repo_state), "hermes": hermes}, default=str, indent=2))
        else:
            print(
                render_onboard_brief(
                    task=args.task,
                    repo_state=repo_state,
                    hermes_version=hermes["version"],
                    mcp_list=hermes["mcp_list"],
                    cron_text=hermes["cron_list"],
                ),
                end="",
            )
        return 0

    if args.command == "pulse":
        hermes = collect_hermes_text()
        failures = parse_cron_failures(hermes["cron_list"])
        if args.json:
            print(json.dumps({"cron_failures": [asdict(failure) for failure in failures]}, indent=2))
        else:
            print(render_pulse(hermes["cron_list"]), end="")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
