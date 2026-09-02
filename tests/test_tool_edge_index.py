"""The tool edge index must not silently lose edges.

`scripts/dev/tool_edge_index.py` recovers the action->delegate map by reading
the closure of the function `action_router` generates. That is the only way to
get it — the map is never bound to a module-level name — but it is also the
fragile part: rename the inner `router` function or its `actions` local and the
extraction returns nothing. The generator would keep succeeding, the CI
freshness gate would keep passing, and the index would quietly drop every
consolidated tool's routing table, which is the half a reader cannot recover by
grep.

So the guard here is a cross-check, not a snapshot. `ToolDefinition.known_actions`
is built by `action_router` from `frozenset(actions.keys())` at decoration time —
an independent path to the same truth. If the closure read breaks, the two
disagree.
"""

import copy
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts/dev"))
sys.path.insert(0, str(REPO))

import tool_edge_index as tei  # noqa: E402


@pytest.fixture(scope="module")
def collected():
    return tei.collect()


@pytest.fixture(scope="module")
def audit_snapshot(collected):
    return tei.build_audit_snapshot(*collected)


def test_every_registered_tool_is_indexed(collected):
    """The index must cover the whole shipped registry, not a subset of it.

    Tools declared outside this repo's packages — an entry-point plugin's, or a
    probe a test module registered — are not shipped, and are the one thing
    the index is required to leave out (see the generator's *Reproducibility*
    note, factor 1).
    """
    from src.mcp_handlers.decorators import (
        _TOOL_DEFINITIONS,
        list_plugin_registered_tools,
    )

    tools, _aliases, _failures, _unbound = collected
    first_party = set(_TOOL_DEFINITIONS) - set(list_plugin_registered_tools())
    assert {t.name for t in tools} == first_party, (
        "indexed tools diverge from the live registry — a tool is missing from "
        "the only readable map of dispatch"
    )


def test_router_actions_match_the_decorator_derived_set(collected):
    """Closure-read actions must equal the router's own known_actions.

    Two independent derivations of the routing table: this one reads the
    closure, the decorator's came from the same dict at registration. Drift
    means the closure extraction silently broke.
    """
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    tools, _aliases, _failures, _unbound = collected
    routers = [t for t in tools if t.actions]
    assert routers, (
        "no consolidated tools resolved — action_router's closure shape "
        "changed and the index lost every routing table"
    )

    for tool in routers:
        known = _TOOL_DEFINITIONS[tool.name].known_actions or frozenset()
        assert {edge.action for edge in tool.actions} == set(known), (
            f"{tool.name}: closure-read actions disagree with known_actions"
        )


def test_every_action_resolves_to_a_real_source_location(collected):
    """An unresolved delegate is worse than no row — it reads as an answer."""
    tools, _aliases, _failures, _unbound = collected
    unresolved = [
        f"{tool.name}(action={edge.action!r})"
        for tool in tools
        for edge in tool.actions
        if edge.target == tei.UNKNOWN or ":" not in edge.target
    ]
    assert not unresolved, f"delegates with no source location: {unresolved}"


def test_handler_modules_all_import(collected):
    """A module the generator cannot import is a hole in the index.

    The generator reports these rather than failing, so the doc stays honest
    when a dependency is missing. In this repo, on the test environment's
    dependency floor, there should be none.
    """
    _tools, _aliases, failures, _unbound = collected
    assert not failures, f"handler modules failed to import: {failures}"


def test_render_is_deterministic(collected):
    """Byte-identical across runs, or the CI freshness gate flaps."""
    assert tei.render(*collected) == tei.render(*collected)


def test_dual_snapshot_contract_is_content_addressed(audit_snapshot):
    assert audit_snapshot["schema"] == tei.AUDIT_SCHEMA
    assert audit_snapshot["dispatch"]["schema"] == tei.DISPATCH_SCHEMA
    assert audit_snapshot["exposure"]["schema"] == tei.EXPOSURE_SCHEMA
    assert tei.verify_content_hash(audit_snapshot)
    assert tei.verify_content_hash(audit_snapshot["dispatch"])
    assert tei.verify_content_hash(audit_snapshot["exposure"])
    assert audit_snapshot["summary"]["self_certifying"] is False
    source_files = audit_snapshot["dispatch"]["source_files"]
    assert source_files
    assert (
        tei.content_hash(source_files) == audit_snapshot["dispatch"]["source_revision"]
    )
    assert all(entry["content_hash"].startswith("sha256:") for entry in source_files)


def test_snapshot_matches_versioned_json_schema(audit_snapshot):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(tei.JSON_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(audit_snapshot)


def test_exposure_snapshot_uses_the_production_registration_path(audit_snapshot):
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES
    from src.tool_modes import LITE_MODE_TOOLS

    exposure = audit_snapshot["exposure"]
    assert not exposure["collection_failures"]
    assert set(exposure["modes"]["lite"]["advertised"]) == LITE_MODE_TOOLS
    assert {tool["name"] for tool in exposure["tools"]} == set(
        exposure["modes"]["full"]["advertised"]
    )
    alias_views = {
        tool["name"]: tool
        for tool in exposure["tools"]
        if tool["kind"] == "workflow_alias"
    }
    assert set(alias_views) == set(AGENT_WORKFLOW_ALIASES)


def test_mode_differences_partition_declared_and_advertised(audit_snapshot):
    for mode in audit_snapshot["exposure"]["modes"].values():
        declared = set(mode["declared"])
        advertised = set(mode["advertised"])
        assert set(mode["declared_only"]) == declared - advertised
        assert set(mode["advertised_only"]) == advertised - declared
        assert (
            tei.content_hash(
                {
                    "declared": mode["declared"],
                    "advertised": mode["advertised"],
                    "declared_only": mode["declared_only"],
                    "advertised_only": mode["advertised_only"],
                }
            )
            == mode["content_hash"]
        )


def test_action_injecting_aliases_do_not_expose_action_on_the_wire(audit_snapshot):
    alias_views = [
        tool
        for tool in audit_snapshot["exposure"]["tools"]
        if tool["kind"] == "workflow_alias" and tool["inject_action"]
    ]
    assert alias_views
    assert all("action" not in tool["wire_properties"] for tool in alias_views)
    assert all(not tool["describe_only_properties"] for tool in alias_views)
    for alias_name in (
        "request_review",
        "search_shared_memory",
        "store_finding",
        "update_finding",
    ):
        assert not any(
            finding["code"] == "DESCRIBE_SCHEMA_WIDER_THAN_WIRE"
            and finding["subject"] == alias_name
            for finding in audit_snapshot["findings"]
        )


def test_snapshot_hash_detects_evidence_mutation(audit_snapshot):
    mutated = copy.deepcopy(audit_snapshot)
    mutated["exposure"]["modes"]["lite"]["advertised"].append("invented_tool")
    assert not tei.verify_content_hash(mutated)
    assert not tei.verify_content_hash(mutated["exposure"])


def test_committed_index_is_fresh():
    """The checked-in doc must match what the standalone generator produces.

    Deliberately NOT the module-scoped `collected` fixture: tool registration
    is an import side effect, so the in-process registry contains whatever
    earlier tests happened to import — under full-suite ordering this test saw
    up to 57 tools against a committed index of 55 and failed while the index
    was in fact fresh (passes in isolation, fails after pollution). The
    committed doc's contract is "what `tool_edge_index.py` writes from a clean
    interpreter", so check exactly that, in a subprocess, via the script's own
    --check mode — the same invocation CI's doctor uses.

    The other tests in this file keep the in-process fixture on purpose: they
    cross-check two same-process derivations against each other, which is
    pollution-invariant.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "dev" / "tool_edge_index.py"),
            "--check",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    assert result.returncode == 0, (
        f"docs/dev/TOOL_EDGE_INDEX.md is stale per the standalone generator — "
        f"run: python3 scripts/dev/tool_edge_index.py\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Reproducibility: the committed index must come out identical from every
# supported interpreter. Each factor the generator's docstring names has a
# guard here; the cross-interpreter diff itself is a manual check recorded in
# the PR that introduced these (five Python/pydantic/mcp combinations).
# ---------------------------------------------------------------------------


def _declare_in_module(module_name: str, source: str) -> types.ModuleType:
    """Execute ``source`` as if it lived in ``module_name``.

    ``@mcp_tool`` records ``func.__module__`` as the tool's declaring module,
    which is what separates a plugin's registration from this repo's.
    """
    from src.mcp_handlers.decorators import action_router, mcp_tool

    module = types.ModuleType(module_name)
    module.__dict__.update(mcp_tool=mcp_tool, action_router=action_router)
    exec(source, module.__dict__)
    return module


def test_generator_refuses_entry_point_plugins_at_the_loader(collected):
    """Factor 1, first fence: the loader flag is set before any handler import,
    so an installed ``governance_mcp.plugins`` package can never register."""
    assert os.environ.get("UNITARES_DISABLE_PLUGINS") == "1"


def test_tools_registered_outside_the_repo_are_not_indexed():
    """Factor 1, second fence: a tool whose declaring module is not one of this
    repo's packages is left out of both the dispatch tables and the wire
    catalog, even when it is sitting in the live registry."""
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS
    import src.tool_registration as tool_registration

    name = "test_edge_index_foreign_probe"
    _declare_in_module(
        "fake_governance_plugin.handlers",
        f"@mcp_tool({name!r})\n"
        "async def handle_probe(arguments):\n"
        "    return []\n",
    )
    try:
        assert name in _TOOL_DEFINITIONS, "probe did not register; test is inert"
        tools, _aliases, failures, _unbound = tei.collect()
        assert not failures
        assert name not in {tool.name for tool in tools}
        catalog, collection_failures = tei._collect_wire_catalog()
        assert not collection_failures
        assert name not in catalog
        assert "start_session" in catalog, "the fence removed more than the probe"
    finally:
        _TOOL_DEFINITIONS.pop(name, None)
        TOOL_HANDLERS.pop(name, None)
        cache = getattr(tool_registration, "_tool_wrappers_cache", None)
        if isinstance(cache, dict):
            cache.pop(name, None)


def _reverse_unions(node):
    """Flip every anyOf/oneOf/allOf in place — the drift factor 2 produces."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
                value.reverse()
            _reverse_unions(value)
    elif isinstance(node, list):
        for item in node:
            _reverse_unions(item)
    return node


def test_schema_hash_is_invariant_to_union_member_order():
    """Factor 2: ``bool | str | None`` renders as [boolean, string, null] or
    [string, boolean, null] depending on typing's Union cache; the two accept
    the same documents and must hash the same. A real change must not."""
    schema = {
        "type": "object",
        "title": "start_sessionArguments",
        "properties": {
            "force_new": {
                "anyOf": [{"type": "boolean"}, {"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Force New",
            }
        },
    }
    flipped = _reverse_unions(copy.deepcopy(schema))
    assert flipped != schema, "fixture did not flip; the test is inert"
    assert tei.normalize_schema(schema) == tei.normalize_schema(flipped)
    assert tei.schema_hash(schema) == tei.schema_hash(flipped)

    changed = copy.deepcopy(schema)
    changed["properties"]["force_new"]["anyOf"][0]["type"] = "integer"
    assert tei.schema_hash(changed) != tei.schema_hash(schema)


def test_normalize_schema_drops_titles_but_keeps_a_parameter_named_title():
    """``title`` is a schema keyword at schema level and a parameter name under
    ``properties``; only the former is presentation. Literal data (``default``)
    is never rewritten, whatever keys it happens to contain."""
    schema = {
        "title": "xArguments",
        "type": "object",
        "properties": {
            "title": {"type": "string", "title": "Title", "default": {"title": "kept"}}
        },
        "required": ["title"],
    }
    normalized = tei.normalize_schema(schema)
    assert "title" not in normalized
    assert normalized["properties"] == {
        "title": {"type": "string", "default": {"title": "kept"}}
    }
    assert normalized["required"] == ["title"]
    assert tei.normalize_schema(normalized) == normalized, "must be idempotent"


def test_every_printed_schema_hash_survives_the_typing_union_cache(audit_snapshot):
    """Factor 2 on the real catalog: every hash the index prints must be the
    same whichever way the interpreter ordered union members, and must be the
    hash of the recorded schema's normalized form (so a verifier can recompute
    it from the JSON)."""
    views = audit_snapshot["exposure"]["tools"]
    assert views
    exercised = False
    for view in views:
        assert tei.schema_hash(view["input_schema"]) == view["input_schema_hash"]
        assert (
            tei.schema_hash(view["describe_input_schema"])
            == view["describe_input_schema_hash"]
        )
        flipped = _reverse_unions(copy.deepcopy(view["input_schema"]))
        exercised |= flipped != view["input_schema"]
        assert tei.schema_hash(flipped) == view["input_schema_hash"], view["name"]
    assert exercised, (
        "no union-typed parameter on the wire; the invariance this guards was "
        "never exercised"
    )


def test_stale_report_names_the_differing_lines_and_ends_with_the_verdict():
    """Factor 3: a stale verdict must say WHICH lines differ and under what
    interpreter. The verdict stays LAST because the doctor classifies the run
    by its last line (``unitares_doctor._generator_crashed``)."""
    import unitares_doctor

    current = "line one\nline two\nline three\n"
    generated = "line one\nline 2\nline three\n"
    report = tei.stale_report(current, generated)
    lines = report.splitlines()
    assert "-line two" in lines
    assert "+line 2" in lines
    assert any(line.startswith("  python ") for line in lines)
    assert any(line.startswith("  mcp ") for line in lines)
    assert any(line.startswith("  pydantic ") for line in lines)
    assert lines[-1] == (
        "docs/dev/TOOL_EDGE_INDEX.md is stale — run: python3 scripts/dev/tool_edge_index.py"
    )
    assert unitares_doctor._generator_crashed(report) is False


def test_stale_report_truncates_a_runaway_diff_but_keeps_the_verdict_last():
    current = "\n".join(f"old {i}" for i in range(400))
    generated = "\n".join(f"new {i}" for i in range(400))
    report = tei.stale_report(current, generated, limit=20)
    lines = report.splitlines()
    assert any("more diff lines" in line for line in lines)
    assert lines[-1] == tei.STALE_VERDICT


def test_pinned_mcp_version_is_read_from_constraints():
    """The banner compares the running mcp against the pin the committed index
    is generated under; that pin must be readable, or the comparison is mute."""
    pinned = tei.pinned_mcp_version()
    assert pinned, "constraints.txt no longer pins mcp — update environment_lines()"
    assert any(line.startswith("mcp ") for line in tei.environment_lines())
