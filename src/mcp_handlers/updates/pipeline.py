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
import time
from typing import Callable, List, NamedTuple

from src.logging_utils import get_logger

logger = get_logger(__name__)


class _EnrichmentEntry(NamedTuple):
    fn: Callable
    order: int
    name: str
    is_async: bool


_ENRICHMENTS: List[_EnrichmentEntry] = []


def enrichment(order: int):
    """Register a function in the enrichment pipeline at *order*."""
    def decorator(fn: Callable) -> Callable:
        _ENRICHMENTS.append(_EnrichmentEntry(
            fn=fn,
            order=order,
            name=fn.__name__,
            is_async=inspect.iscoroutinefunction(fn),
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
    timings: List[tuple[str, int]] = []
    for entry in _ENRICHMENTS:
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
        rendered = " ".join(f"{name}={ms}ms" for name, ms in timings)
        logger.info(f"[enrichment_phases] {rendered}")


def get_enrichment_count() -> int:
    return len(_ENRICHMENTS)


def get_enrichment_names() -> List[str]:
    return [e.name for e in _ENRICHMENTS]
