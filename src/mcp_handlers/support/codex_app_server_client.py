"""One-shot Codex app-server client for host-adapter model provenance.

The host adapter executes this file inside the orchestrator child.  It speaks
the documented JSONL app-server protocol over stdio, emits one normalized
result envelope, and deliberately distinguishes failures before and after a
turn request is sent.  Only the former are safe to retry through ``codex exec``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO


RESULT_SCHEMA = "unitares.codex_app_server_result.v1"
FALLBACK_SAFE_EXIT = 75
_CLIENT_NAME = "unitares_host_adapter"


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
            "sandbox": sandbox,
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


def _spawn(cli: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            cli,
            "app-server",
            "--stdio",
            "-c",
            "mcp_servers={}",
            "-c",
            "hooks={}",
        ],
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

    process: subprocess.Popen[str] | None = None
    try:
        process = _spawn(cli)
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
            cwd=os.getcwd(),
        )
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 0 if result["finish_reason"] == "completed" and result["text"] else 1
    except AppServerClientError as exc:
        print(f"Codex app-server instrumentation failed: {exc}", file=sys.stderr)
        return FALLBACK_SAFE_EXIT if exc.fallback_safe else 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Codex app-server unavailable: {exc}", file=sys.stderr)
        return FALLBACK_SAFE_EXIT
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
