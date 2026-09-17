"""Tests for the operator-side resident anchor provisioner.

The script exists because a rostered identity minted through ``start_session``
(not the SDK) has no anchor, and under strict identity the first process
boundary strands it: binding-only resume is refused, and ``force_new`` /
``parent_agent_id`` mint a successor instead of resuming. The revenue-engine
run-up worker lost its fifth session to exactly that on 2026-09-03.

What these pin:

1. Nothing is written on a dry run, on any refusal, or when the live resume
   does not come back with the same UUID.
2. The minted token's ``aid`` claim is the resident's UUID and is signed with
   the same secret the server reads, so PATH 0 accepts it.
3. The script never mints an identity and never writes tags: its only calls
   are an ``agent get`` read-back and one ``identity`` resume.
"""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

from src.mcp_handlers.identity.session import extract_token_agent_uuid
from src.mcp_handlers.identity.shared import make_client_session_id

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "provision_resident_anchor.py"
)
_spec = importlib.util.spec_from_file_location("provision_resident_anchor", MODULE_PATH)
pra = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pra)


UUID = "b1b28308-dca3-46df-8e8b-5a23a524fe39"
OTHER_UUID = "34b595dc-9628-41ff-b960-54857f99ba72"
NAME = "worker-a"
SECRET = "test-continuity-secret"


def _agent_get(status: str = "active", tags: list[str] | None = None, nested: bool = False) -> dict:
    row = {"status": status, "tags": ["persistent", "autonomous"] if tags is None else tags}
    return {"name": "agent", "success": True,
            "result": {"agent": row} if nested else row}


def _identity_ok(uuid: str = UUID, fresh_token: str | None = "v1.fresh.sig") -> dict:
    body = {"success": True, "tool": "identity", "agent_uuid": uuid, "resumed": True,
            "raw_governance": {"uuid": uuid}}
    if fresh_token:
        body["continuity_token"] = fresh_token
    return {"name": "identity", "success": True, "result": body}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    anchors = tmp_path / "anchors"
    monkeypatch.setattr(pra, "ANCHOR_DIR", anchors)
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_API_TOKEN", raising=False)
    return anchors / f"{NAME}.json"


def _wire(monkeypatch, responses):
    """Route _call by tool name; a callable response is invoked with the arguments."""
    seen = []

    def fake_call(name, arguments, token):
        seen.append((name, arguments))
        resp = responses[name]
        return resp(arguments) if callable(resp) else resp

    monkeypatch.setattr(pra, "_call", fake_call)
    return seen


def test_dry_run_reads_but_writes_nothing(env, monkeypatch, capsys):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME]) == 0
    assert not env.exists()
    assert [n for n, _ in seen] == ["agent"]
    assert "Dry run" in capsys.readouterr().out


def test_apply_mints_token_bound_to_uuid_and_writes_anchor(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0

    identity_calls = [args for name, args in seen if name == "identity"]
    assert len(identity_calls) == 1
    call = identity_calls[0]
    assert call["agent_uuid"] == UUID
    assert call["resume"] is True
    # The proof PATH 0 checks: signature under the server's secret, aid == uuid.
    assert extract_token_agent_uuid(call["continuity_token"]) == UUID

    data = json.loads(env.read_text())
    assert data["agent_uuid"] == UUID
    assert data["client_session_id"] == make_client_session_id(UUID)
    assert data["display_name"] == NAME
    # The server-reissued token is what the SDK would have stored.
    assert data["continuity_token"] == "v1.fresh.sig"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_only_reads_agent_and_resumes_never_mints_or_tags(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get(nested=True), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert sorted({n for n, _ in seen}) == ["agent", "identity"]
    for name, args in seen:
        if name == "agent":
            assert args["action"] == "get"
            assert "tags" not in args
        assert name != "start_session"
        assert not args.get("force_new")


def test_keeps_minted_token_when_server_reissues_none(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok(fresh_token=None)})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    stored = json.loads(env.read_text())["continuity_token"]
    assert extract_token_agent_uuid(stored) == UUID


def test_no_verify_skips_resume_and_stores_minted_token(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply", "--no-verify"]) == 0
    assert [n for n, _ in seen] == ["agent"]
    stored = json.loads(env.read_text())["continuity_token"]
    assert extract_token_agent_uuid(stored) == UUID


def test_refuses_existing_anchor_without_force(env, monkeypatch, capsys):
    env.parent.mkdir(parents=True)
    env.write_text(json.dumps({"agent_uuid": OTHER_UUID}))
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert json.loads(env.read_text())["agent_uuid"] == OTHER_UUID
    assert seen == []
    assert "--force" in capsys.readouterr().err

    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply", "--force"]) == 0
    assert json.loads(env.read_text())["agent_uuid"] == UUID


def test_refuses_without_signing_secret(env, monkeypatch, capsys):
    monkeypatch.delenv("UNITARES_CONTINUITY_TOKEN_SECRET")
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert seen == []
    assert "UNITARES_CONTINUITY_TOKEN_SECRET" in capsys.readouterr().err


def test_refuses_archived_identity(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(status="archived"), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()


def test_refuses_when_roster_tags_were_not_granted(env, monkeypatch, capsys):
    """An identity without persistent+autonomous is archived by the orphan sweep."""
    seen = _wire(monkeypatch, {"agent": _agent_get(tags=["ephemeral"]), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert "identity" not in {n for n, _ in seen}
    assert "UNITARES_RESIDENTS" in capsys.readouterr().err


def test_refuses_when_resume_returns_a_different_uuid(env, monkeypatch, capsys):
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok(uuid=OTHER_UUID)})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert OTHER_UUID in capsys.readouterr().err


def test_refuses_when_resume_is_refused(env, monkeypatch, capsys):
    refusal = {"name": "identity", "success": True,
               "result": {"success": False, "error": "Bare agent_uuid resume is not permitted.",
                          "recovery": {"reason": "bare_uuid_resume_denied"}}}
    _wire(monkeypatch, {"agent": _agent_get(), "identity": refusal})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert "bare_uuid_resume_denied" in capsys.readouterr().err


def test_rejects_malformed_uuid(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", "not-a-uuid", "--name", NAME, "--apply"]) == 2
    assert seen == []
    assert not env.exists()
