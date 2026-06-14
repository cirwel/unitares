#!/usr/bin/env python3
"""Agent workspace closeout helper.

This is the pre-final-response hygiene check that agents should run when
they have modified the repo or started local services. It answers two
questions the user should not have to audit manually:

1. Is the git worktree still dirty?
2. Has local git state actually been delivered upstream?
3. Are any long-running processes still rooted inside this workspace?

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
    detached: bool = False
    head: str = ""
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    delivery_status: str = "unknown"
    delivery_detail: str = "delivery state not computed"


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


@dataclass
class WorkspaceIsolation:
    kind: str  # main_checkout | deploy_worktree | agent_worktree | unknown
    shared: bool
    git_dir: str
    common_dir: str
    detail: str


@dataclass
class StartCheckResult:
    workspace: str
    checked_existing_baseline: bool
    closeout: CloseoutResult
    baseline_written: bool
    new_baseline_path: str | None
    new_baseline_process_count: int
    # Advisory by default: a shared-checkout warning is surfaced but does NOT
    # fail the check unless require_worktree (strict mode) was requested. Default
    # None keeps existing constructors/tests working.
    isolation: WorkspaceIsolation | None = None
    isolation_enforced: bool = False


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


# Names whose presence in the workspace path marks a live/shared checkout that is
# linked-but-not-agent-owned (the deploy worktree). Override via env (comma-sep).
# The main dev checkout is detected structurally (git_dir == git_common_dir) and
# needs no name match.
_DEFAULT_LIVE_CHECKOUT_NAMES = ("unitares-deploy",)


def _live_checkout_names() -> tuple[str, ...]:
    raw = os.getenv("UNITARES_LIVE_CHECKOUT_NAMES", "")
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    return names or _DEFAULT_LIVE_CHECKOUT_NAMES


def classify_workspace(root: Path) -> "WorkspaceIsolation":
    """Classify a workspace as the main checkout, the deploy worktree, or an
    agent-owned linked worktree.

    The main working tree has git_dir == git_common_dir; a linked worktree's
    git_dir is `<common>/worktrees/<name>`. Agents should mutate code only in an
    agent-owned linked worktree — never the shared main checkout or the deploy
    worktree (see docs/proposals/worktree-isolation-vs-lease-default.md).
    """
    rc_a, git_dir_out, _ = _run(["git", "rev-parse", "--absolute-git-dir"], root)
    rc_b, common_out, _ = _run(["git", "rev-parse", "--git-common-dir"], root)
    if rc_a != 0 or rc_b != 0:
        return WorkspaceIsolation(
            kind="unknown", shared=False, git_dir="", common_dir="",
            detail="not a git work tree; isolation check skipped",
        )
    git_dir = Path(git_dir_out.strip()).resolve()
    common_raw = Path(common_out.strip())
    common_dir = (common_raw if common_raw.is_absolute() else (root / common_raw)).resolve()

    if git_dir == common_dir:
        return WorkspaceIsolation(
            kind="main_checkout", shared=True,
            git_dir=str(git_dir), common_dir=str(common_dir),
            detail="this is the main/shared checkout, not an agent-owned worktree",
        )
    for name in _live_checkout_names():
        if name and name in str(root):
            return WorkspaceIsolation(
                kind="deploy_worktree", shared=True,
                git_dir=str(git_dir), common_dir=str(common_dir),
                detail=f"path matches a live checkout name ({name!r})",
            )
    return WorkspaceIsolation(
        kind="agent_worktree", shared=False,
        git_dir=str(git_dir), common_dir=str(common_dir),
        detail="agent-owned linked worktree",
    )


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
    state.detached = not bool(state.branch)
    state.head = _git_stdout(["git", "rev-parse", "--short", "HEAD"], root)
    upstream = _git_stdout(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        root,
    )
    state.upstream = upstream or None
    if state.upstream:
        state.ahead, state.behind = ahead_behind(root, state.upstream)
    state.delivery_status, state.delivery_detail = compute_delivery_state(
        state,
        default_upstream=default_upstream(root),
    )
    return state


def _git_stdout(args: list[str], root: Path) -> str:
    rc, stdout, _ = _run(args, root)
    return stdout.strip() if rc == 0 else ""


def ahead_behind(root: Path, upstream: str) -> tuple[int | None, int | None]:
    rc, stdout, _ = _run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
        root,
    )
    if rc != 0:
        return None, None
    parts = stdout.strip().split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def default_upstream(root: Path) -> str | None:
    """Return the best local name for origin's default branch."""
    symbolic = _git_stdout(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        root,
    )
    if symbolic:
        return symbolic
    for candidate in ("origin/master", "origin/main"):
        if _git_stdout(["git", "rev-parse", "--verify", "--quiet", candidate], root):
            return candidate
    return None


def compute_delivery_state(
    state: GitState,
    *,
    default_upstream: str | None = None,
) -> tuple[str, str]:
    """Summarize whether local work is committed, pushed, and plausibly merged.

    This is intentionally local-git based. It does not claim a feature branch is
    merged unless the current checkout is clean and synced with the default
    upstream; for feature branches it reports that PR/merge state is not proven.
    """
    if state.dirty:
        return "local_changes", "not committed, not pushed, not merged"
    if state.detached:
        return "detached", "clean detached HEAD; switch/create a branch before delivery"
    if not state.branch:
        return "unknown", "could not identify the current branch"
    if not state.upstream:
        return "no_upstream", "clean branch has no upstream; push it before asking GitHub to merge"
    if state.ahead is None or state.behind is None:
        return "unknown", f"could not compare HEAD with {state.upstream}"
    if state.ahead > 0 and state.behind > 0:
        return (
            "diverged",
            f"{state.ahead} local commit(s) ahead and {state.behind} behind {state.upstream}",
        )
    if state.ahead > 0:
        return "unpushed_commits", f"{state.ahead} local commit(s) not pushed to {state.upstream}"
    if state.behind > 0:
        return "behind_upstream", f"clean but {state.behind} commit(s) behind {state.upstream}"
    if default_upstream and state.upstream == default_upstream:
        return "synced_default", f"clean and synced with {state.upstream}"
    return (
        "pushed_branch",
        f"clean and pushed to {state.upstream}; PR/merge state not proven by local git",
    )


def delivery_needs_attention(state: GitState) -> bool:
    """Return true for delivery states that mean local work is definitely stuck."""
    return state.delivery_status in {"local_changes", "unpushed_commits", "diverged"}


def delivery_next_step(state: GitState) -> str | None:
    """Human guidance for the next delivery action.

    The closeout output is the operator-facing source of truth for delivery
    state. Include the next mechanical command so agents don't turn
    "local_changes" into a vague status report when the standard delivery path
    is already known.
    """
    if state.delivery_status == "local_changes":
        return (
            "ship with: ./scripts/dev/ship.sh --stage-all "
            "\"type(scope): concise message\" if the whole worktree belongs; "
            "otherwise stage intended files and run ./scripts/dev/ship.sh "
            "\"type(scope): concise message\""
        )
    if state.delivery_status == "unpushed_commits":
        return (
            "push/open draft PR with: git push -u origin HEAD && "
            "gh pr create --draft --fill"
        )
    if state.delivery_status == "detached":
        return (
            "create a branch or use ship.sh from staged changes; "
            "auto mode will mint an agent-prefixed draft-PR branch"
        )
    return None


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


def start_check(
    root: Path, *, use_baseline: bool = True, require_worktree: bool = False
) -> StartCheckResult:
    """Validate session-start hygiene and refresh the process baseline.

    Dirty git state always blocks baseline refresh. Repo-rooted process residue
    blocks refresh only when a previous baseline exists; without a baseline, the
    current process set is treated as the initial expected resident/control-plane
    set.

    Also classifies workspace isolation (main checkout vs agent-owned worktree).
    Advisory by default — editing the shared/main checkout is surfaced as a
    warning. With ``require_worktree=True`` (strict mode) a shared checkout
    becomes a hard failure, matching the lease-plane advisory->strict pattern.
    """
    isolation = classify_workspace(root)
    baseline_file = baseline_path(root)
    checked_existing_baseline = use_baseline and baseline_file.exists()

    if checked_existing_baseline:
        result = closeout(root, use_baseline=True)
    else:
        state = git_state(root)
        result = CloseoutResult(
            workspace=str(root),
            git=state,
            baseline_path=str(baseline_file) if baseline_file.exists() else None,
            baseline_used=False,
            stashed=False,
            stash_message=None,
            repo_processes=[],
            stopped_processes=[],
            booted_out_labels=[],
            errors=[],
        )

    baseline_written = False
    new_baseline_path: str | None = None
    process_count = 0
    if not result_has_issues(result):
        processes = repo_rooted_processes(root, baseline_keys=None)
        path = write_process_baseline(root, processes)
        baseline_written = True
        new_baseline_path = str(path)
        process_count = len(processes)

    return StartCheckResult(
        workspace=str(root),
        checked_existing_baseline=checked_existing_baseline,
        closeout=result,
        baseline_written=baseline_written,
        new_baseline_path=new_baseline_path,
        new_baseline_process_count=process_count,
        isolation=isolation,
        isolation_enforced=bool(require_worktree and isolation.shared),
    )


def result_has_issues(result: CloseoutResult) -> bool:
    return (
        delivery_needs_attention(result.git)
        or bool(result.repo_processes)
        or bool(result.errors)
    )


def isolation_is_issue(result: StartCheckResult) -> bool:
    """A shared-checkout warning is an *issue* only in strict mode."""
    return bool(result.isolation and result.isolation.shared and result.isolation_enforced)


def start_check_has_issues(result: StartCheckResult) -> bool:
    return (
        result_has_issues(result.closeout)
        or not result.baseline_written
        or isolation_is_issue(result)
    )


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

    branch = result.git.branch or "(detached)"
    upstream = result.git.upstream or "(none)"
    ahead = "?" if result.git.ahead is None else str(result.git.ahead)
    behind = "?" if result.git.behind is None else str(result.git.behind)
    lines.append(
        f"delivery: {result.git.delivery_status} - {result.git.delivery_detail}"
    )
    lines.append(
        f"  branch={branch} head={result.git.head or '?'} "
        f"upstream={upstream} ahead={ahead} behind={behind}"
    )
    next_step = delivery_next_step(result.git)
    if next_step:
        lines.append(f"  next={next_step}")

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


def render_start_check_text(result: StartCheckResult) -> str:
    lines = [f"Workspace start check: {result.workspace}"]
    if result.checked_existing_baseline:
        lines.append("previous baseline: checked")
    else:
        lines.append(
            "previous baseline: not present; "
            "treating current services as initial baseline"
        )

    if result.isolation is not None:
        iso = result.isolation
        if not iso.shared:
            lines.append(f"workspace isolation: ok ({iso.kind})")
        else:
            marker = "BLOCKED" if isolation_is_issue(result) else "warning"
            lines.append(
                f"workspace isolation: {marker} — {iso.detail}. "
                "Prefer an agent-owned worktree "
                "(git worktree add ../unitares-wt/<task> -b <branch> origin/master)."
            )

    if result_has_issues(result.closeout):
        lines.append("status: blocked")
        lines.append(render_text(result.closeout))
    else:
        lines.append("status: clean")

    if result.baseline_written:
        lines.append(
            "baseline: wrote "
            f"{result.new_baseline_process_count} process(es) to "
            f"{result.new_baseline_path}"
        )
    else:
        lines.append("baseline: not written")

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


def start_check_to_jsonable(result: StartCheckResult) -> dict[str, Any]:
    return {
        "workspace": result.workspace,
        "checked_existing_baseline": result.checked_existing_baseline,
        "closeout": to_jsonable(result.closeout),
        "baseline_written": result.baseline_written,
        "new_baseline_path": result.new_baseline_path,
        "new_baseline_process_count": result.new_baseline_process_count,
        "isolation": asdict(result.isolation) if result.isolation else None,
        "isolation_enforced": result.isolation_enforced,
        "clean": not start_check_has_issues(result),
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
        "--start-check",
        action="store_true",
        help=(
            "fail on dirty git or prior-session process residue, then refresh "
            "the process baseline when clean"
        ),
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore .unitares/workspace-closeout-baseline.json",
    )
    parser.add_argument(
        "--require-worktree",
        action="store_true",
        help=(
            "strict mode: fail --start-check when run in the shared/main checkout "
            "or deploy worktree instead of an agent-owned linked worktree "
            "(advisory warning only without this flag)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    root = repo_root(Path(args.cwd).resolve())
    if args.start_check:
        try:
            result = start_check(
                root,
                use_baseline=not args.no_baseline,
                require_worktree=args.require_worktree,
            )
        except RuntimeError as exc:
            print(f"workspace-closeout: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(start_check_to_jsonable(result), indent=2, sort_keys=True))
        else:
            print(render_start_check_text(result))
        return 1 if start_check_has_issues(result) else 0

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
