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

_ALIAS_TO_STRING_FIELD = {
    alias: target_key
    for target_key, aliases in _STRING_FIELDS.items()
    for alias in aliases
}

_MAPPING_FIELDS = {
    "locus",
    "affordance_state",
    "identity_assurance",
}

_S22_PROVENANCE_KEYS = (
    set(_MAPPING_FIELDS)
    | {"tool_surface", "identity_lineage_fork", "provenance_context"}
    | {alias for aliases in _STRING_FIELDS.values() for alias in aliases}
)
_MANGLED_PROVENANCE_WARNING = (
    "recovered_mangled_provenance: lifted S22 provenance fields out of "
    "recent_tool_results"
)


def recover_mangled_s22_provenance(arguments: dict[str, Any]) -> list[str]:
    """Lift S22 metadata that an LLM placed inside recent_tool_results.

    The public tool-result evidence list is for outcome evidence, not write-local
    provenance. Hermes/native MCP one-shots can still misplace prose-described S22
    fields into that list when the public schema lacks an obvious slot. Recover the
    known provenance keys before Pydantic validation/outcome emission so a mangled
    payload can still persist provenance without creating bogus tool evidence.

    Explicit top-level provenance values always win over recovered values.
    """
    if not isinstance(arguments, dict):
        return []
    recent_tool_results = arguments.get("recent_tool_results")
    if not isinstance(recent_tool_results, list):
        return []

    recovered_any = False
    cleaned_results: list[Any] = []
    for item in recent_tool_results:
        if not isinstance(item, Mapping):
            cleaned_results.append(item)
            continue

        recovered = _extract_s22_provenance_from_mapping(item)
        if not recovered:
            cleaned_results.append(item)
            continue

        recovered_any = True
        _merge_recovered_s22_provenance(arguments, recovered)

        evidence_part = {
            key: value
            for key, value in item.items()
            if key not in _S22_PROVENANCE_KEYS
        }
        # Preserve real tool evidence while stripping misplaced S22 keys. If the
        # remaining dict does not satisfy the required evidence shape, drop it so
        # validation does not turn provenance mangling into a hard failure or a
        # bogus outcome_event.
        if evidence_part.get("tool") and evidence_part.get("summary"):
            cleaned_results.append(evidence_part)

    if not recovered_any:
        return []

    arguments["recent_tool_results"] = cleaned_results
    arguments["_recovered_mangled_provenance"] = True
    return [_MANGLED_PROVENANCE_WARNING]


def _extract_s22_provenance_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    recovered: dict[str, Any] = {}
    nested = value.get("provenance_context")
    if isinstance(nested, Mapping):
        for nested_key, nested_value in nested.items():
            if nested_key in _S22_PROVENANCE_KEYS and nested_key != "provenance_context":
                recovered[nested_key] = nested_value
    for key, item_value in value.items():
        if key in _S22_PROVENANCE_KEYS and key != "provenance_context":
            recovered[key] = item_value
    return recovered


def _merge_recovered_s22_provenance(
    arguments: dict[str, Any],
    recovered: Mapping[str, Any],
) -> None:
    public_context = _public_provenance_context(arguments)
    existing_recovered = arguments.get("_recovered_s22_context")
    if isinstance(existing_recovered, Mapping):
        recovered_context = dict(existing_recovered)
    else:
        recovered_context = {}

    for key, value in recovered.items():
        if key == "provenance_context":
            if isinstance(value, Mapping):
                _merge_recovered_s22_provenance(arguments, value)
            continue
        if _can_accept_recovered_provenance_key(key, arguments, public_context):
            recovered_context.setdefault(key, value)

    if recovered_context:
        arguments["_recovered_s22_context"] = recovered_context


def _can_accept_recovered_provenance_key(
    key: str,
    arguments: Mapping[str, Any],
    public_context: Mapping[str, Any],
) -> bool:
    """Return whether recovered mangled metadata may fill this provenance key."""
    target_key = _ALIAS_TO_STRING_FIELD.get(key)
    if target_key is not None:
        aliases = _STRING_FIELDS[target_key]
        return (
            _first_text(arguments, aliases) is None
            and _first_text(public_context, aliases) is None
        )

    if key == "tool_surface":
        return (
            not _normalize_text_list(arguments.get("tool_surface"))
            and not _normalize_text_list(public_context.get("tool_surface"))
        )

    if key in _MAPPING_FIELDS:
        return (
            not isinstance(arguments.get(key), Mapping)
            and not isinstance(public_context.get(key), Mapping)
        )

    return False


def _public_provenance_context(
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    public_context = arguments.get("provenance_context")
    if isinstance(public_context, Mapping):
        return dict(public_context)
    return {}


def _first_text_by_precedence(
    arguments: Mapping[str, Any],
    public_context: Mapping[str, Any],
    keys: Sequence[str],
) -> Optional[str]:
    return _first_text(arguments, keys) or _first_text(public_context, keys)


def build_s22_write_context(
    arguments: Mapping[str, Any],
    *,
    meta: Optional[Any] = None,
    context_source: str,
    default_governance_mode: Optional[str] = None,
    episode_fork_kind: Optional[str] = None,
    identity_lineage_fork: Optional[bool] = None,
) -> dict[str, Any]:
    """Build a compact S22 context from explicit args plus request contextvars.

    ``episode_fork_kind`` and ``identity_lineage_fork`` accept server-side R6
    classification (see ``src.thread_identity.classify_episode_fork``). When
    provided they override any client-supplied values in ``arguments`` —
    fork-kind is a server-authoritative determination, not a client claim.
    """
    public_context = _public_provenance_context(arguments)
    context: dict[str, Any] = {}

    for target_key, aliases in _STRING_FIELDS.items():
        value = _first_text_by_precedence(arguments, public_context, aliases)
        if value is not None:
            context[target_key] = value

    tool_surface = _normalize_text_list(arguments.get("tool_surface"))
    if not tool_surface:
        tool_surface = _normalize_text_list(public_context.get("tool_surface"))
    if tool_surface:
        context["tool_surface"] = tool_surface

    for key in _MAPPING_FIELDS:
        value = arguments.get(key)
        if not isinstance(value, Mapping):
            value = public_context.get(key)
        if isinstance(value, Mapping):
            context[key] = dict(value)

    arg_lineage_fork = arguments.get("identity_lineage_fork")
    if arg_lineage_fork is None:
        arg_lineage_fork = public_context.get("identity_lineage_fork")
    if arg_lineage_fork is not None:
        context["identity_lineage_fork"] = _coerce_bool(arg_lineage_fork)

    if episode_fork_kind is not None:
        context["episode_fork_kind"] = episode_fork_kind
    if identity_lineage_fork is not None:
        context["identity_lineage_fork"] = bool(identity_lineage_fork)

    _merge_meta_defaults(context, meta)
    _merge_request_context_defaults(context)

    recovered_context = arguments.get("_recovered_s22_context")
    if (
        default_governance_mode
        and (context or isinstance(recovered_context, Mapping))
        and "governance_mode" not in context
    ):
        context["governance_mode"] = default_governance_mode
    _merge_recovered_context_defaults(context, recovered_context)

    # Do not persist an envelope containing only bookkeeping. The helper is
    # optional by design and should not create noisy empty context records.
    if not context:
        return {}

    return {
        "schema": SCHEMA_VERSION,
        "context_source": context_source,
        **context,
    }


def classify_fork_for_s22_context(
    meta: Optional[Any],
    agent_uuid: Optional[str],
) -> tuple[Optional[str], Optional[bool]]:
    """Classify the R6 fork kind for S22 persistence.

    Returns ``(None, None)`` when ``meta`` is absent so ``build_s22_write_context``
    omits the fields rather than stamping ``None``. The new-agent path
    intentionally falls through here: there is no metadata yet, the geometry is
    unknown, and any client-supplied ``identity_lineage_fork`` claim in
    ``arguments`` survives — that is acceptable because the next call (once the
    registry has the row) reclassifies and overrides.

    Used by both the ``process_agent_update`` write path (early stamp at
    ``prepare_unlocked_inputs`` plus a post-mutation re-stamp after
    ``execute_locked_update`` increments ``node_index``) and the
    ``knowledge.store`` write path. The post-mutation re-stamp prevents
    divergence with ``enrich_thread_identity``, which runs in the response
    pipeline at order=230 against the post-mutation ``node_index``.
    """
    if meta is None:
        return (None, None)
    from src.thread_identity import classify_episode_fork

    try:
        position = int(getattr(meta, "node_index", 1) or 1)
    except (TypeError, ValueError):
        position = 1
    parent_uuid = getattr(meta, "parent_agent_id", None)
    spawn_reason = getattr(meta, "spawn_reason", None)
    agent_uuid_for_fork = (
        agent_uuid
        or getattr(meta, "agent_uuid", None)
        or getattr(meta, "agent_id", None)
    )
    return classify_episode_fork(
        position,
        agent_uuid_for_fork,
        parent_uuid,
        spawn_reason,
    )


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


def _merge_recovered_context_defaults(
    context: dict[str, Any],
    recovered: Any,
) -> None:
    """Fill still-empty S22 slots from repaired mangled provenance metadata."""
    if not isinstance(recovered, Mapping):
        return

    for target_key, aliases in _STRING_FIELDS.items():
        if target_key in context:
            continue
        value = _first_text(recovered, aliases)
        if value is not None:
            context[target_key] = value

    if "tool_surface" not in context:
        tool_surface = _normalize_text_list(recovered.get("tool_surface"))
        if tool_surface:
            context["tool_surface"] = tool_surface

    for key in _MAPPING_FIELDS:
        if key in context:
            continue
        value = recovered.get(key)
        if isinstance(value, Mapping):
            context[key] = dict(value)

    if "identity_lineage_fork" not in context:
        value = recovered.get("identity_lineage_fork")
        if value is not None:
            context["identity_lineage_fork"] = _coerce_bool(value)


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
