"""Wave 0 coordination-event emitter (RFC ).

Single-surface replay log for coordination-class failures across all services
(sentinel, governance_mcp, lease_plane, vigil, chronicler, watcher). Wave 1's
exit criterion (incident-rate trends, the "stop or proceed to BEAM port"
signal) reads from `audit.coordination_events`.

Wave 0 lands the foundation:
  - Migration 035 (the table)
  - This module (the emitter)
  - Tests pinning the envelope contract

Wave 0 step 2 wires actual call sites (asyncpg connect errors, anyio task-
group cancellations, executor pool exhaustion, MCP handler timeouts). Wave 0
step 3 lands the Chronicler projection. Wave 0 step 4 lands the dashboard panel.

Stability discipline: `event_type` extends ONLY by adding new dotted
namespaces (coordination_recovery.*, coordination_lifecycle.*, ...). Never
reuse or rename an existing event_type. The migration's CHECK constraint
enforces the regex `^(coordination_failure)\\.[a-z_]+$` today; new families
extend the alternation in a follow-up migration, never silently in caller code.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# Service enum mirrors migration 035's coordination_events_service_check.
# Drift caught by test_emit_rejects_unknown_service.
Service = Literal[
    "sentinel",
    "governance_mcp",
    "lease_plane",
    "vigil",
    "chronicler",
    "watcher",
]

# Wave 0 event_type values. Future families extend the regex in a follow-up
# migration AND add their values here in the same PR. Drift caught by
# test_event_type_constants_match_documented_set.
COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR = "coordination_failure.asyncpg_connect_error"
COORDINATION_FAILURE_ANYIO_CANCELLATION = "coordination_failure.anyio_cancellation"
COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION = "coordination_failure.executor_pool_exhaustion"
COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT = "coordination_failure.mcp_handler_timeout"

# Wave 2 schema extension (RFC roadmap §"Wave 2 — Wave 0 schema extension"):
# `coordination_failure.beam_python_boundary.*` namespace. Lays down the
# typed constants before Wave 3's handler-dispatch port so Wave 3's exit
# criterion #3 ("no new substrate-tax pattern at the Python-handler-body
# boundary") becomes measurable. Two directional subtypes — failures
# travelling either way across the BEAM/Python REST boundary — match the
# architect council C5 finding that subtype discrimination belongs in the
# event_type contract, not in a payload subtype enum.
#
# Documented payload shape (per Stability discipline §"event_type extends
# only by adding new dotted namespaces"; payload contract pinned at landing):
#
#   python_to_beam_request_failed:
#     payload = {
#       "endpoint": str,           # BEAM endpoint URL or stable identifier
#       "method": str,             # GET/POST/etc
#       "error_class": str,        # "timeout" | "connect_error" | "non_200" |
#                                  # "decode_error" | "other"
#       "status_code": int | None, # populated when error_class == "non_200"
#       "elapsed_ms": int | None,  # wall-clock from request start
#     }
#
#   beam_to_python_request_failed:
#     payload = {
#       "endpoint": str,           # governance-mcp endpoint identifier
#       "method": str,
#       "error_class": str,        # same enum as python_to_beam
#       "status_code": int | None,
#       "elapsed_ms": int | None,
#     }
#
# Wire-up call sites land in Wave 3 (or earlier if Wave 2 §"Lease-integration
# boundary hardening" produces them first). Adding new subtypes — e.g.,
# `coordination_failure.beam_python_boundary.contract_violation` — extends
# this section AND the WAVE_0_EVENT_TYPES set in the same PR; never silently.
COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED = (
    "coordination_failure.beam_python_boundary.python_to_beam_request_failed"
)
COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED = (
    "coordination_failure.beam_python_boundary.beam_to_python_request_failed"
)

# Wave 3a stop-sign event types (RFC docs/proposals/beam-wave-3a-read-only-handlers.md
# §4.2). PR #2 of v0.2 sequencing lands the constants ahead of PR #3's Python
# transport per-tool routing table — the routing-table fallback path is the
# first emitter of `coordination_failure.wave_3a.fallback`, the BEAM-side
# timeout enforcement is the first emitter of
# `coordination_failure.wave_3a.timeout`, and the BEAM response-shape validator
# is the first emitter of `coordination_failure.wave_3a.envelope_invalid`.
# Landing the constants here lets PR #3/PR #4 emit without re-extending the
# allowlist.
#
# Payload shape (per Stability discipline §"event_type extends only by adding
# new dotted namespaces"; payload contract pinned at landing):
#
#   coordination_failure.wave_3a.fallback:
#     payload = {
#       "tool_name": str,         # tool whose dispatch fell back to Python
#       "trigger": str,           # "timeout" | "non_200" | "decode_error" |
#                                 # "connect_error"
#       "elapsed_ms": int | None, # wall-clock spent before falling back
#     }
#
#   coordination_failure.wave_3a.timeout:
#     payload = {
#       "tool_name": str,
#       "elapsed_ms": int,        # exceeded the 500ms hard budget
#       "budget_ms": int,         # 500 in Wave 3a per §3.2
#     }
#
#   coordination_failure.wave_3a.envelope_invalid:
#     payload = {
#       "tool_name": str,
#       "detail": str,            # which §2.2 envelope key was missing/wrong
#       "envelope_keys": list[str],
#     }
#
# Migration 035's CHECK regex `^(coordination_failure)(\.[a-z_]+)+$` already
# accepts arbitrarily deep subtypes under `coordination_failure`, so no
# constraint modification is needed.
COORDINATION_FAILURE_WAVE_3A_FALLBACK = "coordination_failure.wave_3a.fallback"
COORDINATION_FAILURE_WAVE_3A_TIMEOUT = "coordination_failure.wave_3a.timeout"
COORDINATION_FAILURE_WAVE_3A_ENVELOPE_INVALID = (
    "coordination_failure.wave_3a.envelope_invalid"
)

# Wave 3 §14 prereq PR #1 (RFC docs/proposals/beam-wave-3-handler-dispatch.md
# §8.4): shadow-window divergence, ETS↔PG reconciliation divergence, and the
# cutover 503 circuit-breaker breach. Constants land ahead of their emitters
# per the Wave 3a precedent — the shadow comparator runner
# (scripts/ops/wave3_shadow_divergence_check.py, this PR) is the first
# shadow_divergence emitter; the ETS reconciliation (§10.3) and 503 aggregator
# (§3.2) emitters land with the Wave 3 implementation.
#
# Payload shape (pinned at landing per the Stability discipline above):
#
#   shadow_divergence (§8.2; payloads built ONLY by
#   governance_core.coordination_events_helpers.make_shadow_divergence_payload):
#     payload = {
#       "table_name": str,          # "identities" | "agents"
#       "agent_id": str,            # join-key value of the divergent row
#       "kind": str,                # "canonical_missing" | "shadow_missing" |
#                                   # "column_mismatch"
#       "divergent_columns": list,  # canonical column names; non-empty iff
#                                   # kind == "column_mismatch"
#     }
#
#   ets_pg_divergence (§10.3 pins {key, ets_value_hash, pg_value_hash};
#   `store` is added here because TWO ETS tables share this event_type and
#   §10.2's readiness-gated-:cold path is a second emitter — discriminators
#   belong in the payload contract, not relitigated per call site):
#     payload = {
#       "store": str,               # "agent_baselines" | "feature_flags"
#       "key": str,                 # agent_id or flag key
#       "ets_value_hash": str | None,  # None when ETS side missing/cold
#       "pg_value_hash": str | None,   # None when PG side missing
#     }
#
#   cutover_503_rate_breach (§3.2; numerator/denominator are the
#   MEASUREMENT_GOVERNANCE_MCP_* events below, same 60s window):
#     payload = {
#       "window_seconds": int,      # 60 per §3.2
#       "rate": float,              # count_503 / count_request in the window
#       "threshold": float,         # 0.01 per §3.2
#       "count_503": int,
#       "count_request": int,
#     }
COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE = (
    "coordination_failure.beam_python_boundary.shadow_divergence"
)
COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_ETS_PG_DIVERGENCE = (
    "coordination_failure.beam_python_boundary.ets_pg_divergence"
)
COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH = (
    "coordination_failure.governance_mcp.cutover_503_rate_breach"
)

WAVE_0_EVENT_TYPES: frozenset[str] = frozenset({
    COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
    COORDINATION_FAILURE_ANYIO_CANCELLATION,
    COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION,
    COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT,
    # Wave 2 extension — see the docstring above the constants.
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED,
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED,
    # Wave 3a extension — see RFC §4.2 stop signs.
    COORDINATION_FAILURE_WAVE_3A_FALLBACK,
    COORDINATION_FAILURE_WAVE_3A_TIMEOUT,
    COORDINATION_FAILURE_WAVE_3A_ENVELOPE_INVALID,
    # Wave 3 §14 prereq PR #1 — see the §8.4 docstring above the constants.
    # Migration 035/041's CHECK regex already accepts these — no DB follow-up.
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE,
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_ETS_PG_DIVERGENCE,
    COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH,
})

# Wave 3 cutover measurement channel (RFC §3.2, §6.3): informational
# numerator/denominator events for the 503 circuit-breaker. These target
# `audit.coordination_measurements` (migration 041) — NOT
# `audit.coordination_events` — so they are deliberately absent from
# WAVE_0_EVENT_TYPES and rejected by `_validate_event_type` / `emit_event`,
# which guard the failure channel only. Emitters land with prereq PR #6
# (lease-plane measurement bridge) and PR #10 (transport 503 emission);
# constants land first so those PRs emit without re-extending.
#
# Live-schema note (verified 2026-06-10): migration 041's CHECK is
# `^measurement(\.[a-z0-9_]+)+$` — digits are legal (so `503_emission` is a
# valid segment), and the `telemetry` family from the RFC §6.1 sketch was NOT
# shipped; introducing telemetry.* later requires a migration.
#
#   measurement.governance_mcp.request  (denominator — emitted per request
#     accepted for proxying during cutover):
#     payload = {"request_path": str}
#
#   measurement.governance_mcp.503_emission  (numerator — emitted per 503
#     returned during cutover):
#     payload = {"request_path": str, "error_reason": str}
#
#   measurement.lease_plane.request  (§14 prereq PR #6 — the disconfirmer-(B)
#     Phase A baseline; one row per client lease RPC, drained in batches by
#     perf_monitor_persist_task from src/lease_plane/client.py samples):
#     meta payload = make_measurement_payload(...) + {"samples_dropped_total"}
#     columns: endpoint = request path, status = outcome string,
#     elapsed_ms = client-perceived wall clock (the §0(B) quantity).
#
#   measurement.beam_python_boundary.request  (§6.3 — Wave 3 impl emits per
#     successful boundary call; constant lands ahead per the Wave 3a
#     precedent):
#     payload = {"endpoint": str, "method": str, "elapsed_ms": int,
#                "payload_bytes": int}
MEASUREMENT_GOVERNANCE_MCP_REQUEST = "measurement.governance_mcp.request"
MEASUREMENT_GOVERNANCE_MCP_503_EMISSION = "measurement.governance_mcp.503_emission"
MEASUREMENT_LEASE_PLANE_REQUEST = "measurement.lease_plane.request"
MEASUREMENT_BEAM_PYTHON_BOUNDARY_REQUEST = "measurement.beam_python_boundary.request"

MEASUREMENT_EVENT_TYPES: frozenset[str] = frozenset({
    MEASUREMENT_GOVERNANCE_MCP_REQUEST,
    MEASUREMENT_GOVERNANCE_MCP_503_EMISSION,
    MEASUREMENT_LEASE_PLANE_REQUEST,
    MEASUREMENT_BEAM_PYTHON_BOUNDARY_REQUEST,
})

# Cached emitter context — git_commit and host don't change at runtime.
# service_pid and running_since are per-process; the rest are per-host.
_CONTEXT_CACHE: dict[str, Any] | None = None


def _git_commit_short() -> str:
    """Return the short git SHA of the running deploy, or 'unknown' on failure.

    Captured once at first emit and cached. The captured value is the SHA at
    process startup — long-running services that survive across deploys will
    still report their original startup SHA, which is the correct attribution
    semantics (the running binary IS that SHA, regardless of what's checked
    out on disk now). Per memory `feedback_running-process-vs-master-commit`.
    """
    try:
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[coord-events] git_commit lookup failed: %r", exc)
    return "unknown"


def _build_context() -> dict[str, Any]:
    """Build the emitter context envelope. Cached after first call.

    Per the roadmap envelope spec: `{git_commit, service_pid, running_since, host}`.
    These are facts about the emitter, not the event — callers don't pass this.
    """
    global _CONTEXT_CACHE
    if _CONTEXT_CACHE is not None:
        return _CONTEXT_CACHE

    _CONTEXT_CACHE = {
        "git_commit": _git_commit_short(),
        "service_pid": os.getpid(),
        "running_since": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
    }
    return _CONTEXT_CACHE


def _validate_event_type(event_type: str) -> None:
    """Mirror of migration 035's regex CHECK on event_type, raised client-side
    so callers get a precise typed error before the DB rejects.

    Sub-namespaces are ALLOWED — `coordination_failure.mcp_handler_timeout` and
    `coordination_failure.mcp_handler_timeout.identity_step` both pass. Per the
    council architect C5 finding (post-v0.1 review): subtype discrimination
    belongs in the event_type contract, not in a payload subtype enum.

    Segment character class: ``[a-z0-9_]+``. Wave 3a (migration 041) relaxed
    the original ``[a-z_]+`` class to allow digits so ``wave_3a`` is a legal
    segment. The Python validator mirrors that broadened class — keeping
    client-side and DB-side acceptance sets identical.
    """
    if not isinstance(event_type, str):
        raise ValueError(f"event_type must be a string, got {type(event_type).__name__}")
    parts = event_type.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"event_type {event_type!r} must be 'family.subtype' or "
            f"'family.subtype.subsubtype' (RFC roadmap §94)"
        )
    family = parts[0]
    if family != "coordination_failure":
        raise ValueError(
            f"event_type {event_type!r}: family {family!r} not in Wave 0 set "
            f"({{'coordination_failure'}}). Add via migration when extending."
        )
    for segment in parts[1:]:
        if not segment or not all(
            c.islower() or c.isdigit() or c == "_" for c in segment
        ):
            raise ValueError(
                f"event_type {event_type!r}: every segment must be "
                f"lowercase + digits + underscores (failed segment: {segment!r})"
            )


async def emit_event(
    pool,
    *,
    service: Service,
    event_type: str,
    payload: dict[str, Any] | None = None,
    agent_id: str | None = None,
    ts: datetime | None = None,
) -> UUID:
    """Emit a coordination event into `audit.coordination_events`.

    Args:
        pool: an asyncpg connection pool. Caller-supplied so this module
              doesn't take a hard dependency on a specific pool helper.
        service: emitter identity, must be in the migration-035 service enum.
        event_type: dotted family.subtype, validated client-side here AND
                    server-side by the namespace CHECK constraint.
        payload: event-type-specific structure. MUST be a dict (mirrors the
                 jsonb_typeof = 'object' CHECK). Empty dict is valid.
        agent_id: optional UNITARES UUID when the event is agent-attributable.
        ts: optional override; defaults to now() in UTC. Pass-through for
            tests and for events captured asynchronously where the emit
            happens later than the event itself.

    Returns the event_id UUID (server-generated; useful for tests and replay).

    Failure semantics: this is observability infrastructure for OTHER bugs.
    A failure to emit MUST NOT crash the caller — but the caller is also
    not expected to wrap this in try/except. Errors here are logged at
    WARNING and re-raised so that test infrastructure can catch them; the
    individual call-site wrapping (Wave 0 step 2) controls swallow vs
    propagate per-site based on whether the emitter caller can afford to
    fail (most can't — they're already in an error path).
    """
    _validate_event_type(event_type)
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")

    event_id = uuid4()
    effective_ts = ts or datetime.now(UTC)
    context = _build_context()
    payload_to_write = payload or {}

    sql = """
    INSERT INTO audit.coordination_events
        (ts, event_id, service, event_type, agent_id, payload, context)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
    """

    import json
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            effective_ts,
            event_id,
            service,
            event_type,
            agent_id,
            json.dumps(payload_to_write),
            json.dumps(context),
        )

    logger.debug(
        "[coord-events] emitted %s/%s event_id=%s agent_id=%s",
        service,
        event_type,
        event_id,
        agent_id,
    )
    return event_id


def reset_context_cache_for_tests() -> None:
    """Clear the cached context so tests can re-derive without process reload."""
    global _CONTEXT_CACHE
    _CONTEXT_CACHE = None
