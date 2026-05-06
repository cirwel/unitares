"""S22 write-local provenance context helpers.

These fields describe the situation around a governance write. They are not
identity proof and must stay separate from lineage facts in ``provenance_chain``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional


SCHEMA_VERSION = "s22.write_context.v1"

_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "harness_id": ("harness_id",),
    "harness_type": ("harness_type", "harness"),
    "process_instance_id": ("process_instance_id",),
    "transport": ("transport",),
    "thread_id": ("thread_id",),
    "episode_id": ("episode_id",),
    "episode_fork_kind": ("episode_fork_kind",),
    "invocation_id": ("invocation_id",),
    "model_provider": ("model_provider",),
    "model": ("model", "model_type"),
    "memory_context": ("memory_context",),
    "governance_mode": ("governance_mode",),
    "verification_source": ("verification_source",),
    "comparison_key": ("comparison_key",),
    "task_label": ("task_label", "task"),
    "task_outcome": ("task_outcome", "outcome"),
    "parent_agent_id": ("parent_agent_id",),
    "spawn_reason": ("spawn_reason",),
}

_MAPPING_FIELDS = {
    "locus",
    "affordance_state",
    "identity_assurance",
}


def build_s22_write_context(
    arguments: Mapping[str, Any],
    *,
    meta: Optional[Any] = None,
    context_source: str,
    default_governance_mode: Optional[str] = None,
) -> dict[str, Any]:
    """Build a compact S22 context from explicit args plus request contextvars."""
    context: dict[str, Any] = {}

    for target_key, aliases in _STRING_FIELDS.items():
        value = _first_text(arguments, aliases)
        if value is not None:
            context[target_key] = value

    tool_surface = _normalize_text_list(arguments.get("tool_surface"))
    if tool_surface:
        context["tool_surface"] = tool_surface

    for key in _MAPPING_FIELDS:
        value = arguments.get(key)
        if isinstance(value, Mapping):
            context[key] = dict(value)

    identity_lineage_fork = arguments.get("identity_lineage_fork")
    if identity_lineage_fork is not None:
        context["identity_lineage_fork"] = _coerce_bool(identity_lineage_fork)

    _merge_meta_defaults(context, meta)
    _merge_request_context_defaults(context)

    if default_governance_mode and context and "governance_mode" not in context:
        context["governance_mode"] = default_governance_mode

    # Do not persist an envelope containing only bookkeeping. The helper is
    # optional by design and should not create noisy empty context records.
    if not context:
        return {}

    return {
        "schema": SCHEMA_VERSION,
        "context_source": context_source,
        **context,
    }


def attach_s22_context(
    provenance: Optional[dict[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach S22 context to a write-local provenance dict."""
    merged = dict(provenance or {})
    if context:
        merged["s22_context"] = dict(context)
    return merged


def _first_text(arguments: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = arguments.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = [value]

    items: list[str] = []
    seen = set()
    for item in raw_items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _merge_meta_defaults(context: dict[str, Any], meta: Optional[Any]) -> None:
    if meta is None:
        return
    for target_key, attr_name in (
        ("parent_agent_id", "parent_agent_id"),
        ("spawn_reason", "spawn_reason"),
        ("thread_id", "thread_id"),
    ):
        if target_key in context:
            continue
        value = getattr(meta, attr_name, None)
        if value:
            context[target_key] = str(value)


def _merge_request_context_defaults(context: dict[str, Any]) -> None:
    try:
        from src.mcp_handlers.context import (
            get_context_client_hint,
            get_session_resolution_source,
            get_session_signals,
        )

        signals = get_session_signals()
        if "transport" not in context and signals and signals.transport:
            context["transport"] = signals.transport

        client_hint = get_context_client_hint() or (
            signals.client_hint if signals else None
        )
        if "harness_type" not in context and client_hint:
            context["harness_type"] = client_hint

        resolution_source = get_session_resolution_source()
        if resolution_source:
            context["session_resolution_source"] = resolution_source
    except Exception:
        return
