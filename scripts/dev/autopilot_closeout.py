#!/usr/bin/env python3
"""Policy-driven closeout wrapper for agent autopilot workflows.

This command keeps the existing safety boundaries in ``workspace_closeout.py``
but bundles the checks agents otherwise repeat by hand:

- unresolved Watcher output
- optional test-cache execution
- workspace closeout and optional branch hygiene
- optional ship.sh plan or explicit ship execution

It does not stash, kill processes, stage files, commit, push, or create a PR
unless the caller explicitly asks for shipping via ``--ship``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import workspace_closeout as closeout_lib  # noqa: E402


DEFAULT_TIMEOUT_SEC = 600
DOC_ONLY_PREFIXES = ("docs/",)
DOC_COMMAND_PREFIXES = ("commands/",)
DOC_ONLY_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
DOC_ONLY_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CODEX_START.md",
    "README.md",
}
WATCHER_TOTAL_RE = re.compile(r"Total unresolved:\s+(\d+)")
WATCHER_OTHER_RE = re.compile(r"Plus\s+(\d+)\s+finding\(s\) in other worktrees")


@dataclass
class CommandProbe:
    name: str
    status: str
    detail: str
    command: list[str]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass
class PolicyResult:
    policy: str
    detail: str
    blockers: list[str]
    human_required: list[str]


@dataclass
class AutopilotResult:
    workspace: str
    watcher: CommandProbe
    tests: CommandProbe
    ship_plan: CommandProbe
    ship: CommandProbe
    closeout: closeout_lib.CloseoutResult
    policy: PolicyResult


def run_cmd(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> CommandProbe:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        status = "ok" if completed.returncode == 0 else "failed"
        return CommandProbe(
            name=Path(args[0]).name,
            status=status,
            detail=f"exit {completed.returncode}",
            command=args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandProbe(
            name=Path(args[0]).name,
            status="failed",
            detail=f"timed out after {timeout}s",
            command=args,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        )
    except OSError as exc:
        return CommandProbe(
            name=Path(args[0]).name,
            status="failed",
            detail=str(exc),
            command=args,
            returncode=127,
        )


def porcelain_path(entry: str) -> str:
    if len(entry) <= 3:
        return entry.strip()
    path = entry[3:].strip()
    if " -> " in path:
        return path.split(" -> ", 1)[1]
    return path


def changed_paths(state: closeout_lib.GitState) -> list[str]:
    paths = {porcelain_path(entry) for entry in state.entries}
    return sorted(path for path in paths if path)


def is_docs_only_path(path: str) -> bool:
    if path in DOC_ONLY_FILES:
        return True
    if path.startswith(DOC_ONLY_PREFIXES):
        return True
    if path.startswith(DOC_COMMAND_PREFIXES):
        return path.endswith(".md")
    return path.endswith(DOC_ONLY_SUFFIXES)


def test_command_for_policy(
    policy: str,
    state: closeout_lib.GitState,
) -> list[str] | None:
    if policy == "skip":
        return None
    if policy == "always":
        return ["./scripts/dev/test-cache.sh"]
    if policy == "fresh":
        return ["./scripts/dev/test-cache.sh", "--fresh"]
    if policy == "staged":
        return ["./scripts/dev/test-cache.sh", "--staged"]

    paths = changed_paths(state)
    if not paths:
        return None
    if all(is_docs_only_path(path) for path in paths):
        return None
    return ["./scripts/dev/test-cache.sh"]


def skipped_probe(name: str, detail: str) -> CommandProbe:
    return CommandProbe(name=name, status="skipped", detail=detail, command=[])


def run_watcher(root: Path, mode: str) -> CommandProbe:
    if mode == "skip":
        return skipped_probe("watcher", "disabled by --watcher-mode skip")

    script = root / "agents" / "watcher" / "agent.py"
    if not script.exists():
        return skipped_probe("watcher", "agents/watcher/agent.py not present")

    flag = "--surface-pending" if mode == "surface" else "--print-unresolved"
    probe = run_cmd([sys.executable, str(script), flag], root, timeout=60)
    probe.name = "watcher"
    total_unresolved, other_worktree = parse_watcher_counts(probe.stdout)
    if probe.returncode == 0:
        if total_unresolved is None:
            unresolved_lines = [
                line for line in probe.stdout.splitlines() if line.strip()
            ]
            total_unresolved = len(unresolved_lines)
        if total_unresolved:
            probe.status = "attention"
            verb = "surfaced" if mode == "surface" else "reported"
            probe.detail = f"{verb} {total_unresolved} unresolved finding(s)"
        else:
            probe.status = "ok"
            probe.detail = "no current-worktree unresolved findings"
        if other_worktree:
            probe.detail += f"; {other_worktree} other-worktree finding(s) listed"
    return probe


def parse_watcher_counts(stdout: str) -> tuple[int | None, int]:
    total: int | None = None
    other_worktree = 0
    for line in stdout.splitlines():
        total_match = WATCHER_TOTAL_RE.search(line)
        if total_match:
            total = int(total_match.group(1))
        other_match = WATCHER_OTHER_RE.search(line)
        if other_match:
            other_worktree = int(other_match.group(1))
    return total, other_worktree


def run_tests(root: Path, policy: str, state: closeout_lib.GitState) -> CommandProbe:
    command = test_command_for_policy(policy, state)
    if command is None:
        detail = "disabled by --test-policy skip"
        if policy == "auto":
            paths = changed_paths(state)
            detail = "no changed paths" if not paths else "docs-only change set"
        return skipped_probe("test-cache", detail)

    probe = run_cmd(command, root, timeout=1800)
    probe.name = "test-cache"
    return probe


def run_ship_plan(root: Path, message: str | None, *, stage_all: bool) -> CommandProbe:
    if not message:
        return skipped_probe("ship-plan", "no --ship-plan message")
    command = ["./scripts/dev/ship.sh"]
    if stage_all:
        command.append("--stage-all")
    command.extend(["--plan", message])
    probe = run_cmd(command, root, timeout=60)
    probe.name = "ship-plan"
    return probe


def run_ship(root: Path, message: str | None, *, stage_all: bool) -> CommandProbe:
    if not message:
        return skipped_probe("ship", "no --ship message")
    command = ["./scripts/dev/ship.sh"]
    if stage_all:
        command.append("--stage-all")
    command.append(message)
    probe = run_cmd(command, root, timeout=1800)
    probe.name = "ship"
    return probe


def blocked_ship_probe(message: str, blockers: list[str]) -> CommandProbe:
    return CommandProbe(
        name="ship",
        status="failed",
        detail="not run because preflight failed: " + "; ".join(blockers),
        command=["./scripts/dev/ship.sh", message],
        returncode=1,
    )


def ship_preflight_blockers(
    *,
    watcher: CommandProbe,
    tests: CommandProbe,
    closeout: closeout_lib.CloseoutResult,
) -> list[str]:
    blockers: list[str] = []
    if watcher.status == "failed":
        blockers.append(f"Watcher failed ({watcher.detail})")
    elif watcher.status == "attention":
        blockers.append(f"Watcher has current-worktree findings ({watcher.detail})")
    if tests.status == "failed":
        blockers.append(f"tests failed ({tests.detail})")
    blockers.extend(closeout.errors)
    return blockers


def policy_from_result(
    *,
    closeout: closeout_lib.CloseoutResult,
    watcher: CommandProbe,
    tests: CommandProbe,
    ship_plan: CommandProbe,
    ship: CommandProbe,
    ship_requested: bool,
) -> PolicyResult:
    blockers: list[str] = []
    human_required: list[str] = []

    for probe in (watcher, tests, ship_plan, ship):
        if probe.status == "failed":
            blockers.append(f"{probe.name} failed: {probe.detail}")

    blockers.extend(closeout.errors)

    if watcher.status == "attention":
        human_required.append(watcher.detail)

    delivery = closeout.git.delivery_status
    if delivery == "local_changes" and not ship_requested:
        human_required.append("local changes are not committed, pushed, or merged")
    elif delivery in {
        "unpushed_commits",
        "diverged",
        "detached",
        "no_upstream",
        "behind_upstream",
        "unknown",
    }:
        human_required.append(f"delivery state needs review: {delivery}")

    if closeout.repo_processes:
        human_required.append(
            f"{len(closeout.repo_processes)} repo-rooted process(es) remain"
        )

    hygiene = closeout.branch_hygiene
    if hygiene is not None:
        if hygiene.errors:
            blockers.extend(hygiene.errors)
        if hygiene.holds_count:
            human_required.append(
                f"{hygiene.holds_count} branch hygiene hold(s) need review"
            )
        if hygiene.dry_run and (
            hygiene.branches_prunable
            or hygiene.worktrees_removable
            or hygiene.origin_orphans_deletable
        ):
            human_required.append("branch hygiene has dry-run cleanup candidates")

    if blockers:
        return PolicyResult(
            policy="blocked",
            detail="automation hit a failing check",
            blockers=blockers,
            human_required=human_required,
        )
    if human_required:
        return PolicyResult(
            policy="needs_human",
            detail="automation finished; judgment or explicit delivery remains",
            blockers=[],
            human_required=human_required,
        )
    return PolicyResult(
        policy="proceed",
        detail="automation finished with no attention items",
        blockers=[],
        human_required=[],
    )


def output_excerpt(text: str, *, max_lines: int = 12) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return lines
    head = max_lines // 2
    tail = max_lines - head
    return [*lines[:head], "...", *lines[-tail:]]


def render_probe(probe: CommandProbe) -> list[str]:
    command = " ".join(probe.command) if probe.command else "(none)"
    lines = [f"{probe.name}: {probe.status} - {probe.detail}", f"  cmd: {command}"]
    combined = "\n".join(part for part in (probe.stdout, probe.stderr) if part)
    for line in output_excerpt(combined):
        lines.append(f"  {line}")
    return lines


def render_text(result: AutopilotResult) -> str:
    lines = [
        f"Autopilot closeout: {result.workspace}",
        f"policy: {result.policy.policy} - {result.policy.detail}",
    ]

    if result.policy.blockers:
        lines.append("blockers:")
        for item in result.policy.blockers:
            lines.append(f"  {item}")

    if result.policy.human_required:
        lines.append("human-required:")
        for item in result.policy.human_required:
            lines.append(f"  {item}")

    lines.append("actions:")
    for probe in (result.watcher, result.tests, result.ship_plan, result.ship):
        lines.extend(f"  {line}" for line in render_probe(probe))

    lines.append("")
    lines.append(closeout_lib.render_text(result.closeout))
    return "\n".join(lines)


def to_jsonable(result: AutopilotResult) -> dict[str, Any]:
    return {
        "workspace": result.workspace,
        "watcher": asdict(result.watcher),
        "tests": asdict(result.tests),
        "ship_plan": asdict(result.ship_plan),
        "ship": asdict(result.ship),
        "closeout": closeout_lib.to_jsonable(result.closeout),
        "policy": asdict(result.policy),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="workspace directory")
    parser.add_argument(
        "--watcher-mode",
        choices=("print", "surface", "skip"),
        default="print",
        help="Watcher action to run before closeout",
    )
    parser.add_argument(
        "--test-policy",
        choices=("auto", "skip", "always", "staged", "fresh"),
        default="auto",
        help="test-cache policy; auto skips docs-only changes",
    )
    parser.add_argument(
        "--branch-hygiene",
        action="store_true",
        help="include dry-run branch/worktree hygiene findings",
    )
    parser.add_argument(
        "--ship-plan",
        metavar="MESSAGE",
        help="preview the ship.sh route for this commit message",
    )
    parser.add_argument(
        "--ship",
        metavar="MESSAGE",
        help="explicitly run ship.sh after diagnostics/tests",
    )
    parser.add_argument(
        "--stage-all",
        action="store_true",
        help="pass --stage-all to ship.sh plan or execution",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def run_autopilot(args: argparse.Namespace) -> AutopilotResult:
    root = closeout_lib.repo_root(Path(args.cwd).resolve())

    watcher = run_watcher(root, args.watcher_mode)
    initial_closeout = closeout_lib.closeout(
        root,
        use_baseline=True,
        branch_hygiene=args.branch_hygiene,
    )
    tests = run_tests(root, args.test_policy, initial_closeout.git)
    ship_plan = run_ship_plan(root, args.ship_plan, stage_all=args.stage_all)
    preflight_blockers = ship_preflight_blockers(
        watcher=watcher,
        tests=tests,
        closeout=initial_closeout,
    )
    if args.ship and preflight_blockers:
        ship = blocked_ship_probe(args.ship, preflight_blockers)
    else:
        ship = run_ship(root, args.ship, stage_all=args.stage_all)

    final_closeout = initial_closeout
    if ship.status != "skipped":
        final_closeout = closeout_lib.closeout(
            root,
            use_baseline=True,
            branch_hygiene=args.branch_hygiene,
        )

    policy = policy_from_result(
        closeout=final_closeout,
        watcher=watcher,
        tests=tests,
        ship_plan=ship_plan,
        ship=ship,
        ship_requested=bool(args.ship),
    )
    return AutopilotResult(
        workspace=str(root),
        watcher=watcher,
        tests=tests,
        ship_plan=ship_plan,
        ship=ship,
        closeout=final_closeout,
        policy=policy,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run_autopilot(args)
    except RuntimeError as exc:
        print(f"autopilot-closeout: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(to_jsonable(result), indent=2, sort_keys=True))
    else:
        print(render_text(result))

    return 0 if result.policy.policy == "proceed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
