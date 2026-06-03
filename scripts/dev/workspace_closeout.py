#!/usr/bin/env python3
"""Agent workspace closeout helper.

This is the pre-final-response hygiene check that agents should run when
they have modified the repo or started local services. It answers two
questions the user should not have to audit manually:

1. Is the git worktree still dirty?
2. Are any long-running processes still rooted inside this workspace?

By default the script reports only. With explicit flags it can stash dirty
work and terminate or boot out repo-rooted processes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CMD_TIMEOUT_SEC = 10
_BASELINE_RELATIVE_PATH = Path(".unitares") / "workspace-closeout-baseline.json"


@dataclass
class GitState:
    branch: str
    dirty: bool
    entries: list[str]
    staged: list[str]
    unstaged: list[str]
    untracked: list[str]


@dataclass
class ProcessInfo:
    pid: int
    ppid: int | None
    cwd: str
    command: str
    launch_label: str | None = None


@dataclass
class CloseoutResult:
    workspace: str
    git: GitState
    baseline_path: str | None
    baseline_used: bool
    stashed: bool
    stash_message: str | None
    repo_processes: list[ProcessInfo]
    stopped_processes: list[int]
    booted_out_labels: list[str]
    errors: list[str]


def _run(args: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def repo_root(cwd: Path) -> Path:
    rc, stdout, _ = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    if rc == 0 and stdout.strip():
        return Path(stdout.strip()).resolve()
    return cwd.resolve()


def parse_git_porcelain(output: str) -> GitState:
    entries: list[str] = []
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for raw_line in output.splitlines():
        if not raw_line:
            continue
        entries.append(raw_line)
        if len(raw_line) < 3:
            continue
        x_status = raw_line[0]
        y_status = raw_line[1]
        path = raw_line[3:]
        if x_status == "?" and y_status == "?":
            untracked.append(path)
            continue
        if x_status not in (" ", "?"):
            staged.append(path)
        if y_status not in (" ", "?"):
            unstaged.append(path)

    return GitState(
        branch="",
        dirty=bool(entries),
        entries=entries,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
    )


def git_state(root: Path) -> GitState:
    rc, status_stdout, status_stderr = _run(
        ["git", "status", "--porcelain=v1", "-uall"], root
    )
    if rc != 0:
        raise RuntimeError(status_stderr.strip() or "git status failed")

    state = parse_git_porcelain(status_stdout)
    rc_branch, branch_stdout, _ = _run(["git", "branch", "--show-current"], root)
    state.branch = branch_stdout.strip() if rc_branch == 0 else ""
    return state


def build_stash_message(branch: str, file_count: int, timestamp: str) -> str:
    branch_label = branch or "(detached)"
    return (
        f"workspace-closeout auto-stash [{branch_label}] "
        f"{timestamp} - {file_count} files"
    )


def stash_dirty(root: Path, state: GitState) -> str | None:
    if not state.dirty:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = build_stash_message(state.branch, len(state.entries), timestamp)
    rc, _, stderr = _run(["git", "stash", "push", "-u", "-m", message], root)
    if rc != 0:
        raise RuntimeError(stderr.strip() or "git stash failed")
    return message


def parse_ps(output: str) -> dict[int, tuple[int | None, str]]:
    processes: dict[int, tuple[int | None, str]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        try:
            ppid = int(parts[1])
        except ValueError:
            ppid = None
        command = parts[2] if len(parts) == 3 else ""
        processes[pid] = (ppid, command)
    return processes


def ps_map(root: Path) -> dict[int, tuple[int | None, str]]:
    rc, stdout, stderr = _run(["ps", "-axo", "pid=,ppid=,command="], root)
    if rc != 0:
        raise RuntimeError(stderr.strip() or "ps failed")
    return parse_ps(stdout)


def parse_lsof_field_output(output: str) -> dict[int, str]:
    cwd_by_pid: dict[int, str] = {}
    current_pid: int | None = None
    saw_cwd = False
    for line in output.splitlines():
        if not line:
            continue
        tag = line[0]
        value = line[1:]
        if tag == "p":
            try:
                current_pid = int(value)
            except ValueError:
                current_pid = None
            saw_cwd = False
        elif tag == "f":
            saw_cwd = value == "cwd"
        elif tag == "n" and current_pid is not None and saw_cwd:
            cwd_by_pid[current_pid] = value
    return cwd_by_pid


def cwd_map(root: Path) -> dict[int, str]:
    proc_dir = Path("/proc")
    if proc_dir.exists():
        result: dict[int, str] = {}
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                result[int(entry.name)] = str((entry / "cwd").resolve())
            except OSError:
                continue
        return result

    rc, stdout, _ = _run(["lsof", "-nP", "-F", "pfn", "-d", "cwd"], root)
    if rc != 0:
        return {}
    return parse_lsof_field_output(stdout)


def is_under(path: str | Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
    except OSError:
        return False


def ancestor_pids(pid: int, processes: dict[int, tuple[int | None, str]]) -> set[int]:
    ancestors = {pid}
    current = pid
    while True:
        parent = processes.get(current, (None, ""))[0]
        if parent is None or parent <= 0 or parent in ancestors:
            return ancestors
        ancestors.add(parent)
        current = parent


def parse_launchctl_print(output: str) -> dict[int, str]:
    labels: dict[int, str] = {}
    pattern = re.compile(r"^\s*\d+:\s+(\d+)\s+\S+\s+(\S+)\s*$")
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        pid = int(match.group(1))
        if pid > 0:
            labels[pid] = match.group(2)
    return labels


def launch_labels_by_pid(root: Path) -> dict[int, str]:
    if sys.platform != "darwin":
        return {}
    rc, stdout, _ = _run(["launchctl", "print", f"gui/{os.getuid()}"], root)
    if rc != 0:
        return {}
    return parse_launchctl_print(stdout)


def process_key(proc: ProcessInfo) -> str:
    if proc.launch_label:
        return f"launch_label:{proc.launch_label}"
    return f"pid:{proc.pid}|cwd:{proc.cwd}|command:{proc.command}"


def baseline_path(root: Path) -> Path:
    return root / _BASELINE_RELATIVE_PATH


def write_process_baseline(root: Path, processes: list[ProcessInfo]) -> Path:
    path = baseline_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "workspace": str(root),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "process_keys": sorted(process_key(proc) for proc in processes),
        "processes": [asdict(proc) for proc in processes],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def read_process_baseline(root: Path) -> set[str]:
    path = baseline_path(root)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    keys = payload.get("process_keys", [])
    return {str(key) for key in keys if isinstance(key, str)}


def repo_rooted_processes(
    root: Path,
    *,
    baseline_keys: set[str] | None = None,
) -> list[ProcessInfo]:
    processes = ps_map(root)
    cwd_by_pid = cwd_map(root)
    excluded = ancestor_pids(os.getpid(), processes)
    launch_labels = launch_labels_by_pid(root)
    found: list[ProcessInfo] = []

    for pid, cwd in cwd_by_pid.items():
        if pid in excluded:
            continue
        if not is_under(cwd, root):
            continue
        if pid not in processes:
            continue
        ppid, command = processes[pid]
        found.append(
            ProcessInfo(
                pid=pid,
                ppid=ppid,
                cwd=cwd,
                command=command,
                launch_label=launch_labels.get(pid),
            )
        )

    if baseline_keys:
        found = [proc for proc in found if process_key(proc) not in baseline_keys]

    return sorted(found, key=lambda item: item.pid)


def stop_repo_processes(
    root: Path,
    processes: list[ProcessInfo],
    *,
    bootout_launch_agents: bool,
) -> tuple[list[int], list[str], list[str]]:
    stopped: list[int] = []
    labels: list[str] = []
    errors: list[str] = []
    booted_pids: set[int] = set()

    if bootout_launch_agents:
        for proc in processes:
            if not proc.launch_label:
                continue
            rc, _, stderr = _run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{proc.launch_label}"],
                root,
            )
            if rc == 0:
                labels.append(proc.launch_label)
                booted_pids.add(proc.pid)
            else:
                errors.append(
                    f"launchctl bootout failed for {proc.launch_label}: "
                    f"{stderr.strip() or 'unknown error'}"
                )

    for proc in processes:
        if proc.pid in booted_pids:
            continue
        try:
            os.kill(proc.pid, signal.SIGTERM)
            stopped.append(proc.pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            errors.append(f"failed to terminate pid {proc.pid}: {exc}")

    return stopped, labels, errors


def closeout(
    root: Path,
    *,
    stash: bool = False,
    stop_processes: bool = False,
    bootout_launch_agents: bool = False,
    use_baseline: bool = True,
) -> CloseoutResult:
    errors: list[str] = []
    state = git_state(root)
    stash_message: str | None = None
    stashed = False
    baseline_keys = read_process_baseline(root) if use_baseline else set()
    baseline_file = baseline_path(root)
    baseline_used = bool(baseline_keys)

    if stash and state.dirty:
        try:
            stash_message = stash_dirty(root, state)
            stashed = stash_message is not None
            state = git_state(root)
        except RuntimeError as exc:
            errors.append(str(exc))

    processes = repo_rooted_processes(root, baseline_keys=baseline_keys)
    stopped: list[int] = []
    labels: list[str] = []
    if stop_processes and processes:
        stopped, labels, stop_errors = stop_repo_processes(
            root,
            processes,
            bootout_launch_agents=bootout_launch_agents,
        )
        errors.extend(stop_errors)
        processes = repo_rooted_processes(root, baseline_keys=baseline_keys)

    return CloseoutResult(
        workspace=str(root),
        git=state,
        baseline_path=str(baseline_file) if baseline_file.exists() else None,
        baseline_used=baseline_used,
        stashed=stashed,
        stash_message=stash_message,
        repo_processes=processes,
        stopped_processes=stopped,
        booted_out_labels=labels,
        errors=errors,
    )


def result_has_issues(result: CloseoutResult) -> bool:
    return result.git.dirty or bool(result.repo_processes) or bool(result.errors)


def render_text(result: CloseoutResult) -> str:
    lines = [f"Workspace closeout: {result.workspace}"]
    if result.baseline_path:
        status = "used" if result.baseline_used else "present but empty"
        lines.append(f"process baseline: {status} - {result.baseline_path}")
    else:
        lines.append("process baseline: none")

    if result.git.dirty:
        lines.append(
            "git: dirty "
            f"({len(result.git.staged)} staged, "
            f"{len(result.git.unstaged)} unstaged, "
            f"{len(result.git.untracked)} untracked)"
        )
        for entry in result.git.entries:
            lines.append(f"  {entry}")
    else:
        lines.append("git: clean")

    if result.stashed:
        lines.append(f"stash: created - {result.stash_message}")

    if result.repo_processes:
        lines.append(f"repo-rooted processes: {len(result.repo_processes)}")
        for proc in result.repo_processes:
            label = f" label={proc.launch_label}" if proc.launch_label else ""
            lines.append(f"  pid={proc.pid}{label} cwd={proc.cwd} cmd={proc.command}")
    else:
        lines.append("repo-rooted processes: none")

    if result.booted_out_labels:
        lines.append("booted out launch labels:")
        for label in result.booted_out_labels:
            lines.append(f"  {label}")

    if result.stopped_processes:
        lines.append("terminated pids:")
        for pid in result.stopped_processes:
            lines.append(f"  {pid}")

    if result.errors:
        lines.append("errors:")
        for error in result.errors:
            lines.append(f"  {error}")

    return "\n".join(lines)


def to_jsonable(result: CloseoutResult) -> dict[str, Any]:
    return {
        "workspace": result.workspace,
        "git": asdict(result.git),
        "baseline_path": result.baseline_path,
        "baseline_used": result.baseline_used,
        "stashed": result.stashed,
        "stash_message": result.stash_message,
        "repo_processes": [asdict(proc) for proc in result.repo_processes],
        "stopped_processes": result.stopped_processes,
        "booted_out_labels": result.booted_out_labels,
        "errors": result.errors,
        "clean": not result_has_issues(result),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent workspace closeout helper")
    parser.add_argument("--cwd", default=".", help="workspace directory")
    parser.add_argument(
        "--stash-dirty",
        action="store_true",
        help="stash dirty state with a closeout label",
    )
    parser.add_argument(
        "--stop-repo-processes",
        action="store_true",
        help="send SIGTERM to remaining processes whose cwd is inside this workspace",
    )
    parser.add_argument(
        "--bootout-launch-agents",
        action="store_true",
        help="on macOS, boot out matching LaunchAgent labels before SIGTERM",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record current repo-rooted processes as expected baseline and exit",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore .unitares/workspace-closeout-baseline.json",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    root = repo_root(Path(args.cwd).resolve())
    if args.write_baseline:
        try:
            processes = repo_rooted_processes(root, baseline_keys=None)
        except RuntimeError as exc:
            print(f"workspace-closeout: {exc}", file=sys.stderr)
            return 2
        path = write_process_baseline(root, processes)
        payload = {
            "workspace": str(root),
            "baseline_path": str(path),
            "process_count": len(processes),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"workspace-closeout: wrote process baseline with "
                f"{len(processes)} process(es) to {path}"
            )
        return 0

    try:
        result = closeout(
            root,
            stash=args.stash_dirty,
            stop_processes=args.stop_repo_processes,
            bootout_launch_agents=args.bootout_launch_agents,
            use_baseline=not args.no_baseline,
        )
    except RuntimeError as exc:
        print(f"workspace-closeout: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(to_jsonable(result), indent=2, sort_keys=True))
    else:
        print(render_text(result))

    return 1 if result_has_issues(result) else 0


if __name__ == "__main__":
    sys.exit(main())
