"""Tests for the doctor-layer identity provisioner.

Every case here is a defect the script shipped with and that made it
unrunnable — it had never successfully provisioned anything, and the anchor it
exists to write (`~/.unitares/anchors/doctor.json`) did not exist, so all 203
doctor findings in the preceding 30 days carried the bare slug
``doctor-findings`` and were rejected 422 by ``http_sentinel_adjudicate``.

1. It read the mint response one level too shallow. ``/v1/tools/call`` is the
   REST shape and nests the tool payload under ``result``; the script read the
   envelope directly, found no uuid on a mint that had *succeeded*, and exited
   1 — leaving a live untagged identity behind every time it was run.
2. It stamped tags via ``action="update_metadata"``, which is not in the
   ``agent`` tool's enum (list/get/update/archive/resume/delete).
3. It tried to have the new identity self-assign ``persistent`` and
   ``autonomous``. Both are in ``PRIVILEGED_TAGS``; the server refuses
   self-assignment by design. Those tags are granted only by the onboard
   classifier, and only when the name is in ``UNITARES_RESIDENTS``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "provision_doctor_identity.py"
)
_spec = importlib.util.spec_from_file_location("provision_doctor_identity", MODULE_PATH)
pdi = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pdi)


UUID = "dc94fa70-6186-4862-aeb6-3fc9801263c8"


def _rest_mint(uuid: str = UUID) -> dict:
    """A real `/v1/tools/call` start_session envelope, trimmed."""
    return {
        "name": "start_session",
        "success": True,
        "result": {
            "success": True,
            "tool": "start_session",
            "client_session_id": "agent-dc94fa70-618",
            "agent_uuid": uuid,
            "raw_governance": {"uuid": uuid, "display_name": "Doctor", "is_new": True},
        },
    }


def _get_with_tags(tags: list[str]) -> dict:
    return {"name": "agent", "success": True, "result": {"agent": {"tags": tags}}}


@pytest.fixture()
def anchor(tmp_path, monkeypatch):
    path = tmp_path / "anchors" / "doctor.json"
    monkeypatch.setattr(pdi, "ANCHOR", path)
    return path


def _wire(monkeypatch, responses):
    """Route _call by tool name; record the calls for assertions."""
    seen = []

    def fake_call(name, arguments, token):
        seen.append((name, arguments))
        return responses[name]

    monkeypatch.setattr(pdi, "_call", fake_call)
    return seen


def test_writes_anchor_from_rest_envelope(anchor, monkeypatch):
    """Defect 1: the uuid lives under `result`, not at the top level."""
    _wire(monkeypatch, {
        "start_session": _rest_mint(),
        "agent": _get_with_tags(["persistent", "autonomous"]),
    })
    assert pdi.main(["--apply"]) == 0
    assert json.loads(anchor.read_text())["agent_uuid"] == UUID


def test_never_self_assigns_privileged_tags(anchor, monkeypatch):
    """Defects 2 and 3: no write to `agent` at all — only a read-back.

    The server rejects self-assignment of `persistent`/`autonomous`, so a
    provisioner that tries is broken by construction. Grant is the onboard
    classifier's job; this script's only business with the `agent` tool is
    confirming the grant happened.
    """
    seen = _wire(monkeypatch, {
        "start_session": _rest_mint(),
        "agent": _get_with_tags(["persistent", "autonomous"]),
    })
    pdi.main(["--apply"])
    agent_calls = [args for name, args in seen if name == "agent"]
    assert agent_calls, "must read tags back rather than assume them"
    for args in agent_calls:
        assert args["action"] == "get", f"mutating call to agent: {args}"
        assert "tags" not in args


def test_refuses_anchor_when_roster_did_not_grant_tags(anchor, monkeypatch, capsys):
    """An untagged identity is archived by the orphan sweep.

    Anchoring to one would point every doctor producer at a ghost, which is
    strictly worse than today's slug: a slug is at least honestly
    unattributable. Refuse, and name the roster as the missing piece.
    """
    _wire(monkeypatch, {
        "start_session": _rest_mint(),
        "agent": _get_with_tags([]),
    })
    assert pdi.main(["--apply"]) == 1
    assert not anchor.exists()
    assert "UNITARES_RESIDENTS" in capsys.readouterr().err


def test_partial_grant_is_still_a_refusal(anchor, monkeypatch):
    """`persistent` alone leaves loop-detection pattern 4 free to starve it."""
    _wire(monkeypatch, {
        "start_session": _rest_mint(),
        "agent": _get_with_tags(["persistent"]),
    })
    assert pdi.main(["--apply"]) == 1
    assert not anchor.exists()


def test_dry_run_mints_nothing(anchor, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run must not call governance")

    monkeypatch.setattr(pdi, "_call", boom)
    assert pdi.main(["--dry-run"]) == 0
    assert not anchor.exists()


def test_idempotent_when_anchor_already_present(anchor, monkeypatch):
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({"agent_uuid": UUID, "display_name": "Doctor"}))

    def boom(*a, **k):
        raise AssertionError("must not mint a second identity")

    monkeypatch.setattr(pdi, "_call", boom)
    assert pdi.main(["--apply"]) == 0
