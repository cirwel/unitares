#!/usr/bin/env python3
"""Create and verify a neutral, offline shared-memory portability bundle.

This is deliberately not an MCP import surface. It proves that full discovery
records can leave UNITARES in a versioned, hash-verified format and run through
a small independent SQLite substitute. Production database extraction and
restore remain separate, operator-controlled steps.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Sequence


BUNDLE_SCHEMA = "unitares.shared-memory.bundle"
BUNDLE_VERSION = 1
RECORDS_FILENAME = "discoveries.jsonl"
MANIFEST_FILENAME = "manifest.json"
_REQUIRED_FIELDS = frozenset({"id", "agent_id", "type", "summary"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class BundleValidationError(ValueError):
    """Raised when a portability bundle cannot be trusted."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BundleValidationError(f"record {index} must be an object")
        missing = sorted(_REQUIRED_FIELDS.difference(record))
        if missing:
            raise BundleValidationError(
                f"record {index} is missing required fields: {', '.join(missing)}"
            )
        discovery_id = str(record["id"]).strip()
        if not discovery_id:
            raise BundleValidationError(f"record {index} has an empty id")
        if discovery_id in seen:
            raise BundleValidationError(f"duplicate discovery id: {discovery_id}")
        seen.add(discovery_id)
        checked.append(dict(record))
    return sorted(checked, key=lambda item: str(item["id"]))


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def create_bundle(
    records: Iterable[dict[str, Any]],
    output_dir: str | Path,
    *,
    created_at: str | None = None,
    source: str = "offline-json",
) -> dict[str, Any]:
    """Write canonical JSONL plus a content manifest and return the manifest."""
    checked = _validate_records(records)
    directory = Path(output_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)

    body = "".join(f"{_canonical_json(record)}\n" for record in checked).encode("utf-8")
    records_path = directory / RECORDS_FILENAME
    _write_private(records_path, body)

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_VERSION,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "files": {
            RECORDS_FILENAME: {
                "sha256": _sha256(body),
                "bytes": len(body),
                "records": len(checked),
            }
        },
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_private(directory / MANIFEST_FILENAME, manifest_bytes)
    return manifest


def validate_bundle(bundle_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate schema, hash, byte and record counts; return manifest + records."""
    directory = Path(bundle_dir)
    try:
        manifest = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"invalid manifest: {exc}") from exc

    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleValidationError("unknown bundle schema")
    if manifest.get("schema_version") != BUNDLE_VERSION:
        raise BundleValidationError(
            f"unsupported schema version: {manifest.get('schema_version')!r}"
        )
    file_meta = manifest.get("files", {}).get(RECORDS_FILENAME)
    if not isinstance(file_meta, dict):
        raise BundleValidationError(f"manifest must describe {RECORDS_FILENAME}")

    try:
        body = (directory / RECORDS_FILENAME).read_bytes()
    except OSError as exc:
        raise BundleValidationError(f"cannot read {RECORDS_FILENAME}: {exc}") from exc
    if len(body) != file_meta.get("bytes"):
        raise BundleValidationError("record byte count mismatch")
    if _sha256(body) != file_meta.get("sha256"):
        raise BundleValidationError("record hash mismatch")

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(body.decode("utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleValidationError(f"invalid JSON on record line {line_number}") from exc
        records.append(value)
    checked = _validate_records(records)
    if len(checked) != file_meta.get("records"):
        raise BundleValidationError("record count mismatch")
    return manifest, checked


def restore_sqlite(bundle_dir: str | Path, database_path: str | Path) -> dict[str, int]:
    """Exactly and idempotently restore a bundle into a private substitute store.

    Restore means replacement, not merge: records absent from the validated
    bundle must not survive from an older restore.  The database is a portable
    offline artifact and is always restricted to the current user.
    """
    _, records = validate_bundle(bundle_dir)
    database = Path(database_path)
    parent_existed = database.parent.exists()
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(database.parent, 0o700)
    if not database.exists():
        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    os.chmod(database, 0o600)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_memory (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                discovery_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT NOT NULL,
                document_json TEXT NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM shared_memory")
        for record in records:
            connection.execute(
                """
                INSERT INTO shared_memory (
                    id, agent_id, discovery_type, summary, details, document_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    discovery_type = excluded.discovery_type,
                    summary = excluded.summary,
                    details = excluded.details,
                    document_json = excluded.document_json
                """,
                (
                    str(record["id"]),
                    str(record["agent_id"]),
                    str(record["type"]),
                    str(record["summary"]),
                    str(record.get("details", "")),
                    _canonical_json(record),
                ),
            )
        restored = connection.execute("SELECT count(*) FROM shared_memory").fetchone()[0]
    os.chmod(database, 0o600)
    return {"bundle_records": len(records), "store_records": int(restored)}


def search_sqlite(
    database_path: str | Path,
    query: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Run deterministic lexical retrieval against the substitute store."""
    terms = _TOKEN_RE.findall(query.lower())
    if not terms or limit <= 0:
        return []
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, summary, details, document_json FROM shared_memory"
        ).fetchall()

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for discovery_id, summary, details, document_json in rows:
        haystack = f"{summary} {details}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score:
            ranked.append((score, str(discovery_id), json.loads(document_json)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [record for _, _, record in ranked[:limit]]


def _load_input(path: Path) -> Sequence[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if isinstance(value, dict) and isinstance(value.get("discoveries"), list):
        return value["discoveries"]
    if not isinstance(value, list):
        raise BundleValidationError("input must be a JSON array, JSONL, or {discoveries: [...]}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--input", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--bundle", required=True, type=Path)

    restore = subparsers.add_parser("restore-sqlite")
    restore.add_argument("--bundle", required=True, type=Path)
    restore.add_argument("--database", required=True, type=Path)

    search = subparsers.add_parser("search-sqlite")
    search.add_argument("--database", required=True, type=Path)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result: Any = create_bundle(_load_input(args.input), args.output)
        elif args.command == "validate":
            manifest, records = validate_bundle(args.bundle)
            result = {"valid": True, "manifest": manifest, "records": len(records)}
        elif args.command == "restore-sqlite":
            result = restore_sqlite(args.bundle, args.database)
        else:
            result = search_sqlite(args.database, args.query, limit=args.limit)
    except (BundleValidationError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
