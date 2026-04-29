"""Tests for atomic persistence of pattern_floor.json.

Per the council review: pattern_floor.json must use tmp+rename atomic
writes (the same pattern as _write_findings_atomic in findings.py:234).
A direct write_text would let a concurrent reader (the surface hook
fires on every UserPromptSubmit) see a truncated JSON file mid-write."""

import json
from datetime import datetime, timezone

import pytest

from agents.watcher.calibration import BucketStats
from agents.watcher.floor_state import (
    FLOOR_FILE_NAME,
    FloorState,
    load_floor,
    save_floor,
)


def _bucket(pattern, fc, *, wc=10.0, wd=2.0, ci=0.6, latest="2026-04-20T00:00:00Z"):
    return BucketStats(
        pattern=pattern,
        file_class=fc,
        weighted_confirmed=wc,
        weighted_dismissed=wd,
        weighted_n=wc + wd,
        ci_lower=ci,
        latest_observation=latest,
    )


class TestFloorRoundTrip:
    def test_save_then_load_round_trip(self, tmp_path):
        state = FloorState(
            updated_at="2026-04-27T00:00:00Z",
            buckets={
                ("P1", "app"): _bucket("P1", "app", ci=0.85),
                ("P1", "test"): _bucket("P1", "test", ci=0.10, wc=1.0, wd=9.0),
            },
        )
        save_floor(state, state_dir=tmp_path)
        loaded = load_floor(state_dir=tmp_path)
        assert loaded.updated_at == "2026-04-27T00:00:00Z"
        assert ("P1", "app") in loaded.buckets
        assert loaded.buckets[("P1", "app")].ci_lower == pytest.approx(0.85)
        assert loaded.buckets[("P1", "test")].ci_lower == pytest.approx(0.10)

    def test_load_missing_file_returns_empty_state(self, tmp_path):
        loaded = load_floor(state_dir=tmp_path)
        assert loaded.buckets == {}
        assert loaded.updated_at is not None

    def test_load_corrupt_file_returns_empty_state(self, tmp_path):
        (tmp_path / FLOOR_FILE_NAME).write_text("{not valid json")
        loaded = load_floor(state_dir=tmp_path)
        assert loaded.buckets == {}

    def test_save_uses_tmp_then_rename(self, tmp_path, monkeypatch):
        """Verify the writer never leaves a half-written floor file
        observable to a concurrent reader."""
        good = FloorState(
            updated_at="2026-04-20T00:00:00Z",
            buckets={("OLD", "app"): _bucket("OLD", "app")},
        )
        save_floor(good, state_dir=tmp_path)
        target = tmp_path / FLOOR_FILE_NAME
        good_payload = target.read_text()

        bad = FloorState(updated_at="2026-04-27T00:00:00Z", buckets={})

        original_dump = json.dump

        def crashing_dump(obj, fp, *args, **kwargs):
            fp.write('{"partial')
            raise IOError("simulated mid-write crash")

        monkeypatch.setattr(json, "dump", crashing_dump)
        with pytest.raises(IOError):
            save_floor(bad, state_dir=tmp_path)

        assert target.read_text() == good_payload, (
            "save_floor must write to tmp + rename so a crash mid-write "
            "doesn't corrupt the live file"
        )
        monkeypatch.setattr(json, "dump", original_dump)

    def test_save_creates_state_dir(self, tmp_path):
        nested = tmp_path / "deep" / "watcher_data"
        save_floor(FloorState(updated_at="t", buckets={}), state_dir=nested)
        assert (nested / FLOOR_FILE_NAME).exists()

    def test_concurrent_writers_use_distinct_tmp_paths(self, tmp_path, monkeypatch):
        """Two callers of save_floor (Vigil cycle vs --recompute-floor CLI)
        must never write to the same tmp filename. Otherwise one writer
        truncates the other's tmp file mid-stream and the rename promotes
        a corrupted file. Council-flagged race condition (conf 92)."""
        observed_tmp_paths = []

        from agents.watcher import floor_state as fs_mod

        original_open = fs_mod.Path.open

        def tracking_open(self, *args, **kwargs):
            if self.name.endswith(".tmp") or ".tmp." in self.name:
                observed_tmp_paths.append(self.name)
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(fs_mod.Path, "open", tracking_open)

        # Two saves in quick succession should produce two DIFFERENT tmp paths
        save_floor(FloorState(updated_at="a", buckets={}), state_dir=tmp_path)
        save_floor(FloorState(updated_at="b", buckets={}), state_dir=tmp_path)

        assert len(observed_tmp_paths) == 2
        assert observed_tmp_paths[0] != observed_tmp_paths[1], (
            f"both saves used the same tmp path {observed_tmp_paths[0]!r} — "
            "concurrent writers would collide"
        )
        # The canonical target file still ends up in place
        assert (tmp_path / FLOOR_FILE_NAME).exists()


class TestFloorBucketLookup:
    def test_get_returns_bucket(self):
        state = FloorState(
            updated_at="t",
            buckets={("P1", "app"): _bucket("P1", "app", ci=0.7)},
        )
        bucket = state.get("P1", "app")
        assert bucket is not None
        assert bucket.ci_lower == pytest.approx(0.7)

    def test_get_returns_none_for_unknown(self):
        state = FloorState(updated_at="t", buckets={})
        assert state.get("P1", "app") is None


from agents.watcher.floor_state import recompute_floor


class TestRecomputeFloor:
    def test_recompute_aggregates_findings_into_state(self, tmp_path):
        findings_file = tmp_path / "findings.jsonl"
        rows = []
        for i in range(15):
            rows.append({
                "pattern": "P1",
                "file": "/repo/src/x.py",
                "line": 1,
                "hint": "h",
                "severity": "medium",
                "status": "confirmed",
                "detected_at": "2026-04-20T00:00:00Z",
                "confirmed_at": "2026-04-21T00:00:00Z",
                "fingerprint": f"abcd{i:04d}",
                "violation_class": "BEH",
            })
        for i in range(12):
            rows.append({
                "pattern": "P2",
                "file": "/repo/tests/test_x.py",
                "line": 1,
                "hint": "h",
                "severity": "medium",
                "status": "dismissed",
                "detected_at": "2026-04-20T00:00:00Z",
                "dismissed_at": "2026-04-21T00:00:00Z",
                "resolution_reason": "fp",
                "fingerprint": f"efgh{i:04d}",
                "violation_class": "BEH",
            })

        with findings_file.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        state = recompute_floor(
            findings_file=findings_file,
            state_dir=tmp_path,
            now=datetime(2026, 4, 22, tzinfo=timezone.utc),
        )
        b1 = state.get("P1", "app")
        assert b1 is not None
        assert b1.ci_lower is not None
        assert b1.ci_lower > 0.7
        b2 = state.get("P2", "test")
        assert b2 is not None
        assert b2.ci_lower is not None
        assert b2.ci_lower < 0.3

        reloaded = load_floor(state_dir=tmp_path)
        assert ("P1", "app") in reloaded.buckets
        assert ("P2", "test") in reloaded.buckets
