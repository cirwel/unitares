"""S22 H5 comparable-entry coverage helpers.

H5 asks for the same bounded task to be recorded through Hermes, Claude Code,
and Codex CLI. This module keeps that gate read-only and explicit: it normalizes
S22 write-context rows, groups them by a shared comparison key, and reports
whether every required harness has a situated entry for the same task.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional

from src.db import get_db


DEFAULT_REQUIRED_HARNESSES = ("hermes", "claude-code", "codex-cli")

AGENT_STATE_S22_SQL = """
SELECT
    s.state_id::TEXT AS entry_id,
    'agent_state' AS source,
    i.agent_id AS agent_id,
    s.recorded_at AS recorded_at,
    s.state_json->'provenance_context' AS s22_context,
    s.state_json AS state_json
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
WHERE s.state_json ? 'provenance_context'
  AND (
    $2::TEXT IS NULL
    OR COALESCE(
        s.state_json#>>'{provenance_context,comparison_key}',
        s.state_json#>>'{provenance_context,task_label}',
        s.state_json#>>'{provenance_context,task}'
    ) = $2
  )
ORDER BY s.recorded_at DESC
LIMIT $1
"""

KG_S22_SQL = """
SELECT
    d.id::TEXT AS entry_id,
    'knowledge_discovery' AS source,
    d.agent_id AS agent_id,
    d.created_at AS recorded_at,
    d.provenance->'s22_context' AS s22_context,
    jsonb_build_object(
        'summary', d.summary,
        'type', d.type,
        'status', d.status,
        'tags', d.tags,
        'response_to_id', d.response_to_id,
        'response_type', d.response_type
    ) AS state_json
FROM knowledge.discoveries d
WHERE d.provenance ? 's22_context'
  AND (
    $2::TEXT IS NULL
    OR COALESCE(
        d.provenance#>>'{s22_context,comparison_key}',
        d.provenance#>>'{s22_context,task_label}',
        d.provenance#>>'{s22_context,task}'
    ) = $2
  )
ORDER BY d.created_at DESC
LIMIT $1
"""


@dataclass(frozen=True)
class S22H5Entry:
    entry_id: str
    source: str
    agent_id: Optional[str]
    recorded_at: Optional[str]
    harness_type: Optional[str]
    canonical_harness: Optional[str]
    comparison_key: Optional[str]
    task_label: Optional[str]
    task_outcome: Optional[str]
    context_source: Optional[str]
    schema: Optional[str]
    model_provider: Optional[str]
    model: Optional[str]
    transport: Optional[str]
    memory_context: Optional[str]
    tool_surface: tuple[str, ...]
    has_situating_metadata: bool

    @property
    def is_comparable(self) -> bool:
        return bool(
            self.canonical_harness
            and self.comparison_key
            and self.has_situating_metadata
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tool_surface"] = list(self.tool_surface)
        data["is_comparable"] = self.is_comparable
        return data


async def collect_s22_h5_entries(
    *,
    db: Optional[Any] = None,
    limit_per_source: int = 200,
    comparison_key: Optional[str] = None,
) -> tuple[S22H5Entry, ...]:
    """Collect S22 write-context entries from durable state and KG rows."""
    if limit_per_source <= 0:
        raise ValueError("limit_per_source must be positive")

    target_key = _clean_text(comparison_key)
    backend = db or get_db()
    async with backend.acquire() as conn:
        state_rows = await conn.fetch(
            AGENT_STATE_S22_SQL,
            limit_per_source,
            target_key,
        )
        kg_rows = await conn.fetch(KG_S22_SQL, limit_per_source, target_key)

    entries: list[S22H5Entry] = []
    for row in [*state_rows, *kg_rows]:
        entry = normalize_s22_h5_entry(row)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def normalize_s22_h5_entry(raw: Mapping[str, Any]) -> Optional[S22H5Entry]:
    """Normalize a DB row or raw S22 context into a comparable-entry record."""
    row = _coerce_mapping(raw)
    context = _extract_context(row)
    if not context:
        return None

    entry_id = _first_text(row, ("entry_id", "state_id", "id")) or "unknown"
    source = _first_text(row, ("source",)) or "unknown"
    agent_id = _first_text(row, ("agent_id", "agent_uuid"))
    harness_type = _first_text(context, ("harness_type", "harness"))
    canonical_harness = normalize_s22_harness(harness_type)
    comparison_key = _first_text(context, ("comparison_key", "task_label", "task"))
    task_label = _first_text(context, ("task_label", "task", "comparison_key"))
    task_outcome = _first_text(
        context,
        ("task_outcome", "outcome", "decision_action", "action"),
    )
    tool_surface = _normalize_text_tuple(context.get("tool_surface"))
    model_provider = _first_text(context, ("model_provider",))
    model = _first_text(context, ("model", "model_type"))
    transport = _first_text(context, ("transport",))
    memory_context = _first_text(context, ("memory_context",))
    has_situating_metadata = bool(
        model_provider or model or transport or memory_context or tool_surface
    )

    state_json = _coerce_mapping(row.get("state_json"))
    if not task_outcome:
        task_outcome = _first_text(state_json, ("task_outcome", "outcome", "action"))

    return S22H5Entry(
        entry_id=entry_id,
        source=source,
        agent_id=agent_id,
        recorded_at=_format_time(row.get("recorded_at")),
        harness_type=harness_type,
        canonical_harness=canonical_harness,
        comparison_key=comparison_key,
        task_label=task_label,
        task_outcome=task_outcome,
        context_source=_first_text(context, ("context_source",)),
        schema=_first_text(context, ("schema",)),
        model_provider=model_provider,
        model=model,
        transport=transport,
        memory_context=memory_context,
        tool_surface=tool_surface,
        has_situating_metadata=has_situating_metadata,
    )


def assess_s22_h5_coverage(
    entries: Sequence[S22H5Entry | Mapping[str, Any]],
    *,
    required_harnesses: Sequence[str] = DEFAULT_REQUIRED_HARNESSES,
) -> dict[str, Any]:
    """Assess whether one shared H5 comparison key spans all required harnesses."""
    required = tuple(
        harness
        for harness in (
            normalize_s22_harness(value) for value in required_harnesses
        )
        if harness
    )
    if not required:
        raise ValueError("required_harnesses must include at least one harness")

    normalized = tuple(_ensure_entry(entry) for entry in entries)
    comparable = tuple(entry for entry in normalized if entry.is_comparable)
    any_present = sorted({
        entry.canonical_harness
        for entry in normalized
        if entry.canonical_harness
    })
    comparable_present = sorted({
        entry.canonical_harness
        for entry in comparable
        if entry.canonical_harness
    })

    groups: dict[str, list[S22H5Entry]] = {}
    for entry in comparable:
        groups.setdefault(entry.comparison_key or "", []).append(entry)

    comparison_sets = []
    complete_keys = []
    required_set = set(required)
    for key, group_entries in sorted(groups.items()):
        harnesses = sorted({
            entry.canonical_harness
            for entry in group_entries
            if entry.canonical_harness
        })
        missing = sorted(required_set - set(harnesses))
        comparison_sets.append({
            "comparison_key": key,
            "entry_count": len(group_entries),
            "harnesses": harnesses,
            "missing_harnesses": missing,
        })
        if not missing:
            complete_keys.append(key)

    missing_comparable = sorted(required_set - set(comparable_present))
    if complete_keys:
        decision = "complete"
        reason = "shared_comparison_key_covers_required_harnesses"
    elif not normalized:
        decision = "incomplete"
        reason = "no_s22_entries"
    elif not comparable:
        decision = "incomplete"
        reason = "no_comparable_entries"
    elif missing_comparable:
        decision = "incomplete"
        reason = "missing_required_harness_entries"
    else:
        decision = "incomplete"
        reason = "no_shared_comparison_key"

    return {
        "decision": decision,
        "reason": reason,
        "required_harnesses": list(required),
        "entry_count": len(normalized),
        "comparable_entry_count": len(comparable),
        "present_harnesses": any_present,
        "comparable_harnesses": comparable_present,
        "missing_comparable_harnesses": missing_comparable,
        "complete_comparison_keys": complete_keys,
        "comparison_sets": comparison_sets,
        "non_comparable_entries": [
            entry.to_dict() for entry in normalized if not entry.is_comparable
        ],
        "recommendations": _recommendations(reason, required, comparison_sets),
    }


def build_s22_h5_missing_payloads(
    assessment: Mapping[str, Any],
    *,
    comparison_key: Optional[str] = None,
    task_label: str = "Run S22 H5 coverage diagnostic",
    task_outcome: str = "diagnostic-complete",
) -> list[dict[str, Any]]:
    """Build ready-to-send process_agent_update payloads for missing H5 entries."""
    target_key = _clean_text(comparison_key)
    comparison_sets = tuple(
        item
        for item in assessment.get("comparison_sets", ())
        if isinstance(item, Mapping)
    )
    if not target_key:
        target_key = _best_comparison_key(comparison_sets)
    if not target_key:
        return []

    missing = _missing_harnesses_for_key(
        comparison_sets,
        target_key,
        assessment.get("missing_comparable_harnesses", ()),
    )
    payloads = []
    for harness in missing:
        payloads.append({
            "name": "process_agent_update",
            "arguments": {
                "response_text": (
                    "S22 H5 comparable task entry recorded for "
                    f"{target_key} via {harness}."
                ),
                "task_type": "testing",
                "complexity": 0.2,
                "confidence": 0.8,
                "harness_type": harness,
                "comparison_key": target_key,
                "task_label": task_label,
                "task_outcome": task_outcome,
                "tool_surface": ["mcp:unitares"],
                "governance_mode": "explicit",
                "verification_source": "harness_self_report",
                "response_mode": "minimal",
            },
        })
    return payloads


def normalize_s22_harness(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None

    normalized = text.lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "hermes": "hermes",
        "hermes-agent": "hermes",
        "claude": "claude-code",
        "claude-cli": "claude-code",
        "claude-code": "claude-code",
        "anthropic-claude-code": "claude-code",
        "codex": "codex-cli",
        "codex-cli": "codex-cli",
        "openai-codex": "codex-cli",
        "chatgpt-codex": "codex-cli",
    }
    return aliases.get(normalized, normalized)


def _best_comparison_key(comparison_sets: Sequence[Mapping[str, Any]]) -> Optional[str]:
    ranked = sorted(
        comparison_sets,
        key=lambda item: (
            -_safe_int(item.get("entry_count")),
            _clean_text(item.get("comparison_key")) or "",
        ),
    )
    if not ranked:
        return None
    return _clean_text(ranked[0].get("comparison_key"))


def _missing_harnesses_for_key(
    comparison_sets: Sequence[Mapping[str, Any]],
    target_key: str,
    fallback_missing: Any,
) -> list[str]:
    for item in comparison_sets:
        if _clean_text(item.get("comparison_key")) == target_key:
            return sorted(
                harness
                for harness in (
                    normalize_s22_harness(value)
                    for value in item.get("missing_harnesses", ())
                )
                if harness
            )
    return sorted(
        harness
        for harness in (
            normalize_s22_harness(value)
            for value in (fallback_missing or ())
        )
        if harness
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ensure_entry(entry: S22H5Entry | Mapping[str, Any]) -> S22H5Entry:
    if isinstance(entry, S22H5Entry):
        return entry
    normalized = normalize_s22_h5_entry(entry)
    if normalized is not None:
        return normalized
    return S22H5Entry(
        entry_id="unknown",
        source="unknown",
        agent_id=None,
        recorded_at=None,
        harness_type=None,
        canonical_harness=None,
        comparison_key=None,
        task_label=None,
        task_outcome=None,
        context_source=None,
        schema=None,
        model_provider=None,
        model=None,
        transport=None,
        memory_context=None,
        tool_surface=(),
        has_situating_metadata=False,
    )


def _extract_context(row: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("s22_context", "provenance_context"):
        context = _coerce_mapping(row.get(key))
        if context:
            return context

    state_json = _coerce_mapping(row.get("state_json"))
    context = _coerce_mapping(state_json.get("provenance_context"))
    if context:
        return context

    provenance = _coerce_mapping(row.get("provenance"))
    context = _coerce_mapping(provenance.get("s22_context"))
    if context:
        return context

    if row.get("schema") == "s22.write_context.v1" or row.get("harness_type"):
        return dict(row)
    return {}


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    try:
        # asyncpg.Record is mapping-like but does not register as Mapping.
        return dict(value)
    except (TypeError, ValueError):
        pass
    return {}


def _first_text(mapping: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        text = _clean_text(mapping.get(key))
        if text:
            return text
    return None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items: Any = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = (value,)

    items = []
    seen = set()
    for item in raw_items:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return tuple(items)


def _format_time(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _clean_text(value)


def _recommendations(
    reason: str,
    required: Sequence[str],
    comparison_sets: Sequence[Mapping[str, Any]],
) -> list[str]:
    if reason == "shared_comparison_key_covers_required_harnesses":
        return ["S22 H5 has at least one complete cross-harness task set."]
    if reason == "no_s22_entries":
        return [
            "Record one bounded task through Hermes, Claude Code, and Codex CLI "
            "with S22 provenance fields enabled."
        ]
    if reason == "no_comparable_entries":
        return [
            "Add comparison_key or task_label plus at least one situating field "
            "(model, transport, memory_context, or tool_surface) to H5 task writes."
        ]
    if reason == "missing_required_harness_entries":
        return [
            "Record comparable H5 task entries for the missing required harnesses."
        ]
    keys = ", ".join(item["comparison_key"] for item in comparison_sets) or "none"
    required_text = ", ".join(required)
    return [
        "Use the same comparison_key for the bounded H5 task across all required "
        f"harnesses ({required_text}); current keys: {keys}."
    ]
