"""Dependency-light workflow-alias schema policy.

This module is shared by production registration, ``describe_tool``, and the
standalone tool-surface audit. Keep it free of server/runtime imports so the
audit works in the repository's minimal development environment.
"""

from __future__ import annotations

import copy

from src.schema_brief import (
    BRIEF_BUDGET,
    DEFAULT_FIELD_DESCRIPTION_MODE,
    apply_field_description_mode,
)


# Subtraction is the compatibility-safe policy for established read aliases:
# every listed field belongs to another action and is never read by the pinned
# action or its helpers. Do not derive this list from ACTION_FIELDS alone: that
# discovery map can lag the handler (search's provenance/type/severity did).
ALIAS_SCHEMA_DROP = {
    "search_shared_memory": frozenset({
        "content",
        "summary",
        "details",
        "discovery_id",
        "supersedes",
        "supersedes_id",
        "superseded_by",
        "resolution_notes",
        "related_files",
        "response_to",
        "task_label",
        "task_outcome",
        "auto_link_related",
        "comparison_key",
        "closure_class",
        "closure_evidence",
        "confidence",
        "dry_run",
        "include_response_chain",
        "including_cold",
        "length",
        "max_chain_depth",
        "memory_context",
        "min_members",
        "top_n",
        "topic",
        "use_llm",
        "use_model",
    }),
}


# Keep-lists define task verbs whose public wire contract intentionally spans
# fewer fields than their implementation router. Names are router parameters;
# identity plumbing is restored separately below.
ALIAS_SCHEMA_KEEP = {
    "store_finding": frozenset({
        "summary",
        "details",
        "content",
        "discovery_type",
        "severity",
        "tags",
        "comparison_key",
        "memory_context",
        "task_label",
        "task_outcome",
    }),
    "update_finding": frozenset({
        "discovery_id",
        "status",
        "details",
        "content",
        "resolution_notes",
        "summary",
        "severity",
        "discovery_type",
        "tags",
        "superseded_by",
    }),
    "request_review": frozenset({
        "issue_description",
        "reason",
        "reasoning",
        "root_cause",
        "proposed_conditions",
        "use_brief_as_thesis",
    }),
}


# Overrides carry the same authoring shape as a Pydantic Field: `description`
# is the full text describe_tool serves, and an optional `brief` is the
# authored short form the advertised wire serves instead (src/schema_brief.py).
ALIAS_SCHEMA_PROPERTY_OVERRIDES = {
    "search_shared_memory": {
        "response_mode": {
            "default": "lean",
            "description": (
                "Friendly read-envelope mode. Defaults to lean: one-line result "
                "digests with a single relevance score and no repeated identity, "
                "detail previews, or score maps. Compact retains more diagnostics; "
                "full includes raw_governance with the complete result set."
            ),
            "brief": (
                "Read-envelope mode. Default lean: one-line digests; compact adds "
                "diagnostics, full adds raw_governance."
            ),
        },
        "include_details": {
            "description": (
                "Expand every result inline only with response_mode='full'. "
                "Compact/lean search suppresses detail serialization upstream "
                "and returns bounded previews; open one result with "
                "knowledge(action='details', discovery_id='...')."
            ),
            "brief": (
                "Expand results inline only with response_mode='full'; otherwise "
                "open one with knowledge(action='details')."
            ),
        },
    },
}


_ALIAS_ALWAYS_KEEP = frozenset({
    "agent_id",
    "client_session_id",
    "continuity_token",
})


def apply_alias_schema_property_overrides(
    alias_name: str,
    schema: dict,
    *,
    field_descriptions: str = DEFAULT_FIELD_DESCRIPTION_MODE,
    budget: int = BRIEF_BUDGET,
) -> None:
    """Apply documented property overrides in place when a property exists.

    These land on the already-registered wire schema, after the catalog's own
    trim has run, so they must observe the same field-description mode — an
    override is otherwise a hole in the contract that re-inflates exactly the
    aliases agents use most.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return
    for parameter, updates in ALIAS_SCHEMA_PROPERTY_OVERRIDES.get(
        alias_name, {}
    ).items():
        definition = properties.get(parameter)
        if isinstance(definition, dict):
            definition.update(
                apply_field_description_mode(
                    updates, field_descriptions, budget=budget
                )
            )


def build_alias_input_schema(
    alias_name: str,
    actual_schema: dict,
    *,
    inject_action: bool,
) -> dict:
    """Return the exact alias wire schema used by registration and discovery."""
    alias_schema = copy.deepcopy(actual_schema)
    if inject_action and alias_schema:
        properties = alias_schema.get("properties", {})
        properties.pop("action", None)
        keep = ALIAS_SCHEMA_KEEP.get(alias_name)
        if keep is not None:
            allowed = keep | _ALIAS_ALWAYS_KEEP
            dropped = frozenset(properties) - allowed
        else:
            dropped = ALIAS_SCHEMA_DROP.get(alias_name) or frozenset()
        for parameter in dropped:
            properties.pop(parameter, None)
        required = alias_schema.get("required", [])
        if "action" in required or dropped:
            alias_schema["required"] = [
                value
                for value in required
                if value != "action" and value not in dropped
            ]
    apply_alias_schema_property_overrides(alias_name, alias_schema)
    return alias_schema
