"""The advertised parameter descriptions are abridged; describe_tool is not.

The wire pays for every parameter description on every ``tools/list``, in
every session, for tools most agents never call. Measured 2026-09-08 before
this trim: ``sync_state`` alone cost ~2,000 tokens and the five-tool
``minimal`` profile ~5,470. A profile cut does not touch that — it removes
names, not words.

What these tests hold:

  1. The trim is real (the advertised catalog shrinks, and no advertised
     description runs past the budget unless a human authored it).
  2. The trim is only a trim (no parameter, type, non-null default or
     requiredness moves, and ``describe_tool`` still serves every word).
  3. The escape hatch is exact (``full`` reproduces the pre-trim surface
     byte-for-byte).

The same three hold for the other half of the advertised schema: the
Pydantic ``title`` keywords, dropped since 2026-09-08. Nobody authored those —
they are the model's class name at the root (``OnboardParams``) and a
titleized echo of the key on each field (``client_session_id`` -> "Client
Session Id") — so unlike a description there is no surface on which keeping
one explains anything, and ``describe_tool`` drops them too. Worth 10.8% of
every profile.
"""

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import src.mcp_handlers  # noqa: F401  (populates the decorator registry)

from src.schema_brief import (
    BRIEF_BUDGET,
    BRIEF_KEY,
    DEFAULT_FIELD_DESCRIPTION_MODE,
    DEFAULT_NULL_DEFAULT_MODE,
    DEFAULT_PROPERTY_TITLE_MODE,
    apply_field_description_mode,
    apply_null_default_mode,
    apply_property_title_mode,
    brief_text,
    resolve_brief_budget,
    resolve_field_description_mode,
    resolve_null_default_mode,
    resolve_property_title_mode,
)
from src.mcp_compat import get_tool_input_schema
from src.tool_schemas import advertised_input_schema, get_pydantic_schemas, get_tool_definitions

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def _schemas(field_descriptions):
    return {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in get_tool_definitions(field_descriptions=field_descriptions)
    }


def _shape(schema):
    """Everything about a schema except the prose."""
    return apply_field_description_mode(schema, "off")


def _descriptions(schema):
    out = {}
    for name, definition in (schema.get("properties") or {}).items():
        if isinstance(definition, dict) and isinstance(definition.get("description"), str):
            out[name] = definition["description"]
    return out


class TestBriefText:
    def test_text_within_budget_is_returned_whole(self):
        text = "Quality score 0.0 (worst) to 1.0 (best). Inferred from type if omitted."
        assert brief_text(text) == text

    def test_first_sentence_survives_and_the_rest_does_not(self):
        text = (
            "Agent-facing response shape. Prefer 'auto' or 'compact' for routine "
            "check-ins. 'mirror' returns actionable self-awareness signals. 'full' "
            "returns the complete payload."
        )
        assert brief_text(text) == "Agent-facing response shape."

    def test_an_abbreviation_is_not_a_sentence_end(self):
        # "The decision the agent took (e.g." was the real regression: the last
        # word before the period is "(e.g.", whose leading paren hid it from
        # the abbreviation table.
        text = (
            "The decision the agent took (e.g. 'proceed', 'pause'). Used by "
            "sequential calibration tracking; for test_passed/test_failed "
            "defaults to 'proceed'."
        )
        assert brief_text(text) == "The decision the agent took (e.g. 'proceed', 'pause')."

    def test_a_too_short_first_sentence_takes_the_next_break(self):
        text = (
            "Required for action=store. One of: architectural_decision, learning, "
            "pattern, bug_fix, refactoring, documentation."
        )
        assert brief_text(text).startswith("Required for action=store. One of:")

    def test_a_runaway_sentence_is_cut_on_a_word_boundary_and_marked(self):
        text = "word " * 100
        out = brief_text(text)
        assert len(out) <= BRIEF_BUDGET + 2  # the " …" marker
        assert out.endswith("…")
        assert not out.endswith("wor …")

    def test_empty_text_stays_empty(self):
        assert brief_text("") == ""
        assert brief_text(None) == ""


class TestApplyFieldDescriptionMode:
    def test_authored_brief_wins_and_never_reaches_a_caller(self):
        node = {
            "properties": {
                "x": {
                    "type": "string",
                    "description": "A very long authored description. With a second sentence.",
                    BRIEF_KEY: "The authored short form.",
                }
            }
        }
        out = apply_field_description_mode(node, "brief")
        assert out["properties"]["x"]["description"] == "The authored short form."
        assert BRIEF_KEY not in out["properties"]["x"]

    def test_full_mode_keeps_the_text_and_still_drops_the_authoring_key(self):
        node = {"properties": {"x": {"description": "Full. Text.", BRIEF_KEY: "Short."}}}
        out = apply_field_description_mode(node, "full")
        assert out["properties"]["x"]["description"] == "Full. Text."
        assert BRIEF_KEY not in out["properties"]["x"]

    def test_off_mode_removes_descriptions_entirely(self):
        node = {"properties": {"x": {"type": "string", "description": "Full. Text."}}}
        out = apply_field_description_mode(node, "off")
        assert out["properties"]["x"] == {"type": "string"}

    def test_a_parameter_named_brief_survives(self):
        # `consult` really does take a parameter called `brief`. A key-name walk
        # deleted it from the advertised schema while leaving it in `required`.
        node = {
            "properties": {"brief": {"type": "string", "description": "The brief."}},
            "required": ["brief"],
        }
        out = apply_field_description_mode(node, "brief")
        assert "brief" in out["properties"]
        assert out["required"] == ["brief"]

    def test_a_default_that_looks_like_a_schema_is_left_alone(self):
        node = {"properties": {"x": {"default": {"description": "data, not documentation"}}}}
        out = apply_field_description_mode(node, "off")
        assert out["properties"]["x"]["default"] == {"description": "data, not documentation"}

    def test_the_input_is_not_mutated(self):
        node = {"properties": {"x": {"description": "Full. Text.", BRIEF_KEY: "Short."}}}
        before = json.dumps(node, sort_keys=True)
        apply_field_description_mode(node, "brief")
        assert json.dumps(node, sort_keys=True) == before


class TestModeResolution:
    def test_the_default_is_brief(self, monkeypatch):
        monkeypatch.delenv("UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS", raising=False)
        monkeypatch.delenv("UNITARES_TOOL_SCHEMA_STRIP_FIELD_DESCRIPTIONS", raising=False)
        assert resolve_field_description_mode() == "brief"
        assert DEFAULT_FIELD_DESCRIPTION_MODE == "brief"

    def test_the_legacy_strip_flag_still_means_off(self, monkeypatch):
        monkeypatch.delenv("UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS", raising=False)
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_STRIP_FIELD_DESCRIPTIONS", "1")
        assert resolve_field_description_mode() == "off"

    def test_an_unknown_mode_falls_back_to_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS", "terse")
        assert resolve_field_description_mode() == "brief"
        assert "terse" in caplog.text

    def test_a_non_integer_budget_falls_back(self, monkeypatch):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_BRIEF_BUDGET", "wide")
        assert resolve_brief_budget() == BRIEF_BUDGET


class TestAdvertisedSurface:
    def test_the_advertised_catalog_is_smaller_than_the_authored_one(self):
        brief = json.dumps(_schemas("brief"), sort_keys=True)
        full = json.dumps(_schemas("full"), sort_keys=True)
        assert len(brief) < len(full)

    def test_the_check_in_stays_under_its_pre_trim_cost(self):
        # The regression this whole module exists to prevent: sync_state's
        # implementation schema creeping back to the ~9,200 characters
        # (~2,300 tokens) it cost on 2026-09-08.
        schema = _schemas("brief")["process_agent_update"]
        assert len(json.dumps(schema)) < 7500

    def test_no_advertised_description_exceeds_the_budget_unless_authored(self):
        authored = set()
        for tool_name, model in get_pydantic_schemas().items():
            for name, definition in (model.model_json_schema().get("properties") or {}).items():
                if isinstance(definition, dict) and BRIEF_KEY in definition:
                    authored.add((tool_name, name))

        for tool_name, schema in _schemas("brief").items():
            for name, description in _descriptions(schema).items():
                if (tool_name, name) in authored:
                    continue
                assert len(description) <= BRIEF_BUDGET + 2, (
                    f"{tool_name}.{name} is {len(description)} chars"
                )

    def test_trimming_moves_no_parameter_name_type_default_or_requiredness(self):
        brief = _schemas("brief")
        full = _schemas("full")
        assert set(brief) == set(full)
        for name in brief:
            assert _shape(brief[name]) == _shape(full[name]), name

    def test_full_mode_reproduces_the_authored_text_exactly(self):
        for tool_name, schema in _schemas("full").items():
            model = get_pydantic_schemas().get(tool_name)
            if model is None:
                continue
            expected = advertised_input_schema(tool_name, model.model_json_schema())
            assert schema == expected, tool_name

    def test_no_advertised_schema_leaks_the_authoring_key(self):
        def schema_nodes(node, in_property_map=False):
            """Yield every schema node, skipping parameter *names*."""
            if isinstance(node, dict):
                if not in_property_map:
                    yield node
                for key, value in node.items():
                    if in_property_map:
                        yield from schema_nodes(value)
                    else:
                        yield from schema_nodes(
                            value,
                            in_property_map=key in ("properties", "$defs", "definitions"),
                        )
            elif isinstance(node, list):
                for item in node:
                    yield from schema_nodes(item)

        for mode in ("brief", "full", "off"):
            for tool_name, schema in _schemas(mode).items():
                for node in schema_nodes(schema):
                    assert BRIEF_KEY not in node, f"{tool_name} ({mode})"

    def test_describe_tool_still_serves_every_word(self):
        model = get_pydantic_schemas()["process_agent_update"]
        described = advertised_input_schema("process_agent_update", model.model_json_schema())
        authored = model.model_json_schema()["properties"]

        for name, definition in described["properties"].items():
            if "description" not in definition:
                continue
            assert definition["description"] == authored[name]["description"], name

    def test_the_instructions_string_says_the_catalog_is_abridged(self):
        from src.tool_modes import build_server_instructions

        for mode in ("minimal", "standard", "lite", "full"):
            instructions = build_server_instructions(mode)
            assert "abridged" in instructions
            assert "describe_tool" in instructions


def _title_nodes(node, path="$"):
    """Every (path, title) pair a caller would receive."""
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):
            found.append((path, node["title"]))
        for key, value in node.items():
            if key in ("default", "const", "enum", "examples", "example"):
                continue
            if key in ("properties", "$defs", "definitions", "patternProperties",
                       "dependentSchemas") and isinstance(value, dict):
                for name, sub in value.items():
                    found += _title_nodes(sub, f"{path}.{key}.{name}")
                continue
            found += _title_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _title_nodes(item, f"{path}[{index}]")
    return found


def _null_default_nodes(node, path="$"):
    """Every schema-node path carrying ``default: null``."""
    found = []
    if isinstance(node, dict):
        if "default" in node and node["default"] is None:
            found.append(path)
        for key, value in node.items():
            if key in ("default", "const", "enum", "examples", "example"):
                continue
            if key in (
                "properties",
                "$defs",
                "definitions",
                "patternProperties",
                "dependentSchemas",
            ) and isinstance(value, dict):
                for name, sub in value.items():
                    found += _null_default_nodes(sub, f"{path}.{key}.{name}")
                continue
            found += _null_default_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _null_default_nodes(item, f"{path}[{index}]")
    return found


_TEST_SUBSCHEMA_MAPS = (
    "properties",
    "$defs",
    "definitions",
    "patternProperties",
    "dependentSchemas",
    "dependencies",
)
_TEST_SUBSCHEMA_VALUES = (
    "additionalItems",
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_TEST_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _schema_nodes(node, path="$"):
    """Yield actual schema nodes, excluding defaults and extension data."""
    if not isinstance(node, (dict, list)):
        return
    yield path, node
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _schema_nodes(item, f"{path}[{index}]")
        return
    for key in _TEST_SUBSCHEMA_MAPS:
        value = node.get(key)
        if isinstance(value, dict):
            for name, subschema in value.items():
                yield from _schema_nodes(subschema, f"{path}.{key}.{name}")
    for key in _TEST_SUBSCHEMA_VALUES:
        if key in node:
            yield from _schema_nodes(node[key], f"{path}.{key}")
    for key in _TEST_SUBSCHEMA_LISTS:
        value = node.get(key)
        if isinstance(value, list):
            yield from _schema_nodes(value, f"{path}.{key}")


def _allows_explicit_null(schema):
    """Whether the local schema shape admits JSON null."""
    if schema is True:
        return True
    if schema is False or not isinstance(schema, dict):
        return False
    declared_type = schema.get("type")
    if declared_type == "null":
        return True
    if isinstance(declared_type, list) and "null" in declared_type:
        return True
    if "const" in schema:
        return schema["const"] is None
    if "enum" in schema:
        return None in schema["enum"]
    if "anyOf" in schema or "oneOf" in schema:
        return any(
            _allows_explicit_null(branch)
            for keyword in ("anyOf", "oneOf")
            for branch in schema.get(keyword, [])
        )
    if "allOf" in schema:
        return all(_allows_explicit_null(branch) for branch in schema["allOf"])
    return declared_type is None and "$ref" not in schema


class TestPropertyTitles:
    """A Pydantic ``title`` is a titleized echo of the key. It is not content.

    Measured 2026-09-08 across the whole advertised roster: dropping the
    property titles and the root model-class titles takes 10.8% off every
    profile (`standard` 52,612 -> 47,047 B), which is more than any profile cut
    achieved without also removing a capability.
    """

    def test_no_advertised_schema_carries_a_title(self):
        for tool_name, schema in _schemas("brief").items():
            titles = _title_nodes(schema)
            assert not titles, f"{tool_name} still advertises titles: {titles[:3]}"

    def test_describe_tool_drops_them_too(self):
        # Unlike a description, a title has no fuller authored form to serve:
        # "Client Session Id" IS the key. There is no surface on which
        # repeating it back explains anything.
        model = get_pydantic_schemas()["process_agent_update"]
        described = advertised_input_schema(
            "process_agent_update", model.model_json_schema()
        )
        assert not _title_nodes(described)

    def test_keep_reproduces_the_pre_strip_surface_exactly(self, monkeypatch):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "keep")
        kept = _schemas("brief")
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "strip")
        stripped = _schemas("brief")

        assert any(_title_nodes(schema) for schema in kept.values())
        assert json.dumps(kept, sort_keys=True) != json.dumps(stripped, sort_keys=True)
        for name, schema in kept.items():
            assert apply_property_title_mode(schema, "strip") == stripped[name], name

    def test_stripping_moves_no_parameter_name_type_default_or_requiredness(
        self, monkeypatch
    ):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "keep")
        kept = _schemas("brief")
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "strip")
        stripped = _schemas("brief")

        assert set(kept) == set(stripped)
        for name in kept:
            before = apply_property_title_mode(_shape(kept[name]), "strip")
            assert before == apply_property_title_mode(_shape(stripped[name]), "strip"), name
            assert kept[name].get("required") == stripped[name].get("required"), name

    def test_a_parameter_named_title_survives(self):
        """The trap the structural walk is written against.

        Under ``properties`` the keys are caller-chosen parameter names, so a
        tool that ships a parameter called ``title`` must keep it while the
        ``title`` KEYWORD on the schema node goes. No tool ships one today,
        which is exactly why this is asserted rather than assumed — the
        equivalent bug for ``brief`` reached the surface once already
        (see _SUBSCHEMA_MAPS in src/schema_brief.py).
        """
        schema = {
            "title": "SomeParams",
            "type": "object",
            "properties": {
                "title": {"type": "string", "title": "Title", "description": "A real parameter."},
                "body": {"type": "string", "title": "Body"},
            },
            "required": ["title"],
        }
        out = apply_property_title_mode(schema, "strip")

        assert "title" in out["properties"], "the PARAMETER named title was deleted"
        assert out["required"] == ["title"]
        assert "title" not in out, "the root title KEYWORD survived"
        assert "title" not in out["properties"]["title"], "the parameter's own title keyword survived"
        assert out["properties"]["title"]["description"] == "A real parameter."

    def test_a_default_that_contains_a_title_is_caller_data_and_survives(self):
        # `default` is a value, not documentation: a tool whose parameter
        # defaults to {"title": "..."} must receive it back intact.
        schema = {
            "type": "object",
            "properties": {
                "payload": {"type": "object", "title": "Payload",
                            "default": {"title": "keep me"}},
            },
        }
        out = apply_property_title_mode(schema, "strip")
        assert out["properties"]["payload"]["default"] == {"title": "keep me"}
        assert "title" not in out["properties"]["payload"]

    def test_the_default_is_strip(self, monkeypatch):
        monkeypatch.delenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", raising=False)
        assert resolve_property_title_mode() == "strip"
        assert DEFAULT_PROPERTY_TITLE_MODE == "strip"

    def test_an_unknown_mode_falls_back_to_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "terse")
        assert resolve_property_title_mode() == "strip"
        assert "terse" in caplog.text

    def test_the_advertised_surface_actually_got_smaller(self, monkeypatch):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "keep")
        kept = len(json.dumps(_schemas("brief"), sort_keys=True))
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "strip")
        stripped = len(json.dumps(_schemas("brief"), sort_keys=True))
        # Measured 10.8% on 2026-09-08; assert a floor well under it so the
        # test fails on a regression, not on ordinary schema churn.
        assert stripped < kept * 0.95


class TestNullDefaults:
    """A null default annotates an optional field; it does not validate it."""

    def test_no_advertised_schema_carries_a_null_default(self):
        for tool_name, schema in _schemas("brief").items():
            null_defaults = _null_default_nodes(schema)
            assert not null_defaults, (
                f"{tool_name} still advertises null defaults: {null_defaults[:3]}"
            )

    def test_keep_reproduces_the_pydantic_annotations_exactly(self, monkeypatch):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "keep")
        kept = _schemas("brief")
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "strip")
        stripped = _schemas("brief")

        assert any(_null_default_nodes(schema) for schema in kept.values())
        assert json.dumps(kept, sort_keys=True) != json.dumps(stripped, sort_keys=True)
        for name, schema in kept.items():
            assert apply_null_default_mode(schema, "strip") == stripped[name], name

    def test_non_null_defaults_and_validation_shape_survive(self):
        schema = {
            "type": "object",
            "properties": {
                "optional": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
                "bounded": {"type": "integer", "minimum": 1, "default": 5},
            },
            "required": ["bounded"],
        }
        out = apply_null_default_mode(schema, "strip")

        assert "default" not in out["properties"]["optional"]
        assert out["properties"]["optional"]["anyOf"] == schema["properties"]["optional"]["anyOf"]
        assert out["properties"]["bounded"] == schema["properties"]["bounded"]
        assert out["required"] == schema["required"]

    def test_every_catalog_null_default_is_optional_and_nullable(self, monkeypatch):
        """Hold the premise that makes this annotation safely redundant."""
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "keep")
        total = 0
        for tool_name, schema in _schemas("brief").items():
            every_default = {
                path
                for path, node in _schema_nodes(schema)
                if isinstance(node, dict)
                and "default" in node
                and node["default"] is None
            }
            property_defaults = set()
            for path, node in _schema_nodes(schema):
                if not isinstance(node, dict):
                    continue
                required = set(node.get("required") or ())
                for name, body in (node.get("properties") or {}).items():
                    if not (
                        isinstance(body, dict)
                        and "default" in body
                        and body["default"] is None
                    ):
                        continue
                    field_path = f"{path}.properties.{name}"
                    property_defaults.add(field_path)
                    assert name not in required, f"{tool_name}:{field_path} is required"
                    assert _allows_explicit_null(body), (
                        f"{tool_name}:{field_path} defaults to null but rejects null"
                    )
            assert every_default == property_defaults, (
                f"{tool_name} has a null default outside a property schema: "
                f"{sorted(every_default - property_defaults)}"
            )
            total += len(every_default)
        assert total > 0

    def test_null_inside_caller_default_data_survives(self):
        schema = {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "default": {"default": None, "value": None},
                }
            },
        }
        out = apply_null_default_mode(schema, "strip")
        assert out["properties"]["payload"]["default"] == {
            "default": None,
            "value": None,
        }

    def test_null_inside_extension_metadata_survives(self):
        schema = {
            "type": "object",
            "x-ui": {"default": None, "nested": {"default": None}},
            "properties": {
                "optional": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "x-ui": {"default": None},
                }
            },
        }
        out = apply_null_default_mode(schema, "strip")
        assert out["x-ui"] == schema["x-ui"]
        assert out["properties"]["optional"]["x-ui"] == {"default": None}
        assert "default" not in out["properties"]["optional"]

    def test_the_default_is_strip(self, monkeypatch):
        monkeypatch.delenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", raising=False)
        assert resolve_null_default_mode() == "strip"
        assert DEFAULT_NULL_DEFAULT_MODE == "strip"

    def test_an_unknown_mode_falls_back_to_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "terse")
        assert resolve_null_default_mode() == "strip"
        assert "terse" in caplog.text

    def test_the_advertised_surface_actually_got_smaller(self, monkeypatch):
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "keep")
        kept = len(json.dumps(_schemas("brief"), sort_keys=True))
        monkeypatch.setenv("UNITARES_TOOL_SCHEMA_NULL_DEFAULTS", "strip")
        stripped = len(json.dumps(_schemas("brief"), sort_keys=True))
        assert stripped < kept * 0.98
