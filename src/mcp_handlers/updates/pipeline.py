"""
Enrichment Pipeline — self-registering decorator + async runner.

Usage:
    from .pipeline import enrichment, run_enrichment_pipeline

    @enrichment(order=10)
    def enrich_foo(ctx): ...

    @enrichment(order=20)
    async def enrich_bar(ctx): ...

    await run_enrichment_pipeline(ctx)
"""

import inspect
import os
import time
from typing import Callable, List, NamedTuple

from src.logging_utils import get_logger
from src.mcp_handlers.response_formatter import canonical_response_mode

logger = get_logger(__name__)


class _EnrichmentEntry(NamedTuple):
    fn: Callable
    order: int
    name: str
    is_async: bool
    lite_safe: bool = False


_ENRICHMENTS: List[_EnrichmentEntry] = []


def _requested_response_mode(ctx) -> str:
    """Return the canonical caller-requested mode before response data exists.

    This mirrors the formatter's request priority for the one mode that matters
    before formatting: legacy `minimal`, whose response-shaping enrichments can
    be skipped safely. `auto` cannot be resolved until response_data exists, so
    it stays `auto` here and runs all enrichments.
    """
    arguments = getattr(ctx, "arguments", None) or {}
    raw_mode = arguments.get("response_mode")

    if not raw_mode:
        meta = getattr(ctx, "meta", None)
        preferences = getattr(meta, "preferences", None)
        if isinstance(preferences, dict):
            raw_mode = preferences.get("verbosity")

    if not raw_mode:
        raw_mode = os.getenv("UNITARES_PROCESS_UPDATE_RESPONSE_MODE", "auto")

    return canonical_response_mode(raw_mode)


def enrichment(order: int, *, lite_safe: bool = False):
    """Register a function in the enrichment pipeline at *order*.

    lite_safe=True marks the enrichment as response-shaping only — safe to
    skip when the caller explicitly requested response_mode='minimal' (the
    legacy skinny path used by embedded brokers that read action+margin and
    discard the rest). Default False keeps existing behavior for callers that
    use richer modes.
    """
    def decorator(fn: Callable) -> Callable:
        _ENRICHMENTS.append(_EnrichmentEntry(
            fn=fn,
            order=order,
            name=fn.__name__,
            is_async=inspect.iscoroutinefunction(fn),
            lite_safe=lite_safe,
        ))
        _ENRICHMENTS.sort(key=lambda e: e.order)
        return fn
    return decorator


async def run_enrichment_pipeline(ctx) -> None:
    """Run every registered enrichment in order. Each is fail-safe.

    Per-step wall-clock is recorded so the [enrichment_phases] log line
    can attribute slow check-ins to a specific enrichment. The pipeline
    runs serially today; this instrumentation is the prerequisite for
    deciding which enrichments are safe to parallelize.
    """
    is_minimal = _requested_response_mode(ctx) == "minimal"
    timings: List[tuple[str, int]] = []
    for entry in _ENRICHMENTS:
        if is_minimal and entry.lite_safe:
            timings.append((entry.name, -1))  # -1 sentinel = skipped (minimal)
            continue
        start = time.perf_counter()
        try:
            if entry.is_async:
                await entry.fn(ctx)
            else:
                entry.fn(ctx)
        except Exception as exc:
            logger.debug(f"Enrichment {entry.name} failed: {exc}")
        timings.append((entry.name, int((time.perf_counter() - start) * 1000)))

    if timings:
        rendered = " ".join(
            f"{name}={'skip' if ms < 0 else f'{ms}ms'}" for name, ms in timings
        )
        logger.info(f"[enrichment_phases] {rendered}")


def get_enrichment_count() -> int:
    return len(_ENRICHMENTS)


def get_enrichment_names() -> List[str]:
    return [e.name for e in _ENRICHMENTS]
