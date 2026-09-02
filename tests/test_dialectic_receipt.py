"""Deployment-signed dialectic resolution receipts (drr.v1).

The receipt is the deployment's Ed25519 signature over the resolution record
as persisted at the terminal "resolved" write. These tests pin the posture
(nothing changes without a key; finalize never mints; failed writes never
mint), the binding (covered fields, session, issuer, kid), the enforced
receipt profile, the canonical form across the JSONB reload path, the
terminal-write wiring in save_session, and the offline CLI's contract.
"""

import dataclasses
import hashlib
import importlib.util
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    covered_fields,
    merge_jwks,
    mint_resolution_receipt,
    record_sha256,
    verify_resolution_receipt,
)
from src.identity.agent_identity_credential import (  # noqa: E402
    _b64u_decode,
    _b64u_encode,
    export_public_jwks,
    generate_signing_key_seed,
    key_id,
    load_signing_key,
    mint_identity_attestation,
    verify_identity_attestation,
)
from src.mcp_handlers.dialectic import session as session_mod  # noqa: E402
from src.mcp_handlers.dialectic.session import (  # noqa: E402
    _reconstruct_session_from_dict,
    save_session,
    seal_resolution_for_persistence,
)

KEY_ENV = "UNITARES_AIC_SIGNING_KEY"
ISSUER_ENV = "UNITARES_LEASE_ATTESTATION_ISSUER"
ISSUER = "governance.test-deployment.example"


@pytest.fixture
def seed():
    return generate_signing_key_seed()


@pytest.fixture
def configured_key(monkeypatch, seed):
    monkeypatch.setenv(KEY_ENV, seed)
    monkeypatch.setenv(ISSUER_ENV, ISSUER)
    return load_signing_key(seed)


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(ISSUER_ENV, raising=False)


def _converged_session(conditions=("agreed", "  padded  ", "")):
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
    for agent in ("agent-a", "agent-b"):
        s.transcript.append(DialecticMessage(
            phase="synthesis", agent_id=agent, timestamp=now,
            proposed_conditions=list(conditions), root_cause="agreed cause",
            reasoning=f"from {agent}", agrees=True,
        ))
    return s


def _sealed(key_b="key-b", conditions=("agreed", "  padded  ", "")):
    s = _converged_session(conditions)
    res = s.finalize_resolution("key-a", key_b)
    record = seal_resolution_for_persistence(s, res, status="resolved")
    return s, res, record


def _legacy_hash(res):
    data = {k: v for k, v in dataclasses.asdict(res).items() if k != "receipt"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _resign(receipt, key, **overrides):
    """A correctly signed receipt with altered claims (an attacker holding the key,
    or a different token type that reuses it)."""
    payload_b64, _ = receipt[len(RECEIPT_PREFIX):].split(".")
    claims = json.loads(_b64u_decode(payload_b64))
    claims.update(overrides)
    payload_b64 = _b64u_encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    sig = key.sign((RECEIPT_PREFIX + payload_b64).encode("ascii"))
    return f"{RECEIPT_PREFIX}{payload_b64}.{_b64u_encode(sig)}"


# ── posture ────────────────────────────────────────────────────────────────


def test_no_key_changes_nothing(no_key):
    s, res, record = _sealed()
    assert res.receipt == ""
    assert "receipt" not in record
    assert record == res.to_dict()
    # stored conditions untouched, and hash() is the pre-receipt formula
    assert "  padded  " in res.conditions and "" in res.conditions
    assert res.hash() == _legacy_hash(res)
    assert res.verify_signatures("key-a", "key-b") is True


def test_finalize_never_mints_even_with_a_key(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    assert res.receipt == ""
    assert "receipt" not in res.to_dict()
    # the gates that run between finalize and the terminal write see the raw candidate
    assert "  padded  " in res.conditions
    assert s.check_hard_limits(res)[0] is True


def test_failed_terminal_write_never_mints(configured_key):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    record = seal_resolution_for_persistence(s, res, status="failed")
    assert res.receipt == "" and "receipt" not in record
    assert "  padded  " in res.conditions


def test_resolved_terminal_write_mints_a_bound_receipt(configured_key):
    s, res, record = _sealed()
    assert res.receipt.startswith(RECEIPT_PREFIX)
    assert record["receipt"] == res.receipt
    # the record is stored in the served shape, so stored == served exactly
    assert res.conditions == ["agreed", "padded"]
    assert res.verify_signatures("key-a", "key-b") is True  # HMAC invariant under that shape

    jwks = export_public_jwks(configured_key)
    claims = verify_resolution_receipt(res.receipt, record, jwks=jwks,
                                       expected_session_id=s.session_id, expected_issuer=ISSUER)
    assert claims["session_id"] == s.session_id
    assert claims["iss"] == ISSUER
    assert claims["paused_agent_id"] == "agent-a"
    assert claims["reviewer_agent_id"] == "agent-b"
    assert claims["kid"] == key_id(configured_key.public_key())
    assert claims["status"] == "resolved"
    assert claims["authorizes"] == [] and claims["stance"] == "descriptive"
    assert claims["record_fields"] == covered_fields(record)
    assert "receipt" not in claims["record_fields"]
    assert claims["record_sha256"] == record_sha256(record, claims["record_fields"])
    assert claims["both_signatures_present"] is True
    assert claims["signature_version"] == 2
    # hash() ignores the receipt, so a receipted record hashes like an unreceipted one
    assert res.hash() == _legacy_hash(res)


def test_sealing_is_idempotent(configured_key):
    s, res, record = _sealed()
    again = seal_resolution_for_persistence(s, res, status="resolved")
    assert again["receipt"] == record["receipt"]


def test_invalid_seed_degrades_with_warning_and_touches_nothing(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ENV, "definitely-not-a-seed")
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    with caplog.at_level(logging.WARNING, logger="src.dialectic_receipt"):
        record = seal_resolution_for_persistence(s, res, status="resolved")
    assert res.receipt == "" and "receipt" not in record
    assert "  padded  " in res.conditions  # before_mint never ran
    assert any("configured but unusable" in r.getMessage() for r in caplog.records)


def test_no_issuer_configured_yields_null_iss(monkeypatch, seed):
    monkeypatch.setenv(KEY_ENV, seed)
    monkeypatch.delenv(ISSUER_ENV, raising=False)
    s, res, record = _sealed()
    claims = verify_resolution_receipt(res.receipt, record, public_key=load_signing_key(seed).public_key())
    assert claims["iss"] is None


def test_llm_assisted_single_signer_is_reported_not_hidden(configured_key):
    s, res, record = _sealed(key_b="")
    claims = verify_resolution_receipt(res.receipt, record, public_key=configured_key.public_key())
    assert claims["both_signatures_present"] is False
    assert res.verify_signatures("key-a", "") is False


# ── binding ────────────────────────────────────────────────────────────────


def test_tampered_covered_field_fails_with_record_mismatch(configured_key):
    s, res, record = _sealed()
    jwks = export_public_jwks(configured_key)
    for field, value in (("conditions", record["conditions"] + ["silently added"]),
                         ("reasoning", "rewritten after the fact"),
                         ("action", "block"),
                         ("signature_b", "")):
        tampered = dict(record, **{field: value})
        with pytest.raises(ReceiptError) as exc:
            verify_resolution_receipt(res.receipt, tampered, jwks=jwks)
        assert exc.value.code == "record_mismatch"


def test_schema_evolution_does_not_invalidate_old_receipts(configured_key):
    s, res, record = _sealed()
    jwks = export_public_jwks(configured_key)
    grown = dict(record, future_field="added by a later release")
    assert verify_resolution_receipt(res.receipt, grown, jwks=jwks)["record_fields"] == covered_fields(record)
    shrunk = dict(record)
    del shrunk["root_cause"]
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, shrunk, jwks=jwks)
    assert exc.value.code == "record_mismatch" and "root_cause" in str(exc.value)


def test_canonical_form_is_exact():
    fields = ["action", "conditions"]
    assert record_sha256({"action": "resume", "conditions": [1]}, fields) != \
        record_sha256({"action": "resume", "conditions": ["1"]}, fields)
    assert record_sha256({"action": "resume", "conditions": [" a"]}, fields) != \
        record_sha256({"action": "resume", "conditions": ["a"]}, fields)
    # key order and uncovered keys do not matter; the receipt key never does
    a = {"conditions": ["x"], "action": "resume", "receipt": "drr.v1.a.b", "extra": 1}
    b = {"action": "resume", "conditions": ["x"]}
    assert canonical_record_bytes(a, fields) == canonical_record_bytes(b, fields)
    assert canonical_record_bytes(b, fields) == b'{"action":"resume","conditions":["x"]}'
    with pytest.raises(ReceiptError) as exc:
        canonical_record_bytes({"action": "resume"}, fields)
    assert exc.value.code == "record_mismatch"


def test_non_ascii_content_is_emitted_raw_and_survives_a_round_trip(configured_key):
    s = _converged_session(("agreed — \U0001F600", "étape"))
    res = s.finalize_resolution("key-a", "key-b")
    record = seal_resolution_for_persistence(s, res, status="resolved")
    raw = canonical_record_bytes(record, covered_fields(record))
    assert "\U0001F600".encode("utf-8") in raw and b"\\u" not in raw
    reloaded = json.loads(json.dumps(record))  # what asyncpg/JSONB hands back
    verify_resolution_receipt(res.receipt, reloaded, public_key=configured_key.public_key())


def test_wrong_key_and_corrupted_signature(configured_key):
    s, res, record = _sealed()
    other = load_signing_key(generate_signing_key_seed())
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record, jwks=export_public_jwks(other))
    assert exc.value.code == "unknown_kid"
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record, public_key=other.public_key())
    assert exc.value.code == "kid_mismatch"
    payload_b64, sig_b64 = res.receipt[len(RECEIPT_PREFIX):].split(".")
    flipped = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(f"{RECEIPT_PREFIX}{payload_b64}.{flipped}", record,
                                  public_key=configured_key.public_key())
    assert exc.value.code == "invalid_signature"


@pytest.mark.parametrize("override", [
    {"authorizes": ["resume"]},
    {"stance": "performative"},
    {"alg": "none"},
    {"status": "failed"},
    {"record_fields": []},
    {"record_fields": "action"},
    {"session_id": ""},
])
def test_receipt_profile_is_enforced_even_when_correctly_signed(configured_key, override):
    s, res, record = _sealed()
    forged = _resign(res.receipt, configured_key, **override)
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(forged, record, public_key=configured_key.public_key())
    assert exc.value.code == "profile_mismatch"


def test_false_kid_under_a_raw_public_key_is_rejected(configured_key):
    s, res, record = _sealed()
    forged = _resign(res.receipt, configured_key, kid="0000000000000000")
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(forged, record, public_key=configured_key.public_key())
    assert exc.value.code == "kid_mismatch"


def test_session_and_issuer_binding(configured_key):
    s, res, record = _sealed()
    pk = configured_key.public_key()
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record, public_key=pk, expected_session_id="other")
    assert exc.value.code == "session_mismatch"
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record, public_key=pk, expected_issuer="someone.else")
    assert exc.value.code == "issuer_mismatch"


def test_envelopes_are_not_interchangeable_and_junk_is_rejected(configured_key):
    s, res, record = _sealed()
    pk = configured_key.public_key()
    aic = mint_identity_attestation(uuid="abc", signing_key=configured_key)
    assert verify_identity_attestation(res.receipt, public_key=pk) is None
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(aic, record, public_key=pk)
    assert exc.value.code == "malformed"
    for junk in ("", None, 42, "drr.v1.", "drr.v1.onlyone", "drr.v1.a.b", "drr.v1..b"):
        with pytest.raises(ReceiptError):
            verify_resolution_receipt(junk, record, public_key=pk)


def test_missing_and_malformed_verification_keys(configured_key):
    s, res, record = _sealed()
    with pytest.raises(ReceiptError) as exc:
        verify_resolution_receipt(res.receipt, record)
    assert exc.value.code == "no_verification_key"
    for bad in ([{}], {"keys": "x"}, {"keys": [1]}, {"keys": [{"kid": 5}]}):
        with pytest.raises(ReceiptError) as exc:
            verify_resolution_receipt(res.receipt, record, jwks=bad)
        assert exc.value.code in {"malformed_jwks", "unknown_kid"}
    with pytest.raises(ReceiptError) as exc:
        merge_jwks([[]])
    assert exc.value.code == "malformed_jwks"


def test_retired_key_verifies_through_a_merged_jwks(configured_key):
    s, res, record = _sealed()
    current = load_signing_key(generate_signing_key_seed())
    merged = merge_jwks([export_public_jwks(current), export_public_jwks(configured_key)])
    assert {k["kid"] for k in merged["keys"]} == {key_id(current.public_key()), key_id(configured_key.public_key())}
    verify_resolution_receipt(res.receipt, record, jwks=merged)


def test_mint_requires_session_id_and_fields(configured_key):
    with pytest.raises(ReceiptError) as exc:
        mint_resolution_receipt({"action": "resume"}, session_id="", paused_agent_id="a",
                                reviewer_agent_id=None, signing_key=configured_key)
    assert exc.value.code == "missing_session_id"
    with pytest.raises(ReceiptError) as exc:
        mint_resolution_receipt({"receipt": "x"}, session_id="s", paused_agent_id="a",
                                reviewer_agent_id=None, signing_key=configured_key)
    assert exc.value.code == "record_mismatch"


# ── reload path ────────────────────────────────────────────────────────────


def test_receipt_survives_jsonb_round_trip_and_reload(configured_key):
    s, res, record = _sealed()
    session_doc = {
        "paused_agent_id": "agent-a",
        "reviewer_agent_id": "agent-b",
        "paused_agent_state": {},
        "phase": "resolved",
        "resolution": json.loads(json.dumps(record)),
    }
    reloaded = _reconstruct_session_from_dict(s.session_id, session_doc)
    assert reloaded.resolution.receipt == res.receipt
    assert reloaded.resolution.signature_version == 2
    assert reloaded.resolution.conditions == ["agreed", "padded"]
    assert reloaded.resolution.verify_signatures("key-a", "key-b") is True
    verify_resolution_receipt(reloaded.resolution.receipt, reloaded.resolution.to_dict(),
                              jwks=export_public_jwks(configured_key),
                              expected_session_id=reloaded.session_id)
    assert reloaded.resolution.hash() == res.hash()


def test_reload_of_legacy_row_has_no_receipt_and_defaults_to_v1():
    doc = {
        "paused_agent_id": "agent-a", "reviewer_agent_id": "agent-b",
        "paused_agent_state": {}, "phase": "resolved",
        "resolution": {"action": "resume", "conditions": ["c"], "root_cause": "r",
                       "reasoning": "x", "signature_a": "sa", "signature_b": "sb",
                       "timestamp": "2026-01-15T12:30:00"},
    }
    reloaded = _reconstruct_session_from_dict("sess-legacy", doc)
    assert reloaded.resolution.signature_version == 1
    assert reloaded.resolution.receipt == ""
    assert "receipt" not in reloaded.resolution.to_dict()


def test_resolution_dataclass_omits_empty_receipt_and_ignores_it_in_hash():
    base = dict(action="resume", conditions=["c"], root_cause="", reasoning="",
                signature_a="", signature_b="", timestamp="t")
    plain = Resolution(**base)
    receipted = Resolution(**base, receipt="drr.v1.x.y")
    assert "receipt" not in plain.to_dict()
    assert receipted.to_dict()["receipt"] == "drr.v1.x.y"
    assert plain.hash() == receipted.hash() == _legacy_hash(plain)
    assert plain.canonical_payload() == receipted.canonical_payload()


# ── the terminal write ─────────────────────────────────────────────────────


def _resolved_session_for_save(phase=DialecticPhase.RESOLVED):
    s = _converged_session()
    res = s.finalize_resolution("key-a", "key-b")
    s.resolution = res
    s.phase = phase
    return s, res


@pytest.mark.asyncio
async def test_save_session_terminal_write_receipts_resolved_and_not_failed(configured_key, monkeypatch):
    monkeypatch.setattr(session_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", False)
    pg = AsyncMock(return_value=True)
    with patch("src.dialectic_db.resolve_session_async", pg), \
         patch("src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve", AsyncMock(return_value=None)):
        s, res = _resolved_session_for_save()
        await save_session(s)
        written = pg.await_args.kwargs["resolution"]
        assert pg.await_args.kwargs["status"] == "resolved"
        assert written["receipt"] == res.receipt and res.receipt.startswith(RECEIPT_PREFIX)
        claims = verify_resolution_receipt(written["receipt"], written,
                                           jwks=export_public_jwks(configured_key),
                                           expected_session_id=s.session_id)
        assert claims["status"] == "resolved"

        pg.reset_mock()
        s, res = _resolved_session_for_save(DialecticPhase.FAILED)
        await save_session(s)
        assert pg.await_args.kwargs["status"] == "failed"
        assert "receipt" not in pg.await_args.kwargs["resolution"] and res.receipt == ""


@pytest.mark.asyncio
async def test_save_session_clears_receipt_when_the_row_is_not_written(configured_key, monkeypatch):
    monkeypatch.setattr(session_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", False)
    with patch("src.dialectic_db.resolve_session_async", AsyncMock(return_value=False)), \
         patch("src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve", AsyncMock(return_value=None)):
        s, res = _resolved_session_for_save()
        await save_session(s)
        assert res.receipt == "" and "receipt" not in res.to_dict()


@pytest.mark.asyncio
async def test_save_session_with_no_key_persists_the_legacy_shape(no_key, monkeypatch):
    monkeypatch.setattr(session_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", False)
    pg = AsyncMock(return_value=True)
    with patch("src.dialectic_db.resolve_session_async", pg), \
         patch("src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve", AsyncMock(return_value=None)):
        s, res = _resolved_session_for_save()
        await save_session(s)
        written = pg.await_args.kwargs["resolution"]
        assert "receipt" not in written
        assert written["conditions"] == res.conditions and "  padded  " in written["conditions"]


# ── offline CLI ────────────────────────────────────────────────────────────


def _load_cli():
    path = project_root / "scripts" / "client" / "verify_resolution_receipt.py"
    spec = importlib.util.spec_from_file_location("verify_resolution_receipt_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cli, capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_contract(configured_key, tmp_path, capsys, monkeypatch):
    cli = _load_cli()
    s, res, record = _sealed()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(export_public_jwks(configured_key)))
    doc_path = tmp_path / "session.json"
    doc_path.write_text(json.dumps({"session_id": s.session_id, "resolution": record}))

    monkeypatch.delenv(KEY_ENV, raising=False)  # the verifier needs no private key
    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path))
    assert code == 0 and out["verified"] is True
    assert out["session_binding"] == "document" and out["issuer"] == ISSUER
    assert any("same document" in w for w in out["warnings"])
    assert any("issuer was not checked" in w for w in out["warnings"])

    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path),
                     "--session-id", s.session_id, "--issuer", ISSUER)
    assert code == 0 and out["session_binding"] == "argument" and out["warnings"] == []

    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path),
                     "--issuer", "someone.else")
    assert code == 1 and out["code"] == "issuer_mismatch"

    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps({"session_id": s.session_id,
                                         "resolution": dict(record, reasoning="rewritten")}))
    code, out = _run(cli, capsys, "verify", "--record", str(tampered_path), "--jwks", str(jwks_path))
    assert code == 1 and out["code"] == "record_mismatch"

    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path),
                     "--session-id", "other")
    assert code == 1 and out["code"] == "session_mismatch"

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({k: v for k, v in record.items() if k != "receipt"}))
    code, out = _run(cli, capsys, "verify", "--record", str(bare), "--jwks", str(jwks_path))
    assert code == 1 and out["code"] == "no_receipt"

    bad_jwks = tmp_path / "bad.json"
    bad_jwks.write_text("[1, 2]")
    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(bad_jwks))
    assert code == 2 and out["code"] == "malformed_jwks"

    retired = tmp_path / "retired.json"
    retired.write_text(json.dumps(export_public_jwks(load_signing_key(generate_signing_key_seed()))))
    code, out = _run(cli, capsys, "verify", "--record", str(doc_path),
                     "--jwks", str(retired), "--jwks", str(jwks_path))
    assert code == 0


def test_cli_single_signer_and_block_are_flagged_not_hidden(configured_key, tmp_path, capsys):
    cli = _load_cli()
    s, res, record = _sealed(key_b="")
    record = dict(record)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(export_public_jwks(configured_key)))
    doc_path = tmp_path / "session.json"
    doc_path.write_text(json.dumps({"session_id": s.session_id, "resolution": record}))
    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path))
    assert code == 0 and out["both_signatures_present"] is False
    assert any("single-signer" in w for w in out["warnings"])


def test_cli_missing_cryptography_is_an_environment_error(configured_key, tmp_path, capsys, monkeypatch):
    cli = _load_cli()
    s, res, record = _sealed()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(export_public_jwks(configured_key)))
    doc_path = tmp_path / "session.json"
    doc_path.write_text(json.dumps({"session_id": s.session_id, "resolution": record}))
    import src.dialectic_receipt as receipt_mod
    monkeypatch.setattr(receipt_mod, "_CRYPTO_AVAILABLE", False)
    code, out = _run(cli, capsys, "verify", "--record", str(doc_path), "--jwks", str(jwks_path))
    assert code == 2 and out["code"] == "crypto_unavailable"


def test_cli_exports_public_jwks_only(configured_key, seed, capsys, monkeypatch):
    cli = _load_cli()
    code, out = _run(cli, capsys, "export-jwks")
    assert code == 0
    assert out["keys"][0]["kid"] == key_id(configured_key.public_key())
    assert seed not in json.dumps(out) and "d" not in out["keys"][0]
    monkeypatch.delenv(KEY_ENV, raising=False)
    code, out = _run(cli, capsys, "export-jwks")
    assert code == 2 and out["code"] == "not_configured"
