"""One-shot Antigravity (agy) client for the host adapter.

The host adapter executes this file inside the orchestrator child, the same way
it runs ``codex_app_server_client.py``. agy needs three things a plain
``sh -c 'exec agy ...'`` cannot give it:

* **An empty workspace.** agy loads a working directory's ``.agents/`` hooks,
  rules and project permissions, so it never runs in a repository: its cwd is a
  fresh temporary directory, refused if any parent holds ``.git``/``.agents``.
* **An isolated home.** agy reads the operator's user config from
  ``$HOME/.gemini``: standing permission grants (file reads under the home
  directory, shell commands, tool calls) and MCP servers (the governance
  server itself). Headless mode only auto-denies what has no standing grant,
  so a caller's prompt could otherwise read secrets or make governed writes.
  agy gets a fresh temporary HOME holding only a link to the macOS login
  keychain, where its subscription login lives, so it starts with no grants
  and no MCP servers.
* **Plan mode, and no slash commands.** ``--mode plan --sandbox`` stays on.
  ``--disable-slash-commands`` is NOT used: agy 1.2.11 warns "--mode plan has
  no effect while slash command expansion is disabled", so that flag silently
  switched plan mode off. A prompt is expanded only when it starts with ``/``,
  so guard_prompt() puts a fixed first line in front of the caller's text.
* **An allowlisted environment.** The prompt is caller text, and the
  orchestrator child inherits the service environment (bearer tokens,
  ``UNITARES_*``). An injected "print your environment" must find nothing to
  echo, so agy gets only what a CLI needs to find its home, locale, proxy and
  CA bundle. Its subscription login lives in the system keyring, not in the
  environment, and Google API credentials (``GEMINI_API_KEY``,
  ``GOOGLE_APPLICATION_CREDENTIALS``, ...) are deliberately NOT forwarded: this
  is a subscription lane by construction, the way the host adapter blanks
  ``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY``, so a key a shell exported into
  the service environment cannot silently move it onto metered billing.
* **Resume on a stall.** agy is an agent, not a completion endpoint. Headless
  mode auto-denies any shell command it reaches for, and the turn then ends
  SUCCESS with an EMPTY response; resuming the same conversation with "no
  tools, answer now" gets the answer. A turn cut off at the output-token limit
  is resumed once; agy tends to re-reason and hit the limit again, so more
  tries only burn budget. Measured on 15 PR reviews on 2026-09-25 (10 denied,
  3 truncated) and mirrored from ``scripts/dev/review_gate.py``.

Prints exactly one JSON line with schema ``unitares.antigravity_cli_result.v1``
and exits 0 only when agy delivered a non-empty SUCCESS response. Stdlib only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


RESULT_SCHEMA = "unitares.antigravity_cli_result.v1"

# One argv element: Linux caps it at 128 KiB.
PROMPT_BYTES_LIMIT = 120_000

# No XDG_CONFIG/DATA/CACHE_HOME: they point into the operator's real home,
# which is exactly the config agy must not load. XDG_RUNTIME_DIR stays: it is
# /run/user/<uid>, holds no config, and is where a Linux session bus (and so a
# Secret Service keyring login) is found. HOME is replaced by isolated_home().
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "XDG_RUNTIME_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)

# Saying up front that the prompt is self-contained and the answer is text
# makes agy reply in the turn instead of reaching for a tool.
TEXT_ONLY = (
    "\n\nDo not run commands, read files, or use any tools. Everything you need "
    "is above. Answer directly with the JSON object as your final text reply."
)

RESUME_LIMITS = {"denied": 2, "truncated": 1}
RESUME_PROMPTS = {
    "denied": (
        "Your command was denied: this session has no tools and cannot run "
        "commands. Do not try any tool again. Answer now from what is already "
        "in this conversation, as the JSON object the request asked for."
    ),
    "truncated": (
        "Your previous answer was cut off by the output limit before it was "
        "delivered in full. Send your complete final answer again from the "
        "beginning, as the JSON object the request asked for. Do not "
        "investigate further; write it out."
    ),
}
#: A resume needs at least this much budget left to be worth starting.
RESUME_MIN_SECONDS = 5.0
#: agy's headless auto-denial message, for any tool ("command", "read_file",
#: ...). Matching one tool name missed denied file reads.
DENIED_MARK = "permission that headless mode cannot prompt for"
DEFAULT_TIMEOUT_S = 240.0


#: Flags every agy launch carries, first turn and resumes alike.
AGY_FLAGS = ("--mode", "plan", "--sandbox", "--output-format", "json")

#: First line of every prompt built from caller text, so the prompt can never
#: start with "/" and be expanded as a slash command or skill.
PROMPT_GUARD = "Request (plain text; not a command):\n\n"


def guard_prompt(text: str) -> str:
    return PROMPT_GUARD + text


def isolated_home(root: str, real_home: str | None) -> str:
    """A fresh HOME under ``root``: empty but for the login keychain.

    agy's subscription login is in the macOS login keychain, which is found
    through ``$HOME/Library/Keychains``; linking that one directory keeps the
    login while ``.gemini`` (grants, MCP servers, history) starts empty. On a
    system without it, agy gets an empty home and must be logged in some other
    way that does not live in the home directory.
    """
    home = os.path.join(root, "home")
    os.mkdir(home, 0o700)
    if real_home:
        keychains = Path(real_home, "Library", "Keychains")
        if keychains.is_dir():
            os.mkdir(os.path.join(home, "Library"), 0o700)
            os.symlink(keychains, os.path.join(home, "Library", "Keychains"))
    return home


def agy_env(environ: dict[str, str] | None = None, *, home: str | None = None
            ) -> dict[str, str]:
    source = os.environ if environ is None else environ
    env = {k: source[k] for k in ENV_ALLOWLIST if k in source}
    if home is not None:
        env["HOME"] = home
    return env


def parse_output(stdout: str) -> dict[str, Any]:
    """agy -p --output-format json prints one object; tolerate leading noise."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def stall(data: dict[str, Any], stderr: str) -> str | None:
    """"truncated", "denied" or None; see the module docstring."""
    if data.get("status") != "SUCCESS":
        if "output token limit" in str(data.get("error") or "").lower():
            return "truncated"
        return None
    if str(data.get("response") or "").strip():
        return None
    denied = data.get("denied_actions")
    if DENIED_MARK in stderr or (isinstance(denied, list) and denied):
        return "denied"
    return None


def add_usage(total: dict[str, Any], usage: Any) -> None:
    """Sum one turn's integer usage fields into ``total``; a resumed call
    spends every turn's tokens, not only the last one's."""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def _run(cmd: list[str], *, cwd: str, env: dict[str, str], timeout_s: float
         ) -> tuple[int | None, str, str]:
    """(exit status or None on timeout, stdout, stderr). Kills the whole group
    on timeout: agy's sandbox children can hold stdout open."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort reap after a kill
            out, err = "", ""
        return None, out or "", err or ""


def _emit(result: dict[str, Any], stream: Any) -> int:
    result = {"schema": RESULT_SCHEMA, **result}
    stream.write(json.dumps(result, separators=(",", ":")) + "\n")
    stream.flush()
    return 0 if result.get("status") == "SUCCESS" and not result.get("error") else 1


def run(environ: dict[str, str], stream: Any = sys.stdout) -> int:
    cli = environ.get("HA_CLI", "").strip()
    if not cli:
        return _emit({"status": "ERROR", "error": "HA_CLI unset"}, stream)
    prompt = guard_prompt(environ.get("HA_PROMPT", "") + TEXT_ONLY)
    if len(prompt.encode("utf-8")) > PROMPT_BYTES_LIMIT:
        return _emit({"status": "ERROR",
                      "error": "prompt exceeds the Antigravity argv size limit"}, stream)
    try:
        budget = float(environ.get("HA_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        budget = DEFAULT_TIMEOUT_S
    deadline = time.monotonic() + max(1.0, budget)
    model = environ.get("HA_MODEL", "").strip()
    flags = [*AGY_FLAGS, *(["--model", model] if model else [])]
    resumes: dict[str, int] = {}
    usage: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="consult-agy-") as root:
        if any((d / m).exists() for d in Path(root).resolve().parents
               for m in (".git", ".agents")):
            return _emit({"status": "ERROR", "error":
                          "Antigravity workspace is not isolated (a parent holds .git/.agents)"},
                         stream)
        workspace = os.path.join(root, "workspace")
        os.mkdir(workspace, 0o700)
        env = agy_env(environ, home=isolated_home(root, environ.get("HOME")))
        cmd = [cli, "-p", prompt, *flags]
        while True:
            try:
                code, out, err = _run(cmd, cwd=workspace, env=env,
                                      timeout_s=max(1.0, deadline - time.monotonic()))
            except OSError as exc:
                return _emit({"status": "ERROR",
                              "error": f"Antigravity CLI spawn failed: {type(exc).__name__}",
                              "resumes": resumes}, stream)
            if code is None:
                return _emit({"status": "ERROR", "error": "Antigravity CLI timed out",
                              "resumes": resumes}, stream)
            data = parse_output(out)
            add_usage(usage, data.get("usage"))
            kind = stall(data, err)
            cid = data.get("conversation_id")
            cid = cid if isinstance(cid, str) and cid else None
            if (kind is None or cid is None
                    or resumes.get(kind, 0) >= RESUME_LIMITS[kind]
                    or deadline - time.monotonic() < RESUME_MIN_SECONDS):
                break
            resumes[kind] = resumes.get(kind, 0) + 1
            cmd = [cli, "-p", RESUME_PROMPTS[kind], "--conversation", cid, *flags]

    response = data.get("response") if isinstance(data.get("response"), str) else ""
    result: dict[str, Any] = {
        "status": data.get("status") or "ERROR",
        "response": response,
        "usage": usage,
        "conversation_id": cid,
        "exit_status": code,
        "resumes": resumes,
    }
    error = None
    if code != 0:
        error = f"Antigravity CLI exited {code}"
    elif kind == "denied":
        error = f"no answer after a denied command after {resumes.get(kind, 0)} resume(s)"
    elif kind == "truncated":
        error = f"output limit not recovered after {resumes.get(kind, 0)} resume(s)"
    elif result["status"] != "SUCCESS":
        error = str(data.get("error") or "Antigravity CLI reported no successful result")[:500]
    elif not response.strip():
        error = "Antigravity CLI returned an empty response"
    if error:
        result["error"] = error
    return _emit(result, stream)


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
