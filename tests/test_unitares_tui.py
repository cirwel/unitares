"""Pure-function tests for scripts/unitares_tui.py (the curses shell is smoke-tested by hand)."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "unitares_tui.py"
_spec = importlib.util.spec_from_file_location("unitares_tui", _PATH)
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

DATA = {
    "health": {"status": "ok", "version": "9.9.9", "build_sha": "abc123",
               "uptime": {"formatted": "1h"}, "database": {"status": "connected"}},
    "tiers": {"success": True, "total": 10, "earned": 3, "tiers": {"verified": 1, "unknown": 9}},
    "residents": {"residents": [
        {"label": "alpha", "status": "healthy", "verdict": "proceed", "silence_seconds": 12,
         "agent_id": "u-1", "eisv": {"E": 0.5, "I": 0.6, "S": 0.2, "V": -0.1},
         "coherence": 0.49, "risk_score": 0.01,
         "history": [{"verdict": "proceed"}, {"verdict": "guide"}],
         "recent_writes": [{"severity": "high", "summary": "silent"}]},
        {"label": "beta", "status": "silent", "verdict": "approve", "silence_seconds": 2_000_000},
    ]},
}


def test_fmt_age_units():
    assert tui.fmt_age(None) == "-"
    assert tui.fmt_age(30) == "30s"
    assert tui.fmt_age(600) == "10m"
    assert tui.fmt_age(7200) == "2.0h"
    assert tui.fmt_age(2_000_000) == "23d"


def test_bar_clamps():
    assert tui.bar(-1, 4) == "[....] 0.00"
    assert tui.bar(2, 4) == "[####] 1.00"
    assert tui.bar(0.5, 4) == "[##..] 0.50"


def test_overview_header_and_tiers():
    lines = [t for t, _ in tui.overview_lines(DATA)]
    assert "v9.9.9 (abc123)" in lines[0]
    assert any("verified 1 | unknown 9" in line for line in lines)


def test_overview_tolerates_missing_tiers():
    data = {"health": {"status": "ok"}, "residents": {}}
    lines = [t for t, _ in tui.overview_lines(data)]
    assert any("unavailable" in line for line in lines)


def test_resident_rows_and_detail():
    assert tui.resident_rows(DATA)[1] == ("beta", "silent", "approve", "23d")
    detail = [t for t, _ in tui.resident_detail_lines(DATA["residents"]["residents"][0])]
    assert any("V valence   -0.100" in line for line in detail)
    assert any(line.endswith("check-ins: P G") for line in detail)
    assert any("Recent findings" in line for line in detail)


def test_detail_without_optional_fields():
    detail = tui.resident_detail_lines(DATA["residents"]["residents"][1])
    assert detail[0][0].startswith("beta")


def test_no_resident_name_branching():
    # Fleet neutrality: the module must not hardcode any roster name.
    src = _PATH.read_text()
    for name in ("Lumen", "Sentinel", "Vigil", "Watcher", "Chronicler"):
        assert name not in src


def test_snapshot_schema_is_stable_and_minimal():
    snap = tui.snapshot(DATA)
    assert snap["schema"] == "unitares.tui.snapshot.v1"
    assert snap["health"]["version"] == "9.9.9"
    assert snap["tiers"]["counts"] == {"verified": 1, "unknown": 9}
    assert [r["label"] for r in snap["residents"]] == ["alpha", "beta"]
    assert "history" not in snap["residents"][0]
    assert "recent_writes" not in snap["residents"][0]


def test_snapshot_tolerates_empty_status():
    snap = tui.snapshot({})
    assert snap["residents"] == [] and snap["health"]["status"] is None


def test_null_panels_do_not_crash():
    # Warmup, auth-gated and fresh-install responses carry null panels.
    for data in (
        {"health": None, "tiers": None, "residents": None},
        {"health": {"status": "ok", "uptime": None, "database": None}, "tiers": {"tiers": None}, "residents": {"residents": None}},
        {},
    ):
        assert tui.overview_lines(data)
        assert tui.resident_rows(data) == []
        assert tui.snapshot(data)["residents"] == []


def test_resident_with_null_fields_renders():
    resident = {
        "label": "gamma", "status": None, "verdict": None, "silence_seconds": None,
        "eisv": {"E": None, "I": 0.4, "S": None, "V": None},
        "coherence": None, "risk_score": None,
        "history": [{"verdict": None}, None, {"verdict": "guide"}],
        "recent_writes": [{"severity": None, "summary": None}],
    }
    data = {"residents": {"residents": [resident]}}
    assert tui.resident_rows(data) == [("gamma", "?", "-", "-")]
    detail = [t for t, _ in tui.resident_detail_lines(resident)]
    assert any("I integrity" in line for line in detail)
    assert not any("E energy" in line for line in detail)
    assert any(line.endswith("check-ins: ? ? G") for line in detail)
    assert tui.snapshot(data)["residents"][0]["verdict"] is None
