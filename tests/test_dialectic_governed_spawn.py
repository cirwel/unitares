"""Tests for the governed reviewer-spawn producer (governed_spawn.py).

The failure-routing matrix is the load-bearing behavior: refusals and ambiguous
outcomes must degrade to the in-process synthetic reviewer (None), never to a
direct orchestrator spawn; availability/config conditions fall back to the
direct path. See the module docstring for the per-bucket rationale.
"""
from __future__ import annotations

import re
from typing import Any, Dict

import pytest

from src.mcp_handlers.dialectic import governed_spawn as gs
from src.mcp_handlers.dialectic.governed_spawn import (
    GovernedOutcome,
    GovernedSpawnResult,
    build_envelope,
    classify_response,
    dispatcher_uuid,
    governed_spawn_enabled,
    sanitize_spec_env,
)

DISPATCHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff"
SESSION = "11111111-2222-3333-4444-555555555555"


def _spec(env_extra: Dict[str, str] | None = None) -> Dict[str, Any]:
    env = {
        "DIALECTIC_SESSION_ID": SESSION,
        "DIALECTIC_THESIS_REASONING": "line one\nline two",
        "PYTHONPATH": "/repo:/repo/agents/sdk/src",
    }
    env.update(env_extra or {})
    return {
        "cmd": "/usr/bin/python3",
        "args": ["-m", "agents.dialectic_reviewer"],
        "cd": "/repo",
        "env": env,
    }


# ---- gates ----


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_GOVERNED_SPAWN", raising=False)
    assert governed_spawn_enabled() is False


def test_enabled_forms(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("UNITARES_DIALECTIC_GOVERNED_SPAWN", v)
        assert governed_spawn_enabled() is True


def test_dispatcher_uuid_validates_shape(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_DISPATCHER_UUID", DISPATCHER.upper())
    assert dispatcher_uuid() == DISPATCHER  # normalized lowercase

    # A malformed value must resolve to None (CONFIG_ERROR path → direct
    # fallback), never reach the plane where it would surface as a misleading
    # governance_blocked on every dialectic.
    monkeypatch.setenv("UNITARES_DIALECTIC_DISPATCHER_UUID", "not-a-uuid")
    assert dispatcher_uuid() is None

    monkeypatch.delenv("UNITARES_DIALECTIC_DISPATCHER_UUID", raising=False)
    assert dispatcher_uuid() is None


# ---- sanitization + envelope ----


def test_sanitize_strips_refused_c0_only():
    env = {"X": "keep\ttabs\nand\rreturns", "Y": "ansi\x1b[31mred\x1b[0m", "Z": "nul\x00bel\x07"}
    out = sanitize_spec_env(env)
    assert out["X"] == "keep\ttabs\nand\rreturns"  # admitted escapes survive
    assert out["Y"] == "ansi[31mred[0m"  # ESC stripped
    assert out["Z"] == "nulbel"


def test_envelope_shape_and_key_derivation(monkeypatch):
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")
    env1 = build_envelope(SESSION, _spec(), DISPATCHER)
    assert env1 is not None and "__config_error__" not in env1

    assert env1["custody_mode"] == "execute"
    assert env1["effect_type"] == "agent_spawn"
    # Canonical single-slash dialectic scheme, verbatim (top-level surface is
    # not canonicalized by the plane — the spelling here is what gets stored
    # and grant-bound).
    assert env1["surface"] == f"dialectic:/{SESSION}"
    assert env1["payload"]["cmd"] == "/usr/bin/python3"
    assert env1["payload"]["cd"] == "/repo"
    assert env1["provenance"]["session_id"] == SESSION
    assert env1["proposer"]["agent_uuid"] == DISPATCHER
    assert env1["proposer"]["continuity_token"].startswith("v1.")
    assert env1["proposer"]["effect_grant"].startswith("gnt.v1.")
    assert re.fullmatch(
        rf"dialectic-reviewer:{SESSION}:[0-9a-f]{{16}}", env1["idempotency_key"]
    )

    # The key embeds the canonical payload hash: same spec → same key; any
    # payload change (including inherited env) → different key. This is what
    # keeps the key and the plane's whole-payload digest from ever disagreeing
    # (a session+thesis key would 409 after a PYTHONPATH change).
    env_same = build_envelope(SESSION, _spec(), DISPATCHER)
    assert env_same["idempotency_key"] == env1["idempotency_key"]
    env_diff = build_envelope(SESSION, _spec({"PYTHONPATH": "/elsewhere"}), DISPATCHER)
    assert env_diff["idempotency_key"] != env1["idempotency_key"]


def test_envelope_sanitizes_rather_than_failing_on_ansi(monkeypatch):
    # Thesis text is authored by the agent under review; ANSI escapes in it
    # must not be able to force the mint to fail (which would degrade the
    # dispatch), let alone force the ungoverned path (routing guarantees that
    # separately).
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")
    env = build_envelope(
        SESSION, _spec({"DIALECTIC_THESIS_REASONING": "\x1b[1mbold claim\x1b[0m"}), DISPATCHER
    )
    assert env is not None and "__config_error__" not in env
    assert "\x1b" not in env["payload"]["env"]["DIALECTIC_THESIS_REASONING"]


def test_envelope_without_secret_is_config_error(monkeypatch):
    for var in (
        "UNITARES_CONTINUITY_TOKEN_SECRET",
        "UNITARES_HTTP_API_TOKEN",
        "UNITARES_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    env = build_envelope(SESSION, _spec(), DISPATCHER)
    assert env is not None and "__config_error__" in env


def test_grant_binds_the_canonical_payload(monkeypatch):
    """The minted grant must verify against the envelope it rides in."""
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")
    from unitares_sdk.lease_plane.canonical import canonical_payload_sha256

    from src.effect_grant import verify_effect_grant

    env = build_envelope(SESSION, _spec(), DISPATCHER)
    psha = canonical_payload_sha256(env["payload"])
    result = verify_effect_grant(
        env["proposer"]["effect_grant"],
        aid=DISPATCHER,
        payload_sha256=psha,
        surface=env["surface"],
        custody_mode="execute",
        idempotency_key=env["idempotency_key"],
    )
    assert result.ok, result.reason


# ---- response classification (the routing matrix) ----


@pytest.mark.parametrize(
    "status,body,outcome",
    [
        # fresh committed spawn → COMMITTED
        (202, {"status": "committed", "agent_id": "ag-1"}, GovernedOutcome.COMMITTED),
        # idempotent replay of a committed spawn → REFUSED (synthetic resolves
        # the session now; a still-running prior reviewer is idempotent-safe)
        (
            202,
            {"status": "committed", "agent_id": "ag-1", "idempotent": True},
            GovernedOutcome.REFUSED,
        ),
        # 202 with no fresh agent_id (e.g. a replayed refusal from an unpatched
        # plane) must NEVER read as success — the finding this guards against
        # stranded sessions at antithesis
        (202, {"status": "governance_blocked", "agent_id": None}, GovernedOutcome.REFUSED),
        (202, {"status": "committed", "agent_id": ""}, GovernedOutcome.REFUSED),
        # real veto → REFUSED (direct spawn would bypass governance)
        (403, {"error": "governance_blocked"}, GovernedOutcome.REFUSED),
        # conflicting claim on an irreversible spawn → REFUSED
        (409, {"error": "idempotency_conflict"}, GovernedOutcome.REFUSED),
        # plane's orchestrator leg errored — spawn state ambiguous → REFUSED
        (502, {"error": "spawn_failed"}, GovernedOutcome.REFUSED),
        # flag off at the plane / pre-spawn plane errors → direct fallback
        (501, {"error": "execute_not_implemented"}, GovernedOutcome.UNAVAILABLE),
        (503, {"error": "persist_failed"}, GovernedOutcome.UNAVAILABLE),
        # producer bug must not take orchestrated review down with it
        (422, {"error": "schema_invalid"}, GovernedOutcome.UNAVAILABLE),
        # bad bearer is operator config
        (401, {"error": "permission_denied"}, GovernedOutcome.CONFIG_ERROR),
    ],
)
def test_classify_response(status, body, outcome):
    assert classify_response(status, body).outcome is outcome


def test_classify_committed_carries_ids():
    r = classify_response(202, {"status": "committed", "agent_id": "ag-9", "effect_id": "ef-1"})
    assert r.agent_id == "ag-9"
    assert r.effect_id == "ef-1"


def test_classify_exception_split():
    import httpx

    r = gs.classify_exception(httpx.ConnectError("refused"))
    assert r.outcome is GovernedOutcome.UNAVAILABLE

    # Anything after the request may have been sent is ambiguous: the plane
    # may have spawned (irreversible) — never retry on the direct path.
    r = gs.classify_exception(httpx.ReadTimeout("slow plane"))
    assert r.outcome is GovernedOutcome.REFUSED


# ---- dispatch routing (governed → direct/None decision) ----


@pytest.mark.asyncio
async def test_dispatch_routing_matrix(monkeypatch):
    """COMMITTED returns the governed payload; REFUSED returns None (in-process);
    UNAVAILABLE/CONFIG_ERROR fall through to the direct path."""
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "bearer")
    monkeypatch.setenv("UNITARES_DIALECTIC_GOVERNED_SPAWN", "1")

    thesis = {"root_cause": "x", "proposed_conditions": ["c"], "reasoning": "r", "situation": "s"}

    async def governed_result(result):
        async def fake(session_id, spec, *, timeout=5.0):
            return result

        return fake

    direct_calls = []

    class _FakeResp:
        status_code = 202

        @staticmethod
        def json():
            return {"ok": True, "agent_id": "ag-direct"}

        text = ""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            direct_calls.append(url)
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    # COMMITTED → governed payload, direct path never touched
    monkeypatch.setattr(
        gs,
        "governed_dispatch",
        await governed_result(
            GovernedSpawnResult(GovernedOutcome.COMMITTED, agent_id="ag-gov", effect_id="ef-2")
        ),
    )
    out = await od.dispatch_orchestrated_review(SESSION, thesis, None)
    assert out == {"ok": True, "agent_id": "ag-gov", "effect_id": "ef-2", "governed": True}
    assert direct_calls == []

    # REFUSED → None (in-process), direct path never touched
    monkeypatch.setattr(
        gs,
        "governed_dispatch",
        await governed_result(GovernedSpawnResult(GovernedOutcome.REFUSED, detail="veto")),
    )
    assert await od.dispatch_orchestrated_review(SESSION, thesis, None) is None
    assert direct_calls == []

    # UNAVAILABLE → direct fallback
    monkeypatch.setattr(
        gs,
        "governed_dispatch",
        await governed_result(GovernedSpawnResult(GovernedOutcome.UNAVAILABLE, detail="down")),
    )
    out = await od.dispatch_orchestrated_review(SESSION, thesis, None)
    assert out == {"ok": True, "agent_id": "ag-direct"}
    assert len(direct_calls) == 1

    # CONFIG_ERROR → direct fallback
    monkeypatch.setattr(
        gs,
        "governed_dispatch",
        await governed_result(GovernedSpawnResult(GovernedOutcome.CONFIG_ERROR, detail="uuid")),
    )
    out = await od.dispatch_orchestrated_review(SESSION, thesis, None)
    assert out == {"ok": True, "agent_id": "ag-direct"}
    assert len(direct_calls) == 2


@pytest.mark.asyncio
async def test_dispatch_flag_off_is_byte_identical(monkeypatch):
    """Flag off → governed_dispatch is never called; behavior is today's."""
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "bearer")
    monkeypatch.delenv("UNITARES_DIALECTIC_GOVERNED_SPAWN", raising=False)

    called = []

    async def fake(session_id, spec, *, timeout=5.0):  # pragma: no cover — must not run
        called.append(1)
        return GovernedSpawnResult(GovernedOutcome.REFUSED)

    monkeypatch.setattr(gs, "governed_dispatch", fake)

    class _FakeResp:
        status_code = 202

        @staticmethod
        def json():
            return {"ok": True, "agent_id": "ag-direct"}

        text = ""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    thesis = {"root_cause": "x", "proposed_conditions": [], "reasoning": "r", "situation": "s"}
    out = await od.dispatch_orchestrated_review(SESSION, thesis, None)
    assert out == {"ok": True, "agent_id": "ag-direct"}
    assert called == []
