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
import re
import subprocess
import sys
import textwrap
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
    when the tree is broken. A third-party module that is not installed is
    the one exception: that is "cannot look" and exits 2 (the *Cannot look*
    tests at the end of this file). In this repo, on the test environment's
    dependency floor, there should be neither.
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


# ---------------------------------------------------------------------------
# Line drift: the committed markdown must not move when only line numbers do.
# One import added at the top of a handler module renumbers every definition
# below it and changes no edge; the freshness gate has to stay green on that
# and go red on a real change. The collected graph still carries ``file:line``
# (``--json`` prints it), so the invariance is a property of the rendering,
# and these tests exercise the rendering on the real graph.
# ---------------------------------------------------------------------------

_SITE_WITH_LINE = re.compile(r"^(?P<path>\S+\.py):(?P<line>\d+)(?P<rest>.*)$")


def _shift_site(site, by):
    """``path:LINE symbol`` -> ``path:LINE+by symbol``; any other shape unchanged."""
    match = _SITE_WITH_LINE.match(site or "")
    if not match:
        return site
    return f"{match['path']}:{int(match['line']) + by}{match['rest']}"


def _shifted(collected, *, by, module=None):
    """A deep copy of the collected graph with every location in ``module``
    (every module when None) moved by ``by`` lines — exactly what inserting
    ``by`` lines at the top of that file does to the generator's input.
    Returns the copy and how many locations moved."""
    tools, aliases, failures, unbound = copy.deepcopy(collected)
    moved = 0

    def shift(site):
        nonlocal moved
        if site and (module is None or site.startswith(f"{module}:")):
            shifted = _shift_site(site, by)
            moved += shifted != site
            return shifted
        return site

    for tool in tools:
        tool.handler = shift(tool.handler)
        tool.schema = shift(tool.schema)
        for edge in tool.actions:
            edge.target = shift(edge.target)
    for alias in aliases:
        alias.param_normalizer = shift(alias.param_normalizer)
    return (tools, aliases, failures, unbound), moved


def test_render_ignores_a_pure_line_shift_in_a_handler_module(collected):
    """Reproduced 2026-09-02: one line prepended to
    src/mcp_handlers/dialectic/handlers.py rewrote sixteen rows of the index
    and ``--check`` called it stale. The rendering must be byte-identical
    under that shift — in one module, and in every module at once."""
    module = "src/mcp_handlers/dialectic/handlers.py"
    shifted, moved = _shifted(collected, by=1, module=module)
    assert moved, f"no location in {module}; the fixture is inert"
    assert tei.render(*shifted) == tei.render(*collected)

    everywhere, moved = _shifted(collected, by=97)
    tools = collected[0]
    assert moved >= len(tools), "fewer locations moved than there are tools"
    assert tei.render(*everywhere) == tei.render(*collected)


@pytest.mark.parametrize(
    "change",
    [
        "delegate_renamed",
        "delegate_moved",
        "action_removed",
        "handler_moved",
        "schema_renamed",
    ],
)
def test_render_moves_on_a_real_edge_change(collected, change):
    """Dropping the line number must not drop the edge: a delegate renamed or
    moved to another file, an action removed, a handler or schema class
    relocated, each still changes the rendered document."""
    tools, aliases, failures, unbound = copy.deepcopy(collected)
    router = next(tool for tool in tools if tool.actions)
    edge = router.actions[0]
    if change == "delegate_renamed":
        edge.target = f"{edge.target}_renamed"
    elif change == "delegate_moved":
        edge.target = edge.target.replace(".py:", "_moved.py:", 1)
    elif change == "action_removed":
        router.actions.pop(0)
    elif change == "handler_moved":
        plain = next(tool for tool in tools if not tool.actions)
        plain.handler = plain.handler.replace(".py:", "_moved.py:", 1)
    elif change == "schema_renamed":
        with_schema = next(tool for tool in tools if tool.schema)
        with_schema.schema = f"{with_schema.schema}V2"
    assert tei.render(tools, aliases, failures, unbound) != tei.render(*collected)


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        (
            "src/mcp_handlers/core.py:223 handle_get_governance_metrics",
            "src/mcp_handlers/core.py handle_get_governance_metrics",
        ),
        (
            "src/mcp_handlers/consolidated.py:354 action_router",
            "src/mcp_handlers/consolidated.py action_router",
        ),
        (
            "src/mcp_handlers/schemas/admin.py:171 AdminParams",
            "src/mcp_handlers/schemas/admin.py AdminParams",
        ),
        ("src/mcp_handlers/core.py:223", "src/mcp_handlers/core.py"),
        ("AdminParams", "AdminParams"),  # _class_site fallback: no source file
        (tei.UNKNOWN, tei.UNKNOWN),  # _site fallback
        (None, None),
        ("", ""),
    ],
)
def test_doc_site_drops_the_line_number_and_nothing_else(site, expected):
    assert tei.doc_site(site) == expected


def test_committed_index_locates_code_by_file_and_symbol_only(audit_snapshot):
    """Guards the split. The markdown carries no ``file:line`` — or line drift
    is back and the gate fails on edits that change no edge. The on-demand
    JSON keeps it — or a verifier loses the precise location. Do not re-inline
    line numbers into the markdown."""
    text = tei.OUT.read_text(encoding="utf-8")
    with_line = re.findall(r"`[^`\n]*\.py:\d+[^`\n]*`", text)
    assert not with_line, with_line[:5]
    located = re.findall(r"`src/[^`\n]*\.py [A-Za-z_]\w*`", text)
    assert located, "no `file symbol` location in the committed index"

    dispatch = audit_snapshot["dispatch"]
    assert all(re.search(r"\.py:\d+ ", tool["handler"]) for tool in dispatch["tools"])
    assert all(
        re.search(r"\.py:\d+ ", edge["target"])
        for tool in dispatch["tools"]
        for edge in tool["actions"]
    )


# ---------------------------------------------------------------------------
# Cannot look: a dependency that is not installed is exit 2, never a verdict.
# The exposure half builds the wire catalog through the production registrar,
# which imports the Prometheus metrics registry — a requirements-full.txt
# dependency. With only requirements-core.txt installed the generator used to
# fold the ModuleNotFoundError into the findings (EXPOSURE_COLLECTION_FAILURE
# plus one ORIENTATION_NAME_NOT_ON_WIRE per tool: 2 errors, 46 warnings) and
# then compare, so --check called a current index stale and the doctor's
# tool_edge_index_fresh reported FAIL where it should SKIP (tool-surface audit
# 2026-09-12, F3). Simulated in a subprocess by blocking the import:
# ``sys.modules[name] = None`` is how CPython spells "not installed", and it
# raises the same ModuleNotFoundError (``.name`` set) a bare venv does.
# ---------------------------------------------------------------------------

GENERATOR = REPO / "scripts" / "dev" / "tool_edge_index.py"
BLOCK_ENV = "TEST_EDGE_INDEX_BLOCK_MODULE"


def _generator_shim(root: pathlib.Path) -> pathlib.Path:
    """Install ``root/scripts/dev/tool_edge_index.py``: a shim that runs the
    real generator with the module named in ``BLOCK_ENV`` made unimportable.

    Same relative path the doctor invokes, so ``check_tool_edge_index_fresh``
    can be pointed at ``root`` and exercised end to end against the real
    generator and the real committed index.
    """
    script = root / "scripts" / "dev" / "tool_edge_index.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            import runpy
            import sys

            blocked = os.environ.get({BLOCK_ENV!r})
            if blocked:
                sys.modules[blocked] = None
            sys.argv = [{str(GENERATOR)!r}, *sys.argv[1:]]
            runpy.run_path({str(GENERATOR)!r}, run_name="__main__")
            """
        ),
        encoding="utf-8",
    )
    return script


def _run_shim(script: pathlib.Path, *args: str, blocked: str | None = None):
    env = {key: value for key, value in os.environ.items() if key != BLOCK_ENV}
    if blocked:
        env[BLOCK_ENV] = blocked
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
        env=env,
    )


def test_repo_packages_covers_every_top_level_package_in_the_tree():
    """``_REPO_PACKAGES`` is what separates a tree defect from an absent
    dependency, so it must not fall behind the tree.

    A new top-level package missing from the set reads as a third-party
    module: a real defect inside it would exit 2, the doctor would SKIP, and
    the defect would go quiet. Silence is the worse direction, which is why
    this asserts coverage rather than equality — a package that loses its
    ``__init__.py`` leaves a harmless extra entry, and failing the suite for
    that would buy no safety.
    """
    on_disk = {
        path.name
        for path in REPO.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert on_disk, "no top-level package found; this guard is inert"
    missing = on_disk - tei._REPO_PACKAGES
    assert not missing, (
        f"top-level package(s) {sorted(missing)} are not in _REPO_PACKAGES, so "
        "a missing module inside them would be misread as an uninstalled "
        "dependency and exit 2 instead of being reported as a tree defect"
    )


def test_missing_dependency_is_drawn_by_package_not_by_exception_type():
    """Only a ModuleNotFoundError for a module outside the repo's packages is
    "not installed". A missing ``src`` module and a bad name are defects in
    the tree — the generator looked and found them — and stay findings."""
    absent = ModuleNotFoundError("No module named 'prometheus_client'", name="prometheus_client")
    assert tei.missing_dependency(absent) == "prometheus_client"
    assert tei.missing_dependency(ModuleNotFoundError("x", name="pkg.sub")) == "pkg.sub"
    assert tei.missing_dependency(ModuleNotFoundError("x", name="src.gone")) is None
    assert tei.missing_dependency(ModuleNotFoundError("x", name="governance_core.gone")) is None
    assert tei.missing_dependency(ModuleNotFoundError("x")) is None
    assert tei.missing_dependency(ImportError("cannot import name 'x' from 'src.y'", name="src.y")) is None
    assert tei.missing_dependency(TypeError("not an import failure")) is None

    err = tei.MissingDependency("prometheus_client", absent)
    assert isinstance(err, ImportError)
    assert err.module == "prometheus_client"
    assert "prometheus_client" in str(err) and "ModuleNotFoundError" in str(err)


# The generator declares which codes read the wire catalog; this test module
# consumes that declaration rather than keeping a second copy that could drift
# from the set actually withheld.
WIRE_DERIVED_CODES = frozenset(tei.WIRE_DERIVED_FINDING_CODES)


def test_wire_catalog_raises_for_an_absent_dependency_and_records_a_tree_defect(
    collected, monkeypatch
):
    """In-process, both sides of the line ``missing_dependency`` draws.

    Absent dependency: the registrar is re-imported with ``prometheus_client``
    blocked, exactly the core-only import chain, and the collector must raise
    rather than hand back an empty catalog. Tree defect: the registrar module
    itself is blocked, and that IS recorded as a collection failure — with the
    wire-derived findings withheld, since nothing was looked at.

    Only the exposure side is asserted. The in-process dispatch registry is
    whatever earlier tests left in it (see ``test_committed_index_is_fresh``),
    so dispatch-side findings such as ``ALIAS_TARGET_MISSING`` can appear under
    full-suite ordering and say nothing about this change.
    """
    monkeypatch.delitem(sys.modules, "src.tool_registration", raising=False)
    monkeypatch.delitem(sys.modules, "src.metrics_registry", raising=False)
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    with pytest.raises(tei.MissingDependency) as raised:
        tei._collect_wire_catalog()
    assert raised.value.module == "prometheus_client"
    assert isinstance(raised.value.__cause__, ModuleNotFoundError)

    monkeypatch.setitem(sys.modules, "src.tool_registration", None)
    catalog, failures = tei._collect_wire_catalog()
    assert catalog == {}
    assert len(failures) == 1 and "src.tool_registration" in failures[0]

    tools, aliases, import_failures, unbound = collected
    dispatch = tei.build_dispatch_snapshot(tools, aliases, import_failures, unbound)
    exposure = tei.build_exposure_snapshot(tools, aliases)
    assert exposure["collection_failures"] == failures
    assert exposure["tools"] == []
    # The empty catalog DOES make every orientation name look off the wire;
    # that is what must not become findings.
    assert exposure["orientation"]["full_orientation_only"], "nothing to withhold"
    found = tei.lint_snapshots(dispatch, exposure)
    codes = {finding.code for finding in found}
    assert "EXPOSURE_COLLECTION_FAILURE" in codes
    assert not codes & WIRE_DERIVED_CODES, sorted(codes & WIRE_DERIVED_CODES)

    # The withholding must be machine-readable: a consumer has to be able to
    # read WHICH checks did not run, rather than infer it from their absence.
    collection = [f for f in found if f.code == "EXPOSURE_COLLECTION_FAILURE"]
    assert len(collection) == 1, "exactly one primary failure, not one per check"
    withheld = collection[0].evidence["withheld_checks"]
    assert set(withheld) == WIRE_DERIVED_CODES
    assert collection[0].evidence["failure"] == failures[0]


def test_declared_wire_derived_codes_match_what_is_actually_withheld():
    """The declaration must not drift from the code.

    ``WIRE_DERIVED_FINDING_CODES`` is published as evidence, so a code that
    lint_snapshots emits after the early return but is not in the tuple would
    be advertised as withheld while still firing — and one that no longer
    exists would be advertised as withheld forever. Both are read off the
    source of the function that does the withholding.
    """
    import inspect

    source = inspect.getsource(tei.lint_snapshots)
    _before, separator, after = source.partition('if exposure["collection_failures"]:')
    assert separator, "the early return moved; this guard is reading the wrong code"
    emitted_after = set(re.findall(r'"([A-Z][A-Z_]{4,})"', after))

    declared = set(tei.WIRE_DERIVED_FINDING_CODES)
    assert declared <= emitted_after, (
        f"declared as withheld but not emitted after the early return: "
        f"{sorted(declared - emitted_after)}"
    )
    assert emitted_after <= declared, (
        f"emitted after the early return but not declared as withheld: "
        f"{sorted(emitted_after - declared)}"
    )


@pytest.mark.parametrize("mode", ["--check", "--lint", "--json"])
def test_every_mode_declines_with_exit_2_when_the_registrar_dependency_is_absent(
    tmp_path, mode
):
    """The audit's reproduction, per mode: exit 2 and no verdict — never the
    exit 1 that means "looked and found drift" (or, under --lint, "found
    errors"). Nothing derived from the empty catalog is printed, --json emits
    no partial snapshot, and the last stderr line is one the doctor will not
    mistake for a crash."""
    import unitares_doctor

    result = _run_shim(_generator_shim(tmp_path), mode, blocked="prometheus_client")
    assert result.returncode == 2, (
        f"expected 2 (cannot look), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert tei.STALE_VERDICT not in combined
    assert "EXPOSURE_COLLECTION_FAILURE" not in combined
    assert "ORIENTATION_NAME_NOT_ON_WIRE" not in combined
    assert result.stdout.strip() == "", "a partial snapshot or verdict was emitted"
    last = [line for line in result.stderr.splitlines() if line.strip()][-1]
    assert last.startswith("cannot look:"), last
    assert "prometheus_client" in last and "requirements-full.txt" in last
    assert unitares_doctor._generator_crashed(result.stderr) is False


def test_doctor_skips_when_the_generator_cannot_look_and_passes_when_it_can(
    tmp_path, monkeypatch
):
    """End to end through the doctor's own check, against the real generator
    and the real committed index. Blocked: SKIP, with the missing module named
    in the detail. Unblocked through the same shim: PASS. Never FAIL — that is
    the false verdict the audit reproduced on a core-only machine."""
    import unitares_doctor

    _generator_shim(tmp_path)

    monkeypatch.setenv(BLOCK_ENV, "prometheus_client")
    blocked = unitares_doctor.check_tool_edge_index_fresh(tmp_path)
    assert blocked.status == unitares_doctor.Status.SKIP, (blocked.message, blocked.detail)
    assert "requirements-full.txt" in blocked.message
    assert "stale" not in blocked.message
    assert tei.STALE_VERDICT not in (blocked.detail or "")
    assert "prometheus_client" in (blocked.detail or "")

    monkeypatch.delenv(BLOCK_ENV)
    control = unitares_doctor.check_tool_edge_index_fresh(tmp_path)
    assert control.status == unitares_doctor.Status.PASS, (control.message, control.detail)
