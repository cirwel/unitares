"""Safe subscription-CLI backends for the standalone dialectic reviewer."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.mcp_handlers.support.host_adapter import (
    extract_cli_result,
    resolve_host_cli,
)


@dataclass(frozen=True)
class HostReviewResult:
    """A verdict candidate plus non-secret execution provenance."""

    text: Optional[str]
    host_id: str
    model_requested: Optional[str] = None
    model_used: Optional[str] = None
    models_used: list[str] = field(default_factory=list)
    tokens_used: int = 0
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    finish_reason: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def provenance(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "backend": "claude",
            "model_requested": self.model_requested,
            "model_used": self.model_used,
            "models_used": list(self.models_used),
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "warnings": list(self.warnings),
        }


def _extract_verdict(text: str) -> Optional[str]:
    """Return the last parseable object carrying an ``agrees`` key."""
    decoder = json.JSONDecoder()
    position = 0
    last: Optional[str] = None
    while True:
        start = text.find("{", position)
        if start < 0:
            return last
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(value, dict) and "agrees" in value:
            last = text[start : start + consumed]
        position = start + consumed


async def call_claude_backend(prompt: str) -> HostReviewResult:
    """Run Claude safely and return exact provider-reported model provenance.

    The CLI inherits the operator's subscription authentication, but not its
    custom instructions, hooks, tools, or conversation state. Prompt and model
    are passed through quoted environment variables, never interpolated into
    shell source.
    """
    host_id = "claude:host-adapter"
    cli_path = resolve_host_cli(host_id)
    model = os.getenv("UNITARES_DIALECTIC_CLAUDE_MODEL", "").strip() or None
    if cli_path is None:
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            error="Claude CLI not found or not executable",
        )

    try:
        timeout_s = float(os.getenv("UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S", "420"))
    except (TypeError, ValueError):
        timeout_s = 420.0
    timeout_s = max(1.0, timeout_s)
    command = (
        'if [ -n "$DR_MODEL" ]; then '
        'exec "$DR_CLI" --safe-mode -p "$DR_PROMPT" --tools "" '
        '--no-session-persistence --output-format json --model "$DR_MODEL" </dev/null; '
        'else exec "$DR_CLI" --safe-mode -p "$DR_PROMPT" --tools "" '
        '--no-session-persistence --output-format json </dev/null; fi'
    )
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            env={
                **os.environ,
                "DR_CLI": cli_path,
                "DR_PROMPT": prompt,
                "DR_MODEL": model or "",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - selected-host failure falls back locally
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"Claude CLI spawn failed: {type(exc).__name__}",
        )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"Claude CLI exceeded {timeout_s:g}s timeout",
        )
    except Exception as exc:  # noqa: BLE001 - selected-host failure falls back locally
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"Claude CLI communication failed: {type(exc).__name__}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=latency_ms,
            error=f"Claude CLI exited {proc.returncode}",
        )

    output_lines = stdout.decode(errors="replace").splitlines()
    text, metadata = extract_cli_result(
        output_lines,
        family="anthropic_claude",
    )
    if metadata.get("provider_is_error"):
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            model_used=metadata.get("model_used"),
            models_used=list(metadata.get("models_used") or []),
            tokens_used=int(metadata.get("tokens_used") or 0),
            cost_usd=metadata.get("cost_usd"),
            latency_ms=latency_ms,
            finish_reason=metadata.get("finish_reason"),
            warnings=list(metadata.get("warnings") or []),
            error="Claude CLI reported an error result",
        )
    verdict_text = _extract_verdict(text) if text else None
    if verdict_text is None:
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            model_used=metadata.get("model_used"),
            models_used=list(metadata.get("models_used") or []),
            tokens_used=int(metadata.get("tokens_used") or 0),
            cost_usd=metadata.get("cost_usd"),
            latency_ms=latency_ms,
            finish_reason=metadata.get("finish_reason"),
            warnings=list(metadata.get("warnings") or []),
            error="Claude CLI returned no parseable dialectic verdict",
        )

    return HostReviewResult(
        text=verdict_text,
        host_id=host_id,
        model_requested=model,
        model_used=metadata.get("model_used"),
        models_used=list(metadata.get("models_used") or []),
        tokens_used=int(metadata.get("tokens_used") or 0),
        cost_usd=metadata.get("cost_usd"),
        latency_ms=latency_ms,
        finish_reason=metadata.get("finish_reason"),
        warnings=list(metadata.get("warnings") or []),
    )
