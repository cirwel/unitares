"""Deployment-signed dialectic resolution receipts (drr.v1).

The receipt is the deployment's Ed25519 signature over the stored resolution
record. It closes one named gap (SCOPE_AND_THREAT_MODEL, "the attestation half
of the same boundary"): a peer holding only the pinned public key can verify
which deployment issued a record, without any agent api_key. These tests pin
the posture (off by default, never fails finalization), the binding (record,
session, kid), the canonical form across the reload path, and the offline CLI.
"""

import importlib.util
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (  # noqa: E402
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
    Resolution,
)
from src.dialectic_receipt import (  # noqa: E402
    RECEIPT_PREFIX,
    ReceiptError,
    canonical_record_bytes,
    is_authorization,
    mint_resolution_receipt,
    record_sha256,
    verify_resolution_receipt,
)
from src.identity.agent_identity_credential import (  # noqa: E402
    export_public_jwks,
    generate_signing_key_seed,
    key_id,
    load_signing_key,
    mint_identity_attestation,
    verify_identity_attestation,
)
from src.mcp_handlers.dialectic.session import _reconstruct_session_from_dict  # noqa: E402

KEY_ENV = "UNITARES_AIC_SIGNING_KEY"


@pytest.fixture
def seed():
    return generate_signing_key_seed()


@pytest.fixture
def configured_key(monkeypatch, seed):
    monkeypatch.setenv(KEY_ENV, seed)
    return load_signing_key(seed)


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)


def _converged_session():
    s = DialecticSession(
        paused_agent_id="agent-a",
        reviewer_agent_id="agent-b",
        session_type="recovery",
    )
    s.phase = DialecticPhase.RESOLVED
    s.synthesis_round = 2
    now = datetime.now(timezone.utc).isoformat()
    s.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-a", timestamp=now,
        root_cause="initial cause", proposed_conditions=["c1"], reasoning="initial",
    ))
    s.transcript.append(DialecticMessage(
        phase="antithesis", agent_id="agent-b", timestamp=now,
        reasoning="counter", concerns=["c"],
    ))
    s.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-a", timestamp=now,
        proposed_conditions=["agreed", "  padded  "], root_cause="agreed cause",
        reasoning="from a", agrees=True,
    ))
    s.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-b", timestamp=now,
        proposed_conditions=["agreed", "  padded  "], root_cause="agreed cause",
        reasoning="from b", agrees=True,
    ))
    return s


# ── posture ────────────────────────────────────────────────────────────────


def test_no_key_configured_leaves_receipt_empty_and_hmac_intact(no_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    assert res.receipt == ""
    assert res.signature_version == 2
    assert res.verify_signatures("key-a", "key-b") is True
    assert "receipt" in res.to_dict()


def test_key_configured_mints_receipt_bound_to_record_session_and_kid(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    assert res.receipt.startswith(RECEIPT_PREFIX)

    jwks = export_public_jwks(configured_key)  # what a /.well-known endpoint would serve
    claims = verify_resolution_receipt(res.receipt, res.to_dict(), jwks=jwks,
                                       expected_session_id=s.session_id)
    assert claims["session_id"] == s.session_id
    assert claims["paused_agent_id"] == "agent-a"
    assert claims["reviewer_agent_id"] == "agent-b"
    assert claims["kid"] == key_id(configured_key.public_key())
    assert claims["record_sha256"] == record_sha256(res.to_dict())
    assert claims["bilateral_symmetric"] is True
    assert claims["signature_version"] == 2
    assert claims["authorizes"] == []
    assert claims["stance"] == "descriptive"
    # The symmetric scheme is untouched: the receipt is outside canonical_payload().
    assert res.verify_signatures("key-a", "key-b") is True


def test_invalid_seed_degrades_with_warning_not_exception(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ENV, "definitely-not-a-seed")
    s = _converged_session()
    with caplog.at_level(logging.WARNING, logger="src.dialectic_receipt"):
        res = s.finalize_resolution("key-a", "key-b")
    assert res.receipt == ""
    assert res.verify_signatures("key-a", "key-b") is True
    assert any("configured but unusable" in r.getMessage() for r in caplog.records)


def test_llm_assisted_single_signer_receipt_reports_not_bilateral(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "")  # reviewer key absent by design
    claims = verify_resolution_receipt(res.receipt, res.to_dict(), public_key=configured_key.public_key())
    assert claims["bilateral_symmetric"] is False
    assert res.verify_signatures("key-a", "") is False


# ── binding ────────────────────────────────────────────────────────────────


def test_tampered_record_fails_with_record_mismatch(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    record = res.to_dict()
    record["conditions"] = list(record["conditions"]) + ["silently added"]
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record, jwks=export_public_jwks(configured_key))
    assert exc.value.code == "record_mismatch"


def test_receipt_for_other_key_fails(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    other = load_signing_key(generate_signing_key_seed())
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, res.to_dict(), jwks=export_public_jwks(other))
    assert exc.value.code == "unknown_kid"
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, res.to_dict(), public_key=other.public_key())
    assert exc.value.code == "invalid_signature"


def test_corrupted_signature_fails(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    payload_b64, sig_b64 = res.receipt[len(RECEIPT_PREFIX):].split(".")
    flipped = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(f"{RECEIPT_PREFIX}{payload_b64}.{flipped}", res.to_dict(),
                                  public_key=configured_key.public_key())
    assert exc.value.code == "invalid_signature"


def test_session_mismatch(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, res.to_dict(), public_key=configured_key.public_key(),
                                  expected_session_id="someone-elses-session")
    assert exc.value.code == "session_mismatch"


def test_envelopes_are_not_interchangeable(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    aic = mint_identity_attestation(uuid="abc", signing_key=configured_key)
    assert verify_identity_attestation(res.receipt, public_key=configured_key.public_key()) is None
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(aic, res.to_dict(), public_key=configured_key.public_key())
    assert exc.value.code == "malformed"
    for junk in ("", None, 42, "drr.v1.", "drr.v1.onlyone", "drr.v1.a.b"):
        with pytest.raises(ReceiptError):
            verify_resolution_receipt(junk, res.to_dict(), public_key=configured_key.public_key())


def test_no_verification_key_supplied(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, res.to_dict())
    assert exc.value.code == "no_verification_key"


def test_receipt_is_never_an_authorization():
    assert is_authorization() is False


def test_mint_requires_session_id(configured_key):
    with pytest.raises(ReceiptError) as exc:
        mint_resolution_receipt({"action": "resume"}, session_id="", paused_agent_id="a",
                                reviewer_agent_id=None, signing_key=configured_key)
    assert exc.value.code == "missing_session_id"


# ── canonical form and the reload path ─────────────────────────────────────


def test_canonical_form_ignores_receipt_key_order_and_condition_padding():
    base = {"action": "resume", "conditions": ["b", "  a  ", ""], "root_cause": "r",
            "reasoning": "x", "signature_a": "sa", "signature_b": "sb",
            "timestamp": "t", "signature_version": 2, "receipt": ""}
    reordered = {k: base[k] for k in reversed(list(base))}
    reordered["receipt"] = "drr.v1.whatever.sig"
    normalized = dict(base, conditions=["b", "a"])  # what the server read path serves
    assert canonical_record_bytes(base) == canonical_record_bytes(reordered)
    assert canonical_record_bytes(base) == canonical_record_bytes(normalized)
    assert b"receipt" not in canonical_record_bytes(base)
    assert record_sha256(base) == record_sha256(normalized)


def test_receipt_survives_json_round_trip_and_reload(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    stored = json.loads(json.dumps(res.to_dict()))  # resolution_json JSONB round trip
    session_doc = {
        "paused_agent_id": "agent-a",
        "reviewer_agent_id": "agent-b",
        "paused_agent_state": {},
        "phase": "resolved",
        "resolution": stored,
    }
    reloaded = _reconstruct_session_from_dict(s.session_id, session_doc)
    assert reloaded is not None and reloaded.resolution is not None
    assert reloaded.resolution.receipt == res.receipt
    assert reloaded.resolution.signature_version == 2
    assert reloaded.resolution.conditions == ["agreed", "padded"]  # read-path normalization
    # finalize_resolution stores conditions already in the served shape, so the
    # symmetric signatures survive the reload (they did not before this change).
    assert res.conditions == ["agreed", "padded"]
    assert reloaded.resolution.verify_signatures("key-a", "key-b") is True
    claims = verify_resolution_receipt(
        reloaded.resolution.receipt, reloaded.resolution.to_dict(),
        jwks=export_public_jwks(configured_key), expected_session_id=reloaded.session_id,
    )
    assert claims["session_id"] == s.session_id
    # hash() is now stable across a reload (it previously drifted on signature_version).
    assert reloaded.resolution.hash() == res.hash()


def test_reload_of_legacy_row_without_attestation_fields_defaults_to_v1():
    session_doc = {
        "paused_agent_id": "agent-a",
        "reviewer_agent_id": "agent-b",
        "paused_agent_state": {},
        "phase": "resolved",
        "resolution": {
            "action": "resume", "conditions": ["c"], "root_cause": "r", "reasoning": "x",
            "signature_a": "sa", "signature_b": "sb", "timestamp": "2026-01-15T12:30:00",
        },
    }
    reloaded = _reconstruct_session_from_dict("sess-legacy", session_doc)
    assert reloaded.resolution.signature_version == 1
    assert reloaded.resolution.receipt == ""
    session_doc["resolution"]["signature_version"] = "2"
    reloaded = _reconstruct_session_from_dict("sess-legacy", session_doc)
    assert reloaded.resolution.signature_version == 2


def test_resolution_dataclass_default_receipt_is_empty():
    r = Resolution(action="resume", conditions=[], root_cause="", reasoning="",
                   signature_a="", signature_b="", timestamp="t")
    assert r.receipt == ""
    assert r.canonical_payload() == Resolution(
        action="resume", conditions=[], root_cause="", reasoning="",
        signature_a="", signature_b="", timestamp="t", receipt="drr.v1.x.y",
    ).canonical_payload()


# ── offline CLI ────────────────────────────────────────────────────────────


def _load_cli():
    path = project_root / "scripts" / "client" / "verify_resolution_receipt.py"
    spec = importlib.util.spec_from_file_location("verify_resolution_receipt_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_verifies_and_rejects_offline(configured_key, tmp_path, capsys, monkeypatch):
    cli = _load_cli()
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(export_public_jwks(configured_key)))
    doc = {"session_id": s.session_id, "resolution": res.to_dict()}
    doc_path = tmp_path / "session.json"
    doc_path.write_text(json.dumps(doc))

    # The verifier must not need the private key or any api_key.
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert cli.main(["verify", "--record", str(doc_path), "--jwks", str(jwks_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verified"] is True and out["claims"]["session_id"] == s.session_id

    tampered = dict(doc)
    tampered["resolution"] = dict(res.to_dict(), reasoning="rewritten after the fact")
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered))
    assert cli.main(["verify", "--record", str(tampered_path), "--jwks", str(jwks_path)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verified"] is False and out["code"] == "record_mismatch"

    assert cli.main(["verify", "--record", str(doc_path), "--jwks", str(jwks_path),
                     "--session-id", "other"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "session_mismatch"

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(dict(res.to_dict(), receipt="")))
    assert cli.main(["verify", "--record", str(bare), "--jwks", str(jwks_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "no_receipt"


def test_cli_exports_public_jwks_only(configured_key, seed, capsys, monkeypatch):
    cli = _load_cli()
    assert cli.main(["export-jwks"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["keys"][0]["kid"] == key_id(configured_key.public_key())
    assert seed not in json.dumps(out)
    assert "d" not in out["keys"][0]  # no private component
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert cli.main(["export-jwks"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "not_configured"
