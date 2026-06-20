"""Recall-miss telemetry for KG search failures.

Instrument-first (2026-06-20). Before building query expansion, measure whether
vocab-gap and zero-result searches accumulate in live use. This appends one
JSONL row per failed or low-confidence text search under
``data/telemetry/recall_misses.jsonl`` so later analysis can distinguish real
vocab gaps from absent knowledge and automated-battery noise.

Fail-open: telemetry errors must never break a search.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()

ZERO_RESULT = "zero_result"
LOW_CONFIDENCE = "low_confidence"


def _telemetry_file() -> Path:
    data_dir = Path(__file__).parent.parent / "data" / "telemetry"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "recall_misses.jsonl"


def record_recall_event(
    event_class: str,
    query: Optional[str],
    *,
    query_terms: Optional[int] = None,
    search_mode: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one recall-failure event without raising into the caller."""
    try:
        query_str = str(query or "")
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "class": event_class,
            "query": query_str[:500],
            "query_terms": (
                query_terms if query_terms is not None else len(query_str.split())
            ),
            "search_mode": search_mode,
            "detail": detail or {},
        }
        line = json.dumps(row, sort_keys=True)
        with _lock:
            with open(_telemetry_file(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        logger.debug(f"recall telemetry append failed (non-fatal): {exc}")


def summarize(limit: int = 50000) -> Dict[str, Any]:
    """Return coarse recent counts by event class."""
    try:
        path = _telemetry_file()
        if not path.exists():
            return {"total": 0, "by_class": {}, "file": str(path)}
        with _lock:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        if len(lines) > limit:
            lines = lines[-limit:]

        by_class: Dict[str, int] = {}
        total = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event_class = json.loads(line).get("class", "unknown")
            except Exception:
                continue
            by_class[event_class] = by_class.get(event_class, 0) + 1
            total += 1
        return {"total": total, "by_class": by_class, "file": str(path)}
    except Exception as exc:
        logger.debug(f"recall telemetry summarize failed: {exc}")
        return {"total": 0, "by_class": {}, "error": str(exc)}
