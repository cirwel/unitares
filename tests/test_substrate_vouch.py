"""Orchestrator-vouched identity — INERT PoC seam (pure-logic tests).

Covers ``src/substrate/vouch.py``. The module under test is NOT wired into
identity resolution (see its docstring + RFC orchestrator-vouched-identity-v0.md);
these tests construct ``VouchedBinding`` objects directly and never route through
the live resolution path — that boundary is the C2 inertness invariant.

Verified behaviour:
- no_binding / expired / missing_start_tvsec (both sides) / pid_mismatch /
  start_mismatch (PID reuse) / uuid_mismatch rejections
- happy path accept
- server-side TOFU is rejected (a row without start_tvsec never passes)
- the flag is default-off and inert
- the new label constants are ABSENT from the live strong/caller-asserted sets
  (drift-guard: the PoC must not create a live gating point)
"""
from __future__ import annotations

import pytest

from src.substrate.vouch import (
    PROOF_ORIGIN_ORCHESTRATOR_VOUCHED,
    SESSION_SOURCE_BEAM_ORCHESTRATED,
    VOUCH_FLAG_ENV,
    VouchedBinding,
    is_orchestrator_vouch_enabled,
    verify_vouched_binding,
)

_NOW = 1_000_000
_PID = 4242
_START = 9_999
_UUID = "11111111-1111-4111-8111-111111111111"
_VOUCHER = "22222222-2222-4222-8222-222222222222"


def _binding(**overrides) -> VouchedBinding:
    base = dict(
        child_uuid=_UUID,
        child_os_pid=_PID,
        child_start_tvsec=_START,
        voucher_uuid=_VOUCHER,
        expires_at_epoch=_NOW + 300,
    )
    base.update(overrides)
    return VouchedBinding(**base)


def _verify(binding, *, peer_pid=_PID, live_start=_START, uuid=_UUID, now=_NOW):
    return verify_vouched_binding(
        binding,
        peer_pid=peer_pid,
        live_start_tvsec=live_start,
        claimed_child_uuid=uuid,
        now_epoch=now,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_full_match_accepts():
    result = _verify(_binding())
    assert result.accepted is True
    assert result.failure_code is None
    assert str(_PID) in result.reason and _VOUCHER in result.reason


# --------------------------------------------------------------------------- #
# Rejections (check order)
# --------------------------------------------------------------------------- #


def test_no_binding_rejects():
    result = _verify(None)
    assert result.accepted is False
    assert result.failure_code == "no_binding"


def test_expired_rejects():
    result = _verify(_binding(expires_at_epoch=_NOW - 1))
    assert result.accepted is False
    assert result.failure_code == "expired"


def test_expiry_is_exclusive_at_boundary():
    # now == expires_at must be treated as expired (>=), not still-valid.
    result = _verify(_binding(expires_at_epoch=_NOW))
    assert result.accepted is False
    assert result.failure_code == "expired"


def test_binding_missing_start_tvsec_rejects_no_tofu():
    # The rejected server-side-TOFU case: a row that lacks start_tvsec must
    # fail, never silently pass (RFC O3 / dialectic seam 2).
    result = _verify(_binding(child_start_tvsec=None))
    assert result.accepted is False
    assert result.failure_code == "missing_start_tvsec"


def test_live_start_tvsec_unreadable_rejects():
    result = _verify(_binding(), live_start=None)
    assert result.accepted is False
    assert result.failure_code == "missing_start_tvsec"


def test_pid_mismatch_rejects():
    result = _verify(_binding(child_os_pid=_PID + 1))
    assert result.accepted is False
    assert result.failure_code == "pid_mismatch"


def test_start_mismatch_is_pid_reuse_rejection():
    # Same pid, different live start time → pid recycled to another process.
    result = _verify(_binding(), live_start=_START + 1)
    assert result.accepted is False
    assert result.failure_code == "start_mismatch"


def test_uuid_mismatch_rejects():
    result = _verify(_binding(), uuid="33333333-3333-4333-8333-333333333333")
    assert result.accepted is False
    assert result.failure_code == "uuid_mismatch"


# --------------------------------------------------------------------------- #
# Flag is inert / default-off
# --------------------------------------------------------------------------- #


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(VOUCH_FLAG_ENV, raising=False)
    assert is_orchestrator_vouch_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_truthy_spellings(monkeypatch, val):
    monkeypatch.setenv(VOUCH_FLAG_ENV, val)
    assert is_orchestrator_vouch_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "garbage"])
def test_flag_falsey_spellings(monkeypatch, val):
    monkeypatch.setenv(VOUCH_FLAG_ENV, val)
    assert is_orchestrator_vouch_enabled() is False


# --------------------------------------------------------------------------- #
# Drift-guard: the PoC constants must NOT be in the live gating sets.
# This is the C2 inertness invariant made executable — if a future change adds
# the vouched session_source to a live strong/caller-asserted set WITHOUT the
# vouch lookup behind it, this test fails.
# --------------------------------------------------------------------------- #


def test_vouched_session_source_absent_from_strong_sources():
    from src.services.identity_payloads import _STRONG_IDENTITY_SOURCES

    assert SESSION_SOURCE_BEAM_ORCHESTRATED not in _STRONG_IDENTITY_SOURCES


def test_vouched_labels_absent_from_session_classification_source():
    # session.py's caller-asserted classification set (_CALLER_ASSERTED_SOURCES)
    # is function-local, so it can't be imported. Guard at the source level: the
    # vouched label literals must not appear in session.py at all while the PoC
    # is inert. If a future change wires them there WITHOUT the vouch lookup, this
    # fails (the C2 inertness invariant made executable).
    import inspect

    from src.mcp_handlers.identity import session as session_mod

    src = inspect.getsource(session_mod)
    assert SESSION_SOURCE_BEAM_ORCHESTRATED not in src
    assert PROOF_ORIGIN_ORCHESTRATOR_VOUCHED not in src
