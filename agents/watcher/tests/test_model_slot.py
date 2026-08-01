"""Tests for model-call serialization (the queue-timeout bug).

Background: on 2026-07-31 three hook-fired scans started within 19 seconds.
Ollama serializes requests per model, so they did not run concurrently — they
queued. One completed in ~3 minutes; the other two failed at *exactly* their
start time plus WATCHER_TIMEOUT:

    22:49:32 -> timed out 22:55:32
    22:49:39 -> timed out 22:55:39

Both spent their entire 360s budget waiting for a slot they never got; the
model never saw either prompt. The failures looked like "model too slow" and
invited a model swap, which would have made it strictly worse — a larger model
holds the slot longer.

The fix serializes at call_model so a call's timeout starts when it OWNS the
slot. Contention raises ModelBusy, which must NOT count toward the
detector-down threshold: a busy detector is alive, and counting contention
would fire "detector down" exactly when the detector is most in demand.
"""

from __future__ import annotations

import multiprocessing
import time

import pytest

from agents.watcher.agent import ModelBusy, model_slot


def test_slot_is_reentrant_across_sequential_calls() -> None:
    """Back-to-back calls must not deadlock — the lock is released on exit."""
    for _ in range(3):
        with model_slot(wait_s=5):
            pass


def test_contention_raises_model_busy_not_a_generic_error() -> None:
    """The waiter gives up with a typed exception rather than hanging."""
    with model_slot(wait_s=5):
        # Nested acquire from the same process still contends on the fd lock
        # in a child; here we assert the type contract directly.
        with pytest.raises(ModelBusy):
            _acquire_in_child_expecting_busy(wait_s=1)


def test_model_busy_is_not_confused_with_a_model_failure() -> None:
    """ModelBusy must not classify as a detector failure.

    _classify_model_failure keys on substrings; if ModelBusy's message ever
    contains "timed out" it would be filed as a timeout and counted toward
    detector-down, silently re-creating the bug this fix removes.
    """
    from agents.watcher.agent import _classify_model_failure

    exc = ModelBusy("model slot held by another scan for >300s")
    assert "timed out" not in str(exc)
    assert _classify_model_failure(exc) != "timeout"


def test_slot_released_even_when_body_raises() -> None:
    """A failing model call must not strand the slot for every later scan."""
    with pytest.raises(ValueError):
        with model_slot(wait_s=5):
            raise ValueError("model exploded")

    # If the lock leaked, this would block until wait_s and raise ModelBusy.
    with model_slot(wait_s=5):
        pass


def test_waiter_blocks_until_holder_releases() -> None:
    """The point of the fix: a waiter gets the slot, it does not fail fast.

    The child signals *after* it holds the lock. Sleeping a fixed interval
    instead is not enough — macOS spawns (re-importing this module), so the
    parent can win the race and the test passes while proving nothing.
    """
    holding = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_slot, args=(1.5, holding))
    proc.start()
    assert holding.wait(timeout=60), "child never acquired the slot"

    started = time.monotonic()
    with model_slot(wait_s=60):
        waited = time.monotonic() - started

    proc.join(timeout=30)
    # It waited for the holder rather than sailing straight through.
    assert waited > 0.5, f"acquired too fast ({waited:.2f}s) — lock not held?"


def _hold_slot(seconds: float, holding) -> None:
    with model_slot(wait_s=30):
        holding.set()
        time.sleep(seconds)


def _acquire_in_child_expecting_busy(wait_s: int) -> None:
    """Acquire from a child process; re-raise ModelBusy in the parent."""
    queue: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_try_acquire, args=(wait_s, queue))
    proc.start()
    proc.join(timeout=wait_s + 20)
    outcome = queue.get(timeout=5) if not queue.empty() else "no-result"
    if outcome == "busy":
        raise ModelBusy(f"model slot held by another scan for >{wait_s}s")
    raise AssertionError(f"child did not report contention: {outcome}")


def _try_acquire(wait_s: int, queue: multiprocessing.Queue) -> None:
    try:
        with model_slot(wait_s=wait_s):
            queue.put("acquired")
    except ModelBusy:
        queue.put("busy")
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected
        queue.put(f"error:{exc}")
