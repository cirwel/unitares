#!/usr/bin/env python3
"""Classify every guarded dialectic-sweeper write in a window, and grade the heartbeat.

The pre-registered read of the Wave 3 reduced-scope gate's §7 window (instrument
v2). It answers three questions from durable tables only, READ ONLY:

1. Is the record complete? (``completeness``) Any unmatched unit makes the
   whole reading INCONCLUSIVE, and the counts are then printed as lower bounds,
   never as a reading -- a zero is not reported while a unit is unmatched.
2. For each guarded write the Python stuck-session sweeper attempted, did it
   collide with another writer, and if so, how? (``classify``)
3. Can the gaps between cycle rows be told apart -- lost audit write, restart,
   hung loop? (``heartbeat``)

Sources: ``audit.events`` (``dialectic_guarded_write``,
``dialectic_sweep_cycle``, ``dialectic_session_write``),
``core.dialectic_sessions``,
``core.dialectic_messages`` and ``coordination.session_resolution_sagas`` (which
is never deleted from). Nothing here writes; the connection is opened READ ONLY
by the server, not by convention.

PRE-REGISTERED DEFINITIONS
--------------------------
These are stated before the window is read, as the measurement-authority rule
requires, and every one is a choice. Changing one is an amendment to the gate,
not a re-run.

*Sweeper write.* One ``dialectic_guarded_write`` row (instrument v2). Rows
written before v2 are out of scope: the window starts at the first periodic v2
cycle row, which this report prints.

*Competing write.* Any write to the same session by a writer other than the
sweeper, placed by a CAUSE time and an EFFECT time:

========================= ============================== ==========================
competing write           cause                          effect
========================= ============================== ==========================
saga (BEAM resolve)       ``created_at``                 ``pg_committed_at``, else
                                                         ``reverted_at``, else
                                                         ``updated_at``
Python-initiated session  the attempt's ``decision_ts``  the response's ``ts``
write (BEAM request or    (when the writer read the
Python write, any kind)   session); if none, the latest
                          protocol message before the
                          attempt, else the attempt
protocol message          its ``timestamp``              its ``timestamp``
system message (not the   --                             its ``timestamp``;
sweeper's)                                               ordering (b) only
========================= ============================== ==========================

A Python-initiated session write is a ``dialectic_session_write`` response
whose ``outcome`` is ``written`` (or ``already_terminal`` for a BEAM resolve),
from any ``via`` but ``sweeper``. The competing-writer set is the checked-in
inventory (``wave3_writer_inventory.py``, enforced by a static test), and
every message insert bumps the sweeper's staleness clock, so messages are
competing writes too; the report prints each inventory writer with the
durable trace it leaves and how many trace rows the window holds.
BEAM ``DialecticLiveness`` resolves inside BEAM and never reaches Python, so
no ``dialectic_session_write`` row can record it; it only ever writes
``failed``, so its already-terminal outcomes on a row the sweeper reaped are
contention-benign by construction, and the report says so instead of
claiming to observe them. Its commits appear as saga rows
(``reason: liveness_timeout``).

*Completeness (A12)*, over the window, unit by unit:

(a) every ``dialectic_session_write`` attempt has its response (per kind);
(b) each cycle's ``dialectic_guarded_write`` rows equal its
    ``write_attempt_count``, keyed by ``process_boot_id`` + ``cycle_seq``, and
    every guarded-write row belongs to a cycle row;
(c) ``cycle_seq`` is continuous within each boot;
(d) every committed saga that is not BEAM liveness's is named by a
    session-write response's ``saga_id``;
(e) the durable emit-failure ledger could be read and each line placed;
(f) no write is AMBIGUOUS.

*Uncovered intervals.* A recorded emit failure -- a cycle row's
``emit_failures_since_last_cycle``, or a line in the durable ledger
(``data/dialectic/instrument_emit_failures.jsonl``, appended and fsynced on
each failure so it survives a crash) -- marks the interval from the last cycle
row before it to the first after it as UNCOVERED, like a heartbeat gap: units
inside it are explained rather than counted, writes inside it are excluded
from the reading, and coverage is computed without it, so the window must
accrue its exposure elsewhere. A ``cycle_seq`` gap a recorded failure explains
becomes such an interval; an unexplained gap stays an unmatched unit.
⛔Residual: a failure whose ledger append ALSO failed (disk full, unwritable
data dir) and whose process then died before its next cycle row is recorded
nowhere; it is visible only as a boot change.

ANY unmatched unit makes the reading INCONCLUSIVE, and the counts are then
printed as lower bounds; the unmatched units are listed. A window with no
periodic v2 cycle row is NOT STARTED, not complete.

*Classes*, mutually exclusive, assigned in this order (HARM first):

``harm``
    A SUCCEEDED sweeper write with an adverse consequence:
    (a) CONTRADICTED -- a competing write in flight across the sweeper's commit
        (cause before it, effect after it, both within ``--correlation-hours``,
        default 6) that the commit defeated: refused or answered
        ``already_terminal`` after the sweeper made the row terminal, or a
        saga whose commit could no longer land; or
    (b) STALE DECISION -- a competing write (a reviewer, phase, facilitation,
        reopen or terminal write, a message, a saga commit) that took effect
        between the sweeper's decision read and its commit
        (``decision_read_ts < effect <= commit_ts``): the sweeper acted on
        state that had already changed, even when both writes succeeded.
    (c) REVERSED -- after a SUCCEEDED reap, a competing write that landed within
        the bound and invalidates it: a message appended to the failed
        session, a reopen, a reviewer, phase or facilitation write that
        revives it -- even when the final status equals the sweeper's intent.
    A same-value overlap is never harm. A refused write cannot be harm: its
    write never landed. ⛔Every harm row is a CANDIDATE for adjudication, not a
    verdict.
``contention_divergent`` / ``contention_benign``
    A REFUSED write (another writer made the row terminal first); a SUCCEEDED
    write with a same-value overlap (the competing write would leave the value
    the sweeper wrote: both intend ``failed``, both set
    ``awaiting_facilitation=true``, both name the same reviewer) -- no adverse
    consequence, so benign, never harm; or a SUCCEEDED reap followed by a
    competing terminal write (caused after the commit, within the bound), or
    overwritten by a competing write in flight across it. Benign when the
    final status equals the sweeper's intended status (``intended_status`` on
    the row: ``failed`` for a reap, ``active`` for a facilitation request or a
    reassignment), divergent otherwise. A refusal whose winner status was not
    recorded is ``contention_unattributed``.
``ambiguous``
    A succeeded write whose only non-benign evidence is a competing write known
    only as an interval (attempt to response; a BEAM write over HTTP has no
    database commit time Python can read) that contains the sweeper's commit
    or its decision read, so the two cannot be ordered. Reported separately;
    it makes the reading INCONCLUSIVE and is never counted as harm or as a
    clean zero. Definite harm evidence on the same write still makes it harm.
``uncontended``
    A succeeded write with none of the above.
``unknown_outcome``
    The write raised; whether it landed is unknown.

Later protocol activity after a SUCCEEDED facilitation request or
reassignment is the outcome that write intended, so it is not contention.

*Reviewer drift.* Each write carries the reviewer and phase the sweeper read.
A later saga or protocol message naming a different reviewer than the one read
(and than the one the sweeper wrote) shows a reviewer change the durable tables
cannot otherwise place; so does the session row itself naming one. The change
happened after the read and no later than the evidence (for the row: its
``updated_at``, the time of its last write of any kind). When that upper bound
falls inside (b)'s interval, or when the row's terminal write is provably this
reap (below), the drift is harm evidence; otherwise it is listed for
adjudication. A reap is provably the row's terminal write when the row is
``failed`` with no ``resolution_json`` (every other terminal writer records
one) and no later sweeper write touched the session: reviewer writes are
refused on a terminal row, so the reviewer the row names was written before
the reap -- ordering (b) with no event of its own, which is how a BEAM
``update_reviewer`` just before a reap is seen.

*Saga/row mismatch.* A ``pg_committed`` saga whose payload is not the session
row's ``resolution_json``: the BEAM commit wrote zero rows because the row was
already terminal (``commit_session_row`` returns ``:ok`` on zero rows).

*Heartbeat.* Per ``process_boot_id``, in ``cycle_seq`` order: a missing
sequence number is a lost audit write; a boot change is a restart; an in-boot
silence between periodic rows longer than ``--silence-minutes`` (default 22,
about two missed 10-minute cycles) is a hung or dead loop. Coverage is the
share of the window spanned by consecutive periodic rows no further apart than
that. These are reported as telemetry; the gate's operator-set priors decide
what they mean.

Usage::

    wave3_collision_report.py --since 2026-10-01T00:00:00Z [--until ...] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 30000

DEFAULT_DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

INSTRUMENT_VERSION = "wave3-instrument-v2"
DEFAULT_CORRELATION_HOURS = 6.0
DEFAULT_SILENCE_MINUTES = 22.0
DEFAULT_WINDOW_DAYS = 30

CLASSES = (
    "harm",
    "ambiguous",
    "contention_divergent",
    "contention_benign",
    "contention_unattributed",
    "uncontended",
    "unknown_outcome",
)
TERMINAL_STATUSES = ("resolved", "failed")

# ---------------------------------------------------------------------------
# Queries. Every one is bounded by the window (plus the correlation bound on
# either side, so a competing write just outside the window is still seen).
# ---------------------------------------------------------------------------

EVENTS_QUERY = """
SELECT ts, event_type, session_id, payload
FROM audit.events
WHERE event_type = ANY(%(event_types)s)
  AND ts >= %(lo)s AND ts < %(hi)s
ORDER BY ts
"""

SESSIONS_QUERY = """
SELECT session_id, status, phase, reviewer_agent_id, paused_agent_id, updated_at,
       resolution_json->>'reason' AS resolution_reason,
       resolution_json
FROM core.dialectic_sessions
WHERE session_id = ANY(%(session_ids)s)
"""

# System messages are included: every message insert bumps the sweeper's
# staleness clock, so one landing inside a sweeper write's read-to-commit
# interval is ordering (b) whoever wrote it.
MESSAGES_QUERY = """
SELECT session_id, agent_id, message_type, timestamp
FROM core.dialectic_messages
WHERE session_id = ANY(%(session_ids)s)
  AND timestamp >= %(lo)s AND timestamp < %(hi)s
ORDER BY timestamp
"""

SAGAS_QUERY = """
SELECT saga_id::text AS saga_id, session_id, paused_agent_id, reviewer_agent_id,
       state, created_at, pg_committed_at, reverted_at, updated_at,
       resolution_payload_json->>'reason' AS payload_reason
FROM coordination.session_resolution_sagas
WHERE session_id = ANY(%(session_ids)s)
  AND created_at >= %(lo)s AND created_at < %(hi)s
ORDER BY created_at
"""

# BEAM's commit_session_row returns :ok on zero rows, so a saga can commit
# while the session row keeps an earlier writer's status and payload.
MISMATCH_QUERY = """
SELECT g.saga_id::text AS saga_id, g.session_id, g.created_at, g.pg_committed_at,
       s.status AS row_status, s.resolution_json->>'reason' AS row_reason,
       EXISTS (
           SELECT 1 FROM audit.events e
           WHERE e.event_type = 'dialectic_guarded_write'
             AND e.session_id = g.session_id
             AND e.payload->>'outcome' = 'succeeded'
             AND e.payload->>'attempted' = 'reap_failed'
             AND e.ts >= %(lo_bound)s AND e.ts < %(hi_bound)s
       ) AS sweeper_reaped
FROM coordination.session_resolution_sagas g
JOIN core.dialectic_sessions s ON s.session_id = g.session_id
WHERE g.state = 'pg_committed'
  AND g.pg_committed_at >= %(lo)s AND g.pg_committed_at < %(hi)s
  AND s.resolution_json IS DISTINCT FROM g.resolution_payload_json
ORDER BY g.pg_committed_at
"""

# A terminal row with a resolution and no committed saga carrying that same
# payload was written by Python. The fallback event makes that first-class
# from instrument v2 on; this inference covers rows it cannot (an emit that
# failed, or a Python path that is not a BEAM fallback).
INFERRED_PY_TERMINAL_QUERY = """
SELECT s.session_id, s.status, s.updated_at, s.resolution_json->>'reason' AS reason
FROM core.dialectic_sessions s
WHERE s.status IN ('resolved', 'failed')
  AND s.resolution_json IS NOT NULL
  AND s.updated_at >= %(lo)s AND s.updated_at < %(hi)s
  AND NOT EXISTS (
      SELECT 1 FROM coordination.session_resolution_sagas g
      WHERE g.session_id = s.session_id
        AND g.state = 'pg_committed'
        AND g.resolution_payload_json = s.resolution_json
  )
ORDER BY s.updated_at
"""

# Committed sagas created in the window, every session: the resolve emitter's
# saga-side cross-check. BEAM liveness's are marked by their payload reason.
WINDOW_SAGAS_QUERY = """
SELECT saga_id::text AS saga_id, session_id, created_at, pg_committed_at,
       resolution_payload_json->>'reason' AS payload_reason
FROM coordination.session_resolution_sagas
WHERE state = 'pg_committed'
  AND created_at >= %(lo)s AND created_at < %(hi)s
"""

WRITE_EVENT = "dialectic_guarded_write"
CYCLE_EVENT = "dialectic_sweep_cycle"
SESSION_WRITE_EVENT = "dialectic_session_write"

COMPETING_EVENT_TYPES = (SESSION_WRITE_EVENT,)
TERMINAL_KINDS = ("resolve", "status")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _ts(value: Any) -> Optional[dt.datetime]:
    """Parse a timestamp from a column or an ISO payload string, as aware UTC."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Competing writes
# ---------------------------------------------------------------------------

def _latest_message_at_or_before(
    messages: Sequence[Dict[str, Any]], when: dt.datetime
) -> Optional[dt.datetime]:
    """The latest PROTOCOL (non-system) message at or before ``when``."""
    best = None
    for m in messages:
        if m.get("agent_id") == "system":
            continue
        t = _ts(m.get("timestamp"))
        if t is not None and t <= when and (best is None or t > best):
            best = t
    return best


def pair_session_writes(
    events: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """``attempt_id`` -> {"attempt": row|None, "response": row|None}."""
    pairs: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("event_type") != SESSION_WRITE_EVENT:
            continue
        p = _payload(e)
        aid = p.get("attempt_id")
        if not aid:
            continue
        slot = pairs.setdefault(aid, {"attempt": None, "response": None})
        row = {**p, "ts": _ts(e.get("ts")),
               "session_id": e.get("session_id") or p.get("session_id")}
        if p.get("stage") == "attempt":
            slot["attempt"] = row
        elif p.get("stage") == "response":
            slot["response"] = row
    return pairs


def competing_writes(
    session_id: str,
    *,
    sagas: Sequence[Dict[str, Any]],
    messages: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Every non-sweeper write to ``session_id``, with cause and effect times.

    ``events`` are ``dialectic_session_write`` audit rows. Pure: the caller
    does the reads.
    """
    session_messages = [m for m in messages if m.get("session_id") == session_id]
    pairs = pair_session_writes(events)
    saga_values = {
        str(pr["response"].get("saga_id")): pr["response"].get("requested")
        for pr in pairs.values()
        if pr["response"] is not None and pr["response"].get("saga_id")
    }
    out: List[Dict[str, Any]] = []

    for g in sagas:
        if g.get("session_id") != session_id:
            continue
        cause = _ts(g.get("created_at"))
        effect = (_ts(g.get("pg_committed_at")) or _ts(g.get("reverted_at"))
                  or _ts(g.get("updated_at")) or cause)
        if cause is None:
            continue
        liveness = g.get("payload_reason") == "liveness_timeout"
        out.append({
            "kind": "saga",
            "cause_ts": cause,
            "effect_ts": effect,
            "terminal": g.get("state") in ("pg_committed",),
            "landed": g.get("state") == "pg_committed",
            # BEAM liveness writes `failed` only; a Python-routed saga's status
            # is filled in from its session-write response when one names it.
            "value": "failed" if liveness else saga_values.get(str(g.get("saga_id"))),
            "value_kind": "status",
            "reviewer_agent_id": g.get("reviewer_agent_id"),
            "detail": {"saga_id": g.get("saga_id"), "state": g.get("state"),
                       "liveness": liveness},
        })

    for m in session_messages:
        t = _ts(m.get("timestamp"))
        if t is None:
            continue
        system = m.get("agent_id") == "system"
        out.append({
            "kind": "system_message" if system else "protocol_message",
            "cause_ts": t,
            "effect_ts": t,
            "terminal": False,
            # A system message moves only the staleness clock; it can place a
            # write in ordering (b), never (a) or later-writer contention.
            "b_only": system,
            "landed": True,
            "value": None,  # activity, never "the same value" as a sweeper write
            "value_kind": "message",
            "agent_id": m.get("agent_id"),
            "detail": {"message_type": m.get("message_type"), "agent_id": m.get("agent_id")},
        })

    for pair in pairs.values():
        response, attempt = pair["response"], pair["attempt"]
        if response is None or response.get("session_id") != session_id:
            continue
        if response.get("via") == "sweeper":
            continue  # the sweeper's own write
        if response.get("kind") == "message":
            # Already observed above from core.dialectic_messages, with its
            # exact database timestamp; the attempt/response bracket would
            # only duplicate it less precisely.
            continue
        outcome = response.get("outcome")
        # Outcomes whose effect is unknown: the write may or may not have
        # landed (a BEAM request that timed out after the server committed, a
        # DB call that raised after taking effect, a cancellation, a BEAM phase
        # OK that also covers a terminal no-op). They are placed by their
        # bracket and can only ever be AMBIGUOUS, never harm and never clean.
        uncertain = outcome in ("no_response", "error", "interrupted",
                                "accepted_effect_unknown", "unknown")
        if outcome not in ("written", "not_written", "already_terminal") and not uncertain:
            continue  # a known no-op: nothing landed
        effect = response["ts"]
        if effect is None:
            continue
        # The write's cause is its decision time: when the writer read the
        # session state it acted on (`decision_ts` on the attempt). A writer
        # that acted on a cached copy has none; then the latest protocol
        # message before the attempt, else the attempt itself, stands in.
        started = (attempt or {}).get("ts") or effect
        cause = (_ts((attempt or {}).get("decision_ts"))
                 or _latest_message_at_or_before(session_messages, started) or started)
        kind = response.get("kind")
        # The effect's exact time is known only to the database; Python
        # brackets it between the attempt and the response. The DB-clock
        # commit time (`effect_ts` on the response) is used when present.
        exact = _ts(response.get("effect_ts"))
        out.append({
            "kind": f"session_write:{kind}",
            "cause_ts": cause,
            "effect_ts": exact or effect,
            "effect_lo": exact or (attempt or {}).get("ts") or effect,
            "effect_hi": exact or effect,
            "terminal": outcome == "already_terminal" or kind in TERMINAL_KINDS,
            # Landed: the write took effect. Lost: it was refused or found the
            # row already terminal -- the case where a sweeper commit can have
            # contradicted it.
            "landed": outcome == "written",
            # Lost to a terminal guard: refused (not_written) by a guarded
            # writer, or answered already_terminal. A duplicate create is also
            # not_written, but nothing defeated it.
            "lost": (outcome == "already_terminal"
                     or (outcome == "not_written" and kind != "create")),
            "uncertain": uncertain,
            "value": response.get("requested"),
            "value_kind": ("status" if kind in TERMINAL_KINDS else kind),
            "reviewer_agent_id": (response.get("requested")
                                  if kind == "reviewer" and outcome == "written" else None),
            "detail": {k: response.get(k) for k in (
                "via", "site", "requested", "outcome", "origin", "reported_status",
                "saga_id", "terminal_reason", "attempt_id",
            ) if response.get(k) is not None},
        })

    out.sort(key=lambda c: c["effect_ts"])
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _reviewer_drift(
    write: Dict[str, Any],
    competing: Sequence[Dict[str, Any]],
    session_row: Optional[Dict[str, Any]],
    decision_read: Optional[dt.datetime],
    commit: Optional[dt.datetime],
    correlation: dt.timedelta,
) -> List[Dict[str, Any]]:
    """Evidence that the reviewer changed after the sweeper read it.

    Each entry carries ``at`` (the evidence's time, an upper bound on when the
    change happened) and ``placed_before_commit`` when the evidence itself
    proves the change landed inside the sweeper's read-to-commit interval.
    """
    read_reviewer = write.get("read_reviewer_agent_id")
    written = write.get("new_reviewer_agent_id")
    paused = write.get("paused_agent_id")
    known = {r for r in (read_reviewer, written, paused, "system") if r}
    drift = []
    for c in competing:
        if decision_read is not None and c["effect_ts"] <= decision_read:
            continue
        if commit is not None and c["effect_ts"] > commit + correlation:
            continue
        who = c.get("reviewer_agent_id") or (
            c.get("agent_id") if c["kind"] == "protocol_message" else None
        )
        if who and who not in known:
            drift.append({"evidence": c["kind"], "reviewer_agent_id": who,
                          "at": c["effect_ts"], "placed_before_commit": False})

    # The row itself. Reviewer writes (Python and BEAM `update_reviewer`) are
    # refused on a terminal row, and only `reopen_session` leaves one. So when
    # this SUCCEEDED reap is still the row's terminal write, the reviewer the
    # row names was written before the reap committed; if it differs from the
    # one the sweeper read, the change landed between the read and the reap --
    # ordering (b), though no durable event recorded it. "Still the row's
    # terminal write" is: the row is `failed` with no `resolution_json` (every
    # other terminal writer records one; the sweeper's reap does not) and no
    # later sweeper write touched the session. ⛔`updated_at` cannot decide
    # that -- `add_message` bumps it on every message, the sweeper's own
    # transcript line included -- but it is still an upper bound on when the
    # reviewer changed, which `classify_write` uses when it is not placed.
    if session_row is not None:
        current = session_row.get("reviewer_agent_id")
        if current and current not in known:
            placed = bool(
                write.get("outcome") == "succeeded"
                and write.get("intended_status") == "failed"
                and session_row.get("status") == "failed"
                and session_row.get("resolution_json") is None
                and write.get("_latest_sweeper_write_on_session", False)
            )
            drift.append({"evidence": "session_row", "reviewer_agent_id": current,
                          "at": None if placed else _ts(session_row.get("updated_at")),
                          "placed_before_commit": placed})
    return drift


def _same_value(c: Dict[str, Any], write: Dict[str, Any]) -> bool:
    """Would the competing write leave the value the sweeper wrote?

    Same-value overlaps have no adverse consequence -- both writers wanted the
    row to end the same way -- so they are contention-benign, never harm.
    """
    mutation = write.get("mutation")
    kind, value = c.get("value_kind"), c.get("value")
    if mutation == "reap":
        return kind == "status" and value == "failed"
    if mutation == "facilitation":
        return kind == "facilitation" and value is True
    if mutation == "reassignment":
        return kind == "reviewer" and value is not None \
            and value == write.get("new_reviewer_agent_id")
    return False


def classify_write(
    write: Dict[str, Any],
    *,
    competing: Sequence[Dict[str, Any]],
    session_row: Optional[Dict[str, Any]],
    correlation: dt.timedelta,
) -> Dict[str, Any]:
    """Assign one sweeper write to exactly one class, with its evidence.

    ``write`` is a ``dialectic_guarded_write`` payload plus its event ``ts``.
    HARM needs an adverse consequence: the sweeper acted on state a competing
    write had already changed (stale decision, ordering b), or its commit
    contradicted a competing write in flight (ordering a). A same-value
    overlap is contention-benign.
    """
    outcome = write.get("outcome")
    intended = write.get("intended_status")
    terminal_intent = intended in TERMINAL_STATUSES
    decision_read = _ts(write.get("decision_read_ts"))
    commit = _ts(write.get("commit_ts")) or _ts(write.get("ts"))
    evidence: Dict[str, Any] = {"harm_a": [], "harm_b": [], "harm_reverse": [],
                                "ambiguous": [], "same_value_overlap": [],
                                "later_writer": [], "reviewer_drift": [], "adjudicate": []}
    final_status = session_row.get("status") if session_row else None

    def _placed(c):
        return {**c.get("detail", {}), "kind": c["kind"], "cause_ts": _iso(c["cause_ts"]),
                "effect_ts": _iso(c["effect_ts"])}

    if outcome == "error":
        klass = "unknown_outcome"
    elif outcome == "refused":
        winner = write.get("winner_status")
        # Only a terminal row refuses a guarded write. A non-terminal winner
        # means the row changed again between the refused UPDATE and the
        # follow-up read (a reopen, say), so the read does not name the winner.
        # A reopen that landed around the refusal means the same even when the
        # read found a terminal row again: the read may describe the later
        # transition, not the one that refused the write.
        reopened_around = any(
            c["kind"] == "session_write:reopen" and c.get("landed")
            and decision_read is not None and commit is not None
            and decision_read < c["effect_ts"] <= commit + dt.timedelta(seconds=5)
            for c in competing
        )
        if winner is None or winner not in TERMINAL_STATUSES or reopened_around:
            klass = "contention_unattributed"
        elif winner == intended:
            klass = "contention_benign"
        else:
            klass = "contention_divergent"
        final_status = winner or final_status
    else:
        for c in competing:
            cause, effect = c["cause_ts"], c["effect_ts"]
            if commit is None:
                continue
            same = _same_value(c, write)
            # AMBIGUOUS: the competing effect is known only as an interval, and
            # that interval contains the sweeper's commit (or its decision
            # read), so the two cannot be ordered. Never harm, never clean.
            # Only a write that LANDED can be ambiguous: one refused or answered
            # already_terminal found the row terminal, which already places its
            # effect after the commit that made it so.
            lo, hi = c.get("effect_lo"), c.get("effect_hi")
            if lo is not None and hi is not None and lo < hi and (
                lo <= commit <= hi
                or (decision_read is not None and lo <= decision_read <= hi)
            ) and c.get("landed") and not same:
                evidence["ambiguous"].append(
                    {**_placed(c), "effect_lo": _iso(lo), "effect_hi": _iso(hi)})
                continue
            # A write whose effect is unknown is ambiguous wherever it could
            # matter: its bracket meets the read-to-bound interval.
            if c.get("uncertain"):
                start = decision_read or commit
                if lo is not None and hi is not None and lo <= commit + correlation \
                        and hi >= start and not same:
                    evidence["ambiguous"].append(
                        {**_placed(c), "effect_lo": _iso(lo), "effect_hi": _iso(hi),
                         "effect_unknown": True})
                continue
            # (b) stale decision: the competing write took effect between the
            # sweeper's read and its commit.
            if decision_read is not None and decision_read < effect <= commit \
                    and c.get("landed"):
                evidence["same_value_overlap" if same else "harm_b"].append(_placed(c))
                continue
            if c.get("b_only"):
                continue
            straddles = (cause < commit < effect and commit - cause <= correlation
                         and effect - commit <= correlation)
            after = commit < effect <= commit + correlation
            # (a) in flight across the commit, and defeated by it.
            if straddles and (
                c.get("lost")
                # A saga committing onto the row the reap made terminal writes
                # zero rows; a resolution in flight while the sweeper flagged
                # or reassigned was decided on stale state.
                or c["kind"] == "saga"
            ):
                evidence["same_value_overlap" if same else "harm_a"].append(_placed(c))
                continue
            # (c) REVERSE: after a reap, a competing write that landed and
            # invalidates it -- a message appended to the failed session, a
            # reopen, a reviewer/phase/facilitation write that revives it --
            # whatever the final status says.
            if after and terminal_intent and c.get("landed") and not c["terminal"] \
                    and c["kind"] != "saga":
                evidence["harm_reverse"].append(_placed(c))
                continue
            if straddles:
                evidence["later_writer"].append(_placed(c))
                continue
            # A later terminal writer, caused after the commit, within the bound.
            if c["terminal"] and commit <= cause and effect - commit <= correlation \
                    and terminal_intent:
                evidence["later_writer"].append(_placed(c))

        for d in _reviewer_drift(write, competing, session_row, decision_read, commit,
                                 correlation):
            at = d.get("at")
            entry = {**d, "at": _iso(at)}
            inside = (at is not None and decision_read is not None and commit is not None
                      and decision_read < at <= commit)
            if d["placed_before_commit"] or inside:
                evidence["harm_b"].append({"kind": "reviewer_drift", **entry})
            else:
                evidence["reviewer_drift"].append(entry)
                evidence["adjudicate"].append(
                    "reviewer changed after the sweeper's read; the change is placed "
                    "no later than " + (entry["at"] or "an unknown time")
                )

        if evidence["harm_a"] or evidence["harm_b"] or evidence["harm_reverse"]:
            klass = "harm"
        elif evidence["ambiguous"]:
            klass = "ambiguous"
        elif evidence["later_writer"] and final_status is not None \
                and final_status != intended and terminal_intent:
            klass = "contention_divergent"
        elif evidence["later_writer"] or evidence["same_value_overlap"]:
            klass = "contention_benign"
        else:
            klass = "uncontended"

    return {
        "session_id": write.get("session_id"),
        "mutation": write.get("mutation"),
        "attempted": write.get("attempted"),
        "outcome": outcome,
        "intended_status": intended,
        "final_status": final_status,
        "class": klass,
        "decision_read_ts": write.get("decision_read_ts"),
        "commit_ts": write.get("commit_ts"),
        "read_reviewer_agent_id": write.get("read_reviewer_agent_id"),
        "read_phase": write.get("read_phase"),
        "probe_outcome": write.get("probe_outcome"),
        "winner_status": write.get("winner_status"),
        "winner_reason": write.get("winner_reason"),
        "cycle_id": write.get("cycle_id"),
        "attempt_id": write.get("attempt_id"),
        "evidence": {k: v for k, v in evidence.items() if v},
    }


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def _balanced(p: Dict[str, Any]) -> Optional[bool]:
    keys = ("write_attempt_count", "write_succeeded_count", "write_refused_count",
            "write_error_count", "overlap_clean_count", "overlap_detected_count",
            "overlap_probe_failed_count")
    if any(k not in p for k in keys):
        return None
    n = {k: int(p.get(k) or 0) for k in keys}
    return (
        n["write_attempt_count"] == n["write_succeeded_count"]
        + n["write_refused_count"] + n["write_error_count"]
        and n["write_succeeded_count"] == n["overlap_clean_count"]
        + n["overlap_detected_count"] + n["overlap_probe_failed_count"]
    )


def heartbeat(
    cycles: Sequence[Dict[str, Any]],
    *,
    since: dt.datetime,
    until: dt.datetime,
    silence: dt.timedelta,
) -> Dict[str, Any]:
    """Per-boot coverage of the cycle stream. ``cycles`` are audit rows."""
    rows = []
    legacy = 0
    for c in cycles:
        p = _payload(c)
        t = _ts(c.get("ts"))
        if t is None:
            continue
        if not p.get("process_boot_id"):
            legacy += 1
            continue
        rows.append({"ts": t, **p})
    rows.sort(key=lambda r: r["ts"])

    boots: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        boots[r["process_boot_id"]].append(r)

    boot_reports = []
    for boot_id, brows in boots.items():
        seqs = sorted(int(r["cycle_seq"]) for r in brows if r.get("cycle_seq") is not None)
        missing: List[int] = []
        for a, b in zip(seqs, seqs[1:]):
            if b > a + 1:
                missing.extend(range(a + 1, b))
        periodic = [r for r in brows if r.get("trigger_source") == "periodic"]
        silences = []
        for a, b in zip(periodic, periodic[1:]):
            gap = b["ts"] - a["ts"]
            if gap > silence:
                silences.append({"from": _iso(a["ts"]), "to": _iso(b["ts"]),
                                 "minutes": round(gap.total_seconds() / 60, 1)})
        boot_reports.append({
            "process_boot_id": boot_id,
            "first_ts": _iso(brows[0]["ts"]),
            "last_ts": _iso(brows[-1]["ts"]),
            "rows": len(brows),
            "periodic_rows": len(periodic),
            "code_commits": sorted({str(r.get("code_commit")) for r in brows}),
            "missing_cycle_seq": missing[:50],
            "missing_cycle_seq_count": len(missing),
            "in_boot_silences": silences,
            "timeouts": sum(1 for r in brows if r.get("error") == "timeout"),
            "errors": sum(1 for r in brows if r.get("error") not in (None, "timeout")),
            "unbalanced_rows": sum(1 for r in brows if _balanced(r) is False),
        })
    boot_reports.sort(key=lambda b: b["first_ts"])

    restarts = []
    for a, b in zip(boot_reports, boot_reports[1:]):
        gap = _ts(b["first_ts"]) - _ts(a["last_ts"])
        restarts.append({"from_boot": a["process_boot_id"], "to_boot": b["process_boot_id"],
                         "last_row": a["last_ts"], "first_row": b["first_ts"],
                         "minutes": round(gap.total_seconds() / 60, 1)})

    # Coverage: the share of the window spanned by consecutive periodic rows
    # (across boots, in time order) no further apart than the silence bound.
    periodic_all = [r["ts"] for r in rows if r.get("trigger_source") == "periodic"]
    covered = dt.timedelta(0)
    for a, b in zip(periodic_all, periodic_all[1:]):
        if b - a <= silence:
            covered += b - a
    span = until - since
    first_periodic = next((r for r in rows if r.get("trigger_source") == "periodic"
                           and r.get("instrument_version") == INSTRUMENT_VERSION), None)
    return {
        "legacy_rows_without_boot_id": legacy,
        "v2_rows": len(rows),
        "boots": boot_reports,
        "restarts": restarts,
        # None, not 0%, when there is nothing to measure: no v2 periodic rows
        # means the instrument has not run here, which is not a gap it saw.
        "periodic_coverage": (
            (covered / span) if span.total_seconds() > 0 and periodic_all else None
        ),
        "first_periodic_v2_row": (
            {"ts": _iso(first_periodic["ts"]),
             "code_commit": first_periodic.get("code_commit"),
             "code_commit_source": first_periodic.get("code_commit_source")}
            if first_periodic else None
        ),
    }


Interval = tuple  # (start, end) datetimes


def _merge(intervals: List[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _inside(t: Optional[dt.datetime], intervals: Sequence[Interval]) -> bool:
    return t is not None and any(lo <= t <= hi for lo, hi in intervals)


def _overlap(lo: dt.datetime, hi: dt.datetime, intervals: Sequence[Interval]) -> dt.timedelta:
    total = dt.timedelta(0)
    for a, b in intervals:
        start, end = max(lo, a), min(hi, b)
        if end > start:
            total += end - start
    return total


def uncovered_intervals(
    *,
    cycles: Sequence[Dict[str, Any]],
    ledger_lines: Sequence[Dict[str, Any]],
    since: dt.datetime,
    until: dt.datetime,
) -> Dict[str, Any]:
    """Intervals the instrument did not cover, because one of its emits failed.

    A recorded emit failure -- a cycle row's ``emit_failures_since_last_cycle``
    or a line in the durable ledger -- marks the interval from the last cycle
    row before it to the first cycle row after it as UNCOVERED, like a
    heartbeat gap: whatever happened inside may be missing a record. Its
    exposure is excluded and the window accrues coverage outside it; it does
    not make the whole window inconclusive. A ``cycle_seq`` gap is covered by
    such an interval only when a recorded failure explains it (the lost row's
    own failure is on the ledger); an unexplained gap stays an unmatched unit.
    A ledger line whose time cannot be read cannot be placed, and is returned
    as unplaceable (an unmatched unit).
    """
    rows = []
    for c in cycles:
        p = _payload(c)
        t = _ts(c.get("ts"))
        if t is not None and since <= t < until and p.get("process_boot_id"):
            rows.append((t, p))
    rows.sort(key=lambda r: r[0])
    times = [t for t, _ in rows]

    def _before(t):
        prior = [x for x in times if x < t]
        return prior[-1] if prior else since

    def _after(t):
        later = [x for x in times if x > t]
        return later[0] if later else until

    intervals: List[Interval] = []
    for t, p in rows:
        if int(p.get("emit_failures_since_last_cycle") or 0):
            intervals.append((_before(t), t))
    unplaceable = []
    for ln in ledger_lines:
        t = _ts(ln.get("ts"))
        if t is None:
            unplaceable.append(ln)
        else:
            intervals.append((_before(t), _after(t)))
    failure_intervals = _merge(intervals)

    # cycle_seq gaps, per boot, and whether a recorded failure explains them.
    by_boot: Dict[str, List[tuple]] = defaultdict(list)
    for t, p in rows:
        if p.get("cycle_seq") is not None:
            by_boot[p["process_boot_id"]].append((int(p["cycle_seq"]), t))
    explained_gaps, unexplained_gaps = [], []
    for boot, seqs in by_boot.items():
        seqs.sort()
        for (a, ta), (b, tb) in zip(seqs, seqs[1:]):
            if b > a + 1:
                gap = {"process_boot_id": boot, "missing_cycle_seq": list(range(a + 1, b)),
                       "from": _iso(ta), "to": _iso(tb)}
                if _overlap(ta, tb, failure_intervals) > dt.timedelta(0) or any(
                    ln.get("process_boot_id") == boot
                    and ln.get("cycle_seq") in range(a + 1, b) for ln in ledger_lines
                ):
                    explained_gaps.append(gap)
                    intervals.append((ta, tb))
                else:
                    unexplained_gaps.append(gap)
    merged = _merge(intervals)
    return {"intervals": merged, "explained_seq_gaps": explained_gaps,
            "unexplained_seq_gaps": unexplained_gaps, "unplaceable_ledger_lines": unplaceable}


def completeness(
    *,
    session_write_events: Sequence[Dict[str, Any]],
    guarded_writes: Sequence[Dict[str, Any]],
    cycles: Sequence[Dict[str, Any]],
    heartbeat_report: Dict[str, Any],
    window_sagas: Sequence[Dict[str, Any]],
    since: dt.datetime,
    until: dt.datetime,
    ledger: Optional[Dict[str, Any]] = None,
    ambiguous_writes: int = 0,
) -> Dict[str, Any]:
    """The A12 completeness check. Any unmatched unit => INCONCLUSIVE.

    Over the window, OUTSIDE the uncovered intervals (see
    `uncovered_intervals`), unit by unit:

    (a) every ``dialectic_session_write`` attempt has its response, and every
        response its attempt (per kind);
    (b) each cycle's ``dialectic_guarded_write`` rows equal its
        ``write_attempt_count`` (keyed by ``process_boot_id`` + ``cycle_seq``),
        and every guarded-write row belongs to a cycle row;
    (c) ``cycle_seq`` is continuous within each boot, or its gap is explained
        by a recorded emit failure (then it is an uncovered interval);
    (d) every committed Python-routed saga (not BEAM liveness's) created in
        the window is named by a session-write response;
    (e) the durable emit-failure ledger was readable, and each of its lines
        could be placed in time;
    (f) no classified write is AMBIGUOUS;
    (g) every v2 cycle row balances (attempted = succeeded + refused + error;
        succeeded = clean + detected + probe_failed) -- a row missing a field
        counts as not balancing.

    With no v2 periodic cycle row in the window the reading is
    ``NOT_STARTED``: the instrument did not run, and zero unmatched units over
    nothing is not completeness.

    ``guarded_writes`` and ``cycles`` may extend past the window by the
    correlation bound, so a cycle straddling an edge is matched whole.
    """
    def _in(t):
        return t is not None and since <= t < until

    ledger = ledger or {"path": None, "status": "not_read", "lines": []}
    ledger_lines = [ln for ln in ledger.get("lines", [])
                    if _ts(ln.get("ts")) is None or _in(_ts(ln.get("ts")))]
    unc = uncovered_intervals(cycles=cycles, ledger_lines=ledger_lines,
                              since=since, until=until)
    uncovered = unc["intervals"]
    explained = []

    def _counted(t, item):
        if _inside(t, uncovered):
            explained.append(item)
            return False
        return True

    # (a)
    pairs = pair_session_writes(session_write_events)
    per_kind: Dict[str, Dict[str, int]] = defaultdict(lambda: {"attempts": 0, "matched": 0})
    unmatched_a = []
    for aid, pr in pairs.items():
        attempt, response = pr["attempt"], pr["response"]
        if attempt is not None and _in(attempt["ts"]):
            k = str(attempt.get("kind"))
            per_kind[k]["attempts"] += 1
            if response is not None:
                per_kind[k]["matched"] += 1
            else:
                item = {"attempt_id": aid, "missing": "response", "kind": k,
                        "via": attempt.get("via"), "session_id": attempt.get("session_id"),
                        "ts": _iso(attempt["ts"])}
                if _counted(attempt["ts"], item):
                    unmatched_a.append(item)
        elif attempt is None and response is not None and _in(response["ts"]):
            item = {"attempt_id": aid, "missing": "attempt", "kind": response.get("kind"),
                    "via": response.get("via"), "session_id": response.get("session_id"),
                    "ts": _iso(response["ts"])}
            if _counted(response["ts"], item):
                unmatched_a.append(item)

    # (b)
    rows_per_cycle: Dict[str, int] = defaultdict(int)
    orphan_rows = []
    known_cycles = {_payload(c).get("cycle_id") for c in cycles if _payload(c).get("cycle_id")}
    for w in guarded_writes:
        p = _payload(w)
        cid = p.get("cycle_id")
        if cid:
            rows_per_cycle[cid] += 1
        t = _ts(w.get("ts"))
        if _in(t) and (not cid or cid not in known_cycles):
            item = {"attempt_id": p.get("attempt_id"), "cycle_id": cid,
                    "session_id": p.get("session_id"), "ts": _iso(t)}
            if _counted(t, item):
                orphan_rows.append(item)
    unmatched_b = []
    for c in cycles:
        p = _payload(c)
        t = _ts(c.get("ts"))
        if not _in(t) or not p.get("process_boot_id"):
            continue
        attempts = int(p.get("write_attempt_count") or 0)
        seen = rows_per_cycle.get(p.get("cycle_id"), 0) if p.get("cycle_id") else 0
        if attempts != seen:
            item = {"process_boot_id": p.get("process_boot_id"),
                    "cycle_seq": p.get("cycle_seq"), "cycle_id": p.get("cycle_id"),
                    "ts": _iso(t), "write_attempt_count": attempts,
                    "guarded_write_rows": seen, "cycle_error": p.get("error")}
            if _counted(t, item):
                unmatched_b.append(item)

    # (c)
    unmatched_c = unc["unexplained_seq_gaps"]

    # (g) Every row must balance: a row that does not is an instrument defect,
    # and a missing probe must never read as a clean zero.
    unbalanced = []
    for c in cycles:
        p = _payload(c)
        t = _ts(c.get("ts"))
        if _in(t) and p.get("process_boot_id") and _balanced(p) is not True:
            item = {"process_boot_id": p.get("process_boot_id"),
                    "cycle_seq": p.get("cycle_seq"), "ts": _iso(t)}
            if _counted(t, item):
                unbalanced.append(item)

    # (d) committed Python-routed sagas no response names.
    named = {str(pr["response"].get("saga_id")) for pr in pairs.values()
             if pr["response"] is not None and pr["response"].get("saga_id")}
    python_routed = [g for g in window_sagas if g.get("payload_reason") != "liveness_timeout"]
    unnamed = []
    for g in python_routed:
        if str(g.get("saga_id")) not in named:
            item = {"saga_id": str(g.get("saga_id")), "created_at": _iso(_ts(g.get("created_at")))}
            if _counted(_ts(g.get("created_at")), item):
                unnamed.append(item)

    # (e)
    ledger_unreadable = 1 if ledger.get("status") == "unreadable" else 0
    unplaceable = unc["unplaceable_ledger_lines"]

    unmatched = (len(unmatched_a) + len(orphan_rows) + len(unmatched_b)
                 + sum(len(g["missing_cycle_seq"]) for g in unmatched_c)
                 + len(unnamed) + ledger_unreadable + len(unplaceable) + ambiguous_writes
                 + len(unbalanced))
    if heartbeat_report.get("first_periodic_v2_row") is None:
        reading = "NOT_STARTED"
    else:
        reading = "COMPLETE" if unmatched == 0 else "INCONCLUSIVE"
    return {
        "reading": reading,
        "unmatched_units": unmatched,
        "uncovered_intervals": [{"from": _iso(a), "to": _iso(b),
                                 "minutes": round((b - a).total_seconds() / 60, 1)}
                                for a, b in uncovered],
        "units_explained_by_uncovered_intervals": explained,
        "explained_seq_gaps": unc["explained_seq_gaps"],
        "session_writes_by_kind": {k: dict(v) for k, v in sorted(per_kind.items())},
        "unmatched_session_writes": unmatched_a,
        "cycles_whose_rows_do_not_match": unmatched_b,
        "guarded_write_rows_without_a_cycle": orphan_rows,
        "cycle_seq_gaps": unmatched_c,
        "ambiguous_writes": ambiguous_writes,
        "unbalanced_cycle_rows": unbalanced,
        "emit_failure_ledger": {"path": ledger.get("path"), "status": ledger.get("status"),
                                "lines_in_window": ledger_lines,
                                "unplaceable_lines": unplaceable},
        "saga_crosscheck": {
            "committed_python_routed_sagas": len(python_routed),
            "named_by_a_response": len(python_routed) - len(
                [g for g in python_routed if str(g.get("saga_id")) not in named]),
            "unnamed_saga_ids": [u["saga_id"] for u in unnamed][:50],
            "liveness_sagas_excluded": len(window_sagas) - len(python_routed),
        },
        "_uncovered": uncovered,
    }


def inventory_observation() -> List[Dict[str, Any]]:
    """Each inventory writer and how its writes are observed."""
    inv = _load_inventory()
    return [
        {"file": path, "function": function, "writer": entry.get("writer"),
         "observed_via": inv.observation(path, function)}
        for (path, function), entry in sorted(inv.CALL_SITES.items())
    ]


def _load_inventory():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import wave3_writer_inventory  # noqa: E402 -- sibling module, path set above

    return wave3_writer_inventory


def analyze(
    *,
    writes: Sequence[Dict[str, Any]],
    cycles: Sequence[Dict[str, Any]],
    competing_events: Sequence[Dict[str, Any]],
    sessions: Sequence[Dict[str, Any]],
    messages: Sequence[Dict[str, Any]],
    sagas: Sequence[Dict[str, Any]],
    mismatches: Sequence[Dict[str, Any]],
    inferred_python_terminal: Sequence[Dict[str, Any]],
    since: dt.datetime,
    until: dt.datetime,
    window_sagas: Sequence[Dict[str, Any]] = (),
    emit_failure_ledger: Optional[Dict[str, Any]] = None,
    correlation_hours: float = DEFAULT_CORRELATION_HOURS,
    silence_minutes: float = DEFAULT_SILENCE_MINUTES,
) -> Dict[str, Any]:
    """Pure: check completeness, classify every write, grade the heartbeat.

    ``writes`` are ``dialectic_guarded_write`` audit rows (``ts`` + ``payload``)
    and ``cycles`` ``dialectic_sweep_cycle`` rows; both may extend past the
    window by the correlation bound, for edge matching. Only writes and
    cycles inside the window are classified and graded.
    """
    correlation = dt.timedelta(hours=correlation_hours)
    rows_by_session = {s["session_id"]: s for s in sessions}
    requested_since = since
    # The window cannot start before the instrument did: before the first
    # periodic v2 row nothing was recorded, so a clean zero there would be
    # manufactured. Clamp the effective start to that row.
    first_v2 = min(
        (_ts(c.get("ts")) for c in cycles
         if _payload(c).get("instrument_version") == INSTRUMENT_VERSION
         and _payload(c).get("trigger_source") == "periodic"
         and _ts(c.get("ts")) is not None and since <= _ts(c.get("ts")) < until),
        default=None,
    )
    if first_v2 is not None and first_v2 > since:
        since = first_v2

    def _in(t):
        return t is not None and since <= t < until

    all_payloads = []
    for w in writes:
        p = dict(_payload(w))
        p.setdefault("session_id", w.get("session_id"))
        p["ts"] = _iso(_ts(w.get("ts")))
        all_payloads.append(p)

    # The last sweeper write attempted on each session, for the row-state
    # placement rule in `_reviewer_drift`.
    latest: Dict[str, dt.datetime] = {}
    for p in all_payloads:
        t = _ts(p.get("commit_ts")) or _ts(p.get("ts"))
        sid = p.get("session_id")
        if t is not None and (sid not in latest or t > latest[sid]):
            latest[sid] = t
    for p in all_payloads:
        t = _ts(p.get("commit_ts")) or _ts(p.get("ts"))
        p["_latest_sweeper_write_on_session"] = (
            t is not None and latest.get(p.get("session_id")) == t
        )

    ledger = emit_failure_ledger or {"path": None, "status": "not_read", "lines": []}
    window_cycles = [c for c in cycles if _in(_ts(c.get("ts")))]
    uncovered = uncovered_intervals(
        cycles=window_cycles,
        ledger_lines=[ln for ln in ledger.get("lines", [])
                      if _ts(ln.get("ts")) is None or _in(_ts(ln.get("ts")))],
        since=since, until=until,
    )["intervals"]

    classified = []
    excluded_uncovered = []
    for p in all_payloads:
        if not _in(_ts(p.get("ts"))):
            continue
        commit_t = _ts(p.get("commit_ts")) or _ts(p.get("ts"))
        if _inside(commit_t, uncovered):
            # Its records may be incomplete: excluded from the reading, and
            # the window must accrue its exposure elsewhere.
            excluded_uncovered.append({"session_id": p.get("session_id"),
                                       "mutation": p.get("mutation"),
                                       "commit_ts": p.get("commit_ts")})
            continue
        sid = p.get("session_id")
        comp = competing_writes(sid, sagas=sagas, messages=messages, events=competing_events)
        classified.append(classify_write(
            p, competing=comp, session_row=rows_by_session.get(sid),
            correlation=correlation,
        ))

    counts = {k: 0 for k in CLASSES}
    by_mutation: Dict[str, Dict[str, int]] = defaultdict(lambda: {k: 0 for k in CLASSES})
    for c in classified:
        counts[c["class"]] += 1
        by_mutation[c.get("mutation") or "unknown"][c["class"]] += 1

    hb = heartbeat(window_cycles, since=since, until=until,
                   silence=dt.timedelta(minutes=silence_minutes))
    comp_check = completeness(
        session_write_events=competing_events, guarded_writes=writes, cycles=cycles,
        heartbeat_report=hb, window_sagas=window_sagas, since=since, until=until,
        ledger=ledger, ambiguous_writes=counts["ambiguous"],
    )
    comp_check.pop("_uncovered", None)
    # Coverage again, with the uncovered intervals taken out of the covered time.
    silence = dt.timedelta(minutes=silence_minutes)
    periodic = sorted(_ts(c.get("ts")) for c in window_cycles
                      if _payload(c).get("process_boot_id")
                      and _payload(c).get("trigger_source") == "periodic")
    covered = dt.timedelta(0)
    for a, b in zip(periodic, periodic[1:]):
        if b - a <= silence:
            covered += (b - a) - _overlap(a, b, uncovered)
    span = until - since
    hb["periodic_coverage_excluding_uncovered"] = (
        (covered / span) if periodic and span.total_seconds() > 0 else None)
    hb["uncovered_minutes"] = round(
        sum(((b - a) for a, b in uncovered), dt.timedelta(0)).total_seconds() / 60, 1)

    pairs = pair_session_writes(competing_events)
    already_terminal = [
        {"ts": _iso(pr["response"]["ts"]), "session_id": pr["response"].get("session_id"),
         **{k: pr["response"].get(k) for k in ("requested", "reported_status",
                                               "terminal_reason", "terminal_reason_read")}}
        for pr in pairs.values()
        if pr["response"] is not None and pr["response"].get("outcome") == "already_terminal"
        and _in(pr["response"]["ts"])
    ]

    return {
        "window": {"since": _iso(since), "until": _iso(until),
                   "requested_since": _iso(requested_since),
                   "correlation_hours": correlation_hours,
                   "silence_minutes": silence_minutes},
        "reading": comp_check["reading"],
        "counts_are_lower_bounds": comp_check["reading"] != "COMPLETE",
        "completeness": comp_check,
        "guarded_writes": len(classified),
        "writes_excluded_as_uncovered": excluded_uncovered,
        "class_counts": counts,
        "class_counts_by_mutation": {k: dict(v) for k, v in sorted(by_mutation.items())},
        "writes": classified,
        "saga_row_mismatches": [
            {k: (_iso(v) if isinstance(v, dt.datetime) else v) for k, v in m.items()}
            for m in mismatches
        ],
        "already_terminal": already_terminal,
        "liveness_note": (
            "BEAM DialecticLiveness resolves inside BEAM and never reaches Python, so "
            "no session-write record observes it; it only writes 'failed', so its "
            "overlap with a sweeper reap is contention-benign by construction. Its "
            "commits are saga rows with reason liveness_timeout."
        ),
        "inferred_python_terminal_writes": [
            {k: (_iso(v) if isinstance(v, dt.datetime) else v) for k, v in r.items()}
            for r in inferred_python_terminal
        ],
        "writer_inventory": inventory_observation(),
        "heartbeat": hb,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_text(report: Dict[str, Any]) -> str:
    w = report["window"]
    comp = report["completeness"]
    lower = report["counts_are_lower_bounds"]
    q = ">=" if lower else ""
    lines = [
        f"Wave 3 collision report  {w['since']} .. {w['until']}",
        *([f"  (requested --since {w['requested_since']}; the window starts at the first "
           "periodic v2 row, because nothing before it was recorded)"]
          if w.get("requested_since") != w["since"] else []),
        f"  correlation bound {w['correlation_hours']}h, silence bound {w['silence_minutes']} min",
        "",
    ]
    if report["reading"] == "NOT_STARTED":
        lines.append(
            "READING: NOT STARTED -- no periodic instrument-v2 cycle row in this window. "
            "Nothing below is a reading; no zero here may be cited."
        )
    elif lower:
        lines.append(
            f"READING: INCONCLUSIVE -- {comp['unmatched_units']} unmatched unit(s). "
            "The counts below are lower bounds, NOT a reading; no zero here may be cited."
        )
    else:
        lines.append("READING: COMPLETE -- every unit matched.")
    lines.append("")
    lines.append(f"Guarded sweeper writes: {q}{report['guarded_writes']}")
    for k in CLASSES:
        lines.append(f"  {k:<26} {q}{report['class_counts'][k]}")
    for mutation, counts in report["class_counts_by_mutation"].items():
        nonzero = ", ".join(f"{k}={q}{v}" for k, v in counts.items() if v)
        lines.append(f"    {mutation}: {nonzero or 'none'}")

    flagged = [x for x in report["writes"] if x["class"] != "uncontended"
               or x["evidence"].get("adjudicate")]
    if flagged:
        lines += ["", "Writes to read (harm rows are CANDIDATES for adjudication):"]
        for x in flagged:
            lines.append(
                f"  [{x['class']}] {x['session_id']} {x['mutation']} {x['outcome']} "
                f"commit={x['commit_ts']} final={x['final_status']} "
                f"winner={x['winner_status']}/{x['winner_reason']}"
            )
            for key, items in x["evidence"].items():
                for item in items:
                    lines.append(f"      {key}: {item}")

    if report["writes_excluded_as_uncovered"]:
        lines.append(f"  excluded (inside an uncovered interval): "
                     f"{len(report['writes_excluded_as_uncovered'])}")

    lines += ["", "Completeness (A12)"]
    for u in comp["uncovered_intervals"]:
        lines.append(f"  UNCOVERED {u['from']} .. {u['to']} ({u['minutes']} min): a recorded "
                     "emit failure; exposure here is excluded")
    if comp["units_explained_by_uncovered_intervals"]:
        lines.append(f"  units inside uncovered intervals (explained, not counted): "
                     f"{len(comp['units_explained_by_uncovered_intervals'])}")
    for kind, n in comp["session_writes_by_kind"].items():
        lines.append(f"  session writes [{kind}]: {n['matched']}/{n['attempts']} attempts answered")
    for u in comp["unmatched_session_writes"]:
        lines.append(f"    UNMATCHED {u['missing']} missing: {u}")
    lines.append(f"  cycles whose guarded-write rows differ from write_attempt_count: "
                 f"{len(comp['cycles_whose_rows_do_not_match'])}")
    for u in comp["cycles_whose_rows_do_not_match"]:
        lines.append(f"    UNMATCHED {u}")
    lines.append(f"  guarded-write rows with no cycle row: "
                 f"{len(comp['guarded_write_rows_without_a_cycle'])}")
    for u in comp["guarded_write_rows_without_a_cycle"]:
        lines.append(f"    UNMATCHED {u}")
    lines.append(f"  unexplained cycle_seq gaps: "
                 f"{sum(len(u['missing_cycle_seq']) for u in comp['cycle_seq_gaps'])}")
    for u in comp["cycle_seq_gaps"]:
        lines.append(f"    UNMATCHED boot {u['process_boot_id']} missing {u['missing_cycle_seq']}")
    for u in comp["explained_seq_gaps"]:
        lines.append(f"    explained by a recorded emit failure: boot {u['process_boot_id']} "
                     f"missing {u['missing_cycle_seq']}")
    lines.append(f"  ambiguous writes (cannot be ordered against the commit): "
                 f"{comp['ambiguous_writes']}")
    lines.append(f"  cycle rows that do not balance: {len(comp['unbalanced_cycle_rows'])}")
    for u in comp["unbalanced_cycle_rows"]:
        lines.append(f"    UNMATCHED {u}")
    led = comp["emit_failure_ledger"]
    lines.append(f"  emit-failure ledger ({led['status']}, {led['path']}): "
                 f"{len(led['lines_in_window'])} line(s) in window")
    if led["status"] == "absent":
        lines.append("    (no ledger file: none created yet -- it appears on the first "
                     "failure. If the server runs from another checkout, pass "
                     "--emit-failure-ledger.)")
    if led["status"] == "unreadable":
        lines.append("    UNMATCHED the ledger could not be read")
    for ln in led["unplaceable_lines"]:
        lines.append(f"    UNMATCHED unplaceable ledger line {ln}")
    xc = comp["saga_crosscheck"]
    lines.append(f"  committed Python-routed sagas named by a session-write response: "
                 f"{xc['named_by_a_response']}/{xc['committed_python_routed_sagas']} "
                 f"({xc['liveness_sagas_excluded']} liveness sagas excluded)")
    for sid in xc["unnamed_saga_ids"]:
        lines.append(f"    UNMATCHED saga {sid}")

    lines += ["", f"Saga/row mismatches (saga committed, row kept another status): "
              f"{len(report['saga_row_mismatches'])}"]
    for m in report["saga_row_mismatches"]:
        lines.append(f"  {m.get('session_id')} saga={m.get('saga_id')} row={m.get('row_status')}"
                     f"/{m.get('row_reason')} sweeper_reaped={m.get('sweeper_reaped')}")
    lines.append(f"BEAM already_terminal resolves: {len(report['already_terminal'])}")
    for e in report["already_terminal"]:
        lines.append(f"  {e['ts']} {e['session_id']} reported={e['reported_status']} "
                     f"requested={e['requested']} reason={e['terminal_reason']}")
    lines.append(f"  note: {report['liveness_note']}")
    lines.append(f"Python terminal writes INFERRED (terminal row, no committed saga with its "
                 f"payload): {len(report['inferred_python_terminal_writes'])}")

    lines += ["", "Writer inventory (scripts/ops/wave3_writer_inventory.py):"]
    for wr in report["writer_inventory"]:
        lines.append(f"  {wr['file']}::{wr['function']} -- {wr['observed_via']}")

    hb = report["heartbeat"]
    lines += ["", "Heartbeat"]
    fp = hb["first_periodic_v2_row"]
    lines.append("  first periodic v2 row: " + (
        f"{fp['ts']} code_commit={fp['code_commit']} ({fp['code_commit_source']})"
        if fp else "none in window -- the window has not started"))
    cov = hb["periodic_coverage_excluding_uncovered"]
    lines.append(f"  periodic coverage of --since..--until (gaps <= silence bound, uncovered "
                 f"intervals excluded): "
                 f"{'n/a (no v2 periodic rows)' if cov is None else f'{cov:.1%}'}; "
                 f"uncovered {hb['uncovered_minutes']} min")
    lines.append(f"  rows without process_boot_id (pre-v2, unclassifiable): "
                 f"{hb['legacy_rows_without_boot_id']}")
    for b in hb["boots"]:
        lines.append(
            f"  boot {b['process_boot_id'][:8]} {b['first_ts']} .. {b['last_ts']} "
            f"rows={b['rows']} periodic={b['periodic_rows']} "
            f"lost_audit_writes={b['missing_cycle_seq_count']} "
            f"hung_loop_silences={len(b['in_boot_silences'])} timeouts={b['timeouts']} "
            f"unbalanced={b['unbalanced_rows']} commit={','.join(b['code_commits'])}"
        )
        for sl in b["in_boot_silences"]:
            lines.append(f"      silence {sl['from']} .. {sl['to']} ({sl['minutes']} min)")
    for r in hb["restarts"]:
        lines.append(f"  restart {r['last_row']} -> {r['first_row']} ({r['minutes']} min)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _run_read_only(dsn: str, queries: Iterable[tuple]) -> List[List[Dict[str, Any]]]:
    """Run each (sql, params) in ONE read-only transaction; return row lists."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        # ⛔READ ONLY is enforced by the server: this report must never change
        # a dialectic row, a saga, or an audit event.
        conn.set_session(readonly=True)
        out = []
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
                for sql, params in queries:
                    cur.execute(sql, params)
                    out.append([dict(r) for r in cur.fetchall()])
        return out
    finally:
        conn.close()


def fetch(dsn: str, since: dt.datetime, until: dt.datetime,
          correlation_hours: float) -> Dict[str, List[Dict[str, Any]]]:
    bound = dt.timedelta(hours=correlation_hours)
    lo, hi = since - bound, until + bound
    # Writes and cycles are read past the window by the bound so a cycle that
    # straddles an edge is matched whole; `analyze` classifies only the window.
    writes, cycles, session_writes = _run_read_only(dsn, [
        (EVENTS_QUERY, {"event_types": [WRITE_EVENT], "lo": lo, "hi": hi}),
        (EVENTS_QUERY, {"event_types": [CYCLE_EVENT], "lo": lo, "hi": hi}),
        (EVENTS_QUERY, {"event_types": [SESSION_WRITE_EVENT], "lo": lo, "hi": hi}),
    ])
    session_ids = sorted({w.get("session_id") or _payload(w).get("session_id")
                          for w in writes} - {None})
    sessions, messages, sagas, mismatches, inferred, window_sagas = _run_read_only(dsn, [
        (SESSIONS_QUERY, {"session_ids": session_ids}),
        (MESSAGES_QUERY, {"session_ids": session_ids, "lo": lo, "hi": hi}),
        (SAGAS_QUERY, {"session_ids": session_ids, "lo": lo, "hi": hi}),
        (MISMATCH_QUERY, {"lo": since, "hi": until, "lo_bound": lo, "hi_bound": hi}),
        (INFERRED_PY_TERMINAL_QUERY, {"lo": since, "hi": until}),
        (WINDOW_SAGAS_QUERY, {"lo": since, "hi": until}),
    ])
    return {"writes": writes, "cycles": cycles, "competing_events": session_writes,
            "sessions": sessions, "messages": messages, "sagas": sagas,
            "mismatches": mismatches, "inferred_python_terminal": inferred,
            "window_sagas": window_sagas}


def read_emit_failure_ledger(path: Optional[str]) -> Dict[str, Any]:
    """Read the instrument's durable emit-failure ledger (JSON lines).

    ``absent`` (no file yet: it is created on the first failure) is not a
    failure; ``unreadable`` is, and counts as an unmatched unit.
    """
    if not path:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.environ.get("UNITARES_DIALECTIC_EMIT_FAILURE_LEDGER") or os.path.join(
            os.path.dirname(os.path.dirname(here)), "data", "dialectic",
            "instrument_emit_failures.jsonl")
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {"path": path, "status": "absent", "lines": []}
    try:
        lines = []
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except ValueError:
                        # A torn or corrupt line is still evidence of a failure.
                        lines.append({"ts": None, "raw": raw[:200]})
        return {"path": path, "status": "read", "lines": lines}
    except OSError as exc:
        return {"path": path, "status": "unreadable", "lines": [], "error": str(exc)}


def _parse_when(value: str) -> dt.datetime:
    parsed = _ts(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 timestamp: {value!r}")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--since", type=_parse_when,
                    help=f"Window start, ISO-8601 (default: --until minus {DEFAULT_WINDOW_DAYS} days)")
    ap.add_argument("--until", type=_parse_when, help="Window end, ISO-8601 (default: now)")
    ap.add_argument("--correlation-hours", type=float, default=DEFAULT_CORRELATION_HOURS,
                    help="Bound on harm ordering (a) and later-writer contention (default 6)")
    ap.add_argument("--silence-minutes", type=float, default=DEFAULT_SILENCE_MINUTES,
                    help="In-boot periodic silence that counts as a hung loop (default 22)")
    ap.add_argument("--emit-failure-ledger",
                    help="The instrument's emit-failure ledger (default: "
                         "$UNITARES_DIALECTIC_EMIT_FAILURE_LEDGER, else this checkout's "
                         "data/dialectic/instrument_emit_failures.jsonl)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args(argv)

    until = args.until or dt.datetime.now(dt.timezone.utc)
    since = args.since or until - dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    if since >= until:
        print("wave3_collision_report: --since must be before --until", file=sys.stderr)
        return 2
    try:
        data = fetch(args.dsn, since, until, args.correlation_hours)
    except Exception as exc:  # noqa: BLE001 -- a report reports, never raises
        print(f"wave3_collision_report: query failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    report = analyze(**data, since=since, until=until,
                     emit_failure_ledger=read_emit_failure_ledger(args.emit_failure_ledger),
                     correlation_hours=args.correlation_hours,
                     silence_minutes=args.silence_minutes)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
