"""Per-action views of a consolidated router's flat wire schema.

A router advertises ONE flat schema covering every action it routes, because
the MCP wrapper builds a tool's argument model from top-level ``properties``
alone -- a ``oneOf`` per action would not survive registration. That is the
right wire shape and this module does not change it. What it adds is the
missing half of the contract: which of those flat parameters belong to which
action.

Without that, ``knowledge`` presents 50 parameters for every one of its 12
actions and the caller guesses. Most fields already SAY which action they
serve, in prose, at the end of a description -- so the fact was known, just
not machine-readable, and prose cannot be checked against the routing table.

Each router's parameter model declares it instead, as ``ACTION_FIELDS``, next
to the fields themselves:

    class KnowledgeParams(AgentIdentityMixin):
        ACTION_FIELDS: ClassVar[Mapping[str, tuple[str, ...]]] = {
            "search": ("query", "limit", ...),
            ...
        }

``tests/test_router_action_fields.py`` holds every declaration to the routing
table: its keys must be exactly the router's actions, every field it names
must exist on the model, and every model field must be either classified or
common. A field naming an action that does not route, or an action that
routes with no fields declared, fails there -- which is how the dead
``dialectic`` ``vote`` parameter (documented "for action=vote" against a
router that has no ``vote`` action, and read by no handler) was found.

The result is consumed by ``describe_tool(tool_name=..., action=...)``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

# Parameters that belong to every action of every router, so no declaration
# names them. Identity and session proof are injected or carried by the
# caller on any call; `action`/`op` select the action itself.
#
# Deliberately NOT a place to park an unclassified field: adding a name here
# asserts it is meaningful on every action the router routes. The test treats
# anything else undeclared as unclassified and fails.
COMMON_ROUTER_FIELDS: frozenset[str] = frozenset({
    "action",
    "op",
    "agent_id",
    "client_session_id",
    "continuity_token",
})


def wire_field_names(model: Any) -> Tuple[str, ...]:
    """The model's field names AS PUBLISHED, in declaration order.

    A field with an alias reaches the wire under the alias (``ConfigParams``
    publishes ``validate_params`` as ``validate``), and a caller sends that
    name. ``ACTION_FIELDS`` therefore names published parameters, not internal
    attributes, so it can be compared to the advertised schema directly.
    """
    published = []
    for name, field in (getattr(model, "model_fields", {}) or {}).items():
        published.append(getattr(field, "alias", None) or name)
    return tuple(published)


def declared_action_fields(model: Any) -> Optional[Mapping[str, Tuple[str, ...]]]:
    """The model's ``ACTION_FIELDS`` declaration, or None if it has none.

    Read off the class rather than an instance so it works on the schema
    registry, which holds classes.
    """
    declared = getattr(model, "ACTION_FIELDS", None)
    if not isinstance(declared, Mapping):
        return None
    return declared


def fields_for_action(
    model: Any,
    action: str,
    *,
    include_common: bool = True,
) -> Optional[Tuple[str, ...]]:
    """Field names meaningful for ``action``, or None if not declared.

    Returns the action's own fields plus the common ones, in the model's own
    field order so a narrowed schema reads like the full one.
    """
    declared = declared_action_fields(model)
    if declared is None:
        return None
    own = declared.get((action or "").lower())
    if own is None:
        return None
    wanted = set(own)
    if include_common:
        wanted |= COMMON_ROUTER_FIELDS
    order = list(wire_field_names(model))
    ordered = [f for f in order if f in wanted]
    # A declared name missing from model_fields is a drift the test catches;
    # keep it in the output rather than silently dropping the evidence.
    ordered += [f for f in own if f not in order]
    return tuple(ordered)


def narrow_schema_to_action(
    schema: Mapping[str, Any],
    model: Any,
    action: str,
) -> Optional[Dict[str, Any]]:
    """A copy of ``schema`` restricted to the parameters ``action`` uses.

    Returns None when the model declares no action map or does not know the
    action, so a caller can fall back to the full schema rather than show an
    empty one. ``required`` is recomputed against the kept fields, and
    ``action`` is pinned to the requested value so the narrowed schema is a
    faithful description of that one call.
    """
    keep = fields_for_action(model, action)
    if keep is None:
        return None
    properties = dict(schema.get("properties") or {})
    kept = {name: properties[name] for name in keep if name in properties}
    narrowed: Dict[str, Any] = {
        key: value
        for key, value in schema.items()
        if key not in ("properties", "required")
    }
    narrowed["properties"] = kept
    required: Sequence[str] = schema.get("required") or ()
    still_required = [name for name in required if name in kept]
    if still_required:
        narrowed["required"] = still_required
    if "action" in kept:
        pinned = dict(kept["action"])
        pinned["const"] = action
        kept["action"] = pinned
    return narrowed
