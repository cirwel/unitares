"""One-shot Codex app-server client for host-adapter model provenance.

The host adapter executes this file inside the orchestrator child.  It speaks
the documented JSONL app-server protocol over stdio, emits one normalized
result envelope, and deliberately distinguishes failures before and after a
turn request is sent.  Only the former are safe to retry through ``codex exec``.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, TextIO


RESULT_SCHEMA = "unitares.codex_app_server_result.v1"
FALLBACK_SAFE_EXIT = 75
_CLIENT_NAME = "unitares_host_adapter"

# Codex's legacy ``read-only`` sandbox denies writes but reads the whole disk,
# so a prompt (or text injected into one) could have the agent read the
# operator's credentials and return them in its answer; that was verified on
# 2026-10-15 with a canary file. Every UNITARES codex lane therefore runs:
#
# - with lane_config_overrides(): a permission profile that denies reads
#   everywhere except the platform's minimal runtime paths and the directories
#   holding the codex binary (its sandbox helper re-executes it), with writes
#   denied; commands that see only an allow-list of core environment
#   variables; and no hooks, plugins, apps, browser or computer use;
# - in an empty private working directory (lane_workdir()), so no project
#   ``.codex/config.toml`` or project instructions can load;
# - with every MCP server the loaded configuration still enables switched off
#   by name (mcp_disable_overrides()), refusing to run if any stays enabled.
#
# ``-c`` deep-merges tables into whatever configuration codex loads, which is
# why none of this is done with a table: ``mcp_servers={}`` and ``hooks={}``
# removed nothing, and a fixed profile name could be pre-populated by the
# operator's config with grants of its own, so each launch takes a fresh one.
PERMISSION_PROFILE_PREFIX = "unitares_lane_"

# What a command the agent runs may see; codex matches these as globs.
_COMMAND_ENV_NAMES = ("HOME", "LANG", "LC_*", "LOGNAME", "PATH", "SHELL", "TERM", "TMPDIR", "USER")

_DISABLED_FEATURES = (
    "apps",
    "hooks",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "in_app_browser",
    "plugins",
)


def codex_readable_roots(cli: str) -> list[str]:
    """Directories the sandboxed helper must read to re-execute ``cli``: the
    one it is invoked from and, for a symlinked install, the real binary's.

    An npm install resolves ``codex`` to a JavaScript launcher, while the
    native executable the sandbox re-executes ships in a sibling platform
    package (``@openai/codex-<platform>``), so the ``@openai`` scope is added."""
    real = os.path.realpath(cli)
    candidates = [os.path.dirname(os.path.abspath(cli)), os.path.dirname(real)]
    if os.path.splitext(real)[1] in (".js", ".mjs", ".cjs"):
        parts = real.split(os.sep)
        if "@openai" in parts:
            index = len(parts) - 1 - parts[::-1].index("@openai")
            candidates.append(os.sep.join(parts[: index + 1]))
    roots: list[str] = []
    for directory in candidates:
        if directory and directory not in roots:
            roots.append(directory)
    return roots


def admin_config_paths() -> list[str]:
    """Codex configuration that loads whatever a lane passes: managed layers
    take precedence over ``-c`` overrides, a legacy ``sandbox_mode`` in the
    system file overrides a permission profile, and requirements can make
    codex fall back from a requested value. Locations:
    https://developers.openai.com/codex/enterprise/managed-configuration"""
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        user = os.environ.get("USER") or getpass.getuser()
    except Exception:  # noqa: BLE001 - no name: the per-user path is skipped below
        user = ""
    paths = [
        "/etc/codex/config.toml",
        "/etc/codex/managed_config.toml",
        "/etc/codex/requirements.toml",
        os.path.join(home, "managed_config.toml"),
        # macOS MDM managed preferences (config_toml_base64 / requirements_toml_base64).
        "/Library/Managed Preferences/com.openai.codex.plist",
    ]
    if user:
        paths.append(f"/Library/Managed Preferences/{user}/com.openai.codex.plist")
    return paths


def admin_config_conflict(paths: list[str] | None = None) -> str | None:
    """The first administrator codex configuration that exists, or None. A
    self-hosted machine does not normally have one, and checking contents
    would mean parsing TOML and MDM payloads exactly; a parser gap would be a
    confinement gap, so existence alone refuses the lane."""
    for path in admin_config_paths() if paths is None else paths:
        if os.path.exists(path):
            return path
    return None


def new_profile_name() -> str:
    """A permission-profile name no configuration can have pre-populated."""
    return PERMISSION_PROFILE_PREFIX + secrets.token_hex(8)


def lane_config_overrides(cli: str, profile: str | None = None) -> list[str]:
    """The ``-c key=value`` overrides every codex lane runs with, in order.

    Pass each as its own ``-c`` argument. They replace ``--sandbox read-only``,
    which would select the legacy sandbox and ignore the profile. The list has
    the same length for every ``cli``, so a shell template can name each slot.
    ``profile`` defaults to a fresh name (``new_profile_name``)."""
    profile = profile or new_profile_name()
    entries = {":root": "deny", ":minimal": "read"}
    for root in codex_readable_roots(cli):
        entries[root] = "read"
    table = ", ".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in entries.items())
    return [
        f'default_permissions="{profile}"',
        f"permissions.{profile}.filesystem={{{table}}}",
        # Lists replace rather than merge, and ``include_only`` applies after
        # an operator's ``shell_environment_policy.set``, so this allow-list is
        # what bounds the agent's environment (an ``inherit`` alone left a
        # configured ``set`` variable reaching commands; verified 2026-10-15).
        'shell_environment_policy.inherit="core"',
        f"shell_environment_policy.include_only={json.dumps(list(_COMMAND_ENV_NAMES))}",
        *(f"features.{name}=false" for name in _DISABLED_FEATURES),
    ]


LANE_CONFIG_OVERRIDE_COUNT = len(lane_config_overrides("codex", "x"))

_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _enabled_mcp_servers(cli: str, overrides: list[str], cwd: str) -> list[str]:
    args = [cli, "mcp", "list", "--json"]
    for override in overrides:
        args.extend(["-c", override])
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Fail closed: the lane refuses rather than falling back to exec.
        raise ValueError(f"codex mcp list could not run: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"codex mcp list exited {proc.returncode}")
    servers = json.loads(proc.stdout)
    if not isinstance(servers, list):
        raise ValueError("codex mcp list returned no server list")
    return [str(s.get("name")) for s in servers if isinstance(s, dict) and s.get("enabled")]


def mcp_disable_overrides(cli: str, overrides: list[str], cwd: str) -> list[str]:
    """``mcp_servers.<name>.enabled=false`` for every MCP server the loaded
    configuration still enables under ``overrides``, as codex itself reports
    them. Raises ValueError if the list cannot be read, a name cannot be
    addressed through ``-c``, or any server is still enabled afterwards: the
    lane fails rather than run with a server."""
    disables = []
    for name in _enabled_mcp_servers(cli, overrides, cwd):
        if not _SERVER_NAME.fullmatch(name):
            raise ValueError(f"cannot disable codex MCP server {name!r} through -c")
        disables.append(f"mcp_servers.{name}.enabled=false")
    still = _enabled_mcp_servers(cli, overrides + disables, cwd)
    if still:
        raise ValueError(f"codex MCP servers still enabled: {', '.join(still)}")
    return disables


_CONFINEMENT_VERIFIED: dict[tuple[str, float], bool] = {}


def confinement_verified(cli: str) -> bool:
    """Whether this codex actually enforces the lane's permission profile.

    The confinement rests on configuration keys an older codex may silently
    ignore, so before a lane runs, ``codex sandbox`` (no model call) must fail
    to read a probe file outside the profile's readable paths and must still
    run a harmless command under the same overrides. A codex that cannot run
    the probe at all is unverified too. A success is cached per process,
    resolved binary and mtime, so an upgrade is probed again; the per-call
    app-server client is a fresh process and probes each time (~0.1s)."""
    real = os.path.realpath(cli)
    try:
        stamp = (real, os.stat(real).st_mtime)
    except OSError:
        return False
    if stamp in _CONFINEMENT_VERIFIED:
        return _CONFINEMENT_VERIFIED[stamp]
    # Under the home directory, which the profile denies (":root" deny); the
    # temporary directory may be readable through ":minimal".
    base = os.path.expanduser("~/.local/state/unitares")
    verified = False
    probe_dir = None
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        probe_dir = tempfile.mkdtemp(prefix="codex-probe-", dir=base)
        token = "unitares-confinement-probe-" + secrets.token_hex(8)
        probe = os.path.join(probe_dir, "probe.txt")
        with open(probe, "w") as handle:
            handle.write(token)
        overrides: list[str] = []
        for override in lane_config_overrides(cli):
            overrides.extend(["-c", override])
        run = [cli, "sandbox", *overrides, "--"]
        # In an empty directory, as the lane itself runs, so no project
        # configuration in the caller's directory affects the probe.
        workdir = os.path.join(probe_dir, "work")
        os.mkdir(workdir, 0o700)
        harmless = subprocess.run(run + ["/usr/bin/true"], capture_output=True, text=True,
                                  cwd=workdir, stdin=subprocess.DEVNULL, timeout=20)
        reading = subprocess.run(run + ["/bin/cat", probe], capture_output=True, text=True,
                                 cwd=workdir, stdin=subprocess.DEVNULL, timeout=20)
        verified = (harmless.returncode == 0 and reading.returncode != 0
                    and token not in reading.stdout + reading.stderr)
    except (OSError, subprocess.SubprocessError):
        verified = False
    finally:
        if probe_dir:
            shutil.rmtree(probe_dir, ignore_errors=True)
    if verified:
        # Only a success is cached: a failure may be transient (a timeout),
        # and the next call probes again.
        _CONFINEMENT_VERIFIED[stamp] = True
    return verified


def lane_workdir() -> str:
    """A fresh, empty, private working directory outside any repository, so no
    project configuration or instructions load. The caller removes it."""
    return tempfile.mkdtemp(prefix="unitares-codex-lane-")


def lane_config_args(cli: str, cwd: str) -> list[str]:
    """The full ``-c`` argument list for a lane that loads the operator's
    configuration (the app-server, the dialectic reviewer), run in ``cwd``."""
    overrides = lane_config_overrides(cli)
    overrides += mcp_disable_overrides(cli, overrides, cwd)
    args: list[str] = []
    for override in overrides:
        args.extend(["-c", override])
    return args


class AppServerClientError(RuntimeError):
    """Protocol failure carrying whether a second inference is still safe."""

    def __init__(self, message: str, *, fallback_safe: bool) -> None:
        super().__init__(message)
        self.fallback_safe = fallback_safe


@dataclass
class _ObservedRun:
    thread_id: str | None = None
    turn_id: str | None = None
    model_selected: str | None = None
    model_effective: str | None = None
    model_provider: str | None = None
    service_tier: str | None = None
    provider_user_agent: str | None = None
    messages: list[str] = field(default_factory=list)
    final_messages: list[str] = field(default_factory=list)
    model_reroutes: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn_status: str | None = None


def _send(stdin: TextIO, message: dict[str, Any]) -> None:
    stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    stdin.flush()


def _read(stdout: TextIO) -> dict[str, Any]:
    line = stdout.readline()
    if line == "":
        raise RuntimeError("Codex app-server closed stdout")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex app-server emitted non-JSON stdout") from exc
    if not isinstance(message, dict):
        raise RuntimeError("Codex app-server emitted a non-object message")
    return message


def _message_matches_run(params: dict[str, Any], observed: _ObservedRun) -> bool:
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    if observed.thread_id and thread_id and thread_id != observed.thread_id:
        return False
    if observed.turn_id and turn_id and turn_id != observed.turn_id:
        return False
    return True


def _observe(message: dict[str, Any], observed: _ObservedRun) -> None:
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return
    if not _message_matches_run(params, observed):
        return

    if method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                observed.messages.append(text)
                if item.get("phase") == "final_answer":
                    observed.final_messages.append(text)
        return

    if method == "model/rerouted":
        reroute = {
            key: params[key]
            for key in ("threadId", "turnId", "fromModel", "toModel", "reason")
            if key in params
        }
        observed.model_reroutes.append(reroute)
        to_model = params.get("toModel")
        if isinstance(to_model, str) and to_model:
            observed.model_effective = to_model
        return

    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        if isinstance(token_usage, dict):
            last = token_usage.get("last")
            if isinstance(last, dict):
                observed.token_usage = last
        return

    if method in {"warning", "configWarning"}:
        warning = params.get("message") or params.get("summary")
        if isinstance(warning, str) and warning:
            observed.warnings.append(warning)
        return

    if method == "error":
        error = params.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error:
            observed.errors.append(error)
        return

    if method == "turn/started":
        turn = params.get("turn")
        if isinstance(turn, dict) and not observed.turn_id:
            turn_id = turn.get("id")
            if isinstance(turn_id, str):
                observed.turn_id = turn_id
        return

    if method == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, dict):
            return
        turn_id = turn.get("id")
        if observed.turn_id and turn_id and turn_id != observed.turn_id:
            return
        if isinstance(turn_id, str):
            observed.turn_id = turn_id
        status = turn.get("status")
        if isinstance(status, str):
            observed.turn_status = status
        error = turn.get("error")
        if isinstance(error, dict):
            error_message = error.get("message")
            if isinstance(error_message, str) and error_message:
                observed.errors.append(error_message)


def _response(
    stdin: TextIO,
    stdout: TextIO,
    request_id: int,
    observed: _ObservedRun,
) -> dict[str, Any]:
    while True:
        message = _read(stdout)
        if message.get("id") == request_id:
            error = message.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, sort_keys=True)
                raise RuntimeError(f"app-server request {request_id} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"app-server request {request_id} returned no result")
            return result

        # The one-shot advisory lane disables configured MCP servers and hooks
        # and sets approvalPolicy=never. A server-initiated request is therefore
        # unexpected. Fail closed rather than inventing a response contract.
        if "id" in message and isinstance(message.get("method"), str):
            raise RuntimeError(
                f"unexpected app-server request: {message['method']}"
            )
        _observe(message, observed)


def _normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    fields = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
        "totalTokens": "total_tokens",
    }
    return {
        normalized: value
        for provider, normalized in fields.items()
        if isinstance((value := raw.get(provider)), int)
    }


def _cli_version(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    match = re.search(r"/([0-9]+(?:\.[0-9]+){2})(?:\s|$)", user_agent)
    return match.group(1) if match else None


def _result(observed: _ObservedRun) -> dict[str, Any]:
    usage = _normalize_usage(observed.token_usage)
    tokens_used = usage.get("total_tokens")
    if tokens_used is None:
        tokens_used = sum(
            usage.get(key, 0) for key in ("input_tokens", "output_tokens")
        )

    model_effective = observed.model_effective or observed.model_selected
    warnings = list(dict.fromkeys(observed.warnings))
    if not model_effective:
        warnings.append("Codex app-server did not report a model identifier")

    final_messages = observed.final_messages or observed.messages
    return {
        "schema": RESULT_SCHEMA,
        "text": final_messages[-1] if final_messages else "",
        "thread_id": observed.thread_id,
        "turn_id": observed.turn_id,
        "model_selected": observed.model_selected,
        "model_effective": model_effective,
        "model_used": model_effective,
        "models_used": [model_effective] if model_effective else [],
        "model_provider": observed.model_provider,
        "service_tier": observed.service_tier,
        "model_reroutes": observed.model_reroutes,
        "model_reporting_status": (
            "reported_by_app_server" if model_effective else "unavailable"
        ),
        "provider_usage": usage,
        "tokens_used": tokens_used,
        "finish_reason": observed.turn_status,
        "provider_user_agent": observed.provider_user_agent,
        "codex_cli_version": _cli_version(observed.provider_user_agent),
        "warnings": warnings,
        "errors": list(dict.fromkeys(observed.errors)),
    }


def run_protocol(
    stdin: TextIO,
    stdout: TextIO,
    *,
    prompt: str,
    model: str | None,
    sandbox: str,
    cwd: str,
) -> dict[str, Any]:
    """Run one app-server turn and return a normalized result envelope."""
    observed = _ObservedRun()
    turn_request_sent = False
    try:
        _send(
            stdin,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": _CLIENT_NAME,
                        "title": "UNITARES host adapter",
                        "version": "1",
                    }
                },
            },
        )
        initialized = _response(stdin, stdout, 0, observed)
        user_agent = initialized.get("userAgent")
        if isinstance(user_agent, str):
            observed.provider_user_agent = user_agent
        _send(stdin, {"method": "initialized", "params": {}})

        thread_params: dict[str, Any] = {
            "approvalPolicy": "never",
            "cwd": cwd,
            "ephemeral": True,
            # No "sandbox" here: it would select the legacy whole-disk-read
            # sandbox over the permission profile the server was started with.
            "serviceName": _CLIENT_NAME,
        }
        if model:
            thread_params["model"] = model
        _send(
            stdin,
            {"method": "thread/start", "id": 1, "params": thread_params},
        )
        thread_started = _response(stdin, stdout, 1, observed)
        thread = thread_started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise RuntimeError("thread/start returned no thread id")
        observed.thread_id = thread["id"]
        selected = thread_started.get("model")
        if isinstance(selected, str) and selected:
            observed.model_selected = selected
            observed.model_effective = selected
        provider = thread_started.get("modelProvider")
        if isinstance(provider, str) and provider:
            observed.model_provider = provider
        tier = thread_started.get("serviceTier")
        if isinstance(tier, str) and tier:
            observed.service_tier = tier

        turn_request_sent = True
        _send(
            stdin,
            {
                "method": "turn/start",
                "id": 2,
                "params": {
                    "threadId": observed.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            },
        )
        turn_started = _response(stdin, stdout, 2, observed)
        turn = turn_started.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise RuntimeError("turn/start returned no turn id")
        observed.turn_id = turn["id"]

        while observed.turn_status is None:
            message = _read(stdout)
            if "id" in message and isinstance(message.get("method"), str):
                raise RuntimeError(
                    f"unexpected app-server request: {message['method']}"
                )
            _observe(message, observed)
        return _result(observed)
    except Exception as exc:
        raise AppServerClientError(
            str(exc),
            fallback_safe=not turn_request_sent,
        ) from exc


def _spawn(cli: str, cwd: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            cli,
            "app-server",
            "--stdio",
            *lane_config_args(cli, cwd),
        ],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def main() -> int:
    cli = os.environ.get("HA_CLI", "").strip()
    prompt = os.environ.get("HA_PROMPT", "")
    model = os.environ.get("HA_MODEL", "").strip() or None
    sandbox = os.environ.get("HA_SANDBOX", "read-only").strip() or "read-only"
    if not cli or not prompt:
        print("Codex app-server client missing HA_CLI or HA_PROMPT", file=sys.stderr)
        return FALLBACK_SAFE_EXIT
    if sandbox != "read-only":
        # The permission profile implements read-only only. Refuse rather than
        # widen; the exec fallback refuses the same value.
        print(f"Codex host lane supports only the read-only sandbox, not {sandbox!r}", file=sys.stderr)
        return 1
    conflict = admin_config_conflict()
    if conflict:
        print(
            f"Codex lane refused: {conflict} is administrator codex configuration "
            "that can override the lane's permission profile",
            file=sys.stderr,
        )
        return 1
    if not confinement_verified(cli):
        print("Codex lane refused: this codex does not enforce the lane's permission "
              "profile (codex sandbox probe)", file=sys.stderr)
        return 1

    process: subprocess.Popen[str] | None = None
    workdir = lane_workdir()
    try:
        process = _spawn(cli, workdir)
        if process.stdin is None or process.stdout is None:
            raise AppServerClientError(
                "Codex app-server pipes unavailable",
                fallback_safe=True,
            )
        result = run_protocol(
            process.stdin,
            process.stdout,
            prompt=prompt,
            model=model,
            sandbox=sandbox,
            cwd=workdir,
        )
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 0 if result["finish_reason"] == "completed" and result["text"] else 1
    except AppServerClientError as exc:
        print(f"Codex app-server instrumentation failed: {exc}", file=sys.stderr)
        return FALLBACK_SAFE_EXIT if exc.fallback_safe else 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Codex app-server unavailable: {exc}", file=sys.stderr)
        return FALLBACK_SAFE_EXIT
    except ValueError as exc:
        # The loaded configuration could not be neutralised (see
        # mcp_disable_overrides). Not fallback-safe either: the lane fails
        # closed instead of retrying through exec.
        print(f"Codex lane refused: {exc}", file=sys.stderr)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
