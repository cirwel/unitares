"""Safe subscription-CLI backends for the standalone dialectic reviewer."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    # Which backend produced this. Defaults to the original (and only) producer
    # so existing Claude call sites keep byte-identical provenance.
    backend: str = "claude"

    def provenance(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "backend": self.backend,
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


# --------------------------------------------------------------------------- #
# Operator-configured OpenAI-compatible reviewer host.
#
# Deliberately vendor-NEUTRAL. #66/#80 removed a hardcoded `gemini` provider
# from `call_model` because nothing wired its key, so the branch could only
# return MISSING_CONFIG at runtime — a vendor branch that exists to say "not
# configured" is dead weight. This backend instead takes base_url + model + a
# key ENV NAME from the operator, so one code path serves Gemini, an OpenAI
# endpoint, OpenRouter, a second Ollama, or anything else speaking the same
# protocol, and it is exercisable today against a local endpoint with no key.
#
# Execution-cost policy: off by default, config-gated, and any failure degrades
# to the free local model — an operator with no budget is never worse off.
# --------------------------------------------------------------------------- #

EXTERNAL_HOST_ID = "external:openai-compatible"
_DEFAULT_KEY_ENV = "UNITARES_DIALECTIC_EXTERNAL_API_KEY"

# Thinking-mode models (Gemini 3, DeepSeek-R1, Qwen, gemma4 …) put reasoning in
# a <think> block that /v1/chat/completions returns inline. The local reviewer
# path already strips it; an arbitrary external host is MORE likely to think,
# not less, so the same strip applies here. Measured on the local endpoint:
# without it, gemma4 spends the whole token budget inside <think> and the reply
# arrives finish_reason="length" with no JSON at all.
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _external_host_label(base_url: str) -> str:
    """Host id carrying the endpoint's HOST only — never the path or a key."""
    from urllib.parse import urlparse

    try:
        netloc = urlparse(base_url).netloc
    except Exception:  # noqa: BLE001 - a label is never worth failing a review over
        netloc = ""
    return f"external:{netloc}" if netloc else EXTERNAL_HOST_ID


async def call_openai_compat_backend(prompt: str) -> HostReviewResult:
    """Run the review on an operator-configured OpenAI-compatible endpoint.

    Configuration (all read at call time so a test can patch the environment):
      * ``UNITARES_DIALECTIC_EXTERNAL_BASE_URL`` — required, e.g.
        ``https://generativelanguage.googleapis.com/v1beta/openai/``
      * ``UNITARES_DIALECTIC_EXTERNAL_MODEL`` — required. No default: inventing
        a model id produces a confident 404 instead of an honest misconfig.
      * ``UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV`` — NAME of the variable
        holding the key (default ``UNITARES_DIALECTIC_EXTERNAL_API_KEY``). The
        key itself never appears in a flag value, a log line, or provenance.
      * ``UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S`` — default 180.

    Returns provenance from the provider's own response — ``model`` and token
    usage as reported — so the verdict is attributable to an exact model rather
    than to a family guess.
    """
    base_url = os.getenv("UNITARES_DIALECTIC_EXTERNAL_BASE_URL", "").strip()
    model = os.getenv("UNITARES_DIALECTIC_EXTERNAL_MODEL", "").strip()
    key_env = os.getenv("UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV", "").strip() or _DEFAULT_KEY_ENV
    host_id = _external_host_label(base_url)

    if not base_url or not model:
        missing = " and ".join(
            name
            for name, value in (
                ("UNITARES_DIALECTIC_EXTERNAL_BASE_URL", base_url),
                ("UNITARES_DIALECTIC_EXTERNAL_MODEL", model),
            )
            if not value
        )
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model or None,
            backend="external",
            error=f"External reviewer host is not configured: {missing} unset",
        )

    # An empty key is legitimate — a local OpenAI-compatible endpoint ignores it.
    api_key = os.getenv(key_env, "").strip() or "not-required"

    try:
        timeout_s = float(os.getenv("UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S", "180"))
    except (TypeError, ValueError):
        timeout_s = 180.0
    timeout_s = max(1.0, timeout_s)

    try:
        max_tokens = int(os.getenv("UNITARES_DIALECTIC_REVIEW_MAX_TOKENS", "1024"))
    except (TypeError, ValueError):
        max_tokens = 1024

    started = time.monotonic()
    try:
        from openai import AsyncOpenAI  # local import: only the runner needs it

        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.2,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend="external",
            error=f"External reviewer exceeded {timeout_s:g}s timeout",
        )
    except Exception as exc:  # noqa: BLE001 - selected-host failure falls back locally
        # Type name only: an upstream message can carry the endpoint or key.
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend="external",
            error=f"External reviewer call failed: {type(exc).__name__}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    model_used = getattr(resp, "model", None) or model
    usage = getattr(resp, "usage", None)
    tokens_used = int(getattr(usage, "total_tokens", 0) or 0)
    choices = getattr(resp, "choices", None) or []
    finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
    content = ""
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", "") or ""
    content = THINK_BLOCK.sub("", content).strip()

    verdict_text = _extract_verdict(content) if content else None
    if verdict_text is None:
        # Truncation is an operator-fixable budget problem, not a model refusal;
        # say which one it was so the fallback warning is actionable.
        if finish_reason == "length":
            reason = (
                "External reviewer was truncated at "
                "UNITARES_DIALECTIC_REVIEW_MAX_TOKENS before emitting a verdict"
            )
        else:
            reason = "External reviewer returned no parseable dialectic verdict"
        return HostReviewResult(
            text=None,
            host_id=host_id,
            model_requested=model,
            model_used=model_used,
            models_used=[str(model_used)],
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            backend="external",
            error=reason,
        )

    return HostReviewResult(
        text=verdict_text,
        host_id=host_id,
        model_requested=model,
        model_used=model_used,
        models_used=[str(model_used)],
        tokens_used=tokens_used,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        backend="external",
    )
