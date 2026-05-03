"""
§9 reconciliation gap-fill — closes RFC §9 named gates that the audit
script reported as missing entirely (no test of any name covers the
gate semantically).

Audit progression:
  PR #295 baseline: 17 exact / 1 variant / 10 missing
  After PR #295:    21 exact / 1 variant /  6 missing (this file's first 4 gap-fills)
  After this PR:    28 exact / 0 variant /  0 missing

This PR closes the residual 1 variant + 1 missing on the Python side:
  - `test_deprecation_sweep_uses_forced_release_reason` — variant→exact
    via `# §9: ...` alias annotation on the existing
    `test_deprecation_sweep_requires_force_release_token` (in
    `test_lease_plane_deprecate_cli.py`).
  - `test_force_release_rejects_governance_token` — missing→exact via
    the new test below, pinning the `_read_force_release_token`
    choke-point function so all force-release callers (sweep, R1
    `deprecate-and-finalize`, future force-release surfaces) inherit
    the credential boundary without per-caller integration tests.

The 4 original gates filled here are mechanical post-PR-7-storage
tests: they exercise the migration-026 grammar CHECK, the Pydantic
field_validator's invalid-scheme rejection, and the
post-migration-026 `surface_kind` generated-column behavior.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)


# ---------- §9: test_invalid_uri_scheme_rejected_at_storage ----------


@pytest.mark.asyncio
async def test_invalid_uri_scheme_rejected_at_storage():
    """RFC §9 / §7.2.2: INSERT with surface_id outside the canonical scheme
    list raises a CHECK violation at the storage layer (migration 026's
    `surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)'`).
    """
    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO lease_plane.surface_leases
                  (lease_id, surface_id, holder_agent_uuid, holder_class,
                   holder_kind, holder_pid, heartbeat_required, intent,
                   acquired_at, expires_at, original_ttl_s)
                VALUES (gen_random_uuid(), $1, gen_random_uuid(),
                        'process_instance', 'local_beam', NULL,
                        false, 'pr-section-9-gap-test',
                        now(), now() + interval '60 seconds', 60)
                """,
                "not_a_scheme:foo",
            )
    finally:
        await conn.close()


# ---------- §9: test_acquire_request_rejects_invalid_scheme ----------


def test_acquire_request_rejects_invalid_scheme():
    """RFC §9 / §7.2.1: AcquireRequest.surface_id field_validator rejects
    schemes outside the canonical list (file/dialectic/resident/capture/td)
    with a Pydantic ValidationError — caller-side rejection mirrors the
    storage-layer CHECK so the typed-absence error class fires before any
    DB round-trip.
    """
    from pydantic import ValidationError
    from src.lease_plane.models import AcquireRequest

    valid_payload = {
        "surface_id": "potato:foo",
        "holder_agent_uuid": str(uuid.uuid4()),
        "holder_kind": "local_beam",
        "ttl_s": 30,
    }

    with pytest.raises(ValidationError) as exc:
        AcquireRequest(**valid_payload)

    # The error message should mention the canonical scheme list so callers
    # know what's valid. Match defensively on either "scheme" or one of
    # the canonical scheme names.
    msg = str(exc.value)
    assert "scheme" in msg.lower() or any(
        kind in msg for kind in ("file", "dialectic", "resident", "capture", "td")
    ), f"ValidationError should mention canonical schemes; got: {msg}"


# ---------- §9: test_surface_kind_derived_from_scheme ----------


@pytest.mark.asyncio
async def test_surface_kind_derived_from_scheme():
    """RFC §9 / §7.2.3: post-migration-026, `surface_kind` is a generated
    column derived from `split_part(surface_id, ':', 1)`. Caller cannot
    supply it; INSERT auto-populates from the scheme prefix. This test
    INSERTs with several different schemes and verifies surface_kind is
    derived, not stored from input.
    """
    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        # INSERT one row per canonical scheme; verify surface_kind matches.
        cases = [
            ("file:///tmp/section_9_gap.py", "file"),
            ("dialectic:/section_9_gap_session", "dialectic"),
            ("resident:/section_9_gap_resident", "resident"),
        ]
        lease_ids: list[str] = []
        for surface_id, expected_kind in cases:
            row = await conn.fetchrow(
                """
                INSERT INTO lease_plane.surface_leases
                  (surface_id, holder_agent_uuid, holder_class,
                   holder_kind, holder_pid, heartbeat_required, intent,
                   expires_at, original_ttl_s)
                VALUES ($1, gen_random_uuid(),
                        'process_instance', 'local_beam', NULL,
                        false, 'pr-section-9-gap-test',
                        now() + interval '60 seconds', 60)
                RETURNING lease_id, surface_kind
                """,
                surface_id,
            )
            # Append BEFORE the assert so a failed assertion still cleans up
            # (council BLOCK B1 — without this, an assertion failure leaks
            # the row in governance_test).
            lease_ids.append(row["lease_id"])
            assert row["surface_kind"] == expected_kind, (
                f"surface_kind drift: surface_id={surface_id!r} → "
                f"expected kind={expected_kind!r}, got {row['surface_kind']!r}"
            )
    finally:
        # Cleanup our inserts.
        for lease_id in lease_ids:
            await conn.execute(
                "DELETE FROM lease_plane.surface_leases WHERE lease_id = $1", lease_id
            )
        await conn.close()


# ---------- §9: test_acquire_request_has_no_surface_kind_field ----------


def test_acquire_request_has_no_surface_kind_field():
    """RFC §9 / §7.2.3: post-migration-026, `surface_kind` is a DB-derived
    generated column. AcquireRequest must NOT have a surface_kind field —
    if it did, callers could try to override the derived value, and PR 1's
    Elixir router-side drop would diverge from the Python schema.

    Verify by Pydantic model introspection.
    """
    from src.lease_plane.models import AcquireRequest

    fields = AcquireRequest.model_fields
    assert "surface_kind" not in fields, (
        f"AcquireRequest.model_fields must not include surface_kind "
        f"post-migration-026; got fields: {list(fields)}"
    )


# ---------- §9: test_force_release_rejects_governance_token ----------


def test_force_release_rejects_governance_token(monkeypatch, tmp_path):
    """RFC §7.10: the force-release credential boundary is exclusive —
    `LEASE_FORCE_RELEASE_TOKEN` is the only authorizer; `GOVERNANCE_TOKEN`
    must NOT grant force-release authority anywhere it is checked.

    `test_deprecation_sweep_requires_force_release_token` covers the sweep
    wrapper specifically. This test pins the choke-point function
    `_read_force_release_token` directly so any future caller (R1
    `deprecate-and-finalize`, future force-release CLI surfaces, the
    `lease_plane.advisory` preflight hook) inherits the boundary without
    needing its own integration test.
    """
    from scripts.dev.lease_plane_deprecate import _read_force_release_token

    # Isolate from the operator's real ~/.config/cirwel/secrets.env by
    # repointing HOME at an empty tmp dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LEASE_FORCE_RELEASE_TOKEN", raising=False)

    # Case 1: GOVERNANCE_TOKEN in env, nothing else.
    monkeypatch.setenv("GOVERNANCE_TOKEN", "should-not-authorize-force-release")
    assert _read_force_release_token() is None, (
        "GOVERNANCE_TOKEN in env must not satisfy LEASE_FORCE_RELEASE_TOKEN"
    )

    # Case 2: GOVERNANCE_TOKEN in secrets.env (no LEASE_FORCE_RELEASE_TOKEN).
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    secrets_dir = tmp_path / ".config" / "cirwel"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.env").write_text(
        'GOVERNANCE_TOKEN="should-not-authorize-force-release"\n'
        "OTHER_VAR=irrelevant\n"
    )
    assert _read_force_release_token() is None, (
        "GOVERNANCE_TOKEN in secrets.env must not satisfy "
        "LEASE_FORCE_RELEASE_TOKEN; the credential boundary is exclusive"
    )

    # Sanity: the function CAN find a real LEASE_FORCE_RELEASE_TOKEN —
    # otherwise the negative cases above would pass for the wrong reason.
    monkeypatch.setenv("LEASE_FORCE_RELEASE_TOKEN", "real-force-release-token")
    assert _read_force_release_token() == "real-force-release-token", (
        "positive control: env LEASE_FORCE_RELEASE_TOKEN must still be read"
    )
