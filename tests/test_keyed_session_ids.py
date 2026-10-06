"""Keyed stable session ids (identity/stable_session.py).

The stable client_session_id was ``agent-{uuid[:12]}``: anyone who knew an
agent's UUID could compute it and act as the agent. It is now
``agent-{uuid[:12]}-{tag}``, keyed with the server's continuity key, so knowing
the UUID is not enough. These tests pin the format, that a UUID alone cannot
produce an accepted id, how the resolver treats keyed, forged and legacy ids,
and the migration paths (old tokens, runtime observations).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.identity import stable_session as ss
from src.mcp_handlers.identity.shared import make_client_session_id

VICTIM = "5e728ecb-1234-4abc-8def-0123456789ab"
OTHER = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    """A server with a continuity key, so it issues keyed ids."""
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "keyed-session-test-secret")
    ss.forget_verified()
    yield
    ss.forget_verified()


def _candidates(*rows):
    return patch.object(ss, "_candidates", AsyncMock(return_value=list(rows)))


# --- format -----------------------------------------------------------------

def test_without_a_key_the_legacy_form_is_issued(monkeypatch):
    monkeypatch.delenv("UNITARES_CONTINUITY_TOKEN_SECRET", raising=False)
    assert make_client_session_id(VICTIM) == f"agent-{VICTIM[:12]}"


def test_the_keyed_form(keyed):
    csid = make_client_session_id(VICTIM)

    assert ss.classify(csid) == "keyed" and len(csid) == 39
    assert csid.startswith(f"agent-{VICTIM[:12]}-") and csid[6:18] == VICTIM[:12]
    assert csid == make_client_session_id(VICTIM)          # deterministic per agent
    assert csid != make_client_session_id(OTHER)
    assert set(csid[19:]) <= set("abcdefghijklmnopqrstuvwxyz234567")


def test_rotating_the_key_changes_every_id(keyed, monkeypatch):
    before = make_client_session_id(VICTIM)
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "rotated")
    assert make_client_session_id(VICTIM) != before


# --- the advisory: a UUID alone yields no accepted id ------------------------

def test_knowing_the_uuid_does_not_produce_an_accepted_id(keyed):
    real = make_client_session_id(VICTIM)
    guesses = [
        f"agent-{VICTIM[:12]}",                                             # legacy
        f"agent-{VICTIM[:12]}-" + base64.b32encode(                         # unkeyed HMAC
            hashlib.sha256(b"unitares.csid.v2|" + VICTIM.encode()).digest()).decode().lower()[:20],
        f"agent-{VICTIM[:12]}-" + base64.b32encode(                         # wrong key
            hmac.new(b"guess", b"unitares.csid.v2|" + VICTIM.encode(), hashlib.sha256).digest()
        ).decode().lower()[:20],
        real[:-1] + ("a" if real[-1] != "a" else "b"),                      # one char off
    ]
    for guess in guesses:
        assert not ss.verifies_for(guess, VICTIM), guess
    assert ss.verifies_for(real, VICTIM)
    assert not ss.verifies_for(real, OTHER)


# --- resolution ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_keyed_id_resolves_with_no_stored_binding_and_writes_nothing(keyed):
    from src.mcp_handlers.identity import resolution

    csid = make_client_session_id(VICTIM)
    cache = AsyncMock()
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}), \
         patch.object(resolution, "_get_redis", side_effect=AssertionError("no PATH 1")), \
         patch.object(resolution, "_get_agent_status", AsyncMock(return_value="active")), \
         patch.object(resolution, "_get_agent_label", AsyncMock(return_value="victim")), \
         patch.object(resolution, "_get_agent_id_from_metadata", AsyncMock(return_value=VICTIM)), \
         patch.object(resolution, "_substrate_http_reject", AsyncMock(return_value=None)), \
         patch.object(resolution, "_cache_session", cache):
        result = await resolution.resolve_session_identity(csid, resume=True)

    assert result["agent_uuid"] == VICTIM and result["source"] == "keyed_session"
    assert not result.get("created")
    cache.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, csid_for, reason", [
    ([{"agent_id": VICTIM, "status": "active", "disabled_at": None}], OTHER, "tag_mismatch"),
    ([], VICTIM, "no_such_agent"),
    ([{"agent_id": VICTIM, "status": "active", "disabled_at": None},
      {"agent_id": VICTIM[:12] + "x", "status": "active", "disabled_at": None}],
     VICTIM, "ambiguous_prefix"),
    ([{"agent_id": VICTIM, "status": "deleted", "disabled_at": "2026-10-06"}], VICTIM, "agent_deleted"),
])
async def test_a_bad_keyed_id_is_a_terminal_refusal(keyed, rows, csid_for, reason):
    """Forged, unknown, ambiguous or deleted: refused before PATH 1/2, never a
    fall through to another lookup."""
    from src.mcp_handlers.identity import resolution

    csid = make_client_session_id(csid_for)
    if csid_for == OTHER:  # same prefix as VICTIM, tag made for another agent
        csid = f"agent-{VICTIM[:12]}-{csid.rsplit('-', 1)[1]}"
    with _candidates(*rows), \
         patch.object(resolution, "_get_redis", side_effect=AssertionError("no PATH 1")):
        result = await resolution.resolve_session_identity(csid, resume=True)

    assert result["resume_failed"] is True
    assert result["error"] == "stable_session_id_rejected" and result["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, refused", [("refuse", True), ("log", False), ("accept", False)])
async def test_the_legacy_policy(keyed, monkeypatch, caplog, mode, refused):
    from src.mcp_handlers.identity import resolution

    monkeypatch.setenv("UNITARES_LEGACY_SESSION_IDS", mode)
    with patch.object(resolution, "_get_redis", return_value=None), \
         patch.object(resolution, "get_db", side_effect=RuntimeError("stop after the policy")):
        try:
            result = await resolution.resolve_session_identity(f"agent-{VICTIM[:12]}", resume=True)
        except RuntimeError:
            result = None

    if refused:
        assert result["error"] == "stable_session_id_rejected" and result["reason"] == "legacy_session_id"
    else:
        assert not (result or {}).get("error") == "stable_session_id_rejected"
    assert ("[LEGACY_SESSION_ID]" in caplog.text) is (mode != "accept")


def test_without_a_key_legacy_ids_are_never_refused(monkeypatch):
    monkeypatch.delenv("UNITARES_CONTINUITY_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("UNITARES_LEGACY_SESSION_IDS", "refuse")
    assert ss.legacy_refused(f"agent-{VICTIM[:12]}") is False


def test_the_product_default_refuses(monkeypatch):
    monkeypatch.delenv("UNITARES_LEGACY_SESSION_IDS", raising=False)
    assert ss.legacy_mode() == "refuse"


# --- migration ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_old_token_converts_to_the_keyed_id(keyed):
    """A token issued before keyed ids embeds agent-{uuid12}. It is verified
    and names its agent, so derivation presents that agent's keyed id."""
    from src.mcp_handlers.identity.session import create_continuity_token, derive_session_key

    token = create_continuity_token(VICTIM, f"agent-{VICTIM[:12]}")
    key = await derive_session_key(None, {"continuity_token": token})

    assert key == make_client_session_id(VICTIM) and ss.classify(key) == "keyed"


def test_the_sync_cache_rejects_a_keyed_id_bound_to_another_agent(keyed):
    from src.mcp_handlers.identity.shared import _get_identity_record_sync, _session_identities

    csid = make_client_session_id(VICTIM)
    _session_identities[csid] = {"bound_agent_id": OTHER, "bind_count": 1}
    try:
        with patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            assert _get_identity_record_sync(session_id=csid)["bound_agent_id"] is None
        _session_identities[csid] = {"bound_agent_id": VICTIM, "bind_count": 1}
        with patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            assert _get_identity_record_sync(session_id=csid)["bound_agent_id"] == VICTIM
    finally:
        _session_identities.pop(csid, None)


def test_a_keyed_id_counts_as_a_session_the_caller_holds(keyed):
    from src.mcp_handlers.context import set_credential_proof_uuid, set_session_proof_origin, set_session_resolution_source
    from src.mcp_handlers.identity.credential_issuance import credentials_issuable, note_session_proof

    set_credential_proof_uuid(None)
    set_session_resolution_source("explicit_client_session_id")
    set_session_proof_origin("caller_asserted")
    note_session_proof(VICTIM, make_client_session_id(VICTIM))
    try:
        assert credentials_issuable(VICTIM)[0] is True
    finally:
        set_credential_proof_uuid(None)
        set_session_proof_origin(None)
        set_session_resolution_source(None)


@pytest.mark.asyncio
async def test_runtime_observations_accept_a_keyed_id_without_a_session_row(keyed):
    from src import runtime_observations as ro

    db = MagicMock()
    db.get_session = AsyncMock(return_value=None)
    csid = make_client_session_id(VICTIM)
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}), \
         patch("src.db.get_db", return_value=db), \
         patch.object(ro, "_normalize", return_value=(VICTIM, csid, "e1", None, {"observation_kind": "test"})), \
         patch("src.audit_db.append_audit_event_async", AsyncMock(side_effect=RuntimeError("past the gate"))):
        with pytest.raises(RuntimeError, match="past the gate"):
            await ro.record_runtime_observation({"any": "payload"})

    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}), \
         patch("src.db.get_db", return_value=db), \
         patch.object(ro, "_normalize", return_value=(OTHER, csid, "e1", None, {})):
        with pytest.raises(ro.RuntimeObservationError) as exc:
            await ro.record_runtime_observation({"any": "payload"})
    assert exc.value.code == "identity_session_mismatch"  # refused, never looked up as a row
    db.get_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_archived_agents_keyed_id_resolves_as_archived(keyed):
    """Archiving sets disabled_at but is reversible: onboard(resume=true)
    reactivates the same identity, so the keyed id must still reach it."""
    from src.mcp_handlers.identity import resolution

    csid = make_client_session_id(VICTIM)
    with _candidates({"agent_id": VICTIM, "status": "archived", "disabled_at": "2026-10-01"}), \
         patch.object(resolution, "_get_redis", side_effect=AssertionError("no PATH 1")), \
         patch.object(resolution, "_get_agent_status", AsyncMock(return_value="archived")), \
         patch.object(resolution, "_get_agent_label", AsyncMock(return_value="victim")), \
         patch.object(resolution, "_get_agent_id_from_metadata", AsyncMock(return_value=VICTIM)), \
         patch.object(resolution, "_substrate_http_reject", AsyncMock(return_value=None)):
        result = await resolution.resolve_session_identity(csid, resume=True)

    assert result["agent_uuid"] == VICTIM and result.get("archived") is True


def test_audit_rows_store_a_keyed_id_as_a_digest(keyed):
    csid = make_client_session_id(VICTIM)
    ref = ss.audit_reference(csid)

    assert ref.startswith("csid:") and csid not in ref and ref == ss.audit_reference(csid)
    assert ss.audit_reference(f"agent-{VICTIM[:12]}") == f"agent-{VICTIM[:12]}"
    assert ss.audit_reference("16f5506c-dialectic") == "16f5506c-dialectic"
    assert ss.audit_reference(None) is None


@pytest.mark.asyncio
async def test_the_audit_writer_stores_the_digest(keyed):
    from datetime import datetime, timezone

    from src.db.base import AuditEvent
    from src.db.mixins.audit import AuditMixin

    conn = MagicMock()
    conn.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock(spec=AuditMixin)
    db.acquire = MagicMock(return_value=acquire)
    csid = make_client_session_id(VICTIM)

    await AuditMixin.append_audit_event(db, AuditEvent(
        ts=datetime.now(timezone.utc), event_id="", event_type="t", agent_id=VICTIM,
        session_id=csid, confidence=0.0, payload={}, raw_hash=None,
    ))

    args = conn.execute.await_args.args
    assert csid not in args and ss.audit_reference(csid) in args


@pytest.mark.asyncio
async def test_a_keyed_observation_passes_despite_an_expired_row(keyed):
    from src import runtime_observations as ro

    expired = MagicMock(agent_id=VICTIM)
    db = MagicMock()
    db.get_session = AsyncMock(return_value=expired)
    csid = make_client_session_id(VICTIM)
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}), \
         patch("src.db.get_db", return_value=db), \
         patch.object(ro, "_session_is_live", return_value=False), \
         patch.object(ro, "_normalize", return_value=(VICTIM, csid, "e1", None, {"observation_kind": "t"})), \
         patch("src.audit_db.append_audit_event_async", AsyncMock(side_effect=RuntimeError("past the gate"))):
        with pytest.raises(RuntimeError, match="past the gate"):
            await ro.record_runtime_observation({"any": "payload"})


@pytest.mark.asyncio
async def test_a_fresh_mint_does_not_bind_a_keyed_id_it_was_given(keyed):
    """force_new while presenting an existing keyed id: the new agent must not
    be recorded under the other agent's keyed id."""
    from src.mcp_handlers.identity import resolution

    old_csid = make_client_session_id(VICTIM)
    db = MagicMock()
    db.get_identity = AsyncMock(return_value=MagicMock(identity_id="i-new"))
    db.create_session = AsyncMock()
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    cache = AsyncMock()
    with patch.object(resolution, "get_db", return_value=db), \
         patch.object(resolution, "_cache_session", cache):
        result = await resolution.resolve_session_identity(old_csid, persist=True, force_new=True)

    # The mint itself succeeded, for a different agent...
    assert result.get("created") is True
    new_uuid = result["agent_uuid"]
    assert new_uuid != VICTIM and db.upsert_identity.await_count == 1
    # ...and the keyed id it was given, which names VICTIM, was never bound to it.
    db.create_session.assert_not_awaited()
    cache.assert_not_awaited()


def test_the_verified_cache_stays_bounded(keyed, monkeypatch):
    import time as _time

    monkeypatch.setattr(ss, "_VERIFIED_MAX", 3)
    later = _time.monotonic() + 600
    for i in range(3):
        ss._verified[f"k{i}"] = (VICTIM, later)
    assert len(ss._verified) == 3
    # A fourth live entry with the cache full of live entries starts over.
    import asyncio
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}):
        asyncio.run(ss.resolve_keyed(make_client_session_id(VICTIM)))
    assert len(ss._verified) == 1


@pytest.mark.asyncio
async def test_runtime_observations_apply_the_legacy_policy(keyed, monkeypatch):
    from src import runtime_observations as ro

    monkeypatch.setenv("UNITARES_LEGACY_SESSION_IDS", "refuse")
    db = MagicMock()
    db.get_session = AsyncMock(return_value=MagicMock(agent_id=VICTIM))
    with patch("src.db.get_db", return_value=db), \
         patch.object(ro, "_normalize", return_value=(VICTIM, f"agent-{VICTIM[:12]}", "e1", None, {"observation_kind": "t"})):
        with pytest.raises(ro.RuntimeObservationError) as exc:
            await ro.record_runtime_observation({"any": "payload"})
    assert exc.value.code == "legacy_session_id"
    db.get_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotating_the_key_revokes_a_cached_verification(keyed, monkeypatch):
    csid = make_client_session_id(VICTIM)
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}):
        assert (await ss.resolve_keyed(csid))[0] == VICTIM
        monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "rotated")
        uuid, refusal = await ss.resolve_keyed(csid)
    assert uuid is None and refusal["reason"] == "tag_mismatch"


@pytest.mark.asyncio
async def test_the_presence_lease_names_its_session_by_reference(keyed, monkeypatch):
    """Lease status shows audit_session to any caller, so the keyed id is not sent."""
    from tests.test_agent_presence_lease import _FakeClient, _patch_models
    from src.mcp_handlers.identity import agent_presence_lease as apl

    csid = make_client_session_id(VICTIM)
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    try:
        await apl.heartbeat_agent_presence(VICTIM, csid)
        assert client.acquired[0].audit_session == ss.audit_reference(csid)

        # After a restart the holder comes back from the lease row, in reference
        # form, and must still count as the releasing session's own.
        apl._lease_ids.clear()
        apl._lease_sessions.clear()
        monkeypatch.setattr(
            apl, "_lookup_live_lease",
            AsyncMock(return_value=("lease-123", ss.audit_reference(csid))),
        )
        result = await apl.release_agent_presence(VICTIM, (csid,))
        assert result == {"released": True, "reason": "released"}
    finally:
        for cache in (apl._lease_ids, apl._released_at, apl._released_sessions,
                      apl._locks, apl._lease_sessions, apl._touched):
            cache.clear()


@pytest.mark.asyncio
async def test_tool_usage_rows_store_the_digest(keyed):
    """Usage and outcome rows are read back by other callers' queries."""
    from src.db.mixins.tool_usage import ToolUsageMixin

    conn = MagicMock()
    conn.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock(spec=ToolUsageMixin)
    db.acquire = MagicMock(return_value=acquire)
    csid = make_client_session_id(VICTIM)

    await ToolUsageMixin.append_tool_usage(db, VICTIM, csid, "t", 1, True)

    args = conn.execute.await_args.args
    assert csid not in args and ss.audit_reference(csid) in args


def _keyed_resume_patches(resolution, bind_fp, current_fp, mode="strict"):
    from types import SimpleNamespace

    db = MagicMock()
    db.get_session_binding = AsyncMock(return_value=(
        {"agent_uuid": VICTIM, "bind_ip_ua": bind_fp} if bind_fp else None
    ))
    db.get_session = AsyncMock(return_value=None)
    return [
        _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}),
        patch.object(resolution, "_get_redis", return_value=None),
        patch.object(resolution, "get_db", return_value=db),
        patch.object(resolution, "_get_agent_status", AsyncMock(return_value="active")),
        patch.object(resolution, "_get_agent_label", AsyncMock(return_value="victim")),
        patch.object(resolution, "_get_agent_id_from_metadata", AsyncMock(return_value=VICTIM)),
        patch.object(resolution, "_substrate_http_reject", AsyncMock(return_value=None)),
        patch("config.governance_config.session_fingerprint_check_mode", return_value=mode),
        patch("src.mcp_handlers.context.get_session_signals",
              return_value=SimpleNamespace(ip_ua_fingerprint=current_fp)),
        patch("src.mcp_handlers.identity.handlers._broadcaster", return_value=None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_fp, resumed", [("fp-a", True), ("fp-b", False)])
async def test_a_keyed_resume_meets_the_strict_fingerprint_guard(keyed, current_fp, resumed):
    """A copied keyed id presented from another fingerprint is refused under
    strict mode, as PATH 1 refuses any other session key."""
    from contextlib import ExitStack

    from src.mcp_handlers.identity import resolution

    csid = make_client_session_id(VICTIM)
    with ExitStack() as stack:
        for p in _keyed_resume_patches(resolution, "fp-a", current_fp):
            stack.enter_context(p)
        result = await resolution.resolve_session_identity(csid, resume=True)

    if resumed:
        assert result["agent_uuid"] == VICTIM and not result.get("resume_failed")
    else:
        assert result.get("error") == "resume_rejected_hijack_guard"
        assert result.get("reason") == "fingerprint_mismatch"
        assert result.get("agent_uuid") != VICTIM


@pytest.mark.asyncio
async def test_a_keyed_resume_without_a_recorded_fingerprint_proceeds(keyed):
    """The global check never penalizes a missing fingerprint, and a keyed id
    needs no stored binding at all."""
    from contextlib import ExitStack

    from src.mcp_handlers.identity import resolution

    csid = make_client_session_id(VICTIM)
    with ExitStack() as stack:
        for p in _keyed_resume_patches(resolution, None, "fp-b"):
            stack.enter_context(p)
        result = await resolution.resolve_session_identity(csid, resume=True)

    assert result["agent_uuid"] == VICTIM and not result.get("resume_failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("status, reason", [("deleted", "agent_deleted"), (None, "no_such_agent")])
async def test_a_cached_keyed_id_is_refused_once_its_agent_is_deleted(keyed, status, reason):
    """The 60 s verification cache must not outlive a deletion, which another
    server process may have made: a hit re-reads the status by primary key."""
    csid = make_client_session_id(VICTIM)
    with _candidates({"agent_id": VICTIM, "status": "active", "disabled_at": None}), \
         patch.object(ss, "_status", AsyncMock(return_value="active")):
        assert await ss.resolve_keyed(csid) == (VICTIM, None)
    assert csid in ss._verified

    with patch.object(ss, "_candidates", AsyncMock(side_effect=AssertionError("cache hit"))), \
         patch.object(ss, "_status", AsyncMock(return_value=status)):
        uuid, refused = await ss.resolve_keyed(csid)

    assert uuid is None and refused["reason"] == reason
    assert csid not in ss._verified
