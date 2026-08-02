"""The continuity health surface must not overstate PostgreSQL's role.

`health_check` reported "PostgreSQL as the durable source of truth" for session
continuity while ~81% of live sessions existed only in Redis (measured
2026-08-02: 1,038 Redis `session:*` keys vs 193 `core.sessions` rows). An
operator reading that would conclude a Redis loss was survivable. It is not —
losing Redis loses the live bindings, which is exactly what the 2026-07-09
broker cutover demonstrated when a stale Redis binding left Lumen
governance-dark for 10.5 hours.

These tests pin the honest shape so the claim cannot silently come back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.identity_continuity import (  # noqa: E402
    format_identity_continuity_startup_message,
    get_identity_continuity_status,
)


def test_redis_mode_does_not_claim_postgres_owns_session_bindings():
    status = get_identity_continuity_status(redis_present=True, redis_operational=True)
    assert status["session_binding_source_of_truth"] == "redis"
    assert status["identity_source_of_truth"] == "postgres"
    # The flat field is forwarded into lite health payloads, so it carries the
    # split rather than the old bare "postgres".
    assert status["source_of_truth"] != "postgres"
    assert "redis" in status["source_of_truth"].lower()


def test_redis_mode_note_states_the_loss_consequence():
    note = get_identity_continuity_status(redis_present=True, redis_operational=True)["note"]
    lowered = note.lower()
    assert "authoritative" in lowered
    assert "only in redis" in lowered
    # The specific false claim that motivated this test.
    assert "postgresql as the durable source of truth" not in lowered


def test_degraded_local_names_the_volatile_binding_store():
    status = get_identity_continuity_status(redis_present=False)
    assert status["session_binding_source_of_truth"] == "in-memory"
    assert status["identity_source_of_truth"] == "postgres"


def test_startup_message_separates_identity_from_binding():
    msg = format_identity_continuity_startup_message(
        get_identity_continuity_status(redis_present=True, redis_operational=True)
    )
    assert "identities durable in PostgreSQL" in msg
    assert "redis" in msg.lower()
    assert "PostgreSQL remains the durable source of truth" not in msg


def test_lite_forwarded_keys_still_present():
    """admin/handlers.py and runtime_queries.py copy this fixed key set into the
    lite payload; dropping one would silently empty a field the dashboard reads."""
    status = get_identity_continuity_status(redis_present=True, redis_operational=True)
    for key in ("mode", "redis_present", "source_of_truth", "session_binding_backend"):
        assert key in status, f"lite-forwarded key {key} disappeared"
