"""Navigate appended notes without interpreting or rewriting their claims."""
from datetime import datetime
import re
from typing import Any


_NOTE_HEADER = re.compile(r"^Resolution notes \(([^()\r\n]{1,64})\):\r?\n", re.MULTILINE)
RESOLUTION_NOTES_HINT = (
    "resolution_notes locates blocks in stored text, not verified corrections. "
    "Read with knowledge(action='details', discovery_id=..., offset=latest_offset); "
    "first_offset opens the note history. Offsets count characters."
)


def annotate_resolution_notes(item: dict[str, Any], document: Any) -> None:
    """Expose offsets into the full details, before any preview or pagination.

    These are recognizable text headers, not authenticated update receipts.
    'Latest' means last in text order, never most authoritative. Notes can
    confirm an original claim, correct it, or just append administrative news.
    Both endpoints let a reader inspect earlier notes when the last is trivial.
    """
    details = getattr(document, "details", None)
    if not isinstance(details, str):
        return
    offsets = []
    for match in _NOTE_HEADER.finditer(details):
        stamp = match[1]
        if "T" not in stamp:
            continue
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            continue
        offsets.append(match.start())
    if offsets:
        item["resolution_notes"] = {
            "source": "details_text", "count": len(offsets),
            "first_offset": offsets[0], "latest_offset": offsets[-1],
        }


def add_resolution_notes_hint(payload: dict[str, Any]) -> None:
    rows = payload.get("discoveries", [payload.get("discovery", {})])
    if any(isinstance(row, dict) and "resolution_notes" in row for row in rows):
        payload["resolution_notes_hint"] = RESOLUTION_NOTES_HINT
