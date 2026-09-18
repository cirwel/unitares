from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from src.resident_progress.registry import (
    RESIDENT_PROGRESS_MANIFEST_ENV,
    RESIDENT_PROGRESS_REGISTRY,
    ResidentConfig,
    is_event_driven_label,
    load_resident_progress_registry,
    parse_resident_progress_manifest,
    resolve_resident_uuid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_load_registry_empty_by_default(monkeypatch):
    """Unset manifest => no residents probed (user-agnostic default)."""
    monkeypatch.delenv(RESIDENT_PROGRESS_MANIFEST_ENV, raising=False)
    assert load_resident_progress_registry() == {}
    # Empty/whitespace path is also treated as "no manifest".
    assert load_resident_progress_registry("") == {}


def test_load_registry_missing_file_is_empty(tmp_path):
    """A pointed-but-absent manifest degrades to empty, not a crash."""
    assert load_resident_progress_registry(tmp_path / "nope.json") == {}


def test_load_registry_from_manifest(tmp_path):
    manifest = tmp_path / "residents.json"
    manifest.write_text(json.dumps({
        "_comment": "metadata keys are ignored",
        "vigil": {
            "source": "kg_writes", "metric": "rows_written",
            "window_seconds": 3600, "threshold": 1,
            "expected_cadence_s": 1800,
        },
        "watcher": {
            "source": "watcher_findings", "metric": "rows_any",
            "window_seconds": 21600, "threshold": 1,
            "expected_cadence_s": None,
        },
    }))
    reg = load_resident_progress_registry(manifest)
    assert set(reg) == {"vigil", "watcher"}
    assert reg["vigil"].window == timedelta(seconds=3600)
    assert reg["vigil"].expected_cadence_s == 1800
    assert reg["watcher"].expected_cadence_s is None


def test_parse_manifest_skips_metadata_and_nondict():
    reg = parse_resident_progress_manifest({
        "_comment": "ignored",
        "_version": 1,
        "vigil": {
            "source": "kg_writes", "metric": "rows",
            "window_seconds": 60, "threshold": 1, "expected_cadence_s": 30,
        },
    })
    assert set(reg) == {"vigil"}


_GOOD_ENTRY = {
    "source": "kg_writes", "metric": "rows_written",
    "window_seconds": 3600, "threshold": 1, "expected_cadence_s": 1800,
}

# Each of these raised out of the module import before the per-entry guard,
# from a different exception type. The parametrization is the point: a guard
# that catches only the first one still lets a server start and then stop
# probing every resident.
_MALFORMED_ENTRIES = {
    "missing_source": ({k: v for k, v in _GOOD_ENTRY.items() if k != "source"},
                       "KeyError"),
    "missing_threshold": ({k: v for k, v in _GOOD_ENTRY.items()
                           if k != "threshold"}, "KeyError"),
    "zero_cadence": ({**_GOOD_ENTRY, "expected_cadence_s": 0}, "ValueError"),
    "negative_cadence": ({**_GOOD_ENTRY, "expected_cadence_s": -1},
                         "ValueError"),
    "nonnumeric_window": ({**_GOOD_ENTRY, "window_seconds": "soon"},
                          "ValueError"),
    "null_threshold": ({**_GOOD_ENTRY, "threshold": None}, "TypeError"),
    "nonstring_source": ({**_GOOD_ENTRY, "source": {"name": "kg_writes"}},
                         "TypeError"),
}


@pytest.mark.parametrize(
    "kind,entry,error_name",
    [(k, e, n) for k, (e, n) in _MALFORMED_ENTRIES.items()],
    ids=list(_MALFORMED_ENTRIES),
)
def test_malformed_entry_is_skipped_with_a_warning(kind, entry, error_name, caplog):
    """One bad entry loses that resident, not the roster.

    The parser is the boundary for untrusted deployment config, so it degrades
    the same way the loader already does for an unreadable file.
    """
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        reg = parse_resident_progress_manifest({
            "resident-a": _GOOD_ENTRY,
            "resident-bad": entry,
            "resident-c": {**_GOOD_ENTRY, "expected_cadence_s": None},
        })

    assert set(reg) == {"resident-a", "resident-c"}
    assert reg["resident-a"].window == timedelta(seconds=3600)
    assert reg["resident-c"].expected_cadence_s is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    message = warnings[0].getMessage()
    # The label is what an operator greps for; the error type is what tells
    # them which key to look at.
    assert "resident-bad" in message
    assert error_name in message
    assert "resident-a" not in message


def test_malformed_entries_are_each_warned_about(caplog):
    """Two bad entries produce two warnings, not one summary line."""
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        reg = parse_resident_progress_manifest({
            "resident-a": _GOOD_ENTRY,
            "resident-bad-1": {**_GOOD_ENTRY, "expected_cadence_s": 0},
            "resident-bad-2": {k: v for k, v in _GOOD_ENTRY.items()
                               if k != "source"},
        })

    assert set(reg) == {"resident-a"}
    labels_warned = {
        label
        for label in ("resident-bad-1", "resident-bad-2")
        for r in caplog.records
        if label in r.getMessage()
    }
    assert labels_warned == {"resident-bad-1", "resident-bad-2"}


def test_every_entry_malformed_yields_empty_registry(caplog):
    """The degenerate case still returns a registry, never raises — and says
    which empty it is.

    An empty registry probes nobody, and "no manifest configured" is the
    user-agnostic default, so a wholly-rejected manifest must not read in the
    log like a deployment that never wanted the probe.
    """
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        reg = parse_resident_progress_manifest({
            "resident-bad-1": {**_GOOD_ENTRY, "expected_cadence_s": 0},
            "resident-bad-2": {**_GOOD_ENTRY, "threshold": None},
        })
    assert reg == {}
    assert "named 2 resident(s) and every one was skipped" in caplog.text


def test_metadata_only_manifest_does_not_claim_entries_were_skipped(caplog):
    """A manifest of pure metadata skipped nothing, so it must not say it did."""
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        assert parse_resident_progress_manifest({"_comment": "nothing here"}) == {}
    assert "was skipped" not in caplog.text


def test_load_registry_skips_malformed_entry_from_a_file(tmp_path, caplog):
    """The skip survives the file path, not only a hand-built dict."""
    manifest = tmp_path / "residents.json"
    manifest.write_text(json.dumps({
        "resident-a": _GOOD_ENTRY,
        "resident-bad": {**_GOOD_ENTRY, "window_seconds": "soon"},
    }))
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        reg = load_resident_progress_registry(manifest)
    assert set(reg) == {"resident-a"}
    assert "resident-bad" in caplog.text


@pytest.mark.parametrize("payload", ["[]", '"a string"', "3"],
                         ids=["list", "string", "number"])
def test_load_registry_rejects_non_object_manifest(tmp_path, payload, caplog):
    """A bare array is valid JSON but not a manifest.

    It never reaches the JSONDecodeError arm, so without an explicit shape
    check it raised AttributeError out of the import — the same escape a
    malformed entry used to take.
    """
    manifest = tmp_path / "residents.json"
    manifest.write_text(payload)
    with caplog.at_level(logging.WARNING, logger="src.resident_progress.registry"):
        assert load_resident_progress_registry(manifest) == {}
    assert "not an object" in caplog.text


def test_malformed_entry_manifest_imports_in_a_fresh_interpreter(tmp_path):
    """The server imports this module lazily inside an already running server,
    so an import-time raise passed server start and then stopped progress
    probing for every resident, leaving only a background-task crash line.
    Import it the way a server start does: a new interpreter whose registry
    comes from the manifest env.
    """
    manifest = tmp_path / "resident_progress.json"
    manifest.write_text(json.dumps({
        "resident-a": _GOOD_ENTRY,
        "resident-bad": {k: v for k, v in _GOOD_ENTRY.items() if k != "source"},
        "resident-c": {**_GOOD_ENTRY, "expected_cadence_s": None},
    }))

    proc = subprocess.run(
        [sys.executable, "-c",
         "from src.resident_progress import probe_task; "
         "print(sorted(probe_task.RESIDENT_PROGRESS_REGISTRY))"],
        cwd=REPO_ROOT,
        env={**os.environ, RESIDENT_PROGRESS_MANIFEST_ENV: str(manifest)},
        capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    # The good entries loaded, so the import neither raised nor degraded the
    # whole roster to empty.
    assert proc.stdout.strip() == "['resident-a', 'resident-c']"


def test_event_driven_exemption_survives_a_malformed_sibling(tmp_path):
    """A malformed entry must not silently page a healthy event-driven resident.

    ``background_tasks._get_expected_interval`` consults
    ``is_event_driven_label`` first, inside ``try/except Exception: pass``. When
    the registry import raised, that swallowed the crash and fell through to the
    ``autonomous`` default of 300s — so an event-driven resident went from
    exempt (None) to a 300s expected interval, and the silence detector fires
    ``lifecycle_silent_critical`` at 5x that. One unrelated typo turned a
    quiet-but-healthy resident into a recurring false critical page.
    """
    manifest = tmp_path / "resident_progress.json"
    manifest.write_text(json.dumps({
        "resident-evt": {**_GOOD_ENTRY, "expected_cadence_s": None},
        "resident-bad": {k: v for k, v in _GOOD_ENTRY.items() if k != "source"},
    }))

    proc = subprocess.run(
        [sys.executable, "-c",
         "import types;"
         "from src.background_tasks import _get_expected_interval;"
         "print(_get_expected_interval(types.SimpleNamespace("
         "label='resident-evt', tags=['persistent', 'autonomous'])))"],
        cwd=REPO_ROOT,
        env={**os.environ, RESIDENT_PROGRESS_MANIFEST_ENV: str(manifest)},
        capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "None", (
        "event-driven resident lost its silence exemption to a malformed "
        f"sibling entry (got {proc.stdout.strip()!r})"
    )


def test_registry_has_four_residents():
    # Sentinel is back on a substrate-agnostic source (agent_checkins) after
    # its BEAM migration — it checks in via process_agent_update like the rest.
    # (PR #566 retired it as a stopgap; this re-keys it correctly.) Steward
    # left the reference fleet on 2026-09-13 with unitares-pi-plugin.
    assert set(RESIDENT_PROGRESS_REGISTRY) == {
        "vigil", "watcher", "chronicler", "sentinel"
    }


def test_registry_entries_have_required_fields():
    for label, cfg in RESIDENT_PROGRESS_REGISTRY.items():
        assert isinstance(cfg, ResidentConfig)
        assert cfg.source in {
            "kg_writes", "watcher_findings", "eisv_sync_rows",
            "metrics_series", "sentinel_pulse", "agent_checkins",
        }
        assert cfg.window.total_seconds() > 0
        assert cfg.threshold >= 1
        # Cadence may be None for event-driven residents; otherwise must
        # be positive. Validated at construction by ResidentConfig.
        if cfg.expected_cadence_s is not None:
            assert cfg.expected_cadence_s > 0


def test_registry_cadences_match_resident_natural_periods():
    # Each resident has a natural cadence that the heartbeat-liveness
    # check must respect (alive iff last_update within 3x cadence).
    # A single global default mislabels every non-continuous resident.
    # None means "event-driven, no heartbeat semantics" — Watcher fires
    # on edits, not on a clock.
    cadences = {
        label: cfg.expected_cadence_s
        for label, cfg in RESIDENT_PROGRESS_REGISTRY.items()
    }
    assert cadences["sentinel"] == 300      # BEAM fleet-cycle (~5min)
    assert cadences["vigil"] == 1800        # 30-min launchd cron
    assert cadences["watcher"] is None      # event-driven
    assert cadences["chronicler"] == 86400  # daily


def test_resident_config_rejects_zero_or_negative_cadence():
    # __post_init__ guards against typos like expected_cadence_s=0
    # in a future registry edit. A 0 cadence would silently fall
    # through the heartbeat evaluator's falsy check before this guard.
    with pytest.raises(ValueError, match="must be positive"):
        ResidentConfig(
            source="kg_writes", metric="rows", window=timedelta(seconds=60),
            threshold=1, expected_cadence_s=0,
        )
    with pytest.raises(ValueError, match="must be positive"):
        ResidentConfig(
            source="kg_writes", metric="rows", window=timedelta(seconds=60),
            threshold=1, expected_cadence_s=-1,
        )


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


def test_is_event_driven_label_only_true_for_watcher():
    # Watcher has expected_cadence_s=None — that's the canonical event-driven
    # marker. Every other resident has a positive cadence and is heartbeat-driven.
    # If this test fails after a registry edit, audit dashboard surfaces
    # (residents.js, agents.js) — they consume this flag to suppress
    # "Inactive" badges and pick the right pill style.
    assert is_event_driven_label("watcher") is True
    for label in ("vigil", "steward", "chronicler", "sentinel"):
        assert is_event_driven_label(label) is False, label


def test_is_event_driven_label_handles_unknown_and_empty():
    assert is_event_driven_label(None) is False
    assert is_event_driven_label("") is False
    assert is_event_driven_label("not-a-resident") is False


def test_is_event_driven_label_is_case_insensitive():
    # Dashboard surfaces normalize labels case-insensitively elsewhere
    # (_DEFAULT_RESIDENT_SILENCE_SECONDS.get(label.lower(), ...)). Stay consistent.
    assert is_event_driven_label("Watcher") is True
    assert is_event_driven_label("WATCHER") is True
