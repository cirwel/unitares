"""Bound imported memory in list/search reads; explicit details stays paginated."""
from datetime import datetime, timezone
from typing import Any

from src.knowledge_authority import IMPORTED_CONTEXT, assess_authority

INLINE_BYTES = 2048


def annotate_imported_read(item: dict[str, Any], document: Any) -> None:
    """Project a snapshot without changing stored content or claiming review."""
    if assess_authority(document).tier != IMPORTED_CONTEXT:
        return
    marker: dict[str, Any] = {"trust": "unreviewed", "age_basis": "created_at"}
    raw = getattr(document, "timestamp", None) or getattr(document, "created_at", None)
    try:
        created = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        marker["age_days"] = max(0, (datetime.now(timezone.utc) - created).days)
    except (TypeError, ValueError):
        marker["age_days"] = None
    item["imported_memory"] = marker
    details = item.get("details")
    if not isinstance(details, str):
        return
    encoded = details.encode("utf-8")
    if len(encoded) <= INLINE_BYTES:
        return
    item["details"] = encoded[:INLINE_BYTES].decode("utf-8", errors="ignore")
    item["details_length"] = len(details)
    item["details_bytes"] = len(encoded)
    item["has_more_details"] = True
    item["details_truncated"] = True
    item["details_retrieval"] = {
        "action": "details", "discovery_id": item.get("id"),
        "offset": 0, "length": 2000,
    }
