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
    resolve_brief_budget,
    resolve_field_description_mode,
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
        # Read only by other actions' handlers (2026-09-25): offset by
        # details, epoch_scope by list, scope by audit, and the three
        # promotion receipt fields by promote. The search parser reads none
        # of them, directly or through request.arguments.
        "offset",
        "epoch_scope",
        "scope",
        "evidence_ids",
        "verification_basis",
        "decision_standard",
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


# Two values repeated from runtime modules rather than imported, because this
# module stays free of runtime imports so the standalone tool-surface audit can
# load it. tests/test_mcp_schema_parity.py pins each to its source so neither can
# drift: the regex to schemas/core.UNIT_INTERVAL_STRING_PATTERN, the levels to
# support/param_normalization.NAMED_LEVELS.
UNIT_INTERVAL_STRING_PATTERN = r"^(0(\.\d+)?|1(\.0+)?|\.\d+)$"

# The named levels sync_state's complexity normalizer maps into 0-1.
SYNC_STATE_COMPLEXITY_NAMED_LEVELS = (
    "complex",
    "critical",
    "high",
    "low",
    "medium",
    "minimal",
    "moderate",
    "simple",
    "trivial",
    "very_high",
)

# Overrides carry the same authoring shape as a Pydantic Field: `description`
# is the full text describe_tool serves, and an optional `brief` is the
# authored short form the advertised wire serves instead (src/schema_brief.py).
ALIAS_SCHEMA_PROPERTY_OVERRIDES = {
    # The canonical process_agent_update advertises complexity as a number in
    # 0-1 or a numeric string in 0-1, because that is all the canonical model
    # accepts. sync_state accepts more: its normalizer maps a named level to a
    # number BEFORE validation, so the named levels are advertised here as well.
    # This override replaces the whole anyOf, so it repeats the two canonical
    # branches rather than adding to them.
    #
    # The named levels nest inside the one string branch rather than forming a
    # second one. The MCP transport builds its argument model from this schema,
    # one Python type per top-level branch, so a second string branch gave that
    # model a second `str` member, and a list or object was then refused with
    # the same string error twice. Nested, the top-level branches stay number,
    # string and null: the union the transport built before, error for error.
    #
    # Deliberately narrow, so the advertised schema stays a subset of what is
    # accepted. `complexity` only: nothing normalizes `confidence`, so it keeps
    # the canonical schema. And the named-level enum only: the normalizer also
    # accepts {"value": N, "scale": M}, but its `value <= scale` rule cannot be
    # written in JSON Schema, so advertising that object would call refused
    # values legal.
    "sync_state": {
        "complexity": {
            "anyOf": [
                {"type": "number", "minimum": 0.0, "maximum": 1.0},
                {
                    "type": "string",
                    "anyOf": [
                        {"type": "string", "pattern": UNIT_INTERVAL_STRING_PATTERN},
                        {"type": "string", "enum": list(SYNC_STATE_COMPLEXITY_NAMED_LEVELS)},
                    ],
                },
                {"type": "null"},
            ],
        },
    },
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
        # The router's text for these describes the store action (discovery_type
        # lists every writable type) or names several actions at once. On the
        # search alias each is only a filter.
        "discovery_type": {
            "description": (
                "Filter results by discovery type, e.g. bug_found, insight, "
                "architectural_decision."
            ),
            "brief": "Filter by discovery type, e.g. bug_found.",
        },
        "tags": {
            "description": "Exact any-of tag filter, applied in every search mode.",
            "brief": "Exact any-of tag filter.",
        },
        "severity": {
            "description": "Filter by severity: low, medium, high, critical.",
        },
    },
    # The router's agent_id text describes the read actions ("Filter by
    # agent"). On the store alias it names the writer, and an unbound call
    # honors it verbatim for a low/medium write (params_step keeps an explicit
    # agent_id when no session is bound), so the text steers callers away
    # from setting it. High/critical severity is refused unless the session is
    # bound to the writer (_authorize_store_discovery -> verify_agent_ownership).
    "store_finding": {
        "agent_id": {
            "description": (
                "Leave unset; the bound session is the writer. high/critical "
                "severity needs a session bound to the writing agent."
            ),
            "brief": "Leave unset; the bound session is the writer.",
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
    field_descriptions: str | None = None,
    budget: int | None = None,
) -> dict:
    """Return the exact alias wire schema used by registration and discovery.

    The property overrides land in the same field-description mode as the
    catalog they are applied to — ``UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS``
    unless a mode is passed — so the schema built here is the one every
    transport advertises. The ``/mcp/`` registrar copies it onto the registered
    tool rather than re-applying the overrides to FastMCP's regenerated schema;
    until 2026-09-11 it did the latter, which was the only reason the two
    surfaces could disagree on an alias in a non-default mode.
    """
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
    apply_alias_schema_property_overrides(
        alias_name,
        alias_schema,
        field_descriptions=resolve_field_description_mode(field_descriptions),
        budget=resolve_brief_budget(budget),
    )
    return alias_schema
