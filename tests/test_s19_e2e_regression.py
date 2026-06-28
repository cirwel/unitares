"""S19 PR7: end-to-end regression test against the live test database.

Pins the contract that closes the Hermes-incident leak:

1. **HTTP path is closed for substrate-anchored UUIDs.** When a
   ``core.substrate_claims`` row exists for a UUID, an HTTP token-based
   resume request via PATH 2.8 is refused with the explicit
   ``substrate_anchored_uuid_requires_uds`` error. This is the leak-
   closing test — the exact code path Hermes hit on 2026-04-25.

2. **UDS path verifies via substrate-claim attestation.** When the same
   UUID arrives over UDS (peer_pid set), the substrate gate runs and a
   matching attestation accepts; a mismatched attestation rejects with
   the appropriate failure_code.

The test exercises the full chain: real DB lookup → real verification
logic → real handler decision. Only ``peer_attestation`` (the
launchctl/proc_pidpath/proc_pidinfo macOS calls) is mocked — those are
proven independently in ``test_substrate_peer_attestation.py``, and
mocking them lets the test run as a Python process rather than under a
launchd-managed service.

Skipped when the ``governance_test`` database is unavailable.
"""
from __future__ import annotations

import sys
import uuid as _uuid_module
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg  # noqa: F401
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)

from src.mcp_handlers.context import (
    SessionSignals,
    set_session_signals,
    reset_session_signals,
)
from src.mcp_handlers.identity import resolution as resolution_mod
from src.substrate.handler_gate import reset_cache_for_testing


@pytest.fixture
def db(live_postgres_backend, monkeypatch):
    """Provide the live test backend AND wire ``src.db.get_db`` to point
    at it.

    Without this wiring, ``fetch_substrate_claim`` (and any other module-
    level ``from src.db import get_db`` consumer) sees the production
    singleton — not the test backend the fixture set up. The patch keeps
    the wiring honest for the duration of one test.
    """
    # fetch_substrate_claim uses `from src.db import get_db` lazily inside
    # the function body, so patching the source module is sufficient —
    # the import binding is fresh on every call.
    import src.db as _db_mod
    monkeypatch.setattr(_db_mod, "get_db", lambda: live_postgres_backend)
    return live_postgres_backend


@pytest.fixture(autouse=True)
def _isolate_substrate_cache():
    """Each test starts with a clean module-level VerifiedPairsCache."""
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


async def _seed_substrate_resident(
    db,
    *,
    label: str,
    executable: str,
) -> str:
    """Insert a fresh agent + substrate-claim row. Returns the agent UUID.

    The fixture-shared ``live_postgres_backend`` truncates between tests
    so explicit cleanup isn't needed — the FK from substrate_claims to
    core.agents cascades correctly.
    """
    agent_id = str(_uuid_module.uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')",
            agent_id,
        )
        await conn.execute(
            """
            INSERT INTO core.substrate_claims (
                agent_id, expected_launchd_label, expected_executable_path,
                enrolled_by_operator, enrolled_at
            )
            VALUES ($1, $2, $3, TRUE, NOW())
            """,
            agent_id, label, executable,
        )
    return agent_id


# =============================================================================
# THE LEAK-CLOSING TEST: HTTP token resume for substrate UUID is refused
# =============================================================================


@pytest.mark.asyncio
async def test_http_token_resume_for_substrate_anchored_uuid_is_refused(db):
    """An external process presenting a copied resident anchor token over
    HTTP gets explicit rejection — pins the closure of the Hermes leak."""
    label = "com.unitares.sentinel.test"
    executable = "/opt/homebrew/bin/sentinel-test"
    agent_id = await _seed_substrate_resident(db, label=label, executable=executable)

    # No peer_pid → HTTP path. This is what Hermes' connection looked like.
    signals_token = set_session_signals(SessionSignals())
    try:
        result = await resolution_mod.resolve_session_identity(
            "session-key-hermes-cosplay",
            persist=False,
            resume=True,
            token_agent_uuid=agent_id,
        )
    finally:
        reset_session_signals(signals_token)

    assert result.get("resume_failed") is True
    assert result.get("error") == "substrate_anchored_uuid_requires_uds"
    msg = result.get("message", "")
    assert "UNITARES_UDS_SOCKET" in msg
    assert label in msg, f"message should name the registered label; got {msg!r}"


# =============================================================================
# UDS path: matching peer attestation accepts (substrate-claim verification)
# =============================================================================


@pytest.mark.asyncio
async def test_uds_path_with_matching_attestation_passes(db):
    """When the kernel-attested peer matches the registry (label + exec +
    start-time), the resume flow proceeds. Substrate verification
    substitutes for the Part-C token check, demonstrating the M3-v2
    ownership-equivalence claim end-to-end."""
    label = "com.unitares.vigil.test"
    executable = "/opt/homebrew/bin/vigil-test"
    agent_id = await _seed_substrate_resident(db, label=label, executable=executable)

    # Mock peer_attestation to return the registered values — this stands
    # in for the live launchctl/proc_pidpath calls a real launchd-managed
    # process would produce.
    fake_pa = SimpleNamespace(
        read_service_label=lambda pid: label,
        read_executable_path=lambda pid: executable,
        read_process_start_time=lambda pid: 1_777_000_000,
    )

    from src.substrate import handler_gate as gate_mod
    from src.substrate import verification as verify_mod

    # The PR3e wiring lives inside _try_resume_by_agent_uuid_direct, not
    # in resolve_session_identity. We exercise verification directly via
    # verify_substrate_at_resume — the same code path the handler invokes.
    result = await gate_mod.verify_substrate_at_resume(
        agent_id, peer_pid=12345, pa_module=fake_pa,
    )

    assert result is not None
    assert result.accepted is True, f"expected accept, got {result!r}"


# =============================================================================
# UDS path: mismatched attestation rejects with specific failure_code
# =============================================================================


@pytest.mark.asyncio
async def test_uds_path_with_label_mismatch_rejects(db):
    """A connecting process whose launchd label doesn't match the registry
    is rejected with ``failure_code='label_mismatch'``. Demonstrates that
    M3 actually verifies the claim, not just the existence of a row."""
    label = "com.unitares.chronicler.test"
    executable = "/opt/homebrew/bin/chronicler-test"
    agent_id = await _seed_substrate_resident(db, label=label, executable=executable)

    # Mock peer_attestation to return the WRONG label.
    fake_pa = SimpleNamespace(
        read_service_label=lambda pid: "com.someone.else",
        read_executable_path=lambda pid: executable,
        read_process_start_time=lambda pid: 1_777_000_000,
    )

    from src.substrate import handler_gate as gate_mod

    result = await gate_mod.verify_substrate_at_resume(
        agent_id, peer_pid=12345, pa_module=fake_pa,
    )

    assert result is not None
    assert result.accepted is False
    assert result.failure_code == "label_mismatch"
    assert "com.someone.else" in result.reason


@pytest.mark.asyncio
async def test_uds_path_with_executable_mismatch_rejects(db):
    """A2-escalated coverage: even with the right label, a binary-
    substituted process is rejected with ``failure_code='exec_mismatch'``.
    The core proof that ``expected_executable_path`` is load-bearing."""
    label = "com.unitares.sentinel.test"
    executable = "/opt/homebrew/bin/sentinel-test"
    agent_id = await _seed_substrate_resident(db, label=label, executable=executable)

    # Right label, WRONG executable path — the binary was substituted.
    fake_pa = SimpleNamespace(
        read_service_label=lambda pid: label,
        read_executable_path=lambda pid: "$HOME/projects/swapped-binary",
        read_process_start_time=lambda pid: 1_777_000_000,
    )

    from src.substrate import handler_gate as gate_mod

    result = await gate_mod.verify_substrate_at_resume(
        agent_id, peer_pid=12345, pa_module=fake_pa,
    )

    assert result is not None
    assert result.accepted is False
    assert result.failure_code == "exec_mismatch"


# =============================================================================
# UDS path: PID-reuse cache catches recycled-PID adversary
# =============================================================================


@pytest.mark.asyncio
async def test_uds_path_pid_reuse_rejected_on_second_connect(db):
    """Q3(e) coverage: same PID, different process_start_time across
    connects — the cache pins the first verified pair and rejects the
    second on PID reuse."""
    label = "com.unitares.sentinel.test"
    executable = "/opt/homebrew/bin/sentinel-test"
    agent_id = await _seed_substrate_resident(db, label=label, executable=executable)

    from src.substrate import handler_gate as gate_mod

    # First connect: succeeds, cache pins (pid=999, start_tvsec=1_777_000_000).
    fake_pa_first = SimpleNamespace(
        read_service_label=lambda pid: label,
        read_executable_path=lambda pid: executable,
        read_process_start_time=lambda pid: 1_777_000_000,
    )
    first = await gate_mod.verify_substrate_at_resume(
        agent_id, peer_pid=999, pa_module=fake_pa_first,
    )
    assert first is not None and first.accepted

    # Second connect, same PID, DIFFERENT start_tvsec — PID was recycled.
    fake_pa_second = SimpleNamespace(
        read_service_label=lambda pid: label,
        read_executable_path=lambda pid: executable,
        read_process_start_time=lambda pid: 1_777_000_999,
    )
    second = await gate_mod.verify_substrate_at_resume(
        agent_id, peer_pid=999, pa_module=fake_pa_second,
    )
    assert second is not None
    assert second.accepted is False
    assert second.failure_code == "pid_reuse"


# =============================================================================
# Non-substrate UUID over HTTP is unaffected (gate is self-scoping)
# =============================================================================


@pytest.mark.asyncio
async def test_http_path_non_substrate_uuid_falls_through(db):
    """A UUID with no substrate-claim row is unaffected by the gate.

    Inserts an agent into core.agents WITHOUT a substrate_claims row,
    then verifies PATH 2.8 doesn't reject for substrate reasons (the
    request would still fail downstream because we use persist=False
    and don't seed the broader identity record, but the failure mode
    is NOT the new substrate-HTTP gate)."""
    non_substrate_id = str(_uuid_module.uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')",
            non_substrate_id,
        )

    signals_token = set_session_signals(SessionSignals())  # HTTP path
    try:
        result = await resolution_mod.resolve_session_identity(
            "session-key-non-substrate",
            persist=False,
            resume=True,
            token_agent_uuid=non_substrate_id,
        )
    finally:
        reset_session_signals(signals_token)

    # The substrate-HTTP gate must NOT fire for non-substrate UUIDs. The
    # request may still fail for other reasons (the token-rebind path
    # checks _agent_exists_in_postgres / _get_agent_status); we only
    # assert that the failure mode is not the substrate-HTTP gate.
    assert result.get("error") != "substrate_anchored_uuid_requires_uds"
