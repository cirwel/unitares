"""The advertised parameter descriptions are abridged; describe_tool is not.

The wire pays for every parameter description on every ``tools/list``, in
every session, for tools most agents never call. Measured 2026-09-08 before
this trim: ``sync_state`` alone cost ~2,000 tokens and the five-tool
``minimal`` profile ~5,470. A profile cut does not touch that — it removes
names, not words.

What these tests hold:

  1. The trim is real (the advertised catalog shrinks, and no advertised
     description runs past the budget unless a human authored it).
  2. The trim is only a trim (no parameter, type, default or requiredness
     moves, and ``describe_tool`` still serves every word).
  3. The escape hatch is exact (``full`` reproduces the pre-trim surface
     byte-for-byte).
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
    apply_field_description_mode,
    brief_text,
    resolve_brief_budget,
    resolve_field_description_mode,
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
