#!/usr/bin/env python3
"""Live proof that two participants cannot hold one governed surface at once.

The demo uses only the Python standard library. It acquires a short-lived
maintenance surface for participant A, proves that participant B receives the
typed ``held_by_other`` refusal, transfers ownership through the lease plane's
atomic handoff, verifies B is the new holder, and releases the lease.

This is a coordination proof inside one operator trust boundary. It does not
claim cross-operator federation, interception of clients that bypass the lease
plane, or improved task outcomes.
"""

from __future__ import annotations

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
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://127.0.0.1:8788"
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
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
            },
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


@dataclass(frozen=True)
class DemoResult:
    surface_id: str
    participant_a: str
    participant_b: str
    first_lease_id: str
    second_lease_id: str
    handoff_id: str


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
    participant_a: str | None = None,
    participant_b: str | None = None,
    surface_id: str | None = None,
) -> DemoResult:
    participant_a = participant_a or str(uuid.uuid4())
    participant_b = participant_b or str(uuid.uuid4())
    surface_id = surface_id or f"maintenance:/quickstart-coordination-{uuid.uuid4().hex[:12]}"
    audit_session = f"coordination-demo-{uuid.uuid4().hex[:12]}"

    _require_ok(api.request("GET", "/v1/health"), "lease-plane health check")

    cleanup_lease_id: str | None = None
    handoff_accepted = False
    second_lease_id: str | None = None
    try:
        acquire_common = {
            "surface_id": surface_id,
            "holder_class": "process_instance",
            "holder_kind": "remote_heartbeat",
            "ttl_s": LEASE_TTL_SECONDS,
            "audit_session": audit_session,
        }
        first = api.request(
            "POST",
            "/v1/lease/acquire",
            {
                **acquire_common,
                "holder_agent_uuid": participant_a,
                "intent": "coordination demo: participant A owns the surface",
            },
        )
        first_lease_id = _lease_id(first, "participant A acquire")
        cleanup_lease_id = first_lease_id

        blocked = api.request(
            "POST",
            "/v1/lease/acquire",
            {
                **acquire_common,
                "holder_agent_uuid": participant_b,
                "intent": "coordination demo: participant B contends for the surface",
            },
        )
        if blocked.get("ok") is not False or blocked.get("error") != "held_by_other":
            raise DemoError(f"participant B was not refused with held_by_other: {blocked}")
        if str(blocked.get("held_by_uuid")) != participant_a:
            raise DemoError(f"conflict named the wrong holder: {blocked}")
        if str(blocked.get("blocking_lease_id")) != first_lease_id:
            raise DemoError(f"conflict named the wrong blocking lease: {blocked}")

        offer = _require_ok(
            api.request(
                "POST",
                "/v1/lease/handoff/offer",
                {
                    "lease_id": first_lease_id,
                    "to_holder_agent_uuid": participant_b,
                    "ttl_s": LEASE_TTL_SECONDS,
                },
            ),
            "handoff offer",
        )
        handoff_id = str(offer.get("handoff_id") or "")
        if not handoff_id:
            raise DemoError(f"handoff offer returned no handoff_id: {offer}")

        _require_ok(
            api.request(
                "POST",
                "/v1/lease/handoff/accept",
                {"handoff_id": handoff_id},
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
        if not isinstance(lease, dict) or str(lease.get("holder_agent_uuid")) != participant_b:
            raise DemoError(f"handoff did not make participant B the holder: {status}")
        second_lease_id = str(lease.get("lease_id") or "")
        if not second_lease_id or second_lease_id == first_lease_id:
            raise DemoError(f"handoff did not create a distinct receiving lease: {status}")
        cleanup_lease_id = second_lease_id

        _require_ok(
            api.request(
                "POST",
                "/v1/lease/release",
                {"lease_id": second_lease_id, "release_reason": "normal"},
            ),
            "participant B release",
        )
        cleanup_lease_id = None

        return DemoResult(
            surface_id=surface_id,
            participant_a=participant_a,
            participant_b=participant_b,
            first_lease_id=first_lease_id,
            second_lease_id=second_lease_id,
            handoff_id=handoff_id,
        )
    finally:
        if cleanup_lease_id is not None:
            _best_effort_release(api, cleanup_lease_id)
        elif handoff_accepted and second_lease_id is None:
            # Accept closes A's lease and creates B's. If the status request
            # failed, rediscover that receiving lease before leaving.
            _best_effort_release_surface(api, surface_id, participant_b)


def _best_effort_release(api: LeaseAPI, lease_id: str) -> None:
    try:
        api.request(
            "POST",
            "/v1/lease/release",
            {"lease_id": lease_id, "release_reason": "normal"},
        )
    except Exception:  # noqa: BLE001 - cleanup must preserve the original error
        pass


def _best_effort_release_surface(api: LeaseAPI, surface_id: str, holder: str) -> None:
    try:
        status = api.request(
            "GET",
            "/v1/lease/status",
            query={"surface_id": surface_id},
        )
        lease = status.get("lease")
        if isinstance(lease, dict) and str(lease.get("holder_agent_uuid")) == holder:
            lease_id = lease.get("lease_id")
            if lease_id:
                _best_effort_release(api, str(lease_id))
    except Exception:  # noqa: BLE001 - the 60s TTL is the final cleanup bound
        pass


def print_receipt(result: DemoResult, base_url: str) -> None:
    print(f"[ok] lease plane reachable at {base_url}")
    print(f"[acquired] participant A holds {result.surface_id}")
    print("[protected] participant B refused: held_by_other")
    print(f"[handoff] ownership moved A -> B ({result.handoff_id[:8]}...)")
    print(f"[released] participant B released lease {result.second_lease_id[:8]}...")
    print("\nActivation receipt")
    print("  exclusive surface ownership: active")
    print("  typed collision refusal: active")
    print("  atomic handoff: active")
    print("  audit trail: persisted by the lease plane")
    print("  enforcement boundary: participating clients must acquire before acting")
    print("  not established: cross-operator trust or improved task outcomes")


def main() -> int:
    base_url = lease_base_url()
    api = LeaseAPI(base_url, lease_bearer_token())
    try:
        result = run_demo(api)
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
