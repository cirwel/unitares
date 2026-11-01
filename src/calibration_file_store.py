"""Locked, durable JSON adapters for calibration projections."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from src.calibration_mutations import (
    CalibrationVersionConflict, apply_mutation, mutation_digest,
)


@contextmanager
def state_lock(path: Path):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError("calibration state must be a JSON object")
    return result


def sync_directory(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_state(path: Path, data: dict[str, Any]) -> None:
    """Caller holds the canonical lock. Success includes the rename barrier."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_file_mutation(
    path: Path, *, writer_id: str, sequence: int, mutation: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    digest = mutation_digest(mutation, expected_version)
    with state_lock(path):
        current = read_state(path)
        version = int(current.get("_version", 0))
        receipts = current.get("_writer_receipts", {})
        receipt = receipts.get(writer_id)
        if receipt and receipt["sequence"] == sequence:
            if receipt["digest"] != digest:
                raise CalibrationVersionConflict("calibration operation identity was reused")
            sync_directory(path)
            return {"data": current, "version": version, "applied": False}
        if sequence != (int(receipt["sequence"]) if receipt else 0) + 1:
            raise CalibrationVersionConflict("calibration operations arrived out of order")
        updated = apply_mutation(
            current, mutation, expected_version=expected_version, actual_version=version,
        )
        updated["_writer_receipts"] = {
            **receipts, writer_id: {"sequence": sequence, "digest": digest},
        }
        updated["_version"] = version + 1
        write_state(path, updated)
        return {"data": updated, "version": version + 1, "applied": True}


def deliver_file_sample(
    path: Path, *, outcome_id: str, payload_digest: str,
    observation_time: str, apply_sample: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    with state_lock(path):
        current = read_state(path)
        receipts = current.get("_delivery_receipts", {})
        if outcome_id in receipts:
            if receipts[outcome_id]["digest"] != payload_digest:
                raise CalibrationVersionConflict("outcome delivery identity was reused")
            sync_directory(path)
            return current
        updated = {**current, **apply_sample(current)}
        updated["_delivery_receipts"] = {
            **receipts, outcome_id: {"digest": payload_digest, "observed_at": observation_time},
        }
        updated["_version"] = int(current.get("_version", 0)) + 1
        write_state(path, updated)
        return updated
