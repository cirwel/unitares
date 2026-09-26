"""Tests for the operator-side resident anchor provisioner.

The script exists because a rostered identity minted through ``start_session``
(not the SDK) has no anchor, and under strict identity the first process
boundary strands it: binding-only resume is refused, and ``force_new`` /
``parent_agent_id`` mint a successor instead of resuming.

Most of what follows pins defects found in adversarial review on 2026-09-17,
after the first version shipped. The sharpest one, and the reason this file
looks the way it does:

    ``UNITARES_IDENTITY_STRICT`` defaults to ``log``. In that mode a PATH 0
    resume whose token FAILS its ownership check logs, broadcasts
    ``identity_hijack_suspected``, and resumes anyway. The uuid in the
    response still matches. Reproduced live against v2.22.1 with an expired
    token: ``success: true``, ``resumed: true``, matching uuid, alongside
    ``proof_origin: "server_inferred"``, ``caller_proven: false`` and an
    ``identity_warnings`` entry ``continuity_token_invalid``.

    So the first version's verify -- which compared only the uuid -- returned
    a green result for a token the server had explicitly rejected, and wrote
    an anchor whose failure surfaced only at the resident's next session.
    ``test_verify_refuses_*`` are that repro.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.mcp_handlers.identity.session import extract_token_agent_uuid
from src.mcp_handlers.identity.shared import make_client_session_id

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "ops" / "provision_resident_anchor.py"
_spec = importlib.util.spec_from_file_location("provision_resident_anchor", MODULE_PATH)
pra = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pra)


UUID = "b1b28308-dca3-46df-8e8b-5a23a524fe39"
OTHER_UUID = "34b595dc-9628-41ff-b960-54857f99ba72"
NAME = "Worker-A"          # deliberately mixed case: the filename must lowercase
LOWER = "worker-a"
SECRET = "test-continuity-secret"


def _agent_get(status: str = "active", tags: list[str] | None = None,
               label: str | None = LOWER, nested: bool = False) -> dict:
    row = {"status": status,
           "tags": ["persistent", "autonomous"] if tags is None else tags,
           "label": label}
    return {"name": "agent", "success": True,
            "result": {"agent": row} if nested else row}


def _identity_ok(uuid: str = UUID, fresh_token: str | None = "v1.fresh.sig",
                 proof_origin: str = "caller_asserted",
                 source: str = "continuity_token",
                 warnings: list | None = None) -> dict:
    """A real-shaped PATH 0 response.

    Field names and nesting come from a live v2.22.1 identity() response: the
    uuid arrives as ``uuid`` (not ``agent_uuid``), and the assurance block
    appears both at top level and under ``identity_context``.
    """
    assurance = {"tier": "strong", "score": 1.0, "session_source": source,
                 "caller_proven": proof_origin == "caller_asserted",
                 "proof_origin": proof_origin}
    body = {"success": True, "uuid": uuid, "resumed": True, "resumed_by_uuid": True,
            "client_session_id": make_client_session_id(uuid),
            "session_resolution_source": source,
            "identity_assurance": assurance,
            "identity_context": {"identity_assurance": assurance}}
    if fresh_token:
        body["continuity_token"] = fresh_token
    if warnings:
        body["identity_warnings"] = warnings
    return {"name": "identity", "success": True, "result": body}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    anchors = tmp_path / "anchors"
    monkeypatch.setattr(pra, "ANCHOR_DIR", anchors)
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_API_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    return anchors / f"{LOWER}.json"


def _wire(monkeypatch, responses):
    seen = []

    def fake_call(name, arguments, token):
        seen.append((name, arguments))
        resp = responses[name]
        return resp(arguments) if callable(resp) else resp

    monkeypatch.setattr(pra, "_call", fake_call)
    return seen


# --------------------------------------------------------------------------
# The verify gate: a matching uuid is not proof.
# --------------------------------------------------------------------------

def test_verify_refuses_when_server_rejected_the_token(env, monkeypatch, capsys):
    """The live repro: success, resumed, uuid matches, token was NOT the proof."""
    _wire(monkeypatch, {
        "agent": _agent_get(),
        "identity": _identity_ok(
            proof_origin="server_inferred",
            source="agent_uuid_direct_fastpath",
            warnings=[{"code": "continuity_token_invalid",
                       "resolved_via": "agent_uuid_direct_fastpath",
                       "message": "failed verification"}]),
    })
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    err = capsys.readouterr().err
    assert "REJECTED the token" in err
    assert "signing secret" in err


def test_verify_refuses_when_resolved_by_another_route(env, monkeypatch, capsys):
    """No explicit warning, but the assurance block says it was not the token."""
    _wire(monkeypatch, {
        "agent": _agent_get(),
        "identity": _identity_ok(proof_origin="server_inferred",
                                 source="agent_uuid_direct_fastpath"),
    })
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert "not by the token" in capsys.readouterr().err


def test_verify_accepts_only_a_token_proven_resume(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    call = [a for n, a in seen if n == "identity"][0]
    assert call["agent_uuid"] == UUID and call["resume"] is True
    assert extract_token_agent_uuid(call["continuity_token"]) == UUID
    data = json.loads(env.read_text())
    assert data["agent_uuid"] == UUID
    assert data["client_session_id"] == make_client_session_id(UUID)
    assert data["continuity_token"] == "v1.fresh.sig"


def test_uuid_mismatch_still_refused(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok(uuid=OTHER_UUID)})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()


def test_reads_uuid_from_the_real_field_name(env, monkeypatch):
    """A live PATH 0 response carries `uuid`, not `agent_uuid`."""
    resp = _identity_ok()
    assert "agent_uuid" not in resp["result"], "fixture must match the real shape"
    _wire(monkeypatch, {"agent": _agent_get(), "identity": resp})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0


# --------------------------------------------------------------------------
# Identity replacement must never be silent.
# --------------------------------------------------------------------------

def test_same_uuid_anchor_is_a_noop_exit_zero(env, monkeypatch):
    env.parent.mkdir(parents=True)
    env.write_text(json.dumps({"agent_uuid": UUID}))
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert seen == [], "a re-run on the same identity must not touch the server"


def test_refuses_to_repoint_at_a_different_uuid(env, monkeypatch, capsys):
    env.parent.mkdir(parents=True)
    env.write_text(json.dumps({"agent_uuid": OTHER_UUID}))
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert json.loads(env.read_text())["agent_uuid"] == OTHER_UUID
    assert seen == []
    assert "--replace-identity" in capsys.readouterr().err


def test_replace_identity_names_the_displaced_uuid(env, monkeypatch, capsys):
    env.parent.mkdir(parents=True)
    env.write_text(json.dumps({"agent_uuid": OTHER_UUID}))
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME,
                     "--apply", "--replace-identity"]) == 0
    assert json.loads(env.read_text())["agent_uuid"] == UUID
    assert OTHER_UUID in capsys.readouterr().out


# --------------------------------------------------------------------------
# The filename is a lookup key.
# --------------------------------------------------------------------------

def test_refuses_when_the_server_label_does_not_match_the_filename(env, monkeypatch, capsys):
    _wire(monkeypatch, {"agent": _agent_get(label="something-else"),
                        "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert "resolve_resident_uuid" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "UPPER/../x"])
def test_rejects_names_that_could_escape_the_anchor_dir(env, monkeypatch, bad):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", UUID, "--name", bad, "--apply"]) == 2
    assert seen == []
    assert not list(env.parent.glob("**/*.json")) if env.parent.exists() else True


def test_filename_is_lowercased_from_a_mixed_case_name(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert env.name == f"{LOWER}.json" and env.exists()


# --------------------------------------------------------------------------
# The anchor is a credential: how it reaches disk matters.
# --------------------------------------------------------------------------

def test_anchor_is_never_world_readable_and_dir_is_private(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.parent.stat().st_mode) == 0o700
    assert not list(env.parent.glob("*.tmp")), "temp file must not survive"


def test_existing_directory_mode_is_left_alone(env, monkeypatch):
    """A deliberate 0o750 (group-readable for a service account) must survive."""
    env.parent.mkdir(parents=True)
    os.chmod(env.parent, 0o750)
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert stat.S_IMODE(env.parent.stat().st_mode) == 0o750


def test_concurrent_temp_names_do_not_collide(env, monkeypatch, tmp_path):
    """A fixed '<stem>.tmp' made two simultaneous runs destroy each other."""
    env.parent.mkdir(parents=True)
    (env.parent / f"{LOWER}.tmp").write_text("squatter")
    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert json.loads(env.read_text())["agent_uuid"] == UUID
    assert (env.parent / f"{LOWER}.tmp").read_text() == "squatter"


def test_written_anchor_round_trips_through_the_sdk_reader(env, monkeypatch):
    """The only assertion that catches key-name drift against the real consumer."""
    sdk_src = REPO_ROOT / "agents" / "sdk" / "src"
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    from unitares_sdk.utils import load_json_state

    _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0

    saved = load_json_state(env)
    # These three names are what UnitaresAgent._load_session reads.
    assert saved["agent_uuid"] == UUID
    assert saved["client_session_id"] == make_client_session_id(UUID)
    assert saved["continuity_token"] == "v1.fresh.sig"


def test_uds_persistent_resident_gets_a_token_free_anchor(env, monkeypatch):
    """The SDK deliberately writes uuid-only there; a token would be a leak."""
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "/tmp/unitares.sock")
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply",
                     "--transport", "uds"]) == 0
    data = json.loads(env.read_text())
    assert data == {"agent_uuid": UUID}
    assert "identity" not in {n for n, _ in seen}


def test_server_env_socket_alone_does_not_choose_the_shape(env, monkeypatch, capsys):
    """The documented run loads the server's env, which always sets the socket.

    Before --transport, that alone made every persistent resident uuid-only,
    unverified, even one that resumes over MCP HTTP with a token.
    """
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "/tmp/unitares.sock")
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 2
    assert not env.exists()
    assert seen == []
    assert "--transport" in capsys.readouterr().err


def test_http_transport_under_server_env_writes_a_verified_token_anchor(env, monkeypatch):
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "/tmp/unitares.sock")
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply",
                     "--transport", "http"]) == 0
    saved = json.loads(env.read_text())
    assert saved["agent_uuid"] == UUID
    assert saved["continuity_token"] == "v1.fresh.sig"
    assert [n for n, _ in seen] == ["agent", "identity"]


# --------------------------------------------------------------------------
# Refusals that predate the review, still pinned.
# --------------------------------------------------------------------------

def test_dry_run_reads_but_writes_nothing(env, monkeypatch, capsys):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME]) == 0
    assert not env.exists()
    assert [n for n, _ in seen] == ["agent"]
    assert "Dry run" in capsys.readouterr().out


def test_dry_run_flag_is_accepted_and_contradiction_refused(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--dry-run"]) == 0
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--dry-run", "--apply"]) == 2


def test_only_reads_agent_and_resumes_never_mints_or_tags(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get(nested=True), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 0
    assert sorted({n for n, _ in seen}) == ["agent", "identity"]
    for name, args in seen:
        if name == "agent":
            assert args["action"] == "get" and "tags" not in args
        assert name != "start_session" and not args.get("force_new")


def test_no_verify_writes_an_unproven_anchor_without_touching_the_server(env, monkeypatch, capsys):
    seen = _wire(monkeypatch, {})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply", "--no-verify"]) == 0
    assert seen == [], "--no-verify is for an unreachable server; it must make no calls"
    assert extract_token_agent_uuid(json.loads(env.read_text())["continuity_token"]) == UUID
    assert "UNPROVEN" in capsys.readouterr().out


def test_refuses_without_signing_secret(env, monkeypatch, capsys):
    monkeypatch.delenv("UNITARES_CONTINUITY_TOKEN_SECRET")
    seen = _wire(monkeypatch, {"agent": _agent_get(), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists() and seen == []
    assert "UNITARES_CONTINUITY_TOKEN_SECRET" in capsys.readouterr().err


def test_refuses_archived_identity(env, monkeypatch):
    _wire(monkeypatch, {"agent": _agent_get(status="archived"), "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()


def test_refuses_when_roster_tags_were_not_granted(env, monkeypatch, capsys):
    seen = _wire(monkeypatch, {"agent": _agent_get(tags=["ephemeral"]),
                               "identity": _identity_ok()})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert not env.exists()
    assert "identity" not in {n for n, _ in seen}
    assert "UNITARES_RESIDENTS" in capsys.readouterr().err


def test_agent_read_error_is_reported_not_swallowed_as_inactive(env, monkeypatch, capsys):
    _wire(monkeypatch, {"agent": {"result": {"success": False, "error": "Agent not found"}}})
    assert pra.main(["--agent-uuid", UUID, "--name", NAME, "--apply"]) == 1
    assert "Agent not found" in capsys.readouterr().err


def test_rejects_malformed_uuid(env, monkeypatch):
    seen = _wire(monkeypatch, {"agent": _agent_get()})
    assert pra.main(["--agent-uuid", "not-a-uuid", "--name", NAME, "--apply"]) == 2
    assert seen == [] and not env.exists()


def test_dry_run_does_not_need_the_server_modules(tmp_path):
    """The first version imported server code before the dry-run return, so a
    dry run on a plain interpreter died with ModuleNotFoundError: mcp AFTER
    it had already hit the network.

    The probe blocks ``mcp`` itself instead of trusting the ambient
    interpreter. Whatever runs this suite has the server's dependency tree
    installed, so a version of this test that merely clears PYTHONPATH passes
    without ever exercising the import it exists to protect. The probe also
    asserts that _server_modules() still fails under the block, so the day the
    server helpers stop pulling in mcp this test fails loudly (re-point the
    block at whatever the new hard dependency is) rather than going quietly
    vacuous.
    """
    probe = (
        "import importlib.util, sys\n"
        "class _NoMCP:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ModuleNotFoundError('No module named ' + repr(name))\n"
        "        return None\n"
        "sys.meta_path.insert(0, _NoMCP())\n"
        f"spec = importlib.util.spec_from_file_location('pra', {str(MODULE_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "try:\n"
        "    m._server_modules()\n"
        "except SystemExit:\n"
        "    pass\n"
        "else:\n"
        "    sys.exit(3)\n"
        "m._call = lambda n, a, t: {'result': {'status': 'active', "
        "'tags': ['persistent','autonomous'], 'label': 'probe'}}\n"
        f"m.ANCHOR_DIR = __import__('pathlib').Path({str(tmp_path)!r})\n"
        "sys.exit(m.main(['--agent-uuid','b1b28308-dca3-46df-8e8b-5a23a524fe39',"
        "'--name','probe']))\n"
    )
    env = {**os.environ, "UNITARES_CONTINUITY_TOKEN_SECRET": "x"}
    env.pop("UNITARES_UDS_SOCKET", None)
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode != 3, (
        "the probe no longer reproduces a bare interpreter: _server_modules() "
        "succeeded with mcp blocked. Re-point the block at the server helpers' "
        "current hard dependency."
    )
    assert proc.returncode == 0, f"dry run failed: {proc.stderr[-800:]}"
    assert "ModuleNotFoundError" not in proc.stderr
    assert not list(tmp_path.iterdir()), "a dry run wrote an anchor"
