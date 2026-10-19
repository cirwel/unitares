"""Shipped path defaults honour their deployment env vars.

resident_progress.registry reads resident anchors from the directory the
identity substrate uses (UNITARES_ANCHORS_DIR, else ~/.unitares/anchors), and
resolves to the same path as before when that override is unset.
"""

from __future__ import annotations

import json

from src.identity.substrate import _default_anchors_dir
from src.resident_progress import registry
from src.resident_progress.registry import resolve_resident_uuid

ANCHOR_UUID = "11111111-2222-3333-4444-555555555555"


def _write_anchor(directory, label="vigil", uuid=ANCHOR_UUID):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{label}.json").write_text(json.dumps({"agent_uuid": uuid}))


def test_resident_anchor_dir_honours_unitares_anchors_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ANCHOR_DIR", None)
    anchors = tmp_path / "relocated-anchors"
    _write_anchor(anchors)
    monkeypatch.setenv("UNITARES_ANCHORS_DIR", str(anchors))
    assert resolve_resident_uuid("vigil") == ANCHOR_UUID


def test_resident_anchor_dir_default_unchanged_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ANCHOR_DIR", None)
    monkeypatch.delenv("UNITARES_ANCHORS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _default_anchors_dir() == tmp_path / ".unitares" / "anchors"
    assert resolve_resident_uuid("vigil") is None
    _write_anchor(tmp_path / ".unitares" / "anchors")
    assert resolve_resident_uuid("vigil") == ANCHOR_UUID


def test_resident_anchor_dir_explicit_override_beats_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    _write_anchor(explicit, uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    env_dir = tmp_path / "env"
    _write_anchor(env_dir)
    monkeypatch.setenv("UNITARES_ANCHORS_DIR", str(env_dir))
    monkeypatch.setattr(registry, "ANCHOR_DIR", explicit)
    assert resolve_resident_uuid("vigil") == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
