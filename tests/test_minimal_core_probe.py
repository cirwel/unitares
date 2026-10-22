"""The minimal-core matrix harness: response parsing and the summary table."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/minimal_core_probe.py"
spec = importlib.util.spec_from_file_location("minimal_core_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_first_finds_nested_keys():
    assert probe.first({"a": {"b": [{"uuid": "u1"}]}}, "uuid") == "u1"
    assert probe.first({"uuid": None, "x": {"uuid": "u2"}}, "uuid") == "u2"
    assert probe.first({"x": 1}, "uuid") is None


def test_failed_reads_tool_errors_and_transport_failures():
    assert probe.failed({"success": True}) is None
    assert probe.failed({"success": False, "error_code": "RATE_LIMITED"}) == "RATE_LIMITED"
    assert probe.failed({"_http": 500, "_raw": "boom"}) == "boom"


def test_summarize_renders_one_row_per_config(tmp_path):
    (tmp_path / "matrix-full").mkdir()
    (tmp_path / "matrix-full" / "full.json").write_text(json.dumps({
        "config": "full", "compose_up_rc": 0,
        "ready": {"ok": True}, "identity": {"ok": True},
        "deep_health": {"ok": True, "status": "healthy", "degraded": []},
        "database": {"ok": True, "migrations": "76 applied, max 78", "extensions": "age=1.7.0"},
    }))
    (tmp_path / "matrix-no-redis").mkdir()
    (tmp_path / "matrix-no-redis" / "no-redis.json").write_text(json.dumps({
        "config": "no-redis", "compose_up_rc": 0,
        "ready": {"ok": True}, "identity": {"ok": False, "error": "x"},
    }))
    table = probe.summarize(tmp_path)
    assert "| full | 0 | ✅ | ✅ |" in table
    assert "| no-redis | 0 | ✅ | ❌ |" in table
    assert "no-redis** identity: x" in table
