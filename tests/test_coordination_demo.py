"""Contract tests for the live two-participant coordination demo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_coordination_demo():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "demo"
        / "coordination_demo.py"
    )
    spec = importlib.util.spec_from_file_location("coordination_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coordination_demo = _load_coordination_demo()

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
LEASE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LEASE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
HANDOFF = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SURFACE = "maintenance:/quickstart-coordination-test"
PROOF_A = "v1.participant-a.signature"
PROOF_B = "v1.participant-b.signature"

IDENTITY_A = coordination_demo.ParticipantIdentity(agent_uuid=A, identity_proof=PROOF_A)
IDENTITY_B = coordination_demo.ParticipantIdentity(agent_uuid=B, identity_proof=PROOF_B)


class FakeLeaseAPI:
    def __init__(self, *, bad_conflict: bool = False):
        self.bad_conflict = bad_conflict
        self.calls: list[tuple[str, str, dict | None, dict | None, str | None]] = []
        self.acquire_count = 0

    def request(self, method, path, payload=None, query=None, identity_proof=None):
        self.calls.append((method, path, payload, query, identity_proof))
        if path == "/v1/health":
            return {"ok": True, "status": "ok"}
        if path == "/v1/lease/acquire":
            self.acquire_count += 1
            if self.acquire_count == 1:
                return {
                    "ok": False,
                    "error": "permission_denied",
                    "reason": "identity_proof_invalid",
                }
            if self.acquire_count == 2:
                return {
                    "ok": True,
                    "lease": {
                        "lease_id": LEASE_A,
                        "holder_agent_uuid": A,
                    },
                }
            if self.bad_conflict:
                return {"ok": False, "error": "service_unavailable"}
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": SURFACE,
                "blocking_lease_id": LEASE_A,
                "held_by_uuid": A,
            }
        if path == "/v1/lease/handoff/offer":
            return {"ok": True, "handoff_id": HANDOFF}
        if path == "/v1/lease/handoff/accept":
            return {"ok": True}
        if path == "/v1/lease/status":
            return {
                "ok": True,
                "lease": {
                    "lease_id": LEASE_B,
                    "holder_agent_uuid": B,
                },
            }
        if path == "/v1/lease/release":
            return {"ok": True}
        raise AssertionError(f"unexpected request: {method} {path}")


def test_demo_proves_refusal_handoff_status_and_release(capsys) -> None:
    api = FakeLeaseAPI()

    result = coordination_demo.run_demo(
        api,
        participant_a=IDENTITY_A,
        participant_b=IDENTITY_B,
        surface_id=SURFACE,
    )

    assert result.first_lease_id == LEASE_A
    assert result.second_lease_id == LEASE_B
    assert result.handoff_id == HANDOFF
    assert [call[1] for call in api.calls] == [
        "/v1/health",
        "/v1/lease/acquire",
        "/v1/lease/acquire",
        "/v1/lease/acquire",
        "/v1/lease/handoff/offer",
        "/v1/lease/handoff/accept",
        "/v1/lease/status",
        "/v1/lease/release",
    ]
    spoof_acquire = api.calls[1][2]
    first_acquire = api.calls[2][2]
    second_acquire = api.calls[3][2]
    assert spoof_acquire["holder_agent_uuid"] == B
    assert first_acquire["holder_agent_uuid"] == A
    assert second_acquire["holder_agent_uuid"] == B
    assert first_acquire["surface_id"] == second_acquire["surface_id"] == SURFACE
    assert first_acquire["holder_kind"] == "remote_heartbeat"
    assert api.calls[1][4] == PROOF_A
    assert api.calls[2][4] == PROOF_A
    assert api.calls[3][4] == PROOF_B
    assert api.calls[4][4] == PROOF_A
    assert api.calls[5][4] == PROOF_B
    assert api.calls[7][4] == PROOF_B

    coordination_demo.print_receipt(result, "http://127.0.0.1:8788")
    receipt = capsys.readouterr().out
    assert "participant B refused: held_by_other" in receipt
    assert "A's proof cannot impersonate B" in receipt
    assert "governance identity binding: enforced" in receipt
    assert "cross-operator trust" in receipt
    assert "participating clients must acquire before acting" in receipt


def test_demo_releases_participant_a_when_expected_conflict_is_missing() -> None:
    api = FakeLeaseAPI(bad_conflict=True)

    with pytest.raises(coordination_demo.DemoError, match="not refused"):
        coordination_demo.run_demo(
            api,
            participant_a=IDENTITY_A,
            participant_b=IDENTITY_B,
            surface_id=SURFACE,
        )

    method, path, payload, _query, identity_proof = api.calls[-1]
    assert (method, path) == ("POST", "/v1/lease/release")
    assert payload == {"lease_id": LEASE_A, "release_reason": "normal"}
    assert identity_proof == PROOF_A


def test_connection_settings_follow_environment_then_dotenv(tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LEASE_PLANE_HOST_PORT=19788\nLEASE_PLANE_BEARER_TOKEN='dotenv-token'\n"
    )

    assert coordination_demo.lease_base_url({}, dotenv) == "http://127.0.0.1:19788"
    assert coordination_demo.lease_bearer_token({}, dotenv) == "dotenv-token"
    assert (
        coordination_demo.lease_base_url(
            {"UNITARES_COORDINATION_DEMO_URL": "http://lease.example:8788/"},
            dotenv,
        )
        == "http://lease.example:8788"
    )
    assert (
        coordination_demo.lease_bearer_token(
            {"UNITARES_COORDINATION_DEMO_TOKEN": "explicit-token"},
            dotenv,
        )
        == "explicit-token"
    )
    assert coordination_demo.governance_base_url({}, dotenv) == "http://127.0.0.1:8767"
    assert coordination_demo.governance_bearer_token({}, dotenv) is None


def test_connection_settings_have_loopback_quickstart_defaults(tmp_path) -> None:
    missing = tmp_path / "missing.env"

    assert (
        coordination_demo.lease_base_url({}, missing)
        == coordination_demo.DEFAULT_BASE_URL
    )
    assert (
        coordination_demo.lease_bearer_token({}, missing)
        == coordination_demo.DEFAULT_BEARER_TOKEN
    )
    assert (
        coordination_demo.governance_base_url({}, missing)
        == coordination_demo.DEFAULT_GOVERNANCE_URL
    )


def test_deep_identity_extraction_handles_rest_mcp_text_envelope() -> None:
    payload = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": '{"agent_uuid":"%s","raw_governance":{"continuity_token":"%s"}}'
                    % (A, PROOF_A),
                }
            ]
        }
    }

    assert coordination_demo._deep_first(payload, ("agent_uuid", "uuid")) == A
    assert coordination_demo._deep_first(payload, ("continuity_token",)) == PROOF_A


def test_governance_onboarding_errors_never_echo_identity_proof(monkeypatch) -> None:
    class ProofOnlyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return ('{"continuity_token":"%s"}' % PROOF_A).encode()

    monkeypatch.setattr(
        coordination_demo.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ProofOnlyResponse(),
    )

    governance = coordination_demo.GovernanceAPI("http://governance.test", None)
    with pytest.raises(coordination_demo.DemoError) as error:
        governance.start_participant("participant")

    assert PROOF_A not in str(error.value)
    assert "identity_proof=present" in str(error.value)
