"""In-memory success/failure record for background loops.

A background loop that catches its own exception and logs it at DEBUG fails
silently: the server's log level is INFO, so the line is never written, and
nothing else records that the loop stopped doing its job. Concept extraction
failed on every run for months that way (KG 2026-10-20T03:44:57.244104+00:00).

``record_failure`` logs at WARNING the first time a task fails and again
whenever the error changes, and at DEBUG while the same error repeats, so a
loop that ticks every minute does not flood the log. ``record_success`` logs
recovery at INFO. ``snapshot`` is what the health check and the doctor read:
per task, when it last succeeded and the error it last failed with.

The record lives in this process only. It is empty after a restart until each
task has run once, which is reported as "no data yet", not as healthy.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Error text kept per task. The health payload is operator detail, but an
# unbounded exception string (a whole SQL statement, a traceback repr) does
# not belong in it.
MAX_ERROR_CHARS = 300

_DIGITS = re.compile(r"\d+")


@dataclass
class TaskHealth:
    name: str
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    consecutive_failures: int = 0
    # Error class plus message with digit runs collapsed, so an error that
    # only differs by an id, a count or a timestamp is not "a new error".
    _signature: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "failing" if self.consecutive_failures else "ok",
            "last_success_at": _iso(self.last_success_at),
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
            "consecutive_failures": self.consecutive_failures,
        }


_lock = threading.Lock()
_registry: dict[str, TaskHealth] = {}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _describe(error: BaseException | str) -> tuple[str, str]:
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    text = text[:MAX_ERROR_CHARS]
    return text, _DIGITS.sub("#", text)


def record_success(name: str, *, log: logging.Logger | None = None) -> None:
    """Mark one successful run of ``name``; log recovery once if it was failing."""
    log = log or logger
    now = datetime.now(timezone.utc)
    with _lock:
        entry = _registry.setdefault(name, TaskHealth(name))
        failures = entry.consecutive_failures
        last_error = entry.last_error
        entry.last_success_at = now
        entry.consecutive_failures = 0
        entry._signature = None
    if failures:
        log.info(
            "[TASK_HEALTH] %s recovered after %d failed run(s); last error was: %s",
            name,
            failures,
            last_error,
        )


def record_failure(
    name: str, error: BaseException | str, *, log: logging.Logger | None = None
) -> None:
    """Mark one failed run of ``name``.

    WARNING on the first failure and whenever the error changes; DEBUG while
    the same error repeats. A string ``error`` is for a run that completed but
    did not do its job (an update the store rejected, for example).
    """
    log = log or logger
    text, signature = _describe(error)
    now = datetime.now(timezone.utc)
    with _lock:
        entry = _registry.setdefault(name, TaskHealth(name))
        changed = entry.consecutive_failures == 0 or signature != entry._signature
        entry.consecutive_failures += 1
        entry.last_error = text
        entry.last_error_at = now
        entry._signature = signature
        failures = entry.consecutive_failures
        last_success = entry.last_success_at
    if changed:
        log.warning(
            "[TASK_HEALTH] %s failed (%d in a row; last success %s): %s",
            name,
            failures,
            _iso(last_success) or "never in this process",
            text,
        )
    else:
        log.debug("[TASK_HEALTH] %s failed again (%d in a row): %s", name, failures, text)


def snapshot() -> dict[str, dict[str, Any]]:
    """Per-task record, keyed by task name."""
    with _lock:
        return {name: entry.to_dict() for name, entry in sorted(_registry.items())}


def health_check() -> dict[str, Any]:
    """The ``background_tasks`` entry of the health check.

    ``warning`` while any recorded task's latest run failed; ``no_data_yet``
    before any task has recorded a run; otherwise ``healthy``.
    """
    from src.services.runtime_queries import NO_DATA_YET

    tasks = snapshot()
    if not tasks:
        return {
            "status": NO_DATA_YET,
            "tasks": {},
            "note": "No background task has recorded a run in this process yet.",
        }
    failing = sorted(name for name, entry in tasks.items() if entry["status"] == "failing")
    result: dict[str, Any] = {
        "status": "warning" if failing else "healthy",
        "tasks": tasks,
        "failing": failing,
    }
    if failing:
        result["warning"] = (
            f"{len(failing)} background task(s) failing: {', '.join(failing)}. "
            "See tasks.<name>.last_error."
        )
    return result


def reset_for_tests() -> None:
    with _lock:
        _registry.clear()
