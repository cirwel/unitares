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
import pathlib
import subprocess
import sys

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
    """The index must cover the whole registry, not a subset of it."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    tools, _aliases, _failures, _unbound = collected
    assert {t.name for t in tools} == set(_TOOL_DEFINITIONS), (
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

    # Introspection currently resolves the alias to its canonical schema. The
    # audit records that mismatch instead of silently treating the two surfaces
    # as equivalent; request_review is the smallest concrete regression guard.
    request_review = next(
        tool for tool in alias_views if tool["name"] == "request_review"
    )
    assert "action" in request_review["describe_only_properties"]
    assert any(
        finding["code"] == "DESCRIBE_SCHEMA_WIDER_THAN_WIRE"
        and finding["subject"] == "request_review"
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
