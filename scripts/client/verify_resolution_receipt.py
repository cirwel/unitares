#!/usr/bin/env python3
"""Verify a deployment-signed dialectic resolution receipt offline.

A peer who has pinned a UNITARES deployment's public attestation key can run
this against a resolution record without a server, a database, or any agent
``api_key``. It answers exactly one question: did the holder of that key
persist exactly this record as a resolved session? It does not say whether
either party meant it (see ``src/dialectic_receipt.py`` for what a receipt
proves and what it does not).

Usage::

    # verify: the record is either the resolution dict itself, or a dialectic
    # session / tool response that carries it under "resolution". Pass --jwks
    # more than once to accept a retired key alongside the current one.
    scripts/client/verify_resolution_receipt.py verify \\
        --record session.json --jwks current-jwks.json [--jwks retired-jwks.json] \\
        [--session-id <id>] [--issuer <name>]

    # export the public half of a configured attestation key, to hand to a peer
    UNITARES_AIC_SIGNING_KEY=... scripts/client/verify_resolution_receipt.py export-jwks

Read the ``warnings`` in the output before trusting a ``verified: true``:
a session id taken from the same document that carries the receipt binds
nothing, a single-signer record was never bilaterally attested, and a
verified record whose ``action`` is not ``resume`` is still a verified
record.

Exit status: 0 verified, 1 not verified (reason code in the JSON output),
2 usage or environment error (malformed inputs, or the ``cryptography``
package is missing).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_ENVIRONMENT_CODES = {"crypto_unavailable", "malformed_jwks", "no_verification_key"}


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_record(document: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(resolution_record, session_id)`` from a record or a session document."""
    if not isinstance(document, dict):
        return None, None
    if isinstance(document.get("resolution"), dict):
        return document["resolution"], document.get("session_id")
    nested = document.get("session")
    if isinstance(nested, dict) and isinstance(nested.get("resolution"), dict):
        return nested["resolution"], nested.get("session_id") or document.get("session_id")
    if "action" in document and "timestamp" in document:
        return document, None
    return None, None


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def cmd_verify(args: argparse.Namespace) -> int:
    from src.dialectic_receipt import ReceiptError, merge_jwks, verify_resolution_receipt

    document = _load_json(args.record)
    record, embedded_session_id = _extract_record(document)
    if record is None:
        _emit({"verified": False, "code": "no_record", "message": "no resolution record found in --record"})
        return 2
    receipt = args.receipt or record.get("receipt") or ""
    if not receipt:
        _emit({
            "verified": False,
            "code": "no_receipt",
            "message": "record carries no receipt: the deployment had no attestation key at "
                       "the terminal write, or minting failed (see server logs)",
        })
        return 1

    warnings: List[str] = []
    if args.session_id:
        expected_session_id, binding = args.session_id, "argument"
    elif embedded_session_id:
        expected_session_id, binding = embedded_session_id, "document"
        warnings.append(
            "session id was taken from the same document as the receipt; pass "
            "--session-id from an independent source to bind the record to a session"
        )
    else:
        expected_session_id, binding = None, "none"
        warnings.append("no session id available; the receipt's session binding was not checked")

    try:
        jwks = merge_jwks([_load_json(path) for path in args.jwks])
        claims = verify_resolution_receipt(
            receipt, record, jwks=jwks,
            expected_session_id=expected_session_id, expected_issuer=args.issuer,
        )
    except ReceiptError as exc:
        _emit({"verified": False, "code": exc.code, "message": str(exc)})
        return 2 if exc.code in _ENVIRONMENT_CODES else 1

    if not claims.get("both_signatures_present"):
        warnings.append("single-signer record: only one party's symmetric signature was stored")
    if record.get("action") != "resume":
        warnings.append(f"record action is {record.get('action')!r}, not 'resume'")
    if not args.issuer:
        warnings.append("issuer was not checked; pass --issuer to pin the deployment name")
    _emit({
        "verified": True,
        "issuer": claims.get("iss"),
        "kid": claims.get("kid"),
        "session_id": claims.get("session_id"),
        "session_binding": binding,
        "both_signatures_present": claims.get("both_signatures_present"),
        "record_fields": claims.get("record_fields"),
        "action": record.get("action"),
        "warnings": warnings,
        "claims": claims,
    })
    return 0


def cmd_export_jwks(args: argparse.Namespace) -> int:
    from src.identity.agent_identity_credential import (
        AICError,
        export_public_jwks,
        load_signing_key_if_configured,
    )

    try:
        key = load_signing_key_if_configured()
    except AICError as exc:
        _emit({"exported": False, "code": "invalid_key", "message": str(exc)})
        return 2
    if key is None:
        _emit({
            "exported": False,
            "code": "not_configured",
            "message": "UNITARES_AIC_SIGNING_KEY is not set; nothing to export",
        })
        return 2
    _emit(export_public_jwks(key))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify a receipt against a record and a JWKS")
    verify.add_argument("--record", required=True, help="JSON file: resolution record or session document")
    verify.add_argument("--jwks", required=True, action="append",
                        help="JSON file: the deployment's public JWKS (repeatable)")
    verify.add_argument("--session-id", default=None, help="expected session id from an independent source")
    verify.add_argument("--issuer", default=None, help="expected issuer name (the deployment's declared iss)")
    verify.add_argument("--receipt", default=None, help="receipt string, if not embedded in the record")
    verify.set_defaults(func=cmd_verify)

    export = sub.add_parser("export-jwks", help="print the public JWKS for the configured attestation key")
    export.set_defaults(func=cmd_export_jwks)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ImportError as exc:
        _emit({"verified": False, "code": "crypto_unavailable", "message": str(exc)})
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        _emit({"verified": False, "code": "input_error", "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
