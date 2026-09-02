#!/usr/bin/env python3
"""Verify a deployment-signed dialectic resolution receipt offline.

A peer who has pinned a UNITARES deployment's public attestation key can run
this against a resolution record without a server, a database, or any agent
``api_key``. It answers exactly one question: did *that deployment* issue
exactly *this* record for this session? It does not say whether either party
meant it (see ``src/dialectic_receipt.py`` for what a receipt proves).

Usage::

    # verify: the record is either the resolution dict itself, or a dialectic
    # session / tool response that carries it under "resolution"
    scripts/client/verify_resolution_receipt.py verify \\
        --record session.json --jwks deployment-jwks.json [--session-id <id>]

    # export the public half of a configured attestation key, to hand to a peer
    UNITARES_AIC_SIGNING_KEY=... scripts/client/verify_resolution_receipt.py export-jwks

Exit status: 0 verified, 1 not verified (reason code in the JSON output),
2 usage or environment error (for example the ``cryptography`` package is
missing).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_record(document: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(resolution_record, session_id)`` from a record or a session document."""
    if not isinstance(document, dict):
        return None, None
    if "resolution" in document and isinstance(document["resolution"], dict):
        return document["resolution"], document.get("session_id")
    # A tool response may nest the session under "session".
    nested = document.get("session")
    if isinstance(nested, dict) and isinstance(nested.get("resolution"), dict):
        return nested["resolution"], nested.get("session_id") or document.get("session_id")
    if "action" in document and "timestamp" in document:
        return document, document.get("session_id")
    return None, None


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_verify(args: argparse.Namespace) -> int:
    from src.dialectic_receipt import ReceiptError, verify_resolution_receipt

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
                       "finalization, or minting failed (see server logs)",
        })
        return 1
    jwks = _load_json(args.jwks)
    expected_session_id = args.session_id or embedded_session_id
    try:
        claims = verify_resolution_receipt(
            receipt, record, jwks=jwks, expected_session_id=expected_session_id,
        )
    except ReceiptError as exc:
        _emit({"verified": False, "code": exc.code, "message": str(exc)})
        return 1
    _emit({"verified": True, "claims": claims})
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
    verify.add_argument("--jwks", required=True, help="JSON file: the deployment's public JWKS")
    verify.add_argument("--session-id", default=None, help="expected session id (defaults to the document's)")
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
