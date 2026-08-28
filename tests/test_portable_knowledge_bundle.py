import json
from pathlib import Path

import pytest

from scripts.dev.portable_knowledge_bundle import (
    BUNDLE_VERSION,
    BundleValidationError,
    create_bundle,
    restore_sqlite,
    search_sqlite,
    validate_bundle,
)


def _records():
    return [
        {
            "id": "d-2",
            "agent_id": "agent-b",
            "type": "insight",
            "summary": "Orchestration needs terminal receipts",
            "details": "Timeouts cannot be treated as safe retries.",
            "tags": ["orchestration", "reliability"],
            "status": "open",
            "response_to": {"discovery_id": "d-1", "response_type": "extend"},
            "provenance": {"source": "review"},
        },
        {
            "id": "d-1",
            "agent_id": "agent-a",
            "type": "pattern",
            "summary": "Shared memory reduces duplicate work",
            "details": "Agents retrieve a prior decision before editing.",
            "tags": ["knowledge-graph"],
            "status": "resolved",
            "related_to": ["d-2"],
            "provenance_chain": [{"agent_id": "agent-a"}],
        },
    ]


def test_bundle_round_trip_preserves_full_records_and_hashes(tmp_path):
    bundle = tmp_path / "bundle"
    manifest = create_bundle(
        _records(),
        bundle,
        created_at="2026-08-27T00:00:00+00:00",
    )

    validated_manifest, records = validate_bundle(bundle)

    assert validated_manifest == manifest
    assert validated_manifest["schema_version"] == BUNDLE_VERSION
    assert records == sorted(_records(), key=lambda item: item["id"])
    assert (bundle.stat().st_mode & 0o777) == 0o700
    assert ((bundle / "discoveries.jsonl").stat().st_mode & 0o777) == 0o600


def test_corruption_and_unknown_version_fail_closed(tmp_path):
    bundle = tmp_path / "bundle"
    create_bundle(_records(), bundle)
    records_path = bundle / "discoveries.jsonl"
    records_path.write_text(records_path.read_text() + "{}\n", encoding="utf-8")

    with pytest.raises(BundleValidationError, match="count mismatch|hash mismatch"):
        validate_bundle(bundle)

    create_bundle(_records(), bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="unsupported schema version"):
        validate_bundle(bundle)


def test_restore_is_idempotent_and_substitute_search_runs_without_unitares_db(tmp_path):
    bundle = tmp_path / "bundle"
    database = tmp_path / "substitute.sqlite3"
    create_bundle(_records(), bundle)

    first = restore_sqlite(bundle, database)
    second = restore_sqlite(bundle, database)
    results = search_sqlite(database, "duplicate prior work")

    assert first == {"bundle_records": 2, "store_records": 2}
    assert second == first
    assert [result["id"] for result in results] == ["d-1"]
    assert results[0]["related_to"] == ["d-2"]
    assert results[0]["provenance_chain"] == [{"agent_id": "agent-a"}]
    assert (database.stat().st_mode & 0o777) == 0o600


def test_restore_is_exact_and_removes_rows_absent_from_a_smaller_bundle(tmp_path):
    full_bundle = tmp_path / "full"
    smaller_bundle = tmp_path / "smaller"
    database = tmp_path / "substitute.sqlite3"
    create_bundle(_records(), full_bundle)
    create_bundle([_records()[0]], smaller_bundle)

    assert restore_sqlite(full_bundle, database)["store_records"] == 2
    restored = restore_sqlite(smaller_bundle, database)

    assert restored == {"bundle_records": 1, "store_records": 1}
    assert search_sqlite(database, "shared memory duplicate") == []
    assert [row["id"] for row in search_sqlite(database, "terminal receipts")] == [
        "d-2"
    ]


def test_duplicate_ids_and_incomplete_records_are_rejected(tmp_path):
    with pytest.raises(BundleValidationError, match="duplicate discovery id"):
        create_bundle([_records()[0], _records()[0]], tmp_path / "duplicate")

    with pytest.raises(BundleValidationError, match="missing required fields"):
        create_bundle([{"id": "partial"}], tmp_path / "partial")


def test_cli_validation_reports_a_machine_readable_receipt(tmp_path, capsys):
    from scripts.dev.portable_knowledge_bundle import main

    bundle = tmp_path / "bundle"
    create_bundle(_records(), bundle)

    assert main(["validate", "--bundle", str(bundle)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["records"] == 2
