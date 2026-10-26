"""One-shot Claude CLI client for the host adapter's launch profile.

A launch profile (``elixir/agent_orchestrator/lib/agent_orchestrator/profiles.ex``)
fixes what the orchestrator starts; the caller fills only the variables it
declares. This client is what the ``claude`` profile starts, with the
interpreter in isolated mode (``python -I``, which ignores ``PYTHON*``
variables, user site-packages and the working directory). It owns the
command line, so no caller text can become an option:

* **The prompt goes on standard input.** ``claude -p`` reads it there when no
  prompt argument is given, so a prompt that begins with ``--`` reaches the
  model as text. The orchestrator cannot write a child's standard input (an
  Erlang port cannot half-close it); this client can, and closes it, which
  is also what keeps ``claude -p`` from waiting for more input.
* **Fixed flags.** ``--safe-mode`` (no hooks, plugins, MCP servers or project
  instructions), ``--tools ""`` (none), ``--no-session-persistence`` and
  ``--output-format json``, which the host adapter's parser reads unchanged
  (``host_adapter.extract_cli_result``). The model is the only option a
  caller chooses, and only when it matches ``MODEL_PATTERN``.
* **An empty working directory.** A fresh temporary directory, refused if any
  parent holds ``.git`` or ``CLAUDE.md``, so no repository or project
  instructions are in reach.
* **An allowlisted environment.** The subscription login is found through
  ``HOME`` and ``USER``. Console-API credentials (``ANTHROPIC_API_KEY``,
  ``ANTHROPIC_AUTH_TOKEN``) and provider switches (``CLAUDE_CODE_USE_*``,
  ``ANTHROPIC_BASE_URL``) are not forwarded, so this stays a subscription
  lane by construction, as the raw adapter path blanks the same keys.

Reads ``HA_CLI`` (fixed by the profile), ``HA_PROMPT`` (required),
``HA_MODEL`` and ``HA_TIMEOUT_S`` from the environment. Passes the CLI's own
output and exit status through. Its own refusals go to standard error with
exit 2 (bad input), 124 (deadline) or 127 (CLI missing). Stdlib only.
"""

from __future__ import annotations

import getpass
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TextIO

#: The proposal's model pattern (inference-hosts-config-v0.md §2.5): no
#: leading ``-``, so a model can never read as an option.
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")

#: The orchestrator kills the child at the profile's max_runtime_ms; this is
#: the client's own deadline when the caller names none.
DEFAULT_TIMEOUT_S = 225
MAX_TIMEOUT_S = 3600

#: Seconds allowed for the CLI to exit after SIGTERM before SIGKILL.
KILL_GRACE_S = 5

FIXED_FLAGS = (
    "--safe-mode",
    "-p",
    "--tools", "",
    "--no-session-persistence",
    "--output-format", "json",
)

ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "XDG_RUNTIME_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)

#: A parent holding one of these would put a repository or project
#: instructions in reach of the CLI.
PROJECT_MARKERS = (".git", "CLAUDE.md")


class Refusal(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def claude_env(environ: dict[str, str]) -> dict[str, str]:
    """The allowlisted child environment, with ``USER`` always present."""
    env = {key: environ[key] for key in ENV_ALLOWLIST if environ.get(key)}
    if "USER" not in env:
        # claude -p finds its keychain login by username; a service manager
        # may not pass USER. getpass falls back to a uid lookup.
        try:
            env["USER"] = getpass.getuser()
        except Exception:  # noqa: BLE001 - leave it unset rather than guess
            pass
    return env


def command(environ: dict[str, str]) -> list[str]:
    cli = environ.get("HA_CLI", "")
    if not cli or not os.path.isabs(cli):
        raise Refusal("HA_CLI must be an absolute path")
    if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
        raise Refusal(f"Claude CLI {cli} is not an executable file", 127)
    model = environ.get("HA_MODEL", "")
    if model and not MODEL_PATTERN.fullmatch(model):
        raise Refusal("HA_MODEL is not a valid model name")
    return [cli, *FIXED_FLAGS, *(["--model", model] if model else [])]


def timeout_s(environ: dict[str, str]) -> int:
    raw = environ.get("HA_TIMEOUT_S", "")
    if not raw:
        return DEFAULT_TIMEOUT_S
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_TIMEOUT_S:
        raise Refusal(f"HA_TIMEOUT_S must be a whole number of seconds, 1 to {MAX_TIMEOUT_S}")
    return int(raw)


def _isolated(root: str) -> bool:
    return not any((parent / marker).exists()
                   for parent in Path(root).resolve().parents
                   for marker in PROJECT_MARKERS)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass  # the group is already gone


def _stop(proc: subprocess.Popen) -> None:
    """TERM the CLI's whole process group, then KILL it.

    The KILL goes to the group even when the CLI itself exited on the TERM:
    a descendant that ignores TERM would otherwise outlive the execution.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)


def run(environ: dict[str, str], out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    try:
        prompt = environ.get("HA_PROMPT", "")
        if not prompt.strip():
            raise Refusal("HA_PROMPT is empty")
        cmd = command(environ)
        budget = timeout_s(environ)
    except Refusal as refusal:
        err.write(f"claude_cli_client: {refusal}\n")
        return refusal.code

    with tempfile.TemporaryDirectory(prefix="unitares-claude-lane-") as root:
        if not _isolated(root):
            err.write("claude_cli_client: the temporary directory is inside a project "
                      "(a parent holds .git or CLAUDE.md)\n")
            return 2
        try:
            proc = subprocess.Popen(
                cmd, cwd=root, env=claude_env(environ),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as exc:
            err.write(f"claude_cli_client: Claude CLI spawn failed: {type(exc).__name__}\n")
            return 127
        try:
            stdout, stderr = proc.communicate(prompt, timeout=budget)
        except subprocess.TimeoutExpired:
            _stop(proc)
            try:
                stdout, stderr = proc.communicate(timeout=KILL_GRACE_S)
            except Exception:  # noqa: BLE001 - best-effort reap after a kill
                stdout, stderr = "", ""
            out.write(stdout or "")
            err.write(stderr or "")
            err.write(f"claude_cli_client: Claude CLI timed out after {budget}s\n")
            return 124
    out.write(stdout)
    out.flush()
    err.write(stderr)
    err.flush()
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
