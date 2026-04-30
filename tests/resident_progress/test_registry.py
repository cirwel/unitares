from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.resident_progress.registry import (
    RESIDENT_PROGRESS_REGISTRY,
    ResidentConfig,
    resolve_resident_uuid,
)


def test_registry_has_five_residents():
    assert set(RESIDENT_PROGRESS_REGISTRY) == {
        "vigil", "watcher", "steward", "chronicler", "sentinel"
    }


def test_registry_entries_have_required_fields():
    for label, cfg in RESIDENT_PROGRESS_REGISTRY.items():
        assert isinstance(cfg, ResidentConfig)
        assert cfg.source in {
            "kg_writes", "watcher_findings", "eisv_sync_rows",
            "metrics_series", "sentinel_pulse",
        }
        assert cfg.window.total_seconds() > 0
        assert cfg.threshold >= 1
        assert cfg.expected_cadence_s > 0


def test_registry_cadences_match_resident_natural_periods():
    # Each resident has a natural cadence that the heartbeat-liveness
    # check must respect (alive iff last_update within 3x cadence).
    # A single global default mislabels every non-continuous resident.
    cadences = {
        label: cfg.expected_cadence_s
        for label, cfg in RESIDENT_PROGRESS_REGISTRY.items()
    }
    assert cadences["sentinel"] == 60       # continuous loop
    assert cadences["steward"] == 300       # 5-min EISV sync
    assert cadences["vigil"] == 1800        # 30-min launchd cron
    assert cadences["watcher"] == 21600     # event-driven, 6h window
    assert cadences["chronicler"] == 86400  # daily


def test_resolve_resident_uuid_reads_anchor(tmp_path, monkeypatch):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    (anchor_dir / "vigil.json").write_text(json.dumps({
        "agent_uuid": "11111111-2222-3333-4444-555555555555"
    }))
    monkeypatch.setattr(
        "src.resident_progress.registry.ANCHOR_DIR", anchor_dir
    )
    assert resolve_resident_uuid("vigil") == "11111111-2222-3333-4444-555555555555"


def test_resolve_resident_uuid_returns_none_when_anchor_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.resident_progress.registry.ANCHOR_DIR", tmp_path
    )
    assert resolve_resident_uuid("vigil") is None


def test_resolve_resident_uuid_returns_none_on_malformed_anchor(tmp_path, monkeypatch):
    (tmp_path / "vigil.json").write_text("not-json")
    monkeypatch.setattr(
        "src.resident_progress.registry.ANCHOR_DIR", tmp_path
    )
    assert resolve_resident_uuid("vigil") is None
