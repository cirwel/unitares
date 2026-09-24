"""Frozen-extract checks for the resource-grounded E shadow study (#2410).

No DB: the database fetch is replaced with a stub so the refusal paths, row
validation, window arithmetic and manifest hashing are verified on known input.
"""

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.analysis import resource_shadow_extract as rse

UUID_A = "86ae619f-87e0-4040-8f29-eacece0c7904"
UUID_B = "e0903a57-a1ec-45f5-a260-9c94fbe4e640"


def _row(ts, event="stop", uuid=UUID_A, **extra):
    return {"schema": "resource_shadow.v1", "ts": ts, "host": "claude", "event": event,
            "session_id": "s1", "agent_uuid": uuid, **extra}


def _write_shadow(directory: Path, rows, name="2026-09-24.jsonl", extra_lines=()):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + list(extra_lines)
    (directory / name).write_text("\n".join(lines) + "\n")


def test_prereg_document_exists_at_the_pinned_path():
    assert rse.PREREG.is_file()
    text = rse.PREREG.read_text()
    assert "resource_shadow.v1" in text
    assert "K = 5" in text and "30 minutes" in text


def test_validate_row_reasons():
    assert rse.validate_row(_row("2026-09-24T10:00:00.000000Z")) is None
    assert rse.validate_row(_row("2026-09-24T10:00:00.000000Z", uuid=None)) is None
    assert rse.validate_row([1]) == "not_object"
    assert rse.validate_row({**_row("2026-09-24T10:00:00Z"), "schema": "v0"}) == "wrong_schema"
    assert rse.validate_row(_row("2026-09-24T10:00:00Z", event="nope")) == "unknown_event"
    assert rse.validate_row(_row("2026-09-24 10:00:00")) == "bad_ts"
    assert rse.validate_row(_row("2026-09-24T10:00:00Z", uuid="not-a-uuid")) == "bad_agent_uuid"


def test_load_counts_drops_and_sorts(tmp_path):
    _write_shadow(tmp_path, [
        _row("2026-09-24T10:05:00.000000Z"),
        _row("2026-09-24T10:00:00.000000Z", event="tool-failure", uuid=UUID_B),
        {**_row("2026-09-24T10:01:00Z"), "schema": "other"},
    ], extra_lines=["{not json", ""])
    loaded = rse.load_shadow_rows(rse.shadow_files(tmp_path))
    assert [r["ts"] for r in loaded.rows] == ["2026-09-24T10:00:00.000000Z", "2026-09-24T10:05:00.000000Z"]
    assert loaded.dropped == {"wrong_schema": 1, "not_json": 1}
    assert rse.agent_uuids(loaded.rows) == sorted([UUID_A, UUID_B])


def test_state_window_includes_the_lookback():
    rows = [_row("2026-09-24T10:00:00Z"), _row("2026-09-24T12:00:00Z")]
    start, end = rse.state_window(rows)
    assert start == dt.datetime(2026, 9, 24, 9, 30, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)


def test_preflight_refuses_missing_prereg(tmp_path):
    with pytest.raises(rse.ExtractRefused, match="pre-registration missing"):
        rse.preflight(tmp_path / "absent.md", tmp_path / "out")


def test_preflight_refuses_existing_output(tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(rse.ExtractRefused, match="immutable"):
        rse.preflight(rse.PREREG, tmp_path / "out")


def test_preflight_refuses_output_inside_a_git_work_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    with pytest.raises(rse.ExtractRefused, match="git work tree"):
        rse.preflight(rse.PREREG, tmp_path / "repo" / "nested" / "out")


def test_main_refuses_before_any_db_access(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("database touched")

    monkeypatch.setattr(rse, "fetch_states", boom)
    monkeypatch.setattr(rse, "PREREG", tmp_path / "absent.md")
    _write_shadow(tmp_path / "shadow", [_row("2026-09-24T10:00:00Z")])
    code = rse.main(["--shadow-dir", str(tmp_path / "shadow"), "--out", str(tmp_path / "x")])
    assert code == 2
    assert not (tmp_path / "x").exists()


def test_main_writes_hashed_manifest(tmp_path, monkeypatch):
    seen = {}

    def fake_fetch(db_url, uuids, window):
        seen["uuids"], seen["window"] = uuids, window
        now = dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
        return now, [(UUID_A, dt.datetime(2026, 9, 24, 9, 50, tzinfo=dt.timezone.utc), 0.7, 0.8, 0.3, -0.1)]

    monkeypatch.setattr(rse, "fetch_states", fake_fetch)
    _write_shadow(tmp_path / "shadow", [
        _row("2026-09-24T10:00:00.000000Z", ctx_tokens_band=64000, model="m"),
        _row("2026-09-24T10:02:00.000000Z", event="tool-failure", tool_name="Bash", is_interrupt=False),
        _row("2026-09-24T10:03:00.000000Z", uuid=None),
    ])
    out = tmp_path / "extract"
    assert rse.main(["--shadow-dir", str(tmp_path / "shadow"), "--out", str(out)]) == 0

    assert seen["uuids"] == [UUID_A]
    assert seen["window"][0] == dt.datetime(2026, 9, 24, 9, 30, tzinfo=dt.timezone.utc)
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["prereg_sha256"] == hashlib.sha256(rse.PREREG.read_bytes()).hexdigest()
    for name in ("observations.jsonl", "eisv_states.jsonl"):
        data = (out / name).read_bytes()
        assert manifest["files"][name]["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["files"]["observations.jsonl"]["rows"] == 3
    state = json.loads((out / "eisv_states.jsonl").read_text().splitlines()[0])
    assert state == {"agent_uuid": UUID_A, "recorded_at": "2026-09-24T09:50:00.000000Z",
                     "E": 0.7, "I": 0.8, "S": 0.3, "V": -0.1}
    assert (out / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_no_rows_writes_an_empty_extract_without_db(tmp_path, monkeypatch):
    monkeypatch.setattr(rse, "fetch_states", lambda *a, **k: (_ for _ in ()).throw(AssertionError("db")))
    (tmp_path / "shadow").mkdir()
    out = tmp_path / "extract"
    assert rse.main(["--shadow-dir", str(tmp_path / "shadow"), "--out", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["files"]["observations.jsonl"]["rows"] == 0
    assert manifest["db_snapshot_now"] is None


def test_query_reads_the_documented_columns():
    sql = " ".join(rse.STATE_SQL.split())
    assert "state_json->>'E'" in sql
    assert "s.integrity AS i" in sql and "s.entropy AS s" in sql and "s.volatility AS v" in sql
    assert "synthetic IS NOT TRUE" in sql
    for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
        assert verb not in sql.upper()


def test_extract_keeps_only_documented_fields(tmp_path):
    leaky = _row("2026-09-24T10:00:00.000000Z", event="tool-failure", tool_name="Bash",
                 is_interrupt=False, tool_input={"command": "cat secret"},
                 error="denied", cwd="/Users/x", continuity_token="v1.tok")
    _write_shadow(tmp_path, [leaky])
    loaded = rse.load_shadow_rows(rse.shadow_files(tmp_path))
    assert set(loaded.rows[0]) == set(rse.COMMON_FIELDS) | {"tool_name", "is_interrupt"}
    assert "secret" not in json.dumps(loaded.rows)


@pytest.mark.parametrize("ts", ["2026-09-24T10:00:00+05:00Z", "2026-09-24T10:00:00", "2026-09-24T10:00Z"])
def test_non_canonical_timestamps_are_rejected(ts):
    assert rse.parse_ts(ts) is None


def test_rows_sort_by_parsed_time_not_string():
    # A fraction-less stamp sorts after a fractional one as a string, though it is earlier.
    rows = [_row("2026-09-24T10:00:00.500000Z"), _row("2026-09-24T10:00:00Z")]
    rows.sort(key=lambda r: rse.parse_ts(r["ts"]))
    assert rows[0]["ts"] == "2026-09-24T10:00:00Z"
