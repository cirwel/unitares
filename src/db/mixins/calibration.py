"""Calibration operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from typing import Any, Dict
from uuid import UUID

from src.calibration_mutations import (
    CalibrationVersionConflict, apply_mutation, mutation_digest,
)

from src.logging_utils import get_logger

logger = get_logger(__name__)


class CalibrationMixin:
    """Calibration data operations."""

    async def get_calibration(self) -> Dict[str, Any]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data, updated_at, version FROM core.calibration WHERE id = TRUE"
            )
            if not row:
                return {}
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            data["_updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
            data["_version"] = row["version"]
            return data

    async def update_calibration(self, data: Dict[str, Any]) -> bool:
        """Replace a projection only at the version the caller actually read.

        Observation writers use apply_calibration_mutation instead. A blind
        replacement could erase a committed outcome delivery.
        """
        expected_version = data.get("_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise CalibrationVersionConflict("calibration replacement requires _version")
        clean_data = {k: v for k, v in data.items() if not k.startswith("_")}
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.calibration
                SET data = $1::jsonb, updated_at = now(), version = version + 1
                WHERE id = TRUE AND version = $2
                """,
                json.dumps(clean_data),
                expected_version,
            )
            return "UPDATE 1" in result

    async def apply_calibration_mutation(
        self, *, writer_id: str, sequence: int, mutation: Dict[str, Any],
        expected_version: int | None = None,
    ) -> Dict[str, Any]:
        """Atomically apply a frozen writer operation and its retry receipt."""
        if sequence < 1:
            raise ValueError("calibration writer sequence must be positive")
        writer = UUID(writer_id)
        digest = mutation_digest(mutation, expected_version)
        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT data, version FROM core.calibration WHERE id = TRUE FOR UPDATE"
                )
                if row is None:
                    raise RuntimeError("calibration state is not initialized")
                state = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                version = int(row["version"])
                receipt = await conn.fetchrow(
                    "SELECT sequence, request_digest FROM core.calibration_writer_receipts "
                    "WHERE writer_id = $1", writer,
                )
                if receipt and receipt["sequence"] == sequence:
                    if receipt["request_digest"] != digest:
                        raise CalibrationVersionConflict("calibration operation identity was reused")
                    return {"data": state, "version": version, "applied": False}
                previous = int(receipt["sequence"]) if receipt else 0
                if sequence != previous + 1:
                    raise CalibrationVersionConflict("calibration operations arrived out of order")
                updated = apply_mutation(
                    state, mutation, expected_version=expected_version,
                    actual_version=version,
                )
                await conn.execute(
                    "UPDATE core.calibration SET data = $1::jsonb, "
                    "version = version + 1, updated_at = now() WHERE id = TRUE",
                    json.dumps(updated, allow_nan=False),
                )
                await conn.execute(
                    "INSERT INTO core.calibration_writer_receipts "
                    "(writer_id, sequence, request_digest) VALUES ($1, $2, $3) "
                    "ON CONFLICT (writer_id) DO UPDATE SET sequence = EXCLUDED.sequence, "
                    "request_digest = EXCLUDED.request_digest, updated_at = now()",
                    writer, sequence, digest,
                )
                return {"data": updated, "version": version + 1, "applied": True}
