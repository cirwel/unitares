"""Read-only council advisor lanes, for the ``council.*`` launch profiles.

A council asks several model families the same question about a repository
and compares their answers (dispatch_beam's ``Dispatch.Council`` is the
caller this was written for). Each lane is a launch profile
(``scripts/ops/orchestrator-profiles.py``) that runs this client with the
interpreter in isolated mode (``python -I``). The profile fixes the lane, the
CLI and the directories a lane may read; the caller fills only the prompt,
the directory under review and, where the lane takes one, the model. The
client owns every command line, so no caller text becomes an option:

* **claude** runs in the directory under review with ``--safe-mode`` (no
  hooks, plugins, MCP servers or project instructions) and ``--restricted``,
  which confines the file tools to that directory. Only Read, Grep and Glob
  exist, ``.env`` files and ``.unitares`` caches are denied, and ``dontAsk``
  refuses anything else. A bare ``--allowedTools Read`` without
  ``--restricted`` reads any file on disk, through an absolute path or a
  symlink in the repository (verified 2026-10-26).
* **codex** runs in a fresh empty directory, as review_gate's codex reviewer
  does: started in the repository it would load that repository's
  ``AGENTS.md`` and skills, so the repository could steer its own reviewer.
  The prompt names the directory, which the lane's deny-by-default permission
  profile (``codex_app_server_client.lane_config_overrides``) makes readable,
  with its git directories, and with ``.env`` files and ``.unitares`` caches
  still denied. The lane refuses to run when administrator codex
  configuration exists or the sandbox probe fails.
* **agy** reads no directory: it runs in an empty workspace with an isolated
  ``HOME``, as ``antigravity_cli_client`` runs it, and reviews only the text
  of the prompt (a gate packet carries the diff).

The directory is resolved with ``realpath`` and accepted only at or under one
of the profile's roots (``COUNCIL_ROOTS``, separated by ``os.pathsep``), so a
symlink cannot lead out of them. The prompt goes on standard input where the
CLI reads it there (claude, codex), so a prompt beginning with ``--`` reaches
the model as text; agy takes it as one argument behind a fixed guard line.

The CLI's own output streams through unchanged (``stream-json`` for claude
and agy, ``--json`` for codex), which the caller's lane parsers read, and its
exit status is returned. The client's own refusals go to standard error with
exit 2 (bad input), 3 (confinement unavailable) or 127 (CLI missing). There is
no deadline here: the profile's ``max_runtime_ms`` is the backstop, and the
orchestrator kills the whole process tree. Stdlib only.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TextIO

LANES = ("claude", "codex", "agy")

#: The proposal's model pattern (inference-hosts-config-v0.md §2.5): no
#: leading ``-``, so a model can never read as an option.
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")

#: Exit statuses for the client's own refusals.
EXIT_BAD_INPUT = 2
EXIT_NOT_CONFINED = 3
EXIT_NO_CLI = 127

LANE_SYSTEM_PROMPT = (
    "You are one read-only advisor in a multi-agent council. "
    "Do not modify files, commit, push, install packages, start services, or perform "
    "destructive actions. Return concise advisory findings only."
)

#: What claude may not read inside the directory: credentials, and the local
#: governance continuity cache (it holds a client_session_id and proof
#: material). Rules are relative to the working directory.
CLAUDE_DENIED_READS = (
    "Read(./**/.env*)",
    "Read(./**/*.env)",
    "Read(./**/.unitares/**)",
)

CLAUDE_FLAGS = (
    "-p",
    "--safe-mode",
    "--restricted",
    "--strict-mcp-config",
    "--tools", "Read,Grep,Glob",
    "--allowedTools", "Read", "Grep", "Glob",
    "--disallowedTools", *CLAUDE_DENIED_READS,
    "--permission-mode", "dontAsk",
    "--max-turns", "3",
    "--append-system-prompt", LANE_SYSTEM_PROMPT,
    "--no-session-persistence",
    "--output-format", "stream-json",
    "--verbose",
)

#: Package trees git and other read-only tools load libraries from. They hold
#: programs, not data (review_gate.CODEX_TOOL_ROOTS).
CODEX_TOOL_ROOTS = ("/opt/homebrew/Cellar", "/opt/homebrew/opt", "/usr/local/Cellar",
                    "/usr/local/opt", "/Library/Developer/CommandLineTools")

#: Git is configured through the environment: the profile denies the
#: operator's global and the system configuration.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

#: agy is told up front that it has no tools: headless mode ends the turn at
#: the first denied tool with an empty answer.
AGY_TEXT_ONLY = (
    "\n\nYou cannot see the repository or use tools in this session: a tool "
    "call is denied and ends your turn without an answer. Everything you can "
    "review is in the task above. Answer directly in text."
)

#: One argv element: Linux caps it at 128 KiB
#: (antigravity_cli_client.PROMPT_BYTES_LIMIT).
AGY_ARG_LIMIT = 120_000

#: A parent holding one of these would put project configuration in reach
#: of agy, which walks up from its working directory.
AGY_PROJECT_MARKERS = (".git", ".agents")


class Refusal(Exception):
    def __init__(self, message: str, code: int = EXIT_BAD_INPUT) -> None:
        super().__init__(message)
        self.code = code


def _sibling(name: str) -> ModuleType:
    """A stdlib-only client beside this one, loaded by path: ``-I`` leaves
    this file's directory off ``sys.path``, and the sibling comes from the
    same tree, so it carries the same trust."""
    key = f"_council_{name}"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise Refusal(f"cannot load {path}", EXIT_NOT_CONFINED)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass looks its module up there.
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _glob_unsafe(path: str) -> bool:
    """Whether ``path`` holds glob syntax (or its escape), so a deny glob
    rooted at it may not match what it names (review_gate._glob_unsafe)."""
    return any(ch in path for ch in "*?[]{}\\")


def roots(environ: dict[str, str]) -> list[str]:
    raw = [entry for entry in environ.get("COUNCIL_ROOTS", "").split(os.pathsep) if entry]
    if not raw:
        raise Refusal("this lane has no COUNCIL_ROOTS; the profile names none")
    return [os.path.realpath(entry) for entry in raw]


def resolve_dir(raw: str, allowed: list[str]) -> str:
    """The directory under review, symlinks resolved, at or under a root."""
    if not raw or not os.path.isabs(raw):
        raise Refusal("COUNCIL_DIR must be an absolute path")
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        raise Refusal("COUNCIL_DIR is not a directory")
    if not any(os.path.commonpath([real, root]) == root for root in allowed):
        raise Refusal("COUNCIL_DIR is outside the profile's roots")
    if _glob_unsafe(real):
        raise Refusal("COUNCIL_DIR contains glob characters")
    return real


def cli(environ: dict[str, str]) -> str:
    path = environ.get("COUNCIL_CLI", "")
    if not path or not os.path.isabs(path):
        raise Refusal("COUNCIL_CLI must be an absolute path")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise Refusal(f"council CLI {path} is not an executable file", EXIT_NO_CLI)
    return path


def model(environ: dict[str, str]) -> str | None:
    value = environ.get("COUNCIL_MODEL", "")
    if value and not MODEL_PATTERN.fullmatch(value):
        raise Refusal("COUNCIL_MODEL is not a valid model name")
    return value or None


def git_dirs(directory: str) -> list[str]:
    """The directory's git directory and common directory, absolute (a linked
    worktree keeps them outside it); [] outside a repository. The repository's
    own configuration cannot start a program from rev-parse; fsmonitor is
    switched off anyway."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", directory, "rev-parse",
             "--absolute-git-dir", "--git-common-dir"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **GIT_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return sorted({os.path.realpath(os.path.join(directory, line))
                   for line in proc.stdout.split("\n") if line.strip()})


_TOP_LEVEL = re.compile(r"""^\s*([a-z_]+)\s*=\s*(["'])([A-Za-z0-9._:\-\[\]]+)\2\s*(#.*)?$""")


def codex_user_model(codex_home: str) -> tuple[str | None, str | None]:
    """``model`` and ``model_reasoning_effort`` as the operator's codex config
    sets them at top level. The lane ignores that config (it would load MCP
    servers and project trust), so these two are carried over; only values of
    a plain shape are read."""
    try:
        text = Path(codex_home, "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break
        match = _TOP_LEVEL.match(line)
        if match:
            found[match.group(1)] = match.group(3)
    return found.get("model"), found.get("model_reasoning_effort")


def codex_prompt(prompt: str, directory: str) -> str:
    return (f"The repository under review is the directory {directory}. Your working "
            "directory is empty; read the repository by that absolute path.\n\n" + prompt)


def codex_command(lane: ModuleType, path: str, directory: str, workdir: str,
                  chosen: str | None, codex_home: str) -> list[str]:
    configured, effort = codex_user_model(codex_home)
    overrides = lane.lane_config_overrides(
        path,
        extra_read=[directory, *git_dirs(directory),
                    *(root for root in CODEX_TOOL_ROOTS if os.path.isdir(root))],
        extra_deny=[f"{directory}/**/.env*", f"{directory}/**/*.env",
                    f"{directory}/.unitares", f"{directory}/**/.unitares"],
        set_env=GIT_ENV,
    )
    args = [path, "exec", "--ignore-user-config", "--ephemeral", "--json",
            "--color", "never", "-c", "project_doc_max_bytes=0"]
    if effort and not chosen:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    for override in overrides:
        args += ["-c", override]
    args += ["--skip-git-repo-check", "-C", workdir]
    if chosen or configured:
        args += ["--model", chosen or configured]
    return [*args, "-"]


def _isolated(directory: str, markers: tuple[str, ...]) -> bool:
    return not any((parent / marker).exists()
                   for parent in (Path(directory).resolve(), *Path(directory).resolve().parents)
                   for marker in markers)


def _stream(cmd: list[str], *, cwd: str, env: dict[str, str], stdin_text: str | None,
            err: TextIO) -> int:
    """Run the CLI with its output on this process's own streams."""
    try:
        sys.stdout.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                stdin=subprocess.PIPE if stdin_text is not None
                                else subprocess.DEVNULL)
    except OSError as exc:
        err.write(f"council_lane_client: CLI spawn failed: {type(exc).__name__}\n")
        return EXIT_NO_CLI
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
    return proc.wait()


def run_claude(environ: dict[str, str], prompt: str, err: TextIO) -> int:
    path = cli(environ)
    directory = resolve_dir(environ.get("COUNCIL_DIR", ""), roots(environ))
    env = _sibling("claude_cli_client").claude_env(environ)
    return _stream([path, *CLAUDE_FLAGS], cwd=directory, env=env, stdin_text=prompt, err=err)


def run_codex(environ: dict[str, str], prompt: str, err: TextIO) -> int:
    path = cli(environ)
    directory = resolve_dir(environ.get("COUNCIL_DIR", ""), roots(environ))
    chosen = model(environ)
    lane = _sibling("codex_app_server_client")
    refusal = lane.admin_config_conflict()
    if refusal:
        raise Refusal(f"{refusal} is administrator codex configuration that can override "
                      "the lane's permission profile", EXIT_NOT_CONFINED)
    if not lane.confinement_verified(path):
        raise Refusal("this codex does not enforce the lane's permission profile "
                      "(codex sandbox probe)", EXIT_NOT_CONFINED)
    codex_home = environ.get("CODEX_HOME") or os.path.join(environ.get("HOME", ""), ".codex")
    env = _sibling("claude_cli_client").claude_env(environ)
    if environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = environ["CODEX_HOME"]
    workdir = os.path.realpath(tempfile.mkdtemp(prefix="unitares-council-codex-"))
    try:
        if not _isolated(workdir, (".git", ".agents", "AGENTS.md")):
            raise Refusal("the temporary directory sits inside a project; set TMPDIR to a "
                          "plain directory", EXIT_NOT_CONFINED)
        cmd = codex_command(lane, path, directory, workdir, chosen, codex_home)
        return _stream(cmd, cwd=workdir, env=env,
                       stdin_text=codex_prompt(prompt, directory), err=err)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_agy(environ: dict[str, str], prompt: str, err: TextIO) -> int:
    path = cli(environ)
    chosen = model(environ)
    agy = _sibling("antigravity_cli_client")
    text = agy.guard_prompt(prompt + AGY_TEXT_ONLY)
    if len(text.encode("utf-8")) > AGY_ARG_LIMIT:
        raise Refusal(f"COUNCIL_PROMPT is too long for agy's one argument "
                      f"({AGY_ARG_LIMIT} bytes with the guard line)")
    root = os.path.realpath(tempfile.mkdtemp(prefix="unitares-council-agy-"))
    try:
        workspace = os.path.join(root, "workspace")
        os.mkdir(workspace, 0o700)
        if not _isolated(workspace, AGY_PROJECT_MARKERS):
            raise Refusal("the temporary directory sits under a .git or .agents directory; "
                          "set TMPDIR to a plain directory", EXIT_NOT_CONFINED)
        home = agy.isolated_home(root, environ.get("HOME"))
        cmd = [path, "-p", text, "--mode", "plan", "--sandbox",
               "--output-format", "stream-json", *(["--model", chosen] if chosen else [])]
        return _stream(cmd, cwd=workspace, env=agy.agy_env(environ, home=home),
                       stdin_text=None, err=err)
    finally:
        shutil.rmtree(root, ignore_errors=True)


RUNNERS = {"claude": run_claude, "codex": run_codex, "agy": run_agy}


def run(environ: dict[str, str], err: TextIO = sys.stderr) -> int:
    try:
        lane = environ.get("COUNCIL_LANE", "")
        if lane not in RUNNERS:
            raise Refusal(f"COUNCIL_LANE must be one of {', '.join(LANES)}")
        prompt = environ.get("COUNCIL_PROMPT", "")
        if not prompt.strip():
            raise Refusal("COUNCIL_PROMPT is empty")
        return RUNNERS[lane](environ, prompt, err)
    except Refusal as refusal:
        err.write(f"council_lane_client: {refusal}\n")
        return refusal.code


if __name__ == "__main__":
    raise SystemExit(run(dict(os.environ)))
