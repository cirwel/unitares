#!/usr/bin/env python3
"""Live proof that two participants cannot hold one governed surface at once.

The demo uses only the Python standard library. It onboards two governance
identities, proves that A's signed proof cannot impersonate B, acquires a
short-lived maintenance surface for A, proves that B receives the typed
``held_by_other`` refusal, transfers ownership through the lease plane's atomic
handoff, verifies B is the new holder, and releases the lease.

This is a coordination proof inside one operator trust boundary. It does not
claim cross-operator federation, interception of clients that bypass the lease
plane, or improved task outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://127.0.0.1:8788"
DEFAULT_GOVERNANCE_URL = "http://127.0.0.1:8767"
DEFAULT_BEARER_TOKEN = "unitares-local-lease-plane"
LEASE_TTL_SECONDS = 60


class DemoError(RuntimeError):
    """The live coordination contract returned an unexpected result."""


def _dotenv_values(path: Path | None = None) -> dict[str, str]:
    """Read the small subset of dotenv syntax needed by the quickstart."""
    resolved = path or REPO_ROOT / ".env"
    try:
        lines = resolved.read_text().splitlines()
    except FileNotFoundError:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def lease_base_url(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("UNITARES_COORDINATION_DEMO_URL")
    if explicit:
        return explicit.rstrip("/")

    dotenv = _dotenv_values(dotenv_path)
    port = (
        env.get("UNITARES_COORDINATION_DEMO_PORT")
        or env.get("LEASE_PLANE_HOST_PORT")
        or dotenv.get("LEASE_PLANE_HOST_PORT")
        or "8788"
    )
    return f"http://127.0.0.1:{port}"


def lease_bearer_token(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    env = os.environ if environ is None else environ
    dotenv = _dotenv_values(dotenv_path)
    return (
        env.get("UNITARES_COORDINATION_DEMO_TOKEN")
        or env.get("LEASE_PLANE_BEARER_TOKEN")
        or dotenv.get("LEASE_PLANE_BEARER_TOKEN")
        or DEFAULT_BEARER_TOKEN
    )


def governance_base_url(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("UNITARES_COORDINATION_DEMO_GOVERNANCE_URL")
    if explicit:
        return explicit.rstrip("/")

    dotenv = _dotenv_values(dotenv_path)
    port = (
        env.get("UNITARES_COORDINATION_DEMO_GOVERNANCE_PORT")
        or env.get("GOVERNANCE_HOST_PORT")
        or dotenv.get("GOVERNANCE_HOST_PORT")
        or "8767"
    )
    return f"http://127.0.0.1:{port}"


def governance_bearer_token(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    dotenv = _dotenv_values(dotenv_path)
    return (
        env.get("UNITARES_COORDINATION_DEMO_GOVERNANCE_TOKEN")
        or env.get("UNITARES_HTTP_API_TOKEN")
        or dotenv.get("UNITARES_HTTP_API_TOKEN")
        or None
    )


class LeaseAPI:
    def __init__(self, base_url: str, bearer_token: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        identity_proof: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = _wire_json_bytes(payload)
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        if identity_proof:
            headers["X-Unitares-Identity-Proof"] = identity_proof
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Conflict is a successful part of this demo. Decode error bodies so
            # the typed held_by_other envelope remains visible to the caller.
            raw = exc.read()

        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DemoError(f"{method} {path} returned non-JSON data") from exc
        if not isinstance(decoded, dict):
            raise DemoError(f"{method} {path} returned a non-object JSON value")
        return decoded


def _wire_json_bytes(payload: dict[str, Any] | None) -> bytes | None:
    """The exact serializer shared by attestation minting and lease calls."""
    return json.dumps(payload).encode() if payload is not None else None


class GovernanceAPI:
    def __init__(
        self, base_url: str, bearer_token: str | None, timeout_s: float = 15.0
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_s = timeout_s

    def start_participant(self, label: str) -> "ParticipantIdentity":
        body = json.dumps(
            {
                "name": "start_session",
                "arguments": {
                    "force_new": True,
                    "model_type": "coordination-demo",
                    "name": label,
                    "response_mode": "minimal",
                },
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/tools/call",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                decoded = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise DemoError(
                f"governance onboarding for {label} failed with HTTP {exc.code}"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DemoError(
                f"governance onboarding for {label} returned non-JSON data"
            ) from exc

        agent_uuid = _deep_first(decoded, ("agent_uuid", "uuid"))
        identity_proof = _deep_first(decoded, ("continuity_token",))
        if not agent_uuid or not identity_proof:
            raise DemoError(
                f"governance onboarding for {label} returned an incomplete signed identity "
                f"(agent_uuid={'present' if agent_uuid else 'missing'}, "
                f"identity_proof={'present' if identity_proof else 'missing'})"
            )
        return ParticipantIdentity(agent_uuid=agent_uuid, identity_proof=identity_proof)

    def attest(
        self,
        participant: "ParticipantIdentity",
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> str:
        """Exchange a continuity proof for one exact lease mutation."""
        wire_body = _wire_json_bytes(payload) or b""
        body = json.dumps(
            {
                "identity_proof": participant.identity_proof,
                "holder_agent_uuid": participant.agent_uuid,
                "method": method,
                "path": path,
                "body_sha256": hashlib.sha256(wire_body).hexdigest(),
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/lease-holder/attest",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                decoded = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise DemoError(
                f"governance attestation failed with HTTP {exc.code}"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DemoError("governance attestation returned non-JSON data") from exc

        attestation = decoded.get("attestation") if isinstance(decoded, dict) else None
        if not isinstance(attestation, str) or not attestation.startswith("lat.v1."):
            error = decoded.get("error") if isinstance(decoded, dict) else "invalid_response"
            raise DemoError(f"governance attestation failed: {error}")
        return attestation


def _deep_first(value: Any, keys: tuple[str, ...]) -> str | None:
    """Find a string field in a REST/MCP envelope, including JSON text blocks."""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            found = _deep_first(candidate, keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _deep_first(candidate, keys)
            if found:
                return found
    elif isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _deep_first(decoded, keys)
    return None


@dataclass(frozen=True)
class ParticipantIdentity:
    agent_uuid: str
    identity_proof: str


@dataclass(frozen=True)
class DemoResult:
    surface_id: str
    participant_a: str
    participant_b: str
    first_lease_id: str
    second_lease_id: str
    handoff_id: str


AttestationMinter = Callable[[ParticipantIdentity, str, str, dict[str, Any]], str]


def _require_ok(response: dict[str, Any], operation: str) -> dict[str, Any]:
    if response.get("ok") is not True:
        raise DemoError(f"{operation} failed: {response}")
    return response


def _lease_id(response: dict[str, Any], operation: str) -> str:
    lease = _require_ok(response, operation).get("lease")
    if not isinstance(lease, dict) or not lease.get("lease_id"):
        raise DemoError(f"{operation} returned no lease record: {response}")
    return str(lease["lease_id"])


def run_demo(
    api: LeaseAPI,
    *,
    participant_a: ParticipantIdentity,
    participant_b: ParticipantIdentity,
    surface_id: str | None = None,
    mint_attestation: AttestationMinter | None = None,
) -> DemoResult:
    participant_a_uuid = participant_a.agent_uuid
    participant_b_uuid = participant_b.agent_uuid
    surface_id = (
        surface_id or f"maintenance:/quickstart-coordination-{uuid.uuid4().hex[:12]}"
    )
    audit_session = f"coordination-demo-{uuid.uuid4().hex[:12]}"

    _require_ok(api.request("GET", "/v1/health"), "lease-plane health check")

    cleanup_lease_id: str | None = None
    handoff_accepted = False
    second_lease_id: str | None = None

    def proof_for(
        participant: ParticipantIdentity, path: str, payload: dict[str, Any]
    ) -> str:
        if mint_attestation is None:
            return participant.identity_proof
        return mint_attestation(participant, "POST", path, payload)

    try:
        acquire_common = {
            "surface_id": surface_id,
            "holder_class": "process_instance",
            "holder_kind": "remote_heartbeat",
            "ttl_s": LEASE_TTL_SECONDS,
            "audit_session": audit_session,
        }
        spoof_payload = {
            **acquire_common,
            "holder_agent_uuid": participant_b_uuid,
            "intent": "coordination demo: A must not impersonate B",
        }
        spoof = api.request(
            "POST",
            "/v1/lease/acquire",
            spoof_payload,
            identity_proof=proof_for(participant_a, "/v1/lease/acquire", spoof_payload),
        )
        if (
            spoof.get("ok") is not False
            or spoof.get("error") != "permission_denied"
            or spoof.get("reason") != "identity_proof_invalid"
        ):
            raise DemoError(
                f"participant A's proof was not refused for participant B: {spoof}"
            )

        first_payload = {
            **acquire_common,
            "holder_agent_uuid": participant_a_uuid,
            "intent": "coordination demo: participant A owns the surface",
        }
        first_proof = proof_for(participant_a, "/v1/lease/acquire", first_payload)
        first = api.request(
            "POST",
            "/v1/lease/acquire",
            first_payload,
            identity_proof=first_proof,
        )
        first_lease_id = _lease_id(first, "participant A acquire")
        cleanup_lease_id = first_lease_id

        replayed = api.request(
            "POST",
            "/v1/lease/acquire",
            first_payload,
            identity_proof=first_proof,
        )
        if (
            replayed.get("ok") is not False
            or replayed.get("error") != "permission_denied"
            or replayed.get("reason") != "identity_proof_replayed"
        ):
            raise DemoError(f"captured attestation was not refused on replay: {replayed}")

        blocked_payload = {
            **acquire_common,
            "holder_agent_uuid": participant_b_uuid,
            "intent": "coordination demo: participant B contends for the surface",
        }
        blocked = api.request(
            "POST",
            "/v1/lease/acquire",
            blocked_payload,
            identity_proof=proof_for(participant_b, "/v1/lease/acquire", blocked_payload),
        )
        if blocked.get("ok") is not False or blocked.get("error") != "held_by_other":
            raise DemoError(
                f"participant B was not refused with held_by_other: {blocked}"
            )
        if str(blocked.get("held_by_uuid")) != participant_a_uuid:
            raise DemoError(f"conflict named the wrong holder: {blocked}")
        if str(blocked.get("blocking_lease_id")) != first_lease_id:
            raise DemoError(f"conflict named the wrong blocking lease: {blocked}")

        offer_payload = {
            "lease_id": first_lease_id,
            "to_holder_agent_uuid": participant_b_uuid,
            "ttl_s": LEASE_TTL_SECONDS,
        }
        offer = _require_ok(
            api.request(
                "POST",
                "/v1/lease/handoff/offer",
                offer_payload,
                identity_proof=proof_for(
                    participant_a, "/v1/lease/handoff/offer", offer_payload
                ),
            ),
            "handoff offer",
        )
        handoff_id = str(offer.get("handoff_id") or "")
        if not handoff_id:
            raise DemoError(f"handoff offer returned no handoff_id: {offer}")

        accept_payload = {"handoff_id": handoff_id}
        _require_ok(
            api.request(
                "POST",
                "/v1/lease/handoff/accept",
                accept_payload,
                identity_proof=proof_for(
                    participant_b, "/v1/lease/handoff/accept", accept_payload
                ),
            ),
            "handoff accept",
        )
        handoff_accepted = True
        cleanup_lease_id = None

        status = _require_ok(
            api.request(
                "GET",
                "/v1/lease/status",
                query={"surface_id": surface_id},
            ),
            "post-handoff status",
        )
        lease = status.get("lease")
        if (
            not isinstance(lease, dict)
            or str(lease.get("holder_agent_uuid")) != participant_b_uuid
        ):
            raise DemoError(f"handoff did not make participant B the holder: {status}")
        second_lease_id = str(lease.get("lease_id") or "")
        if not second_lease_id or second_lease_id == first_lease_id:
            raise DemoError(
                f"handoff did not create a distinct receiving lease: {status}"
            )
        cleanup_lease_id = second_lease_id

        release_payload = {"lease_id": second_lease_id, "release_reason": "normal"}
        _require_ok(
            api.request(
                "POST",
                "/v1/lease/release",
                release_payload,
                identity_proof=proof_for(
                    participant_b, "/v1/lease/release", release_payload
                ),
            ),
            "participant B release",
        )
        cleanup_lease_id = None

        return DemoResult(
            surface_id=surface_id,
            participant_a=participant_a_uuid,
            participant_b=participant_b_uuid,
            first_lease_id=first_lease_id,
            second_lease_id=second_lease_id,
            handoff_id=handoff_id,
        )
    finally:
        if cleanup_lease_id is not None:
            cleanup_identity = participant_b if handoff_accepted else participant_a
            _best_effort_release(api, cleanup_lease_id, cleanup_identity, mint_attestation)
        elif handoff_accepted and second_lease_id is None:
            # Accept closes A's lease and creates B's. If the status request
            # failed, rediscover that receiving lease before leaving.
            _best_effort_release_surface(api, surface_id, participant_b, mint_attestation)


def _best_effort_release(
    api: LeaseAPI,
    lease_id: str,
    holder: ParticipantIdentity,
    mint_attestation: AttestationMinter | None,
) -> None:
    try:
        payload = {"lease_id": lease_id, "release_reason": "normal"}
        proof = (
            mint_attestation(holder, "POST", "/v1/lease/release", payload)
            if mint_attestation
            else holder.identity_proof
        )
        api.request(
            "POST",
            "/v1/lease/release",
            payload,
            identity_proof=proof,
        )
    except Exception:  # noqa: BLE001 - cleanup must preserve the original error
        pass


def _best_effort_release_surface(
    api: LeaseAPI,
    surface_id: str,
    holder: ParticipantIdentity,
    mint_attestation: AttestationMinter | None,
) -> None:
    try:
        status = api.request(
            "GET",
            "/v1/lease/status",
            query={"surface_id": surface_id},
        )
        lease = status.get("lease")
        if (
            isinstance(lease, dict)
            and str(lease.get("holder_agent_uuid")) == holder.agent_uuid
        ):
            lease_id = lease.get("lease_id")
            if lease_id:
                _best_effort_release(api, str(lease_id), holder, mint_attestation)
    except Exception:  # noqa: BLE001 - the 60s TTL is the final cleanup bound
        pass


def print_receipt(result: DemoResult, base_url: str) -> None:
    print(f"[ok] lease plane reachable at {base_url}")
    print("[identity] A's proof cannot impersonate B")
    print("[replay] captured request attestation is single-use")
    print(f"[acquired] participant A holds {result.surface_id}")
    print("[protected] participant B refused: held_by_other")
    print(f"[handoff] ownership moved A -> B ({result.handoff_id[:8]}...)")
    print(f"[released] participant B released lease {result.second_lease_id[:8]}...")
    print("\nActivation receipt")
    print("  exclusive surface ownership: active")
    print("  governance identity binding: enforced")
    print("  request-bound replay resistance: active")
    print("  typed collision refusal: active")
    print("  atomic handoff: active")
    print("  audit trail: persisted by the lease plane")
    print("  enforcement boundary: participating clients must acquire before acting")
    print("  not exercised by this local demo: cross-operator trust or improved task outcomes")


def main() -> int:
    base_url = lease_base_url()
    api = LeaseAPI(base_url, lease_bearer_token())
    try:
        governance = GovernanceAPI(governance_base_url(), governance_bearer_token())
        participant_a = governance.start_participant("Coordination demo participant A")
        participant_b = governance.start_participant("Coordination demo participant B")
        result = run_demo(
            api,
            participant_a=participant_a,
            participant_b=participant_b,
            mint_attestation=governance.attest,
        )
    except (DemoError, urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        print(
            f"Coordination demo could not complete against {base_url}.\n"
            "Start the bundled stack first:\n"
            "    docker compose up -d --wait --build\n"
            "If you changed the host port, run for example:\n"
            "    UNITARES_COORDINATION_DEMO_PORT=18788 make coordination-demo\n"
            f"\nError: {exc}",
            file=sys.stderr,
        )
        return 1

    print_receipt(result, base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
