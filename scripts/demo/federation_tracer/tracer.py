"""Run an evidence-oriented federation trace across two independent governors.

This is deliberately an isolated research harness, not a production identity or
authorization path.  It exercises the minimum mechanism the accountable
multi-principal testbed proposal depends on:

* two administrative domains in separate OS processes with distinct Ed25519 keys;
* explicit public-key pinning, with no shared private signing secret;
* compact-JWS vouchers bound to issuer, audience, scope, nonce, expiry, and an
  evidence digest;
* holder-bound authorization whose effect request requires a fresh proof of
  possession, so copying the authorization token alone is insufficient; and
* separate reporting of cryptographic origin and evidence consistency.  A
  legitimately signed false voucher is authentic; only an evidence/policy check
  can reject it as inconsistent.

Run from the repository root:

    python3 -m scripts.demo.federation_tracer.tracer
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


VOUCHER_TYPE = "UNITARES-FED-VOUCHER"
AUTHORIZATION_TYPE = "UNITARES-FED-AUTHZ"
PROTOCOL_VERSION = 1
TRACE_NOW = 1_785_750_000


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _public_jwk(key: Ed25519PublicKey) -> dict[str, str]:
    raw = key.public_bytes_raw()
    kid = hashlib.sha256(raw).hexdigest()[:16]
    return {
        "alg": "EdDSA",
        "crv": "Ed25519",
        "kid": kid,
        "kty": "OKP",
        "use": "sig",
        "x": _b64u_encode(raw),
    }


def _key_from_jwk(jwk: dict[str, str]) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("unsupported_key")
    return Ed25519PublicKey.from_public_bytes(_b64u_decode(jwk["x"]))


def _jwk_thumbprint(jwk: dict[str, str]) -> str:
    material = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}
    return _b64u_encode(hashlib.sha256(_canonical(material)).digest())


def _sign_compact_jws(
    claims: dict[str, Any], token_type: str, key: Ed25519PrivateKey
) -> str:
    header = {
        "alg": "EdDSA",
        "kid": _public_jwk(key.public_key())["kid"],
        "typ": token_type,
        "v": PROTOCOL_VERSION,
    }
    encoded_header = _b64u_encode(_canonical(header))
    encoded_claims = _b64u_encode(_canonical(claims))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    return f"{encoded_header}.{encoded_claims}.{_b64u_encode(key.sign(signing_input))}"


def _unverified_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed_token")
    value = json.loads(_b64u_decode(parts[1]))
    if not isinstance(value, dict):
        raise ValueError("malformed_claims")
    return value


def _tamper_claim(token: str, name: str, value: Any) -> str:
    """Change a claim without re-signing it, to exercise signature rejection."""
    header, payload, signature = token.split(".")
    claims = json.loads(_b64u_decode(payload))
    claims[name] = value
    return f"{header}.{_b64u_encode(_canonical(claims))}.{signature}"


def _proof_context(
    authorization_jti: str,
    audience: str,
    request: dict[str, Any],
    nonce: str,
    now: int,
) -> dict[str, Any]:
    return {
        "aud": audience,
        "authorization_jti": authorization_jti,
        "body_sha256": _sha256(request["body"]),
        "iat": now,
        "method": request["method"],
        "nonce": nonce,
        "path": request["path"],
    }


def _holder_proof(key: Ed25519PrivateKey, context: dict[str, Any]) -> dict[str, Any]:
    return {"context": context, "signature": _b64u_encode(key.sign(_canonical(context)))}


class Governor:
    """One isolated governor's key, explicit trust pins, and replay state."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._key = Ed25519PrivateKey.generate()
        self.public_jwk = _public_jwk(self._key.public_key())
        self._trusted: dict[str, dict[str, str]] = {domain: self.public_jwk}
        self._seen_voucher_nonces: set[str] = set()
        self._seen_proof_nonces: set[str] = set()

    def trust(self, issuer: str, jwk: dict[str, str]) -> dict[str, Any]:
        _key_from_jwk(jwk)
        self._trusted[issuer] = jwk
        return {"accepted": True, "issuer": issuer, "kid": jwk["kid"]}

    def issue_voucher(
        self,
        *,
        audience: str,
        subject: str,
        scope: list[str],
        stated_effect: dict[str, Any],
        nonce: str,
        now: int,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        claims = {
            "aud": audience,
            "evidence": {
                "canonicalization": "json-sort-keys-v1",
                "sha256": _sha256(stated_effect),
            },
            "exp": now + ttl_seconds,
            "iat": now,
            "iss": self.domain,
            "jti": f"voucher:{nonce}",
            "nbf": now,
            "nonce": nonce,
            "scope": sorted(set(scope)),
            "sub": subject,
        }
        return {"token": _sign_compact_jws(claims, VOUCHER_TYPE, self._key)}

    def issue_authorization(
        self,
        *,
        audience: str,
        subject: str,
        holder_jwk: dict[str, str],
        scope: list[str],
        jti: str,
        now: int,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        _key_from_jwk(holder_jwk)
        claims = {
            "aud": audience,
            "cnf": {"jkt": _jwk_thumbprint(holder_jwk)},
            "exp": now + ttl_seconds,
            "iat": now,
            "iss": self.domain,
            "jti": jti,
            "nbf": now,
            "scope": sorted(set(scope)),
            "sub": subject,
        }
        return {"token": _sign_compact_jws(claims, AUTHORIZATION_TYPE, self._key)}

    def _verify_origin(
        self, token: str, expected_type: str
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            encoded_header, encoded_claims, encoded_signature = token.split(".")
            header = json.loads(_b64u_decode(encoded_header))
            claims = json.loads(_b64u_decode(encoded_claims))
            if not isinstance(header, dict) or not isinstance(claims, dict):
                return None, "malformed_token"
            if (
                header.get("alg") != "EdDSA"
                or header.get("typ") != expected_type
                or header.get("v") != PROTOCOL_VERSION
            ):
                return None, "wrong_token_type"
            issuer = claims.get("iss")
            jwk = self._trusted.get(str(issuer))
            if jwk is None:
                return None, "untrusted_issuer"
            if header.get("kid") != jwk.get("kid"):
                return None, "untrusted_key"
            signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
            _key_from_jwk(jwk).verify(_b64u_decode(encoded_signature), signing_input)
            return claims, "origin_verified"
        except InvalidSignature:
            return None, "invalid_signature"
        except Exception:
            return None, "malformed_token"

    @staticmethod
    def _validity_reason(claims: dict[str, Any], now: int) -> str | None:
        try:
            if now < int(claims["nbf"]):
                return "not_yet_valid"
            if now >= int(claims["exp"]):
                return "expired"
        except (KeyError, TypeError, ValueError):
            return "invalid_validity_window"
        return None

    def verify_voucher(
        self,
        *,
        token: str,
        expected_audience: str,
        required_scope: str,
        observed_effect: dict[str, Any],
        now: int,
    ) -> dict[str, Any]:
        claims, reason = self._verify_origin(token, VOUCHER_TYPE)
        if claims is None:
            return {
                "accepted": False,
                "evidence_consistent": None,
                "origin_authentic": False,
                "reason": reason,
            }
        base = {"origin_authentic": True}
        validity_reason = self._validity_reason(claims, now)
        if validity_reason:
            return {**base, "accepted": False, "evidence_consistent": None, "reason": validity_reason}
        if claims.get("aud") != expected_audience or expected_audience != self.domain:
            return {**base, "accepted": False, "evidence_consistent": None, "reason": "wrong_audience"}
        if required_scope not in claims.get("scope", []):
            return {**base, "accepted": False, "evidence_consistent": None, "reason": "insufficient_scope"}
        nonce = str(claims.get("nonce", ""))
        if not nonce:
            return {**base, "accepted": False, "evidence_consistent": None, "reason": "missing_nonce"}
        if nonce in self._seen_voucher_nonces:
            return {**base, "accepted": False, "evidence_consistent": None, "reason": "replay"}
        expected_digest = _sha256(observed_effect)
        supplied_digest = claims.get("evidence", {}).get("sha256")
        consistent = supplied_digest == expected_digest
        self._seen_voucher_nonces.add(nonce)
        if not consistent:
            return {
                **base,
                "accepted": False,
                "evidence_consistent": False,
                "reason": "evidence_mismatch",
            }
        return {
            **base,
            "accepted": True,
            "evidence_consistent": True,
            "issuer": claims["iss"],
            "reason": "accepted",
            "subject": claims["sub"],
        }

    def verify_effect_request(
        self,
        *,
        authorization_token: str,
        holder_jwk: dict[str, str],
        proof: dict[str, Any],
        request: dict[str, Any],
        required_scope: str,
        now: int,
    ) -> dict[str, Any]:
        claims, reason = self._verify_origin(authorization_token, AUTHORIZATION_TYPE)
        if claims is None:
            return {"accepted": False, "origin_authentic": False, "reason": reason}
        base = {"origin_authentic": True}
        validity_reason = self._validity_reason(claims, now)
        if validity_reason:
            return {**base, "accepted": False, "reason": validity_reason}
        if claims.get("aud") != self.domain:
            return {**base, "accepted": False, "reason": "wrong_audience"}
        if required_scope not in claims.get("scope", []):
            return {**base, "accepted": False, "reason": "insufficient_scope"}
        try:
            if claims.get("cnf", {}).get("jkt") != _jwk_thumbprint(holder_jwk):
                return {**base, "accepted": False, "reason": "holder_key_mismatch"}
            context = proof["context"]
            expected_context = _proof_context(
                str(claims["jti"]), self.domain, request, str(context["nonce"]), int(context["iat"])
            )
            if context != expected_context:
                return {**base, "accepted": False, "reason": "request_binding_mismatch"}
            if abs(now - int(context["iat"])) > 30:
                return {**base, "accepted": False, "reason": "stale_proof"}
            nonce = str(context["nonce"])
            if nonce in self._seen_proof_nonces:
                return {**base, "accepted": False, "reason": "replay"}
            _key_from_jwk(holder_jwk).verify(
                _b64u_decode(proof["signature"]), _canonical(context)
            )
        except InvalidSignature:
            return {**base, "accepted": False, "reason": "invalid_holder_signature"}
        except Exception:
            return {**base, "accepted": False, "reason": "malformed_holder_proof"}
        self._seen_proof_nonces.add(nonce)
        return {**base, "accepted": True, "reason": "accepted", "subject": claims["sub"]}

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.pop("operation", None)
        if operation == "describe":
            return {
                "domain": self.domain,
                "kid": self.public_jwk["kid"],
                "pid": os.getpid(),
                "public_jwk": self.public_jwk,
            }
        if operation == "trust":
            return self.trust(**request)
        if operation == "issue_voucher":
            return self.issue_voucher(**request)
        if operation == "verify_voucher":
            return self.verify_voucher(**request)
        if operation == "issue_authorization":
            return self.issue_authorization(**request)
        if operation == "verify_effect_request":
            return self.verify_effect_request(**request)
        if operation == "shutdown":
            return {"shutdown": True}
        raise ValueError(f"unknown_operation:{operation}")


def _worker_main(domain: str) -> None:
    governor = Governor(domain)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = governor.dispatch(request)
            shutdown = bool(response.get("shutdown"))
            print(json.dumps({"ok": True, "result": response}, sort_keys=True), flush=True)
            if shutdown:
                return
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), flush=True)


@dataclass
class GovernorProcess:
    domain: str
    process: subprocess.Popen[str]

    @classmethod
    def start(cls, domain: str) -> "GovernorProcess":
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.demo.federation_tracer.tracer", "--worker", domain],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return cls(domain=domain, process=process)

    def call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("worker_pipe_unavailable")
        self.process.stdin.write(json.dumps({"operation": operation, **arguments}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError(f"worker_exited:{self.process.returncode}:{stderr}")
        response = json.loads(line)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "worker_error"))
        return response["result"]

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.call("shutdown")
            self.process.wait(timeout=3)
        except Exception:
            self.process.terminate()
            self.process.wait(timeout=3)


def run_trace(now: int = TRACE_NOW) -> dict[str, Any]:
    alpha = GovernorProcess.start("principal-alpha.example")
    beta = GovernorProcess.start("principal-beta.example")
    try:
        alpha_description = alpha.call("describe")
        beta_description = beta.call("describe")
        alpha.call(
            "trust",
            issuer=beta_description["domain"],
            jwk=beta_description["public_jwk"],
        )
        beta.call(
            "trust",
            issuer=alpha_description["domain"],
            jwk=alpha_description["public_jwk"],
        )

        effect = {
            "action": "write",
            "path": "artifacts/result.json",
            "sha256": "b8f6f7f6f705a2046c7f5f70f37f3f12512db643e1f54cf93f2b4c312de297de",
        }
        valid = alpha.call(
            "issue_voucher",
            audience=beta.domain,
            subject="agent-alpha-7",
            scope=["effect:write"],
            stated_effect=effect,
            nonce="voucher-valid-001",
            now=now,
            ttl_seconds=120,
        )["token"]
        valid_result = beta.call(
            "verify_voucher",
            token=valid,
            expected_audience=beta.domain,
            required_scope="effect:write",
            observed_effect=effect,
            now=now + 1,
        )
        replay_result = beta.call(
            "verify_voucher",
            token=valid,
            expected_audience=beta.domain,
            required_scope="effect:write",
            observed_effect=effect,
            now=now + 2,
        )
        forgery_result = beta.call(
            "verify_voucher",
            token=_tamper_claim(valid, "scope", ["effect:admin"]),
            expected_audience=beta.domain,
            required_scope="effect:admin",
            observed_effect=effect,
            now=now + 2,
        )
        wrong_audience = alpha.call(
            "issue_voucher",
            audience="principal-gamma.example",
            subject="agent-alpha-7",
            scope=["effect:write"],
            stated_effect=effect,
            nonce="voucher-audience-001",
            now=now,
            ttl_seconds=120,
        )["token"]
        wrong_audience_result = beta.call(
            "verify_voucher",
            token=wrong_audience,
            expected_audience=beta.domain,
            required_scope="effect:write",
            observed_effect=effect,
            now=now + 2,
        )
        expired = alpha.call(
            "issue_voucher",
            audience=beta.domain,
            subject="agent-alpha-7",
            scope=["effect:write"],
            stated_effect=effect,
            nonce="voucher-expired-001",
            now=now - 120,
            ttl_seconds=60,
        )["token"]
        expired_result = beta.call(
            "verify_voucher",
            token=expired,
            expected_audience=beta.domain,
            required_scope="effect:write",
            observed_effect=effect,
            now=now,
        )

        false_statement = {**effect, "path": "artifacts/allowed.json"}
        authentic_false = alpha.call(
            "issue_voucher",
            audience=beta.domain,
            subject="compromised-governor-alpha",
            scope=["effect:write"],
            stated_effect=false_statement,
            nonce="voucher-false-001",
            now=now,
            ttl_seconds=120,
        )["token"]
        authentic_false_result = beta.call(
            "verify_voucher",
            token=authentic_false,
            expected_audience=beta.domain,
            required_scope="effect:write",
            observed_effect=effect,
            now=now + 2,
        )

        holder_key = Ed25519PrivateKey.generate()
        holder_jwk = _public_jwk(holder_key.public_key())
        attacker_key = Ed25519PrivateKey.generate()
        attacker_jwk = _public_jwk(attacker_key.public_key())
        authorization = beta.call(
            "issue_authorization",
            audience=beta.domain,
            subject="agent-alpha-7",
            holder_jwk=holder_jwk,
            scope=["effect:write"],
            jti="authorization-001",
            now=now,
            ttl_seconds=120,
        )["token"]
        request = {"body": effect, "method": "POST", "path": "/effects/commit"}
        attacker_context = _proof_context(
            "authorization-001", beta.domain, request, "proof-stolen-001", now + 3
        )
        stolen_token_result = beta.call(
            "verify_effect_request",
            authorization_token=authorization,
            holder_jwk=attacker_jwk,
            proof=_holder_proof(attacker_key, attacker_context),
            request=request,
            required_scope="effect:write",
            now=now + 3,
        )
        holder_context = _proof_context(
            "authorization-001", beta.domain, request, "proof-valid-001", now + 4
        )
        holder_proof = _holder_proof(holder_key, holder_context)
        holder_result = beta.call(
            "verify_effect_request",
            authorization_token=authorization,
            holder_jwk=holder_jwk,
            proof=holder_proof,
            request=request,
            required_scope="effect:write",
            now=now + 4,
        )
        proof_replay_result = beta.call(
            "verify_effect_request",
            authorization_token=authorization,
            holder_jwk=holder_jwk,
            proof=holder_proof,
            request=request,
            required_scope="effect:write",
            now=now + 5,
        )

        cases = {
            "authentic_but_false_evidence": authentic_false_result,
            "expired_voucher": expired_result,
            "forged_signature": forgery_result,
            "holder_bound_request": holder_result,
            "proof_replay": proof_replay_result,
            "stolen_token_without_holder_key": stolen_token_result,
            "valid_cross_governor_voucher": valid_result,
            "voucher_replay": replay_result,
            "wrong_audience": wrong_audience_result,
        }
        expected = {
            "authentic_but_false_evidence": (False, "evidence_mismatch"),
            "expired_voucher": (False, "expired"),
            "forged_signature": (False, "invalid_signature"),
            "holder_bound_request": (True, "accepted"),
            "proof_replay": (False, "replay"),
            "stolen_token_without_holder_key": (False, "holder_key_mismatch"),
            "valid_cross_governor_voucher": (True, "accepted"),
            "voucher_replay": (False, "replay"),
            "wrong_audience": (False, "wrong_audience"),
        }
        checks = {
            name: cases[name].get("accepted") == expected[name][0]
            and cases[name].get("reason") == expected[name][1]
            for name in sorted(cases)
        }
        process_isolation = (
            alpha_description["pid"] != beta_description["pid"]
            and alpha_description["kid"] != beta_description["kid"]
        )
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "cases": cases,
            "checks": {**checks, "distinct_governor_processes_and_keys": process_isolation},
            "limitations": [
                "This is a two-process protocol tracer, not a full multi-host deployment.",
                "Public-key pinning is an explicit trust bootstrap; there is no shared private signing root.",
                "A valid signature proves issuer origin, not claim truth. The false-voucher case is rejected only because recipient-observed evidence disagrees.",
                "Proof-of-possession shows that a copied authorization token alone is insufficient; it does not protect a stolen token plus its holder private key.",
            ],
            "protocol": {
                "authorization": "compact JWS / Ed25519 with cnf JWK thumbprint plus request proof",
                "trust_bootstrap": "bilateral public-key pinning",
                "voucher": "compact JWS / Ed25519",
            },
            "schema": "unitares.federation-trace.v1",
            "summary": {
                "all_expected_controls_pass": process_isolation and all(checks.values()),
                "case_count": len(cases),
                "passed_checks": sum(checks.values()) + int(process_isolation),
                "total_checks": len(checks) + 1,
            },
            "topology": {
                "governors": [alpha_description, beta_description],
                "private_key_visibility": "each private key remains inside its governor process",
                "shared_private_signing_key": False,
            },
        }
    finally:
        alpha.close()
        beta.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", metavar="DOMAIN", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help="write the JSON trace to this path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.worker:
        _worker_main(args.worker)
        return 0
    trace = run_trace()
    rendered = json.dumps(trace, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if trace["summary"]["all_expected_controls_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
