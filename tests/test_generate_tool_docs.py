"""The tool reference must describe the registry, not a scan of the source.

`scripts/diagnostics/generate_tool_docs.py` replaced a generator that read
``@mcp_tool`` decorators out of the source text. That model of a tool fails in
four ways, and each test group below pins one:

1. it globbed only the top level of ``src/mcp_handlers/`` (its scan found 4 of
   43 tools);
2. it rendered only the categories in a fixed map, dropping the rest;
3. it missed every tool ``action_router`` builds, and once its glob was
   widened it documented ``register=False`` internals as tools;
4. it read only literal timeouts, so a timeout built from a constant came out
   as the 30s default.

Expected values come from the registry and the named constants, never from
literals, so a new tool or a retuned timeout moves the oracle with it.
"""

import asyncio
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "dev"))
sys.path.insert(0, str(REPO))

from scripts.diagnostics import generate_tool_docs as gen  # noqa: E402
from tests.helpers import parse_result  # noqa: E402


@pytest.fixture(scope="module")
def reference():
    return gen.collect()


@pytest.fixture(scope="module")
def markdown(reference):
    return gen.render(reference)


@pytest.fixture(scope="module")
def first_party():
    """Registered first-party tools: what the reference must cover exactly.

    Other test modules register probe tools; those, like an entry-point
    plugin's, are declared outside this repo's packages and are left out.
    """
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, list_plugin_registered_tools

    foreign = set(list_plugin_registered_tools())
    return {name: td for name, td in _TOOL_DEFINITIONS.items() if name not in foreign}


def _entries(reference):
    return {tool.name: tool for section in reference.sections for tool in section.tools}


def _headings(md):
    return re.findall(r"^### `([^`]+)`$", md, re.M)


def _block(md, name):
    """The rendered text of one tool, from its heading to the next heading."""
    match = re.search(rf"^### `{re.escape(name)}`$(.*?)(?=^##)", md + "\n##", re.M | re.S)
    assert match, f"no block for {name}"
    return match.group(1)


def _field(block, label):
    match = re.search(rf"^- \*\*{label}:\*\* (.*)$", block, re.M)
    assert match, f"no {label} line in:\n{block[:400]}"
    return match.group(1)


def _action_rows(block):
    rows = {}
    for line in block.splitlines():
        match = re.match(r"^\| `([^`]+)`(?: \(default\))? \| (\S+) \| (\S+) \|", line)
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3))
    return rows


def _intro(md):
    return md.split("\n## ", 1)[0]


# --- finding 1: coverage comes from the registry -----------------------------


def test_every_registered_tool_is_documented_exactly_once(markdown, first_party):
    headings = _headings(markdown)
    assert len(headings) == len(set(headings)), "a tool is documented twice"
    assert set(headings) == set(first_party)
    # Each missed by the old generator: declared in a subpackage (onboard,
    # consult), built by action_router (knowledge), or filed under a category
    # its fixed map lacked (record_progress_pulse).
    assert {"onboard", "consult", "knowledge", "record_progress_pulse"} <= set(headings)


def test_the_published_counts_are_what_discovery_serves(markdown, first_party):
    """The advertised count is pinned to the served catalog, not to a copy of
    its formula, so a change in what full-mode discovery serves fails here."""
    from src.interface_contract import get_public_tool_definitions
    from src.mcp_handlers.decorators import list_plugin_registered_tools

    foreign = set(list_plugin_registered_tools())
    served = {tool.name for tool in get_public_tool_definitions("full", include_unmounted=True)}
    progressive = {tool.name for tool in get_public_tool_definitions("progressive")}
    registered = sum(1 for td in first_party.values() if not td.hidden)
    assert f"**{registered} registered tools" in markdown
    assert f"{len(served - foreign)} advertised tools** in the full catalog" in markdown
    assert f"progressive `tools/list` starts with {len(progressive - foreign)}" in markdown


def test_a_hidden_tool_is_marked_and_left_out_of_the_advertised_count(monkeypatch):
    from src.interface_contract import get_public_tool_definitions
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, list_plugin_registered_tools

    monkeypatch.setattr(_TOOL_DEFINITIONS["health_check"], "hidden", True)
    reference = gen.collect()
    markdown = gen.render(reference)
    foreign = set(list_plugin_registered_tools())
    served = {tool.name for tool in get_public_tool_definitions("full", include_unmounted=True)}
    hidden = sum(
        1 for name, td in _TOOL_DEFINITIONS.items() if td.hidden and name not in foreign
    )
    assert "health_check" not in served
    assert reference.advertised == len(served - foreign)
    assert f"(+{hidden} hidden)" in markdown
    assert "- **Hidden** from discovery" in _block(markdown, "health_check")


# --- finding 2: every tool is filed, none is dropped --------------------------


def test_every_tool_sits_under_its_tool_meta_category(markdown, first_party):
    from src.mcp_handlers.introspection.tool_catalog import category_presentation
    from src.tool_meta import TOOL_META_BY_NAME

    section_of = {}
    current = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:]
        match = re.match(r"^### `([^`]+)`$", line)
        if match:
            section_of[match.group(1)] = current
    for name in first_party:
        expected = category_presentation(TOOL_META_BY_NAME[name].category)["name"]
        assert section_of[name] == expected, name


def _assert_contents_match(markdown):
    sections = re.findall(r"^## (.+)$", markdown, re.M)
    rows = re.findall(r"^\| \[(.+?)\]\(#([^)]+)\) \| (\d+) \|$", markdown, re.M)
    listed = {title: (link, int(count)) for title, link, count in rows}
    for title in sections:
        if title == "Contents":
            continue
        assert title in listed, f"section {title!r} missing from the contents"
        link, count = listed[title]
        assert link == gen.anchor(title)
        block = markdown.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]
        if title == gen.UNPLACED_TITLE:
            assert count == len(re.findall(r"^\| `", block, re.M))
        else:
            assert count == len(_headings(block))


def test_the_contents_table_counts_and_links_every_section(markdown):
    _assert_contents_match(markdown)
    rows = re.findall(r"^\| \[(.+?)\]\(#[^)]+\) \| (\d+) \|$", markdown, re.M)
    listed_tools = sum(int(count) for title, count in rows if title != gen.UNPLACED_TITLE)
    assert listed_tools == len(_headings(markdown))


def test_a_tool_without_a_meta_record_is_filed_not_dropped(monkeypatch):
    import src.tool_meta

    trimmed = {k: v for k, v in src.tool_meta.TOOL_META_BY_NAME.items() if k != "health_check"}
    monkeypatch.setattr(src.tool_meta, "TOOL_META_BY_NAME", trimmed)
    reference = gen.collect()
    homes = [s.title for s in reference.sections if any(t.name == "health_check" for t in s.tools)]
    assert homes == ["Other"]
    assert "### `health_check`" in gen.render(reference)


def test_github_anchors():
    assert gen.anchor("Identity & Onboarding") == "identity--onboarding"
    assert gen.anchor("Admin & Diagnostics") == "admin--diagnostics"
    assert gen.anchor("Core Governance") == "core-governance"


# --- finding 3: public tools only, routers included, no name dropped ----------


def test_internal_and_older_names_are_never_tools(markdown):
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    headings = set(_headings(markdown))
    assert headings.isdisjoint(_TOOL_ALIASES)
    # register=False delegates that a decorator scan counts as tools.
    for internal in ("request_dialectic_review", "submit_thesis", "list_agents"):
        assert internal not in headings
        assert f"`{internal}`" in markdown, f"{internal} should appear as an older name"


def test_every_alias_is_placed_exactly_somewhere(reference):
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    placed = []
    for tool in _entries(reference).values():
        placed += [alias for alias, _action in tool.workflow_aliases]
        placed += tool.older_names
        placed += [old for entry in tool.actions for old in entry.older_names]
    placed += [old for old, _target in reference.unplaced_aliases]
    assert len(placed) == len(set(placed)), "an alias is listed twice"
    assert set(placed) == set(_TOOL_ALIASES)


def test_an_alias_with_no_documented_target_gets_its_own_table(monkeypatch):
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES, ToolAlias

    monkeypatch.setitem(
        _TOOL_ALIASES, "probe_gone", ToolAlias("probe_gone", "no_such_tool", "renamed")
    )
    monkeypatch.setitem(
        _TOOL_ALIASES,
        "probe_bad_action",
        ToolAlias("probe_bad_action", "knowledge", "consolidated", inject_action="no_such_action"),
    )
    reference = gen.collect()
    markdown = gen.render(reference)
    assert ("probe_gone", "no_such_tool") in reference.unplaced_aliases
    assert ("probe_bad_action", "knowledge(action='no_such_action')") in reference.unplaced_aliases
    assert f"## {gen.UNPLACED_TITLE}" in markdown
    _assert_contents_match(markdown)


def test_every_router_is_documented_with_every_action(markdown, first_party):
    routers = [name for name, td in first_party.items() if td.reads_op_as_action]
    assert {"knowledge", "agent", "calibration", "dialectic"} <= set(routers)
    for name in routers:
        rows = _action_rows(_block(markdown, name))
        assert sorted(rows) == sorted(first_party[name].known_actions), name


def test_workflow_aliases_are_listed_once_and_never_as_older_names(markdown):
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES

    for alias in AGENT_WORKFLOW_ALIASES:
        assert len(re.findall(rf"^- \*\*Workflow alias:\*\* `{alias}`", markdown, re.M)) == 1
        for line in markdown.splitlines():
            if line.startswith("- **Older names:**") or line.startswith("| `"):
                assert f"`{alias}`" not in line, line


def test_older_names_are_said_not_to_work_on_the_mcp_endpoint(markdown):
    """They resolve through the alias table, which only REST and stdio
    dispatch consult; /mcp mounts the advertised names alone."""
    from src.mcp_handlers.introspection.tool_introspection import _registered_public_tool_names
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES, _TOOL_ALIASES

    older = set(_TOOL_ALIASES) - set(AGENT_WORKFLOW_ALIASES)
    assert older.isdisjoint(_registered_public_tool_names())
    intro = _intro(markdown)
    assert "`/mcp`" in intro and "unknown tool" in intro


# --- finding 4: timeouts are the evaluated values ------------------------------


def test_every_tool_timeout_is_the_registry_value(markdown, first_party):
    for name, td in first_party.items():
        line = _field(_block(markdown, name), "Timeout")
        if td.timeout is None:
            assert not re.search(r"\d", line), f"{name}: {line}"
        else:
            assert line.startswith(gen.seconds(td.timeout)), f"{name}: {line}"


def test_a_timeout_built_from_a_constant_is_not_the_default(markdown):
    from src.mcp_handlers.support.consultation import CONSULT_TIMEOUT_S

    assert _field(_block(markdown, "consult"), "Timeout") == gen.seconds(CONSULT_TIMEOUT_S)
    assert gen.seconds(CONSULT_TIMEOUT_S) != "30s"


def test_router_actions_show_the_smaller_of_the_two_limits(markdown, first_party):
    from src.mcp_handlers.dialectic.handlers import (
        REQUEST_REVIEW_TIMEOUT,
        SUBMIT_THESIS_TIMEOUT,
    )

    rows = _action_rows(_block(markdown, "dialectic"))
    router = first_party["dialectic"].timeout
    assert rows["request"][1] == gen.seconds(REQUEST_REVIEW_TIMEOUT)
    assert rows["thesis"][1] == gen.seconds(SUBMIT_THESIS_TIMEOUT)
    assert REQUEST_REVIEW_TIMEOUT < router and SUBMIT_THESIS_TIMEOUT < router

    for name, td in first_party.items():
        for action, (_identity, limit) in _action_rows(_block(markdown, name)).items():
            assert float(limit.rstrip("s")) <= td.timeout, f"{name}.{action}: {limit}"


def test_a_router_limit_below_the_delegates_wins(first_party):
    """The other half of min(): a delegate may declare more time than its
    router allows, and the router's wait_for still ends the call."""
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    dialectic = first_party["dialectic"]

    class Tighter:
        def __getattr__(self, name):
            return 1.0 if name == "timeout" else getattr(dialectic, name)

    entries, routed = gen._action_entries("dialectic", Tighter(), _TOOL_ALIASES, set())
    assert routed
    assert {entry.timeout for entry in entries} == {1.0}


def test_hand_routed_tools_show_the_tool_limit_as_a_ceiling(reference, markdown):
    """A tool that branches in its own body exposes no action table, so its
    rows carry the tool's limit, and the page says that is only a ceiling."""
    hand_routed = [t for t in _entries(reference).values() if t.actions and not t.routed]
    assert {"self_recovery", "cirs_protocol"} <= {t.name for t in hand_routed}
    intro = _intro(markdown)
    for tool in hand_routed:
        assert {entry.timeout for entry in tool.actions} == {tool.timeout}
        assert f"`{tool.name}`" in intro
    assert "Timeout (at most)" in markdown


def test_rest_direct_handlers_that_skip_the_wrapper_are_named(reference, markdown):
    from src.mcp_handlers.decorators import get_tool_registry
    from src.services.http_tool_service import _DIRECT_HTTP_TOOL_HANDLERS

    registry = get_tool_registry()
    bypass = {n for n, h in _DIRECT_HTTP_TOOL_HANDLERS.items() if registry.get(n) is not h}
    assert set(reference.rest_unbounded) == bypass
    for name in bypass:
        assert f"`{name}`" in _intro(markdown)
        assert "not applied on REST" in _field(_block(markdown, name), "Timeout")


def test_an_action_table_that_cannot_be_read_is_refused(monkeypatch):
    monkeypatch.setattr(gen.edge_index, "_router_actions", lambda handler: ({}, {}))
    with pytest.raises(gen.RegistryUnfit, match="does not match known_actions"):
        gen.collect()


# --- the text is what describe_tool serves -------------------------------------


def test_descriptions_and_stability_match_describe_tool(reference, markdown):
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    for name, tool in _entries(reference).items():
        served = parse_result(asyncio.run(handle_describe_tool({"tool_name": name, "lite": False})))
        assert served["tool"]["description"] == tool.description, name
        assert served["tool"]["stability"] == tool.stability, name
        assert gen.description_markdown(tool.description) in _block(markdown, name), name


def test_laid_out_lines_keep_their_layout(reference, markdown):
    """Indented, JSON and column-aligned lines are fenced verbatim; merged
    into a paragraph they would lose their line breaks and indentation."""
    checked = 0
    for name, tool in _entries(reference).items():
        fences = re.findall(r"^(~{3,})text\n(.*?)\n\1$", _block(markdown, name), re.M | re.S)
        fenced_lines = {line for _fence, body in fences for line in body.splitlines()}
        for line in tool.description.splitlines():
            line = line.rstrip()
            if line.strip() and gen._PREFORMATTED.search(line):
                assert line in fenced_lines, f"{name}: {line!r}"
                checked += 1
    assert checked, "no laid-out line found; this guard is inert"


def test_markdown_text_escapes_only_structure():
    assert gen.markdown_text("# not a heading") == "\\# not a heading"
    assert gen.markdown_text("> not a quote") == "\\> not a quote"
    assert gen.markdown_text("above\n---") == "above\n\\---"
    assert gen.markdown_text("Title\n=") == "Title\n\\="
    assert gen.markdown_text("a <b> tag") == "a &lt;b> tag"
    assert gen.markdown_text("pass `<name>` here") == "pass `<name>` here"
    plain = "risk < 0.40, x -> y, a | b, client_session_id"
    assert gen.markdown_text(plain) == plain


def test_description_markdown_fences_layout_and_leaves_prose():
    text = "Prose line.\n- a bullet\n\nRETURNS:\n{\n  \"k\": 1\n}\n\nget    one\nlist   many"
    rendered = gen.description_markdown(text)
    assert rendered.startswith("Prose line.\n- a bullet\n\n~~~text\nRETURNS:\n{\n  \"k\": 1\n}\n~~~")
    assert rendered.endswith("~~~text\nget    one\nlist   many\n~~~")
    assert gen.description_markdown("x\n  ~~~~ y").startswith("~~~~~text\n")
    # A leading laid-out block keeps its first line's indentation.
    assert gen.description_markdown("\n  indented\nnext").startswith("~~~text\n  indented\n")
    # A stray fence in prose is fenced itself instead of opening one.
    assert gen.description_markdown("see:\n```\nunclosed").startswith("~~~text\nsee:\n```")


def _fence_lines_outside_generated_fences(markdown):
    stray, fence = [], None
    for line in markdown.splitlines():
        if fence is None:
            opened = re.match(r"^(~{3,})text$", line)
            if opened:
                fence = opened.group(1)
            elif re.match(r"^(`{3,}|~{3,})", line):
                stray.append(line)
        elif line == fence:
            fence = None
    return stray, fence


def test_no_fence_escapes_into_the_page(markdown):
    """An unbalanced fence would swallow every later heading, and the other
    structural tests read the source, so they would not notice."""
    stray, still_open = _fence_lines_outside_generated_fences(markdown)
    assert stray == [] and still_open is None


def test_rendering_is_deterministic(reference, markdown):
    assert gen.render(reference) == markdown
    # The generator's own framing carries no generation stamp; descriptions
    # below it may quote dates as examples, which is data, not a stamp.
    assert not re.search(r"\b20\d\d-\d\d-\d\d", _intro(markdown)), "a generation stamp was rendered"


# --- exit codes: a verdict only when something was compared --------------------


@pytest.fixture
def isolated_out(tmp_path, monkeypatch):
    out = tmp_path / "TOOL_REFERENCE.md"
    monkeypatch.setattr(gen, "OUT", out)
    # Pinning is for a fresh process; here the handlers are already imported.
    monkeypatch.setattr(gen.edge_index, "pin_timeout_environment", lambda: None)
    return out


def test_write_then_check_then_drift(isolated_out, capsys):
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0
    isolated_out.write_text(isolated_out.read_text() + "edited by hand\n")
    capsys.readouterr()
    assert gen.main(["--check"]) == 1
    last = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()][-1]
    assert last == gen.stale_verdict()


def test_cannot_look_exits_2_and_leaves_the_file_alone(isolated_out, monkeypatch, capsys):
    def unavailable():
        raise gen.edge_index.MissingDependency(
            "mcp", ModuleNotFoundError("No module named 'mcp'", name="mcp")
        )

    isolated_out.write_text("SENTINEL\n")
    monkeypatch.setattr(gen.edge_index, "_load_registries", unavailable)
    assert gen.main([]) == 2
    assert gen.main(["--check"]) == 2
    assert isolated_out.read_text() == "SENTINEL\n"
    assert gen.INSTALL_REMEDY in capsys.readouterr().err


def test_a_registry_that_did_not_import_exits_2_as_a_tree_defect(isolated_out, monkeypatch, capsys):
    def broken():
        raise ImportError("cannot import name 'probe' from 'src.mcp_handlers.core'")

    monkeypatch.setattr(gen.edge_index, "_load_registries", broken)
    assert gen.main([]) == 2
    err = capsys.readouterr().err
    assert "defect in the tree" in err and gen.INSTALL_REMEDY not in err


def test_an_import_error_after_loading_is_a_crash_not_cannot_look(monkeypatch):
    """The registry loaded, so "cannot look" would be false: this generator is
    out of step with the tree, and says so with a traceback."""
    from src.mcp_handlers.introspection import tool_catalog

    monkeypatch.delattr(tool_catalog, "category_presentation")
    with pytest.raises(ImportError):
        gen.build()


@pytest.mark.parametrize("failures", [["src.mcp_handlers.probe: ImportError: boom"]])
def test_a_handler_that_did_not_import_is_refused_not_published(
    isolated_out, monkeypatch, capsys, failures
):
    real = gen.edge_index._load_registries

    def with_failure():
        definitions, schemas, aliases, _ = real()
        return definitions, schemas, aliases, failures

    isolated_out.write_text("SENTINEL\n")
    monkeypatch.setattr(gen.edge_index, "_load_registries", with_failure)
    assert gen.main([]) == gen.EXIT_REFUSED
    assert gen.main(["--check"]) == gen.EXIT_REFUSED
    assert isolated_out.read_text() == "SENTINEL\n"
    err = capsys.readouterr().err
    assert "refusing to generate" in err and "boom" in err
    assert "stale" not in err


# --- the committed file carries shipped defaults ---------------------------------

OVERRIDES = {"UNITARES_DIALECTIC_REVIEW_BUDGET": "70", "UNITARES_CALL_MODEL_TIMEOUT": "999"}


def _render_in_subprocess(env, *, pin):
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from scripts.diagnostics import generate_tool_docs as gen
        if {pin!r}:
            gen.edge_index.pin_timeout_environment()
        status, content = gen.build()
        sys.stdout.write(content if content is not None else f"EXIT {{status}}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(REPO), env=env, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_timeout_variables_cannot_leak_into_the_reference():
    """A developer's overrides must not be committed as if they shipped.

    Each unpinned single-variable run is a control: it proves that variable
    really moves the output, so the pinned run's agreement with the clean one
    is not vacuous, and a renamed variable fails here rather than leaking.
    """
    assert set(OVERRIDES) == set(gen.edge_index.PINNED_TIMEOUT_VARIABLES)
    clean = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    baseline = _render_in_subprocess(clean, pin=True)
    assert _render_in_subprocess(dict(clean, **OVERRIDES), pin=True) == baseline
    for name, value in OVERRIDES.items():
        unpinned = _render_in_subprocess(dict(clean, **{name: value}), pin=False)
        assert unpinned != baseline, f"{name} no longer moves the output"


# --- the doctor ------------------------------------------------------------------


def test_the_doctor_skips_when_the_generator_cannot_look(tmp_path):
    """Through the doctor's own check: a missing dependency is SKIP, never a
    stale verdict about a document nothing was compared against."""
    import unitares_doctor

    shim = tmp_path / "scripts" / "diagnostics" / "generate_tool_docs.py"
    shim.parent.mkdir(parents=True)
    real = REPO / "scripts" / "diagnostics" / "generate_tool_docs.py"
    shim.write_text(
        textwrap.dedent(
            f"""
            import runpy, sys
            sys.modules["mcp"] = None
            sys.argv = [{str(real)!r}, *sys.argv[1:]]
            runpy.run_path({str(real)!r}, run_name="__main__")
            """
        )
    )
    result = unitares_doctor.check_tool_reference_fresh(tmp_path)
    assert result.status == unitares_doctor.Status.SKIP, (result.message, result.detail)
    assert "stale" not in result.message
    assert "mcp" in (result.detail or "") and "requirements-full.txt" in (result.detail or "")


def test_the_doctor_reports_a_refusal_as_unknown_with_its_own_remedy(tmp_path):
    """Exit 3 compared nothing, so it is not "stale"; and its cause is the one
    the generator names, not the generator, so "fix the generator" would
    misdirect."""
    import unitares_doctor

    shim = tmp_path / "scripts" / "diagnostics" / "generate_tool_docs.py"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        "import sys\n"
        "print('refusing to generate docs/dev/TOOL_REFERENCE.md: 1 handler "
        "module(s) failed to import', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    result = unitares_doctor.check_tool_reference_fresh(tmp_path)
    assert result.status == unitares_doctor.Status.WARN, (result.message, result.detail)
    assert "UNKNOWN" in result.message and "stale" not in result.message
    assert "fix the generator" not in (result.detail or "")
    assert "handler module" in (result.detail or "")
