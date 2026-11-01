"""
Calibration State Storage

PostgreSQL-backed calibration state access via get_db().
"""

from __future__ import annotations

from typing import Any, Dict


async def get_calibration_async() -> Dict[str, Any]:
    """Get calibration state from PostgreSQL."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()
    return await db.get_calibration()


async def update_calibration_async(state: Dict[str, Any]) -> bool:
    """Replace state carrying the '_version' returned by get_calibration_async."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()
    return await db.update_calibration(state)


async def calibration_health_check_async() -> Dict[str, Any]:
    """Health check for calibration storage backend."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()
    health = await db.health_check()
    cal_data = await db.get_calibration()
    health["has_calibration_data"] = bool(cal_data)
    return health
