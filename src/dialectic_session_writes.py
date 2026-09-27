"""Attempt/response records for every dialectic session write Python initiates.

WHY THIS EXISTS
---------------
The Wave 3 reduced-scope gate reads its window for collisions between the
Python stuck-session sweeper and every other writer of a session row. Most of
those writers leave no durable record of their own: a phase write, a reviewer
write (Python or BEAM ``update_reviewer``), a reopen, a facilitation flag, and a
BEAM resolve that finds the row already terminal (``origin: :already_terminal``,
``saga_id: nil`` -- no saga row). A record written only when something odd
happens is a positive-only stream whose zero cannot be told from an emitter
that never ran.

So every session write Python initiates -- directly against PostgreSQL through
the ``src.dialectic_db`` helpers, or over HTTP to the BEAM lease plane through
``beam_resolve_client`` -- is recorded as a PAIR of ``dialectic_session_write``
audit rows sharing an ``attempt_id``: ``stage="attempt"`` immediately before
the write, ``stage="response"`` after it returns, raises, or is interrupted.
Responses with a matching attempt, over attempts, is the emitter's coverage,
counted per kind over every attempt; the Wave 3 collision report treats any
unmatched attempt as making its reading inconclusive.

Recorded at the chokepoints (the ``*_async`` helpers in ``src.dialectic_db`` and
the ``beam_*`` writers), not at call sites, so a new call site cannot forget
it; ``tests/test_wave3_writer_inventory.py`` fails if a writer bypasses them.

``via`` says who initiated the write:

* ``"beam"`` -- a request to the lease plane (``beam_resolve_client``).
* ``"python_fallback"`` -- a Python write made because the BEAM write returned
  nothing (flag off, unconfigured, unreachable, or non-OK); set by the handler
  around the fallback call with `session_write_via`, which also names the site.
* ``"sweeper"`` -- a write inside a stuck-session resolver cycle. The sweeper
  additionally keeps its own richer per-write record,
  ``dialectic_guarded_write``, which carries the same ``attempt_id``.
* ``"python"`` -- any other Python write (a Python-only path such as the
  LLM-assisted dialectic, a synthetic review, a reopen, a facilitation flag).

Message inserts are recorded too (``kind="message"``): besides the durable
message row, each one bumps the session's ``updated_at``, which is the
sweeper's staleness clock, so by the gate's rule it is a session write, and
its attempt timestamp is its causal time.

⛔BEAM ``DialecticLiveness`` resolves inside BEAM and never reaches Python, so
none of its writes appear here. It only ever writes ``failed``; its saga rows
carry ``reason: liveness_timeout``.

``decision_ts`` on each record is when this task last read that session's row
from the database (`note_session_read`, called by the `DialecticDB` read
methods): the state the write acts on, and so the write's causal time. It is
None when the writer acted on an in-process cached copy it did not read in
this task.

Every emit is fail-soft: a failed audit write costs observability, never the
session write it describes. Failures are counted (`record_emit_failure`):
in-process, reported on the next ``dialectic_sweep_cycle`` row, and durably,
as a line in an append-only local ledger that is fsynced before the caller
continues, so a failure survives a crash before the next cycle row.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Set

from src.logging_utils import get_logger

logger = get_logger(__name__)

SESSION_WRITE = "dialectic_session_write"

KIND_CREATE = "create"
KIND_PHASE = "phase"
KIND_REVIEWER = "reviewer"
KIND_REOPEN = "reopen"
KIND_FACILITATION = "facilitation"
KIND_STATUS = "status"
KIND_RESOLVE = "resolve"
KIND_MESSAGE = "message"

# (via, site) for writes in this task; None means a plain Python write.
_VIA: ContextVar[Optional[tuple]] = ContextVar("dialectic_session_write_via", default=None)
# An attempt id chosen by a caller that keeps its own record of the same write
# (the sweeper's `dialectic_guarded_write`), so the two rows join.
_ATTEMPT_ID: ContextVar[Optional[str]] = ContextVar(
    "dialectic_session_write_attempt_id", default=None
)

# Response emits scheduled during a cancellation, held so they are not
# garbage-collected before they run.
_BACKGROUND: Set["asyncio.Task[Any]"] = set()

# session_id -> when this task last read that session's row from the
# database. The state a write acts on was read then, so it is the write's
# decision time -- its cause, for the Wave 3 collision report.
_DECISION_TS: ContextVar[Optional[Dict[str, datetime]]] = ContextVar(
    "dialectic_session_decision_ts", default=None
)

# Audit emits of the Wave 3 instrument that failed in this process since the
# last `dialectic_sweep_cycle` row took the count. Every instrument emitter
# (cycle rows, per-write rows, refusal/overlap events, these attempt/response
# records) reports its failures here; the next cycle row carries the total as
# `emit_failures_since_last_cycle`, and the collision report treats any
# nonzero value as making its reading inconclusive.
_EMIT_FAILURES = 0

# Minted once per process; stamped on the instrument's rows and on every
# emit-failure ledger line. `events.PROCESS_BOOT_ID` is this value.
PROCESS_BOOT_ID = str(uuid.uuid4())
# The last cycle_seq this process assigned, for ledger lines written between
# cycle rows. Set by `events.emit_sweep_cycle`.
LAST_CYCLE_SEQ: Optional[int] = None

EMIT_FAILURE_LEDGER_ENV = "UNITARES_DIALECTIC_EMIT_FAILURE_LEDGER"


def emit_failure_ledger_path() -> str:
    """The append-only emit-failure ledger: env override, else data/dialectic/."""
    override = os.environ.get(EMIT_FAILURE_LEDGER_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "data", "dialectic", "instrument_emit_failures.jsonl")


_LEDGER_ENSURED: Optional[str] = None

# Bound on one instrument audit append. The audit path can stall (a locked
# table, an exhausted pool); an attempt record is awaited BEFORE the session
# write it describes, so an unbounded stall there would block the write
# itself. A stalled append is a failed emit: counted and put on the ledger.
EMIT_TIMEOUT_S = 2.0


def ensure_emit_failure_ledger() -> None:
    """Create the ledger file (empty) if it does not exist. Never raises.

    Called by the first cycle row of each process. A present, empty ledger is
    the positive statement "no instrument emit has failed here"; the collision
    report treats an ABSENT ledger as unverified (it may be reading another
    host's or checkout's data directory).
    """
    global _LEDGER_ENSURED
    try:
        path = emit_failure_ledger_path()
        if _LEDGER_ENSURED == path and os.path.exists(path):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        _LEDGER_ENSURED = path
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("instrument emit-failure ledger could not be created: %s", exc)


async def bounded_append(entry: Dict[str, Any]) -> Any:
    """`append_audit_event_async` with `EMIT_TIMEOUT_S`; a stall raises TimeoutError."""
    from src.audit_db import append_audit_event_async

    return await asyncio.wait_for(append_audit_event_async(entry), timeout=EMIT_TIMEOUT_S)


def _append_ledger_line(line: Dict[str, Any]) -> None:
    """Append one line and fsync it before returning. Never raises."""
    try:
        path = emit_failure_ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:  # pragma: no cover - the ledger is the last resort
        logger.warning("instrument emit-failure ledger write failed: %s", exc)


def record_emit_failure(
    event_type: Optional[str] = None,
    session_id: Optional[str] = None,
    cycle_seq: Optional[int] = None,
) -> None:
    """Count one failed instrument emit, in-process AND durably.

    The in-process count is reported on the next cycle row. The ledger line
    survives the process, so a failure followed by a crash before the next
    cycle row is still on record; the collision report reads the ledger for
    its window and treats any line as making the reading inconclusive.
    """
    global _EMIT_FAILURES
    _EMIT_FAILURES += 1
    _append_ledger_line({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "session_id": session_id,
        "process_boot_id": PROCESS_BOOT_ID,
        "cycle_seq": cycle_seq if cycle_seq is not None else LAST_CYCLE_SEQ,
    })


def carry_emit_failures(n: int) -> None:
    """Hand ``n`` already-recorded failures to the next cycle row (no new line)."""
    global _EMIT_FAILURES
    _EMIT_FAILURES += max(0, int(n))


def take_emit_failures() -> int:
    """Return the failures counted since the last call, and reset."""
    global _EMIT_FAILURES
    n, _EMIT_FAILURES = _EMIT_FAILURES, 0
    return n


def note_session_read(session_ids: Any, at: Optional[datetime] = None) -> None:
    """Record that this task just read these sessions' rows (their decision time).

    Called by the `DialecticDB` read methods. Task-local: a write in the same
    task picks it up as ``decision_ts``. Never raises.
    """
    try:
        if isinstance(session_ids, str):
            session_ids = [session_ids]
        when = at or datetime.now(timezone.utc)
        current = dict(_DECISION_TS.get() or {})
        for sid in session_ids:
            if sid:
                current[str(sid)] = when
        _DECISION_TS.set(current)
    except Exception:  # pragma: no cover - observability must not break a read
        pass


def decision_ts_for(session_id: Optional[str]) -> Optional[datetime]:
    return (_DECISION_TS.get() or {}).get(str(session_id)) if session_id else None


@contextmanager
def session_write_via(via: str, site: Optional[str] = None) -> Iterator[None]:
    """Attribute the session writes made inside this block to ``via``/``site``."""
    token = _VIA.set((via, site))
    try:
        yield
    finally:
        _VIA.reset(token)


@contextmanager
def attempt_id_scope(attempt_id: str) -> Iterator[None]:
    """Use ``attempt_id`` for the next session write recorded in this block."""
    token = _ATTEMPT_ID.set(attempt_id)
    try:
        yield
    finally:
        _ATTEMPT_ID.reset(token)


def _beam_routing_enabled() -> Optional[bool]:
    try:
        from src.mcp_handlers.dialectic.beam_resolve_client import beam_resolution_enabled

        return beam_resolution_enabled()
    except Exception:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def _emit(details: Dict[str, Any]) -> None:
    try:
        persisted = await bounded_append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": SESSION_WRITE,
            "agent_id": None,
            "session_id": details.get("session_id"),
            "details": {k: _json_safe(v) for k, v in details.items()},
        })
        if persisted is False:
            record_emit_failure(SESSION_WRITE, details.get("session_id"))
    except Exception as exc:
        record_emit_failure(SESSION_WRITE, details.get("session_id"))
        logger.warning(
            "%s audit emit failed: session=%s stage=%s kind=%s err=%s",
            SESSION_WRITE, details.get("session_id"), details.get("stage"),
            details.get("kind"), exc,
        )


class SessionWriteRecord:
    """The response half of one recorded write; fill it before the block ends."""

    def __init__(self) -> None:
        self.fields: Dict[str, Any] = {}

    def respond(self, **fields: Any) -> None:
        self.fields.update(fields)


@asynccontextmanager
async def record_session_write(
    *,
    kind: str,
    session_id: Optional[str],
    default_via: str = "python",
    requested: Any = None,
) -> AsyncIterator[SessionWriteRecord]:
    """Record one session write as an attempt/response pair.

    Emits ``stage="attempt"`` on entry. On a normal exit emits the response
    with whatever the block passed to ``record.respond(...)`` (``outcome`` at
    least). On an ``Exception`` the response says ``outcome="error"`` and the
    exception class, and the exception propagates. On a cancellation the
    response (``outcome="interrupted"``) is scheduled as a background task
    rather than awaited -- awaiting mid-cancellation could hang the task being
    cancelled -- and the cancellation propagates.

    Never raises on its own account.
    """
    via, site = _VIA.get() or (default_via, None)
    attempt_id = _ATTEMPT_ID.get() or str(uuid.uuid4())
    base = {
        "session_id": session_id,
        "attempt_id": attempt_id,
        "kind": kind,
        "via": via,
        "site": site,
        "requested": requested,
        # When this task last read the session's row: the state the write acts
        # on. None when it acted on a cached copy it never read from the
        # database in this task; the report then falls back to the attempt.
        "decision_ts": decision_ts_for(session_id),
        # Tells a configured Python path (flag off) from a fallback after a
        # BEAM request that was made and failed (flag on).
        "beam_routing_enabled": _beam_routing_enabled(),
    }
    await _emit({**base, "stage": "attempt"})
    record = SessionWriteRecord()
    try:
        yield record
    except Exception as exc:
        await _emit({**base, "stage": "response", **record.fields,
                     "outcome": "error", "error": type(exc).__name__})
        raise
    except BaseException:
        try:
            task = asyncio.get_running_loop().create_task(
                _emit({**base, "stage": "response", **record.fields,
                       "outcome": "interrupted"})
            )
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)
        except Exception:  # pragma: no cover - no loop left to schedule on
            pass
        raise
    else:
        fields = dict(record.fields)
        fields.setdefault("outcome", "unknown")
        await _emit({**base, "stage": "response", **fields})


def written_outcome(result: Any) -> str:
    """Map a Python write helper's return value to a response outcome."""
    if result is True:
        return "written"
    if result is False:
        return "not_written"
    return "written" if result else "not_written"
