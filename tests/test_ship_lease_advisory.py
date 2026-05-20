"""Tests for scripts/dev/_ship_lease_advisory.py — the ship.sh lease-plane helper."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

# Wire the helper into sys.path so we can import it without installing it.
HELPER_DIR = Path(__file__).resolve().parents[1] / "scripts" / "dev"
sys.path.insert(0, str(HELPER_DIR))

import _ship_lease_advisory as shla  # noqa: E402


def _ok_lease_payload(holder_uuid: UUID) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "lease_id": str(uuid4()),
        "surface_id": "resident:/ship_sh_test-branch",
        "surface_kind": "ship_sh",
        "holder_agent_uuid": str(holder_uuid),
        "holder_class": "process_instance",
        "holder_kind": "remote_heartbeat",
        "holder_pid": None,
        "heartbeat_required": True,
        "intent": "test ship",
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=300)).isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "released_at": None,
        "release_reason": None,
        "audit_session": None,
        "original_ttl_s": 300,
        "earned_status": "provisional",
    }


def _scripted_client(responses: list[dict[str, Any]]):
    from src.lease_plane import LeasePlaneClient

    def transport(_req):
        return responses.pop(0)

    return LeasePlaneClient(transport=transport)


def test_acquire_emits_lease_id_on_acquired_new(capsys, monkeypatch):
    holder = uuid4()
    client = _scripted_client(
        [{"ok": True, "lease": _ok_lease_payload(holder), "idempotent": False, "drift_warning": []}]
    )

    monkeypatch.setattr(shla, "make_advisory_client", lambda: client)

    rc = shla.main(
        [
            "acquire",
            "--surface-id=resident:/ship_sh_test-branch",
            "--surface-kind=ship_sh",
            "--intent=test ship",
            "--ttl-s=300",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "acquired_new"
    assert UUID(payload["lease_id"])  # well-formed
    assert payload["enforced"] is False
    assert payload["blocked"] is False


def test_acquire_emits_null_lease_on_held_by_other(capsys, monkeypatch):
    other = uuid4()
    client = _scripted_client(
        [
            {
                "ok": False,
                "error": "held_by_other",
                "surface_id": "resident:/ship_sh_contended",
                "blocking_lease_id": str(uuid4()),
                "held_by_uuid": str(other),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                "retry_after_hint_ms": 5000,
            }
        ]
    )

    monkeypatch.setattr(shla, "make_advisory_client", lambda: client)

    rc = shla.main(
        [
            "acquire",
            "--surface-id=resident:/ship_sh_contended",
            "--surface-kind=ship_sh",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "held_by_other"
    assert payload["lease_id"] is None
    assert payload["blocked"] is False


def test_acquire_marks_blocked_when_surface_kind_enforced(capsys, monkeypatch):
    other = uuid4()
    client = _scripted_client(
        [
            {
                "ok": False,
                "error": "held_by_other",
                "surface_id": "resident:/ship_sh_contended",
                "blocking_lease_id": str(uuid4()),
                "held_by_uuid": str(other),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                "retry_after_hint_ms": 5000,
            }
        ]
    )

    monkeypatch.setattr(shla, "make_advisory_client", lambda: client)
    monkeypatch.setenv("LEASE_PLANE_ENFORCED_SURFACE_KINDS", "resident")

    rc = shla.main(
        [
            "acquire",
            "--surface-id=resident:/ship_sh_contended",
            "--surface-kind=ship_sh",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "held_by_other"
    assert payload["lease_id"] is None
    assert payload["enforced"] is True
    assert payload["blocked"] is True


def test_acquire_emits_service_unavailable_on_disabled(capsys, monkeypatch):
    from src.lease_plane import LeasePlaneDisabledClient

    monkeypatch.setattr(shla, "make_advisory_client", lambda: LeasePlaneDisabledClient())

    rc = shla.main(
        [
            "acquire",
            "--surface-id=resident:/ship_sh_disabled",
            "--surface-kind=ship_sh",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "service_unavailable"
    assert payload["lease_id"] is None


def test_release_happy_path(capsys, monkeypatch):
    client = _scripted_client([{"ok": True}])
    monkeypatch.setattr(shla, "make_advisory_client", lambda: client)

    rc = shla.main(["release", f"--lease-id={uuid4()}"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_release_invalid_lease_id_does_not_raise(capsys):
    """ship.sh calls release in a trap; if the captured lease_id was never
    set (acquire returned null), we must not crash on a bad arg."""
    rc = shla.main(["release", "--lease-id=not-a-uuid"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "invalid" in out["reason"]


def test_release_empty_lease_id_does_not_raise(capsys):
    rc = shla.main(["release", "--lease-id="])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "empty" in out["reason"]


def test_release_failed_release_returns_ok_false(capsys, monkeypatch):
    client = _scripted_client([{"ok": False, "error": "not_found"}])
    monkeypatch.setattr(shla, "make_advisory_client", lambda: client)

    rc = shla.main(["release", f"--lease-id={uuid4()}"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
