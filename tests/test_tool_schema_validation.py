"""
Tool schema and registry validation tests.

Validates structural correctness of tool schemas, handler registry alignment,
alias integrity, and cross-registry consistency without requiring any backend
services or mocking.
"""

import re
import sys
import pytest
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.tool_schemas import (
    get_tool_definitions,
    get_pydantic_schemas,
    TOOL_ORDER,
)
from src.mcp_handlers.decorators import (
    get_tool_registry,
    list_registered_tools,
    _TOOL_DEFINITIONS,
)
from src.mcp_handlers.tool_stability import (
    _TOOL_ALIASES,
    resolve_tool_alias,
    list_all_aliases,
)


# ============================================================================
# Registry Consistency
# ============================================================================

class TestRegistryConsistency:
    """Validate the handler registry from decorators.py."""

    def test_registry_is_non_empty(self):
        """Sanity check: the tool registry should contain registered tools."""
        registry = get_tool_registry()
        assert len(registry) > 0, "Tool registry is empty -- decorators may not have run"

    def test_every_handler_is_callable(self):
        """Every handler value in the registry must be a non-None callable."""
        registry = get_tool_registry()
        for name, handler in registry.items():
            assert handler is not None, f"Handler for '{name}' is None"
            assert callable(handler), f"Handler for '{name}' is not callable"

    def test_no_duplicate_registrations(self):
        """Tool names in _TOOL_DEFINITIONS must be unique (dict enforces this,
        but verify name field matches dict key)."""
        for key, td in _TOOL_DEFINITIONS.items():
            assert td.name == key, (
                f"ToolDefinition key '{key}' doesn't match its name field '{td.name}'"
            )

    def test_handler_names_follow_convention(self):
        """Tool names should be lowercase with underscores (snake_case)."""
        registry = get_tool_registry()
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        violations = [name for name in registry if not pattern.match(name)]
        assert not violations, f"Tool names violating snake_case convention: {violations}"

    def test_list_registered_tools_returns_strings(self):
        """list_registered_tools() should return a list of non-empty strings."""
        tools = list_registered_tools()
        assert isinstance(tools, list), "list_registered_tools() should return a list"
        assert len(tools) > 0, "list_registered_tools() returned empty list"
        for t in tools:
            assert isinstance(t, str), f"Expected str, got {type(t)}: {t}"
            assert len(t) > 0, "list_registered_tools() returned empty string"

    def test_list_registered_tools_sorted(self):
        """list_registered_tools() should return sorted names."""
        tools = list_registered_tools()
        assert tools == sorted(tools), "list_registered_tools() should be sorted"

    def test_list_registered_tools_with_hidden(self):
        """Including hidden tools should return >= the default count."""
        default = list_registered_tools()
        with_hidden = list_registered_tools(include_hidden=True)
        assert len(with_hidden) >= len(default), (
            "Including hidden tools should not reduce the count"
        )


# ============================================================================
# TOOL_ORDER <-> Pydantic schema sync (silent-drop drift gate)
# ============================================================================

class TestToolOrderSchemaSync:
    """Hard gate against the silent tool-registration footgun.

    ``get_tool_definitions()`` iterates ``TOOL_ORDER``; an entry with no
    matching Pydantic ``*Params`` schema is ``print``-warned and ``continue``d,
    so the tool silently vanishes from the advertised MCP surface for every
    client — no exception, no CI failure, just a missing tool. The structural
    tests below this class only inspect what survived that filter, so they
    cannot catch the drop. This class asserts the invariant at the source.
    """

    def test_every_tool_order_entry_has_a_schema(self):
        """Every name in TOOL_ORDER must resolve to a Pydantic schema.

        A missing schema means the tool is silently dropped from the MCP
        registry at boot (tool_schemas.py:get_tool_definitions warns + skips).
        """
        schemas = get_pydantic_schemas()
        missing = [name for name in TOOL_ORDER if name not in schemas]
        assert not missing, (
            "TOOL_ORDER entries with no Pydantic *Params schema will be "
            "SILENTLY DROPPED from the MCP tool surface at boot "
            f"(tool_schemas.py prints a warning and continues): {missing}. "
            "Add the matching *Params model in src/mcp_handlers/schemas/, or "
            "remove the entry from TOOL_ORDER."
        )

    def test_no_duplicate_tool_order_entries(self):
        """TOOL_ORDER must have no duplicate names (ordering/merge drift guard)."""
        seen = set()
        dupes = sorted({n for n in TOOL_ORDER if n in seen or seen.add(n)})
        assert not dupes, f"Duplicate entries in TOOL_ORDER: {dupes}"

    def test_every_tool_order_entry_is_advertised(self):
        """Round-trip: every TOOL_ORDER name must survive into the built
        tool list. Catches a drop from any cause, not just a missing schema."""
        advertised = {t.name for t in get_tool_definitions()}
        dropped = [name for name in TOOL_ORDER if name not in advertised]
        assert not dropped, (
            f"TOOL_ORDER entries that did not reach get_tool_definitions() "
            f"output (silently dropped at boot): {dropped}"
        )


# ============================================================================
# Schema Structure
# ============================================================================

class TestSchemaStructure:
    """Validate the Tool objects returned by get_tool_definitions()."""

    @pytest.fixture(scope="class")
    def full_tools(self):
        return get_tool_definitions(verbosity="full")

    @pytest.fixture(scope="class")
    def short_tools(self):
        return get_tool_definitions(verbosity="short")

    def test_tool_definitions_returns_non_empty_list(self, full_tools):
        assert isinstance(full_tools, list)
        assert len(full_tools) > 0, "get_tool_definitions() returned empty list"

    def test_every_tool_has_name(self, full_tools):
        for tool in full_tools:
            assert hasattr(tool, "name"), f"Tool missing 'name' attribute: {tool}"
            assert isinstance(tool.name, str) and tool.name, (
                f"Tool has empty or non-string name"
            )

    def test_every_tool_has_description(self, full_tools):
        for tool in full_tools:
            assert hasattr(tool, "description"), (
                f"Tool {tool.name} missing 'description' attribute"
            )
            assert isinstance(tool.description, str), (
                f"Tool {tool.name} description is not a string"
            )

    def test_all_descriptions_are_non_empty(self, full_tools):
        """All descriptions should be non-empty strings."""
        empty = [t.name for t in full_tools if not t.description.strip()]
        # Allow a small number of auto-generated stubs
        assert len(empty) <= 5, (
            f"Too many tools with empty descriptions ({len(empty)}): {empty}"
        )

    def test_every_tool_has_input_schema(self, full_tools):
        for tool in full_tools:
            assert hasattr(tool, "inputSchema"), (
                f"Tool {tool.name} missing 'inputSchema' attribute"
            )
            assert isinstance(tool.inputSchema, dict), (
                f"Tool {tool.name} inputSchema is not a dict"
            )

    def test_input_schema_type_is_object(self, full_tools):
        """Every inputSchema must have type: 'object'."""
        for tool in full_tools:
            schema = tool.inputSchema
            assert schema.get("type") == "object", (
                f"Tool {tool.name} inputSchema type should be 'object', "
                f"got '{schema.get('type')}'"
            )

    def test_input_schema_has_properties_key(self, full_tools):
        """Every inputSchema must have a 'properties' key."""
        for tool in full_tools:
            schema = tool.inputSchema
            assert "properties" in schema, (
                f"Tool {tool.name} inputSchema missing 'properties' key"
            )

    def test_no_duplicate_tool_names_in_schema(self, full_tools):
        names = [t.name for t in full_tools]
        seen = set()
        duplicates = set()
        for n in names:
            if n in seen:
                duplicates.add(n)
            seen.add(n)
        assert not duplicates, f"Duplicate tool names in schema: {duplicates}"

    def test_schema_names_are_snake_case(self, full_tools):
        """Tool names in schemas should be lowercase with underscores."""
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        violations = [t.name for t in full_tools if not pattern.match(t.name)]
        assert not violations, (
            f"Schema tool names violating snake_case convention: {violations}"
        )

    def test_required_params_subset_of_properties(self, full_tools):
        """If 'required' is present, every entry must exist in 'properties'."""
        for tool in full_tools:
            schema = tool.inputSchema
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            for param in required:
                assert param in properties, (
                    f"Tool {tool.name}: required param '{param}' not in properties"
                )


# ============================================================================
# Alias Integrity
# ============================================================================

class TestAliasIntegrity:
    """Validate the _TOOL_ALIASES registry from tool_stability.py."""

    KNOWN_REASONS = {"renamed", "consolidated", "deprecated", "intuitive_alias"}

    def test_aliases_registry_non_empty(self):
        assert len(_TOOL_ALIASES) > 0, "Alias registry should not be empty"

    def test_all_alias_targets_exist_in_handler_registry(self):
        """Every alias new_name should point to a tool in the handler registry."""
        registry = get_tool_registry()
        missing = []
        for old_name, alias in _TOOL_ALIASES.items():
            if alias.new_name not in registry:
                missing.append(f"{old_name} -> {alias.new_name}")

        # At least 80% must resolve (some targets may be hidden or register=False)
        coverage = 1 - (len(missing) / len(_TOOL_ALIASES)) if _TOOL_ALIASES else 0
        assert coverage >= 0.80, (
            f"Only {coverage:.0%} of alias targets exist in registry. "
            f"Missing ({len(missing)}): {missing[:10]}"
        )

    def test_every_alias_has_required_fields(self):
        """Each alias must have old_name, new_name, and reason."""
        for key, alias in _TOOL_ALIASES.items():
            assert alias.old_name, f"Alias '{key}' has empty old_name"
            assert alias.new_name, f"Alias '{key}' has empty new_name"
            assert alias.reason, f"Alias '{key}' has empty reason"

    def test_alias_old_name_matches_dict_key(self):
        """ToolAlias.old_name must match its dictionary key."""
        for key, alias in _TOOL_ALIASES.items():
            assert alias.old_name == key, (
                f"Alias dict key '{key}' doesn't match old_name '{alias.old_name}'"
            )

    def test_reason_values_are_from_known_set(self):
        """reason must be one of the known values."""
        for key, alias in _TOOL_ALIASES.items():
            assert alias.reason in self.KNOWN_REASONS, (
                f"Alias '{key}' has unknown reason '{alias.reason}'. "
                f"Known: {self.KNOWN_REASONS}"
            )

    def test_consolidated_aliases_with_inject_action_have_valid_values(self):
        """Aliases with inject_action should have non-empty string actions."""
        for key, alias in _TOOL_ALIASES.items():
            if alias.inject_action is not None:
                assert isinstance(alias.inject_action, str), (
                    f"Alias '{key}' inject_action should be a string"
                )
                assert len(alias.inject_action) > 0, (
                    f"Alias '{key}' inject_action is empty string"
                )

    def test_no_alias_chains(self):
        """No alias should point to another alias (no transitive chains)."""
        alias_names = set(_TOOL_ALIASES.keys())
        chains = []
        for key, alias in _TOOL_ALIASES.items():
            if alias.new_name in alias_names:
                chains.append(f"{key} -> {alias.new_name} (which is also an alias)")
        assert not chains, f"Alias chains detected (alias pointing to alias): {chains}"

    def test_resolve_tool_alias_returns_same_for_non_alias(self):
        """resolve_tool_alias for a non-alias tool should return the same name."""
        name, alias_info = resolve_tool_alias("process_agent_update")
        assert name == "process_agent_update"
        assert alias_info is None

    def test_resolve_tool_alias_returns_target_for_alias(self):
        """resolve_tool_alias for a known alias should return the target."""
        name, alias_info = resolve_tool_alias("status")
        assert name == "get_governance_metrics"
        assert alias_info is not None
        assert alias_info.old_name == "status"

    def test_list_all_aliases_returns_copy(self):
        """list_all_aliases should return a copy, not the original dict."""
        result = list_all_aliases()
        assert isinstance(result, dict)
        assert len(result) == len(_TOOL_ALIASES)
        # Mutating the copy should not affect the original
        result["__test_injection__"] = "bad"
        assert "__test_injection__" not in _TOOL_ALIASES


# ============================================================================
# Cross-Registry Validation
# ============================================================================

class TestCrossRegistryValidation:
    """Validate consistency across schema, handler, and alias registries."""

    def test_schema_tools_overlap_with_handler_registry(self):
        """A significant portion of schema tools should exist in the handler
        registry (either directly or via alias)."""
        tools = get_tool_definitions()
        registry = get_tool_registry()
        schema_names = {t.name for t in tools}

        found = 0
        for name in schema_names:
            if name in registry or name in _TOOL_ALIASES:
                found += 1

        coverage = found / len(schema_names) if schema_names else 0
        assert coverage > 0.50, (
            f"Only {coverage:.0%} of schema tools have handlers or are aliases"
        )

    def test_all_schema_tools_are_callable_directly_or_by_alias(self):
        """Anything advertised by get_tool_definitions must be callable."""
        tools = get_tool_definitions()
        registry = set(get_tool_registry())
        aliases = list_all_aliases()

        missing = []
        for tool in tools:
            alias = aliases.get(tool.name)
            if tool.name not in registry and not (alias and alias.new_name in registry):
                missing.append(tool.name)

        assert not missing, (
            "Advertised schema tools without registered handler or alias target: "
            f"{missing}"
        )

    def test_schema_tool_names_reasonable_length(self):
        """Tool names should not be excessively short or long (basic typo guard)."""
        tools = get_tool_definitions()
        for tool in tools:
            assert len(tool.name) >= 2, (
                f"Tool name too short (possible typo): '{tool.name}'"
            )
            assert len(tool.name) <= 80, (
                f"Tool name too long (possible typo): '{tool.name}'"
            )

    def test_full_verbosity_longer_than_short(self):
        """Full verbosity descriptions should be longer on average than short."""
        short_tools = get_tool_definitions(verbosity="short")
        full_tools = get_tool_definitions(verbosity="full")

        short_map = {t.name: len(t.description) for t in short_tools}
        full_map = {t.name: len(t.description) for t in full_tools}

        common_names = set(short_map.keys()) & set(full_map.keys())
        assert len(common_names) > 0, "No common tool names between short and full"

        longer_count = sum(
            1 for name in common_names
            if full_map[name] >= short_map[name]
        )
        # Full should be >= short for most tools
        ratio = longer_count / len(common_names)
        assert ratio >= 0.50, (
            f"Full descriptions should be >= short for most tools. "
            f"Only {ratio:.0%} ({longer_count}/{len(common_names)}) are."
        )

    def test_short_and_full_have_same_tool_count(self):
        """Short and full verbosity should return the same number of tools."""
        short = get_tool_definitions(verbosity="short")
        full = get_tool_definitions(verbosity="full")
        assert len(short) == len(full), (
            f"Short ({len(short)}) and full ({len(full)}) should have same tool count"
        )

    def test_short_descriptions_are_concise(self):
        """Short-mode descriptions should not be excessively long."""
        short_tools = get_tool_definitions(verbosity="short")
        for tool in short_tools:
            assert len(tool.description) < 2000, (
                f"Tool {tool.name} short description is too long "
                f"({len(tool.description)} chars)"
            )
