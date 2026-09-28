"""Every code path that writes a dialectic session row, and how each is observed.

The Wave 3 reduced-scope gate defines its competing-writer set BY RULE: every
code path, in either runtime, that writes the ``status``, ``phase``,
``reviewer_agent_id`` or ``awaiting_facilitation`` column of
``core.dialectic_sessions``, or moves its ``updated_at`` (the sweeper's
staleness clock, which decides whether a session is swept at all). That last
clause brings in `DialecticDB.add_message`, whose separate ``UPDATE ... SET
updated_at`` runs on every persisted message, terminal rows included.

The gate also requires every such write to be OBSERVED: each emits a
``dialectic_session_write`` attempt record before it and a response record
after it (``src/dialectic_session_writes.py``), and the attempt's timestamp is
the write's causal time. ``tests/test_wave3_writer_inventory.py`` enforces
both halves against the code:

* every ``UPDATE``/``INSERT`` of ``core.dialectic_sessions`` in ``src/`` is a
  ``DialecticDB`` method listed in ``SQL_WRITERS``, that method is called only
  from its ``wrapped_by`` helper, and that helper records the pair;
* every BEAM client writer records the pair around its HTTP request;
* every call site of those helpers is listed in ``CALL_SITES``;
* every Elixir writer is listed with how it is observed: through the Python
  HTTP call that triggered it (``via="beam"``), or, for BEAM
  ``DialecticLiveness``, not from Python at all -- it writes ``failed`` only,
  so its overlap with a sweeper reap is contention-benign by construction, and
  its commits are saga rows carrying ``reason: liveness_timeout``.

It fails in both directions: a writer the inventory does not list, and an
entry naming a writer that no longer exists.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# The columns the gate's rule is about: the four coordination columns, and
# the staleness clock that decides whether the sweeper considers a session.
COORDINATION_COLUMNS = ("status", "phase", "reviewer_agent_id", "awaiting_facilitation")
SWEEP_ELIGIBILITY_COLUMNS = ("updated_at",)
RULE_COLUMNS = COORDINATION_COLUMNS + SWEEP_ELIGIBILITY_COLUMNS

SESSION_WRITE_EVENT = "dialectic_session_write"

DIALECTIC_DB = "src/dialectic_db.py"
BEAM_CLIENT = "src/mcp_handlers/dialectic/beam_resolve_client.py"
SAGA_EX = "elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex"
ROUTER_EX = "elixir/lease_plane/lib/unitares_lease_plane/http_router.ex"
LIVENESS_EX = "elixir/lease_plane/lib/unitares_lease_plane/dialectic_liveness.ex"
AUTO_RESOLVE = "src/mcp_handlers/dialectic/auto_resolve.py"
HANDLERS = "src/mcp_handlers/dialectic/handlers.py"
SESSION = "src/mcp_handlers/dialectic/session.py"

_OBSERVED_VIA_BEAM = (
    f"{SESSION_WRITE_EVENT} via=beam, recorded by the Python caller around the HTTP "
    "request that triggers it"
)

# (file, function) -> the SQL writer and how its writes are observed.
SQL_WRITERS: Dict[Tuple[str, str], Dict[str, object]] = {
    (DIALECTIC_DB, "DialecticDB.create_session"): {
        "columns": RULE_COLUMNS, "wrapped_by": "create_session_async"},
    (DIALECTIC_DB, "DialecticDB.update_session_phase"): {
        "columns": ("phase", "updated_at"), "wrapped_by": "update_session_phase_async"},
    (DIALECTIC_DB, "DialecticDB.reopen_session"): {
        "columns": ("status", "phase", "updated_at"), "wrapped_by": "reopen_session_async"},
    (DIALECTIC_DB, "DialecticDB.update_session_reviewer"): {
        "columns": ("reviewer_agent_id", "updated_at"),
        "wrapped_by": "update_session_reviewer_async"},
    (DIALECTIC_DB, "DialecticDB.update_session_status"): {
        "columns": ("status", "phase", "updated_at"),
        "wrapped_by": "update_session_status_async"},
    (DIALECTIC_DB, "DialecticDB.mark_awaiting_facilitation"): {
        "columns": ("awaiting_facilitation",),
        "wrapped_by": "mark_awaiting_facilitation_async"},
    (DIALECTIC_DB, "DialecticDB.update_session_awaiting_facilitation"): {
        "columns": ("awaiting_facilitation", "updated_at"),
        "wrapped_by": "update_session_awaiting_facilitation_async"},
    (DIALECTIC_DB, "DialecticDB.resolve_session"): {
        "columns": ("status", "phase", "updated_at"), "wrapped_by": "resolve_session_async"},
    (DIALECTIC_DB, "DialecticDB.add_message"): {
        "columns": SWEEP_ELIGIBILITY_COLUMNS, "wrapped_by": "add_message_async"},
    (SAGA_EX, "create_session"): {
        "columns": RULE_COLUMNS, "observed_via": _OBSERVED_VIA_BEAM},
    (SAGA_EX, "update_phase"): {
        "columns": ("phase", "updated_at"), "observed_via": _OBSERVED_VIA_BEAM},
    (SAGA_EX, "update_reviewer"): {
        "columns": ("reviewer_agent_id", "updated_at"), "observed_via": _OBSERVED_VIA_BEAM},
    (SAGA_EX, "commit_session_row"): {
        "columns": ("status", "phase", "updated_at"),
        "observed_via": (
            f"{_OBSERVED_VIA_BEAM} (resolve); BEAM liveness commits are saga rows with "
            "reason liveness_timeout, failed-only, benign by construction"
        )},
}

# The BEAM client functions that make a session write over HTTP; each must
# record the attempt/response pair itself.
BEAM_WRITERS = ("beam_create_session", "beam_update_phase", "beam_update_reviewer",
                "beam_resolve")

# (file, function) -> the writer helpers it calls. Every Python call site is
# observed through the helpers' own records; the entry says who the writer is.
CALL_SITES: Dict[Tuple[str, str], Dict[str, object]] = {
    (AUTO_RESOLVE, "_auto_resolve_stuck_sessions"): {
        "calls": ("add_message_async", "update_session_status_async",
                  "update_session_reviewer_async", "mark_awaiting_facilitation_async",
                  "update_session_awaiting_facilitation_async"),
        "writer": "sweeper (via=sweeper; plus its own dialectic_guarded_write per guarded write)",
    },
    (HANDLERS, "_apply_reviewer_reassignment"): {
        "calls": ("add_message_async", "reopen_session_async", "beam_update_reviewer",
                  "update_session_reviewer_async",
                  "update_session_awaiting_facilitation_async"),
        "writer": "request reassignment: reopen, BEAM update_reviewer, Python fallback",
    },
    (HANDLERS, "handle_submit_thesis"): {
        "calls": ("add_message_async", "beam_update_phase", "update_session_phase_async"),
        "writer": "thesis: message, phase advance (BEAM, Python fallback)",
    },
    (HANDLERS, "handle_submit_antithesis"): {
        "calls": ("add_message_async", "beam_update_phase", "update_session_phase_async",
                  "beam_update_reviewer", "update_session_reviewer_async"),
        "writer": "antithesis: message, phase advance, first-responder reviewer",
    },
    (HANDLERS, "handle_submit_synthesis"): {
        "calls": ("add_message_async", "beam_update_phase", "update_session_phase_async",
                  "beam_resolve", "resolve_session_async",
                  "update_session_awaiting_facilitation_async"),
        "writer": "synthesis: message, phase, terminal resolution, facilitation flag",
    },
    (HANDLERS, "_set_awaiting_facilitation"): {
        "calls": ("update_session_awaiting_facilitation_async",),
        "writer": "facilitation flag (request path)",
    },
    (HANDLERS, "handle_get_dialectic_session"): {
        "calls": ("add_message_async", "update_session_phase_async",
                  "update_session_awaiting_facilitation_async"),
        "writer": "timeout / stuck-reviewer handling on read",
    },
    (HANDLERS, "handle_request_dialectic_review"): {
        "calls": ("beam_create_session", "create_session_async"),
        "writer": "session creation",
    },
    (HANDLERS, "handle_llm_assisted_dialectic"): {
        "calls": ("add_message_async", "create_session_async", "update_session_phase_async",
                  "resolve_session_async"),
        "writer": "one-call LLM-assisted dialectic (Python only)",
    },
    (HANDLERS, "_run_synthetic_review"): {
        "calls": ("add_message_async", "update_session_phase_async", "resolve_session_async"),
        "writer": "synthetic review (Python only)",
    },
    (SESSION, "save_session"): {
        "calls": ("beam_resolve", "resolve_session_async", "beam_update_phase",
                  "update_session_phase_async"),
        "writer": "catch-all session flush (BEAM, Python fallback)",
    },
    # --- BEAM callers of DialecticSaga writers --------------------------------
    (ROUTER_EX, "POST /v1/dialectic/session"): {
        "calls": ("create_session",), "writer": "BEAM create (from Python)",
        "observed_via": _OBSERVED_VIA_BEAM},
    (ROUTER_EX, "POST /v1/dialectic/phase"): {
        "calls": ("update_phase",), "writer": "BEAM phase (from Python)",
        "observed_via": _OBSERVED_VIA_BEAM},
    (ROUTER_EX, "POST /v1/dialectic/reviewer"): {
        "calls": ("update_reviewer",), "writer": "BEAM update_reviewer (from Python)",
        "observed_via": _OBSERVED_VIA_BEAM},
    (ROUTER_EX, "POST /v1/dialectic/resolve"): {
        "calls": ("resolve",), "writer": "BEAM resolve (from Python)",
        "observed_via": _OBSERVED_VIA_BEAM},
    (LIVENESS_EX, "fail_stuck"): {
        "calls": ("resolve",), "writer": "BEAM DialecticLiveness (4 h, writes failed only)",
        "observed_via": (
            "not from Python: saga rows with reason liveness_timeout; failed-only, so "
            "its overlap with a sweeper reap is contention-benign by construction"
        )},
}

# Python helpers whose calls make a function a writer call site.
PY_WRITER_HELPERS = tuple(
    sorted({str(v["wrapped_by"]) for v in SQL_WRITERS.values() if "wrapped_by" in v})
) + BEAM_WRITERS
# Elixir DialecticSaga functions that reach a SQL writer.
EX_WRITER_FUNCTIONS = ("create_session", "update_phase", "update_reviewer", "resolve")


def observation(file: str, function: str) -> str:
    """How writes through a call site are observed, in words."""
    entry = CALL_SITES.get((file, function)) or {}
    if "observed_via" in entry:
        return str(entry["observed_via"])
    return f"{SESSION_WRITE_EVENT} attempt/response, recorded by the write helpers"


def python_call_sites() -> List[Tuple[str, str]]:
    return sorted(k for k in CALL_SITES if k[0].startswith("src/"))
