"""Write-capable build lane, for the ``build.claude`` launch profile.

A build lane runs Claude Code unattended in one working directory with a
brief the operator fixed, so that an agent with little quota can commission
work from one with more (the first consumer is a project's PR-driven kit
builder; the poller that prepares the directory and publishes the result
lives with that project, not here). Each lane is a launch profile
(``scripts/ops/orchestrator-profiles.py``) that runs this client with the
interpreter in isolated mode (``python -I``). The profile fixes the CLI, the
brief, the roots a directory may sit under and the directories the file tools
may also read; the caller fills only the directory and, optionally, the model.
The client owns every command line, so no caller text becomes an option.

What the lane is, and is not:

* **The brief is fixed.** It is read from the profile's file at run time and
  goes to ``claude -p`` on standard input. The caller cannot add to it; the
  request's own text reaches the model only as files in the directory, which
  the brief tells it to treat as data.
* **The directory is confined.** ``BUILD_DIR`` is resolved with ``realpath``
  and accepted only at or under one of the profile's roots (``BUILD_ROOTS``),
  never a ``.env`` or ``.unitares`` path. Extra read directories
  (``BUILD_READ_ROOTS``) go to ``--add-dir`` with ``Edit``, ``Write`` and
  ``NotebookEdit`` denied under them, so they stay read-only for the file
  tools (Bash is not confined; see below).
* **Customizations stay on.** Unlike the host and council lanes this runs
  without ``--safe-mode``: the project's instructions, the operator's hooks
  and the governance plugin are what make an unattended build behave like an
  attended one, and the plugin's session-start hook turns the orchestrator's
  provisioned lineage (``UNITARES_PARENT_AGENT_ID``, ``UNITARES_SPAWN_REASON``)
  into the child's own onboard call. The tool set is fixed (``--tools``, and
  the same names in ``--allowedTools`` so nothing prompts), MCP servers come
  only from the profile's ``BUILD_MCP_CONFIG`` (``--strict-mcp-config``: no
  chat, browser or GitHub servers from the user's configuration), and
  ``--permission-mode dontAsk --permission-prompts none`` denies anything
  that would otherwise ask a person.
* **Publishing is the caller's job.** A shim directory first on ``PATH``
  refuses ``gh`` and the operator's GitHub App runner outright, and refuses
  ``git push``, ``git add -f``, ``git remote`` and ``git config`` writes
  before handing everything else to the real ``git``. ``GIT_CONFIG_GLOBAL``
  points at ``/dev/null`` and ``GIT_CONFIG_NOSYSTEM`` is set, so no credential
  helper, alias or hook path from the operator's configuration is in reach.
* **An allowlisted environment.** The subscription login is found through
  ``HOME`` and ``USER``; console-API credentials and provider switches are not
  forwarded (``claude_cli_client.ENV_ALLOWLIST``), and the orchestrator's own
  bearer never is. The runner's provisioned lineage variables are passed
  through.
* **The result outlives the orchestrator's retention.** The client removes
  any earlier run's ``result.json`` and ``build.log`` from
  ``BUILD_DIR/.build-lane/``, writes ``started.json`` as it starts, streams
  the CLI's ``stream-json`` output to ``build.log`` as well as to its own
  standard output, and writes ``result.json`` (exit status, times, the CLI's
  final result text, model and cost where reported) when the CLI exits or
  hits the client's deadline. A caller that polls every few minutes reads
  these; the orchestrator keeps a finished result for five minutes only. A
  stop through the orchestrator (``DELETE /v1/executions/:id``) is a SIGKILL
  of the whole tree, so it leaves ``started.json`` and the log but **no**
  ``result.json``: a caller reads that absence, once the execution is no
  longer live, as "stopped or killed".

**What this does not bound.** Bash is not path-confined, and a process running
as the operator's user can read whatever that user can, the GitHub App's key
included. The shims and tool rules are guardrails against a builder going
off-script, not a security boundary against a hostile one: the trust in the
request content comes from the caller, which must build from a trusted base
and copy in only what it validated. Document that where the lane is used.

Reads ``BUILD_CLI``, ``BUILD_ROOTS``, ``BUILD_BRIEF``, ``BUILD_READ_ROOTS``,
``BUILD_MCP_CONFIG`` and ``BUILD_DEADLINE_S`` (fixed by the profile),
``BUILD_DIR`` (required) and ``BUILD_MODEL`` from the environment. The CLI's exit status is returned; the
client's own refusals go to standard error with exit 2 (bad input), 124
(deadline) or 127 (CLI missing). There is no prompt here beyond the brief; the
profile's ``max_runtime_ms`` is the outer backstop and the client's own
deadline (``DEADLINE_S``) sits inside it so the result file is written before
the orchestrator's kill. Stdlib only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TextIO

#: The proposal's model pattern (inference-hosts-config-v0.md §2.5): no
#: leading ``-``, so a model can never read as an option.
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")

EXIT_BAD_INPUT = 2
EXIT_DEADLINE = 124
EXIT_NO_CLI = 127

#: The profile's max_runtime_ms is 7_200_000; the client stops the CLI first
#: so result.json is written before kill_tree arrives. ``BUILD_DEADLINE_S``,
#: fixed by the profile, overrides it (tests use a short one).
DEADLINE_S = 7_000
#: Seconds allowed for the CLI to exit after SIGTERM before SIGKILL.
KILL_GRACE_S = 15
#: A turn cap, so a looping build ends before the deadline does.
MAX_TURNS = 400

#: Where the client leaves its own records inside the directory. The caller
#: keeps it out of version control (``.git/info/exclude``).
STATE_DIR = ".build-lane"

#: The built-in tools a build may use. Bash is what runs a project's pipeline;
#: there is no network tool beyond fetch and search.
TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "Agent")

#: Every built-in tool is pre-approved (nothing may prompt), and so is the
#: governance server the profile may load through BUILD_MCP_CONFIG.
ALLOWED_TOOLS = (*TOOLS, "mcp__unitares-governance__*")

#: Guardrails inside Bash and the file tools (see the module docstring for
#: what they are not). Publishing and service control are the caller's.
DISALLOWED_TOOLS = (
    "Bash(git push*)",
    "Bash(gh *)",
    "Bash(unitares-github *)",
    "Bash(launchctl *)",
    "Bash(systemctl *)",
    "Bash(open *)",
    "Bash(osascript *)",
    "Bash(ssh *)",
    "Bash(sudo *)",
    "Read(./**/.env*)",
    "Read(./**/*.env)",
    "Read(./**/.unitares/**)",
)

#: Variables the runner provisions for lineage and the server; passed through
#: so the governance plugin's session-start hook can suggest the lineage.
LINEAGE_PASSTHROUGH = ("UNITARES_PARENT_AGENT_ID", "UNITARES_SPAWN_REASON",
                       "UNITARES_SERVER_URL")

#: Git reads only the repository's own configuration.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

#: The ``git`` shim: a refusal list, then the real binary. ``add -f`` is
#: refused because a project's ignored paths are ignored for a reason.
GIT_SHIM = """#!/bin/sh
# build lane: publishing and remote configuration are the caller's job.
real="$BUILD_LANE_REAL_GIT"
sub=""
skip=0
for a in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$a" in
    -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env) skip=1 ;;
    -*) ;;
    *) sub="$a"; break ;;
  esac
done
case "$sub" in
  push|remote|config|credential|credential-*|fetch|pull|submodule)
    echo "build lane: git $sub is not available to the builder (the caller publishes)" >&2
    exit 3 ;;
  add)
    for a in "$@"; do
      case "$a" in
        --force|-*f*) echo "build lane: git add -f is refused (ignored paths stay ignored)" >&2; exit 3 ;;
      esac
    done ;;
esac
exec "$real" "$@"
"""

REFUSE_SHIM = """#!/bin/sh
echo "build lane: {name} is not available to the builder (the caller publishes)" >&2
exit 3
"""


class Refusal(Exception):
    def __init__(self, message: str, code: int = EXIT_BAD_INPUT) -> None:
        super().__init__(message)
        self.code = code


def _sibling(name: str) -> ModuleType:
    """A stdlib-only client beside this one, loaded by path (``-I`` leaves this
    file's directory off ``sys.path``)."""
    key = f"_build_{name}"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise Refusal(f"cannot load {path}", EXIT_NO_CLI)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _protected(component: str) -> bool:
    return (component == ".unitares" or component.startswith(".env")
            or component.endswith(".env"))


def roots(environ: dict[str, str]) -> list[str]:
    raw = [entry for entry in environ.get("BUILD_ROOTS", "").split(os.pathsep) if entry]
    if not raw:
        raise Refusal("this lane has no BUILD_ROOTS; the profile names none")
    return [os.path.realpath(entry) for entry in raw]


def resolve_dir(raw: str, allowed: list[str]) -> str:
    """The build directory, symlinks resolved, at or under a root."""
    if not raw or not os.path.isabs(raw):
        raise Refusal("BUILD_DIR must be an absolute path")
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        raise Refusal("BUILD_DIR is not a directory")
    if not any(os.path.commonpath([real, root]) == root for root in allowed):
        raise Refusal("BUILD_DIR is outside the profile's roots")
    if any(ch in real for ch in "*?[]{}\\"):
        raise Refusal("BUILD_DIR contains glob characters")
    if any(_protected(part) for part in Path(real).parts):
        raise Refusal("BUILD_DIR is, or is inside, a .env or .unitares path")
    return real


def read_roots(environ: dict[str, str]) -> list[str]:
    """Extra directories the file tools may read; each must exist."""
    out: list[str] = []
    for entry in environ.get("BUILD_READ_ROOTS", "").split(os.pathsep):
        if not entry:
            continue
        if not os.path.isabs(entry):
            raise Refusal("a BUILD_READ_ROOTS entry is not an absolute path")
        real = os.path.realpath(entry)
        if not os.path.isdir(real):
            raise Refusal("a BUILD_READ_ROOTS entry is not a directory")
        if real not in out:
            out.append(real)
    return out


def cli(environ: dict[str, str]) -> str:
    path = environ.get("BUILD_CLI", "")
    if not path or not os.path.isabs(path):
        raise Refusal("BUILD_CLI must be an absolute path")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise Refusal(f"build CLI {path} is not an executable file", EXIT_NO_CLI)
    return path


def brief(environ: dict[str, str]) -> str:
    path = environ.get("BUILD_BRIEF", "")
    if not path or not os.path.isabs(path):
        raise Refusal("BUILD_BRIEF must be an absolute path")
    try:
        info = os.stat(path)
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refusal(f"cannot read the brief: {type(exc).__name__}") from None
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Refusal("the brief is writable by group or others")
    if not text.strip():
        raise Refusal("the brief is empty")
    return text


def mcp_config(environ: dict[str, str]) -> str | None:
    path = environ.get("BUILD_MCP_CONFIG", "")
    if not path:
        return None
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise Refusal("BUILD_MCP_CONFIG is not an absolute path to a file")
    return path


def deadline_s(environ: dict[str, str]) -> float:
    raw = environ.get("BUILD_DEADLINE_S", "")
    if not raw:
        return float(DEADLINE_S)
    try:
        value = float(raw)
    except ValueError:
        raise Refusal("BUILD_DEADLINE_S must be a number of seconds") from None
    if value <= 0:
        raise Refusal("BUILD_DEADLINE_S must be positive")
    return value


def model(environ: dict[str, str]) -> str | None:
    value = environ.get("BUILD_MODEL", "")
    if value and not MODEL_PATTERN.fullmatch(value):
        raise Refusal("BUILD_MODEL is not a valid model name")
    return value or None


def command(path: str, *, config: str | None, chosen: str | None,
            add_dirs: list[str]) -> list[str]:
    args = [path, "-p", "--strict-mcp-config"]
    if config:
        args += ["--mcp-config", config]
    args += ["--tools", ",".join(TOOLS),
             "--allowedTools", *ALLOWED_TOOLS,
             "--disallowedTools", *DISALLOWED_TOOLS, *read_only_rules(add_dirs),
             "--permission-mode", "dontAsk",
             "--permission-prompts", "none",
             "--no-session-persistence",
             "--max-turns", str(MAX_TURNS),
             "--output-format", "stream-json", "--verbose"]
    for directory in add_dirs:
        args += ["--add-dir", directory]
    if chosen:
        args += ["--model", chosen]
    return args


def read_only_rules(add_dirs: list[str]) -> list[str]:
    """Deny rules so an extra read directory stays read-only for the file
    tools (a deny outranks the pre-approved Edit and Write)."""
    rules: list[str] = []
    for directory in add_dirs:
        for tool in ("Edit", "Write", "NotebookEdit"):
            # A rule path with one leading slash is relative to the project
            # root; two anchor it at the filesystem root.
            rules.append(f"{tool}(/{directory}/**)")
    return rules


def write_shims(directory: str, real_git: str | None) -> None:
    for name in ("gh", "unitares-github"):
        shim = Path(directory, name)
        shim.write_text(REFUSE_SHIM.format(name=name), encoding="utf-8")
        shim.chmod(0o700)
    if real_git:
        shim = Path(directory, "git")
        shim.write_text(GIT_SHIM, encoding="utf-8")
        shim.chmod(0o700)


def child_env(environ: dict[str, str], shim_dir: str, real_git: str | None) -> dict[str, str]:
    env = _sibling("claude_cli_client").claude_env(environ)
    for name in LINEAGE_PASSTHROUGH:
        if environ.get(name):
            env[name] = environ[name]
    env["PATH"] = os.pathsep.join([shim_dir, env.get("PATH", "/usr/bin:/bin")])
    env.update(GIT_ENV)
    if real_git:
        env["BUILD_LANE_REAL_GIT"] = real_git
    return env


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, document: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _stop(proc: subprocess.Popen) -> None:
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)


def _final_result(line: str, summary: dict) -> None:
    """Keep what the CLI's final ``result`` event reports."""
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    if event.get("type") == "system" and event.get("subtype") == "init":
        summary["session_id"] = event.get("session_id")
        summary["model"] = event.get("model")
    if event.get("type") == "result":
        summary["result_text"] = event.get("result")
        summary["is_error"] = bool(event.get("is_error"))
        summary["num_turns"] = event.get("num_turns")
        summary["total_cost_usd"] = event.get("total_cost_usd")
        usage = event.get("modelUsage")
        if isinstance(usage, dict) and usage:
            summary["models"] = sorted(usage)


def run(environ: dict[str, str], out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    try:
        directory = resolve_dir(environ.get("BUILD_DIR", ""), roots(environ))
    except Refusal as refusal:
        err.write(f"build_lane_client: {refusal}\n")
        return refusal.code

    state = Path(directory, STATE_DIR)
    state.mkdir(mode=0o700, exist_ok=True)
    # A result from an earlier run in this directory would read as this run's
    # completion to a caller polling for the file, so it goes before anything
    # else can refuse; a refusal from here on leaves its own result.
    for stale in ("result.json", "build.log"):
        try:
            (state / stale).unlink()
        except FileNotFoundError:
            pass
    started = _now()
    summary: dict = {"started": started, "dir": directory,
                     "parent_agent_id": environ.get("UNITARES_PARENT_AGENT_ID"),
                     "spawn_reason": environ.get("UNITARES_SPAWN_REASON")}
    _write_json(state / "started.json", summary)
    try:
        path = cli(environ)
        text = brief(environ)
        config = mcp_config(environ)
        chosen = model(environ)
        add_dirs = read_roots(environ)
        deadline_s(environ)
    except Refusal as refusal:
        err.write(f"build_lane_client: {refusal}\n")
        summary.update({"ended": _now(), "exit": refusal.code, "killed": None,
                        "refusal": str(refusal)})
        _write_json(state / "result.json", summary)
        return refusal.code
    summary["cli"] = path

    real_git = None
    for candidate in environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        git = os.path.join(candidate, "git")
        if candidate and os.path.isfile(git) and os.access(git, os.X_OK):
            real_git = git
            break

    with tempfile.TemporaryDirectory(prefix="unitares-build-lane-") as shim_dir:
        os.chmod(shim_dir, 0o700)
        write_shims(shim_dir, real_git)
        cmd = command(path, config=config, chosen=chosen, add_dirs=add_dirs)
        log = open(state / "build.log", "a", encoding="utf-8")  # noqa: SIM115 - closed below
        try:
            proc = subprocess.Popen(
                cmd, cwd=directory, env=child_env(environ, shim_dir, real_git),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as exc:
            log.close()
            err.write(f"build_lane_client: CLI spawn failed: {type(exc).__name__}\n")
            summary.update({"ended": _now(), "exit": EXIT_NO_CLI, "killed": None,
                            "refusal": f"CLI spawn failed: {type(exc).__name__}"})
            _write_json(state / "result.json", summary)
            return EXIT_NO_CLI
        assert proc.stdin is not None and proc.stdout is not None
        killed: list[str] = []

        def on_deadline() -> None:
            killed.append("deadline")
            _stop(proc)

        # The deadline is armed before the brief is sent, and the brief goes
        # from its own thread while this one drains output: a brief larger
        # than the pipe, with a CLI that prints before it reads, must not
        # deadlock the two with the deadline never armed.
        timer = threading.Timer(deadline_s(environ), on_deadline)
        timer.daemon = True
        timer.start()

        def feed() -> None:
            try:
                proc.stdin.write(text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        code = None
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                out.write(line)
                out.flush()
                _final_result(line.rstrip("\n"), summary)
            code = proc.wait()
            # The CLI is gone; anything it left behind in the group (a
            # background process with its output redirected) must not outlive
            # the build it belongs to.
            _signal_group(proc.pid, signal.SIGKILL)
        except KeyboardInterrupt:
            killed.append("signal")
            _stop(proc)
            code = proc.wait()
        finally:
            # cancel() does not stop a callback already running; its _stop()
            # may still owe the group its SIGKILL, so wait for it before the
            # result is written and the interpreter can exit.
            timer.cancel()
            timer.join(KILL_GRACE_S + 5)
            log.close()
            if "deadline" in killed:
                err.write("build_lane_client: build stopped at the client's deadline\n")
            exit_code = (code if code is not None and code >= 0
                         else 128 - (code or 0))
            if "deadline" in killed:
                exit_code = EXIT_DEADLINE
            summary.update({"ended": _now(), "exit": exit_code,
                            "killed": killed[0] if killed else None})
            _write_json(state / "result.json", summary)
    return exit_code


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
