"""Internal synchronous EISV invocation, awaited within the caller's agent lock.

Admission, checkpoint delivery, policy actuation and persistence remain with
their existing callers. This adapter creates/hydrates the live monitor and
advances it; it is not a pure function or a replay boundary.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from src.perf_monitor import record_ms as _perf_record_ms

if TYPE_CHECKING:
    from src.governance_monitor import UNITARESMonitor


@dataclass(frozen=True)
class EisvRequest:
    """Call-local handoff, shallowly frozen rather than an input snapshot.

    Keep the original state mapping, including absent versus explicit values
    and checkpoint provenance injected after admission. Do not copy or
    normalize it; the existing estimator can mutate this same mapping.
    """

    agent_id: str
    agent_state: dict[str, Any]
    confidence: float | None = None


@dataclass(frozen=True)
class EisvAssessment:
    """Original result and live monitor for the caller's legacy effects.

    Neither object is an immutable assessment snapshot. Returning the monitor
    is a transitional bridge for existing policy and persistence orchestration.
    """

    result: dict[str, Any]
    monitor: UNITARESMonitor


async def assess_eisv(request: EisvRequest) -> EisvAssessment:
    """Run the existing monitor invocation without changing executor semantics.

    The caller owns admission and locking. Exceptions and cancellation propagate;
    cancelling an executor await does not stop work already running in its thread.
    """
    from src.agent_lifecycle import get_or_create_monitor

    loop = asyncio.get_running_loop()
    _t_setup = time.perf_counter()
    monitor = await loop.run_in_executor(None, get_or_create_monitor, request.agent_id)
    # Hydrate a missing file's monitor from the existing DB history before
    # advancing it, at the same await point as the legacy admitted execution.
    from src.agent_monitor_state import hydrate_from_db_if_fresh
    await hydrate_from_db_if_fresh(monitor, request.agent_id)
    _perf_record_ms("ode.monitor_setup_ms", (time.perf_counter() - _t_setup) * 1000.0)

    task_type = request.agent_state.get("task_type", "mixed")
    # This span includes default-executor queue wait as well as numpy work.
    # Keep run_in_executor: to_thread would additionally propagate contextvars.
    _t_numpy = time.perf_counter()
    result = await loop.run_in_executor(
        None,
        partial(
            monitor.process_update, request.agent_state,
            confidence=request.confidence, task_type=task_type,
        ),
    )
    _perf_record_ms("ode.numpy_step_ms", (time.perf_counter() - _t_numpy) * 1000.0)
    return EisvAssessment(result=result, monitor=monitor)
