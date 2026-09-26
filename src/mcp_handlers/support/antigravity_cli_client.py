"""One-shot Antigravity (agy) client for the host adapter.

The host adapter executes this file inside the orchestrator child, the same way
it runs ``codex_app_server_client.py``. agy needs three things a plain
``sh -c 'exec agy ...'`` cannot give it:

* **An empty workspace.** agy loads a working directory's ``.agents/`` hooks,
  rules and project permissions, so it never runs in a repository: its cwd is a
  fresh temporary directory, refused if any parent holds ``.git``/``.agents``.
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

ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
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
DENIED_MARK = 'required the "command" permission'
DEFAULT_TIMEOUT_S = 240.0


def agy_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {k: source[k] for k in ENV_ALLOWLIST if k in source}


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
    prompt = environ.get("HA_PROMPT", "") + TEXT_ONLY
    if len(prompt.encode("utf-8")) > PROMPT_BYTES_LIMIT:
        return _emit({"status": "ERROR",
                      "error": "prompt exceeds the Antigravity argv size limit"}, stream)
    try:
        budget = float(environ.get("HA_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        budget = DEFAULT_TIMEOUT_S
    deadline = time.monotonic() + max(1.0, budget)
    model = environ.get("HA_MODEL", "").strip()
    flags = ["--mode", "plan", "--sandbox", "--output-format", "json",
             *(["--model", model] if model else [])]
    env = agy_env(environ)
    resumes: dict[str, int] = {}
    usage: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="consult-agy-") as workspace:
        if any((d / m).exists() for d in Path(workspace).resolve().parents
               for m in (".git", ".agents")):
            return _emit({"status": "ERROR", "error":
                          "Antigravity workspace is not isolated (a parent holds .git/.agents)"},
                         stream)
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
