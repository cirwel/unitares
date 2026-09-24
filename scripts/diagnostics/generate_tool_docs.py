#!/usr/bin/env python3
"""Generate docs/dev/TOOL_REFERENCE.md — what every MCP tool does.

A reader's reference to the tool surface: each registered tool's tier,
identity class, timeout, actions, and the other names that reach it, followed
by the description ``describe_tool`` serves, grouped by category. Its sibling
``docs/dev/TOOL_EDGE_INDEX.md`` (``scripts/dev/tool_edge_index.py``) maps the
same surface to the code that runs; this file says what each tool is for.

Read from the runtime registry, never from the source text. The generator this
replaced scanned handler files for ``@mcp_tool`` decorators, a model of a tool
that fails in four ways the registry cannot:

1. It globbed only the top level of ``src/mcp_handlers/``, where 12 of the 43
   registered tools are declared, and its scan found 4 of those. The registry
   is populated by importing the package, wherever a tool is declared.
2. It rendered only the categories named in a fixed map and dropped every
   other tool without a word. Here every tool is filed under its
   ``src/tool_meta.py`` category, and a tool with no record lands under the
   fallback label instead of being skipped.
3. It missed every tool ``action_router`` builds, the 8 declared in
   ``consolidated.py`` among them, because a router carries no decorator at
   its call site. Its model also counts every decorated function as a tool,
   so widening the glob does not fix it: #2355 tried a recursive glob that
   rendered every category, and review found it documenting the
   ``register=False`` internals that consolidated tools delegate to. The
   registry holds exactly what dispatch serves; an internal name that still
   resolves appears only as an older name for its tool.
4. It read only literal ``timeout=`` arguments, so a timeout built from a
   constant is documented as the 30s default; the same review found
   ``consult``'s 480s shown as 30s. The registry holds the evaluated value.

The registry is loaded, and a router's action table read, through
``scripts/dev/tool_edge_index.py`` rather than a second implementation of
either, so the two generated documents cannot disagree about which tools exist
or where a router's actions go. Timeouts are pinned to their shipped defaults
by the same helper that script uses (``PINNED_TIMEOUT_VARIABLES`` there).

Usage:
    python3 scripts/diagnostics/generate_tool_docs.py          # write the reference
    python3 scripts/diagnostics/generate_tool_docs.py --check  # exit 1 if stale

Exit codes:
    0 — reference written, or up to date under --check
    1 — the reference is stale (--check)
    2 — cannot look: a dependency is not installed, or the handler package
        raised ImportError while importing. Nothing was generated or
        compared. Distinct from 1 so a caller can tell "cannot look" from
        "looked and found drift"; the doctor SKIPs on 2.
    3 — refused: the registry was read but is not fit to publish, because a
        handler module failed to import, a router's action table could not be
        read, or no first-party tool registered. Nothing is written, an
        existing reference is left as it was, and nothing was compared, so
        this is not a stale verdict either; the doctor reports it as UNKNOWN.
    Any other exception is a crash: Python prints the traceback and exits 1.
    That is not a stale verdict, and the doctor, which reads the traceback,
    reports it as UNKNOWN.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Standard library only at import time, like this module, so a runner with no
# project dependencies can import both and exit 2 rather than crash.
from scripts.dev import tool_edge_index as edge_index  # noqa: E402

OUT = REPO / "docs" / "dev" / "TOOL_REFERENCE.md"
GENERATOR = "scripts/diagnostics/generate_tool_docs.py"
INSTALL_REMEDY = "install requirements-full.txt to generate or check this reference"
UNPLACED_TITLE = "Names that reach no documented tool or action"


class RegistryDidNotImport(Exception):
    """The handler package raised ImportError for a reason in the tree."""


class RegistryUnfit(Exception):
    """The registry was read, but what it holds is not fit to publish."""


@dataclass
class ActionEntry:
    action: str
    is_default: bool
    identity: str
    timeout: float | None
    older_names: list[str] = field(default_factory=list)


@dataclass
class ToolEntry:
    name: str
    category: str
    tier: str
    operation: str
    stability: str
    identity: str
    pre_onboard_actions: list[str]
    timeout: float | None
    deprecated: bool
    hidden: bool
    superseded_by: str | None
    description: str
    depends_on: list[str]
    related_to: list[str]
    workflow_aliases: list[tuple[str, str | None]] = field(default_factory=list)
    older_names: list[str] = field(default_factory=list)
    actions: list[ActionEntry] = field(default_factory=list)
    routed: bool = False
    rest_unbounded: bool = False


@dataclass
class Section:
    title: str
    blurb: str
    tools: list[ToolEntry]


@dataclass
class Reference:
    sections: list[Section]
    advertised: int
    progressive: int
    workflow_alias_count: int
    unplaced_aliases: list[tuple[str, str]]
    rest_unbounded: list[str]


def _alias_call(alias) -> str:
    if alias.inject_action:
        return f"{alias.new_name}(action={alias.inject_action!r})"
    return alias.new_name


def served_description(name: str, definition, schema_model) -> str:
    """The description ``describe_tool`` serves for ``name``, resolved the same way.

    Mirrors ``handle_describe_tool``'s two branches; the tests hold the two
    equal for every tool. ``ToolDefinition.description`` alone is not it for a
    tool with a params model: a router's is the generated ``— actions: ...``
    summary, which no discovery surface serves.
    """
    from src.tool_descriptions import TOOL_DESCRIPTIONS

    if schema_model is not None:
        return TOOL_DESCRIPTIONS.get(name) or schema_model.__doc__ or f"Tool: {name}"
    return definition.description or TOOL_DESCRIPTIONS.get(name) or f"Tool: {name}"


def _action_entries(
    name: str, definition, aliases: dict, workflow: set[str]
) -> tuple[list[ActionEntry], bool]:
    """A tool's actions with a ceiling on each one's run time, and whether it routes.

    A router's delegate is usually itself an ``@mcp_tool(register=False)``
    wrapper with its own ``asyncio.wait_for``, nested inside the router's, so
    the call ends by the smaller of the two limits; ``functools.wraps``
    carries the delegate's ``_mcp_timeout`` onto that wrapper. That is a
    ceiling, not an exact limit: a delegate that hands some inputs to another
    decorated handler stops at that handler's limit instead. A tool that
    declares ``known_actions`` by hand and branches in its own body
    (``self_recovery``) exposes no action table to read, so each of its
    actions is shown with the tool's own limit, which is again only a ceiling.
    """
    from src.mcp_handlers.decorators import get_call_identity_requirement

    action_map, _param_maps = edge_index._router_actions(definition.handler)
    known = set(definition.known_actions or ())
    if definition.reads_op_as_action and set(action_map) != known:
        # Only action_router sets reads_op_as_action, and it derives
        # known_actions from the same map read here out of its closure. A
        # mismatch means that read broke, and every per-action limit below
        # would silently fall back to the router's.
        raise RegistryUnfit(
            f"{name}: the action table read from the router closure "
            f"({sorted(action_map)}) does not match known_actions ({sorted(known)})"
        )

    entries = []
    for action in sorted(known):
        limit = definition.timeout
        declared = getattr(action_map.get(action), "_mcp_timeout", None)
        if declared is not None:
            limit = declared if limit is None else min(limit, declared)
        entries.append(
            ActionEntry(
                action=action,
                is_default=action == definition.default_action,
                identity=get_call_identity_requirement(name, {"action": action}),
                timeout=limit,
                older_names=sorted(
                    old for old, alias in aliases.items()
                    if alias.new_name == name and alias.inject_action == action
                    and old not in workflow
                ),
            )
        )
    return entries, bool(action_map)


def _rest_unbounded(definitions: dict) -> set[str]:
    """Tools REST answers through a direct handler that skips the timeout wrapper.

    ``/v1/tools/call`` short-circuits dispatch for the tools in
    ``_DIRECT_HTTP_TOOL_HANDLERS``. Where the mapped handler is the registered
    ``@mcp_tool`` wrapper the limit still applies; where it is a separate
    function nothing wraps it in ``asyncio.wait_for``.
    """
    from src.services.http_tool_service import _DIRECT_HTTP_TOOL_HANDLERS

    return {
        name for name, handler in _DIRECT_HTTP_TOOL_HANDLERS.items()
        if name in definitions and handler is not definitions[name].handler
    }


def _load_registries():
    try:
        return edge_index._load_registries()
    except edge_index.MissingDependency:
        raise
    except ImportError as exc:
        # Only the loader's own import failure means "cannot look". An
        # ImportError later in collect() or render() is this generator out of
        # step with the tree, and propagates as the crash it is.
        raise RegistryDidNotImport(exc) from exc


def collect() -> Reference:
    """Load the registries and build the reference model.

    Raises ``tool_edge_index.MissingDependency`` or ``RegistryDidNotImport``
    when the registry cannot be read at all, and ``RegistryUnfit`` when it can
    be read but would publish an incomplete surface.
    """
    definitions, schemas, aliases, failures = _load_registries()
    if failures:
        raise RegistryUnfit(
            f"{len(failures)} handler module(s) failed to import, so tools may "
            "be missing: " + "; ".join(failures)
        )
    if not definitions:
        raise RegistryUnfit("no first-party tool is registered")

    from src.mcp_handlers.introspection.tool_catalog import category_presentation
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES, get_tool_stability
    from src.tool_meta import TOOL_META_BY_NAME, WIRE_ORDER

    wire_position = {name: index for index, name in enumerate(WIRE_ORDER)}
    workflow = set(AGENT_WORKFLOW_ALIASES)
    rest_unbounded = _rest_unbounded(definitions)

    tools: list[ToolEntry] = []
    for name, definition in definitions.items():
        meta = TOOL_META_BY_NAME.get(name)
        actions, routed = _action_entries(name, definition, aliases, workflow)
        tools.append(
            ToolEntry(
                name=name,
                category=meta.category if meta else "",
                tier=meta.tier if meta else "—",
                operation=meta.operation if meta else "—",
                stability=get_tool_stability(name).value,
                identity=definition.requires_identity,
                pre_onboard_actions=sorted(definition.pre_onboard_actions or ()),
                timeout=definition.timeout,
                deprecated=definition.deprecated,
                hidden=definition.hidden,
                superseded_by=definition.superseded_by,
                description=served_description(name, definition, schemas.get(name)),
                depends_on=list(meta.depends_on) if meta else [],
                related_to=list(meta.related_to) if meta else [],
                workflow_aliases=[
                    (old, aliases[old].inject_action)
                    for old in AGENT_WORKFLOW_ALIASES
                    if old in aliases and aliases[old].new_name == name
                ],
                older_names=sorted(
                    old for old, alias in aliases.items()
                    if alias.new_name == name and not alias.inject_action
                    and old not in workflow
                ),
                actions=actions,
                routed=routed,
                rest_unbounded=name in rest_unbounded,
            )
        )

    # Every alias is shown somewhere. One the lists above did not place (a
    # target that is not registered, or an action the target does not take)
    # gets a table of its own rather than vanishing.
    placed = {alias for tool in tools for alias, _ in tool.workflow_aliases}
    placed |= {old for tool in tools for old in tool.older_names}
    placed |= {old for tool in tools for entry in tool.actions for old in entry.older_names}

    by_category: dict[str, list[ToolEntry]] = {}
    for tool in tools:
        by_category.setdefault(tool.category, []).append(tool)

    ordered = sorted(
        by_category.items(),
        key=lambda item: (
            category_presentation(item[0])["priority"],
            category_presentation(item[0])["name"],
        ),
    )
    sections = []
    for category, members in ordered:
        presentation = category_presentation(category)
        members.sort(key=lambda t: (wire_position.get(t.name, len(wire_position)), t.name))
        sections.append(
            Section(
                title=presentation["name"],
                blurb=presentation.get("description", ""),
                tools=members,
            )
        )

    # What full-mode discovery serves: hidden tools, and a workflow alias whose
    # tool is hidden, never reach it. The progressive listing (the default
    # tools/list) is read from the catalog builder with its mode named, so the
    # environment's advertisement setting cannot change the number.
    from src.interface_contract import get_public_tool_definitions

    visible = {tool.name for tool in tools if not tool.hidden}
    served_aliases = {
        alias for alias in workflow
        if alias in aliases and aliases[alias].new_name in visible
    }
    catalog = visible | served_aliases
    progressive = {tool.name for tool in get_public_tool_definitions("progressive")}
    return Reference(
        sections=sections,
        advertised=len(catalog),
        progressive=len(progressive & catalog),
        workflow_alias_count=len(served_aliases),
        unplaced_aliases=sorted(
            (old, _alias_call(alias)) for old, alias in aliases.items() if old not in placed
        ),
        rest_unbounded=sorted(rest_unbounded),
    )


# --- rendering ---------------------------------------------------------------

_HTML_START = re.compile(r"<(?=[A-Za-z/!?])")
_SETEXT_UNDERLINE = re.compile(r"^(-+|=+)$")
_CODE_SPAN = re.compile(r"(`+).+?\1")
# A line whose layout carries meaning: indented, a JSON or table edge, columns
# aligned with runs of spaces, or a code fence of its own.
_PREFORMATTED = re.compile(r"^\s|^[\[\]{}|]|\S {3,}\S|^(?:`{3,}|~{3,})")


def _escape_html(body: str) -> str:
    """``<`` before a letter, outside code spans, where it could open a tag."""
    out, last = [], 0
    for span in _CODE_SPAN.finditer(body):
        out.append(_HTML_START.sub("&lt;", body[last:span.start()]))
        out.append(span.group(0))
        last = span.end()
    out.append(_HTML_START.sub("&lt;", body[last:]))
    return "".join(out)


def markdown_text(text: str) -> str:
    """Prose as markdown, escaping only what would change its structure.

    Left as it is, a line opening with ``#`` or ``>`` would render as a
    heading or a quote, a line of ``-`` or ``=`` would turn the line above it
    into a heading, and ``<`` before a letter could open an HTML tag.
    """
    lines = []
    for line in text.strip().splitlines():
        line = line.rstrip()
        body = line.lstrip()
        indent = line[: len(line) - len(body)]
        if body.startswith(("#", ">")) or _SETEXT_UNDERLINE.match(body):
            body = "\\" + body
        lines.append(indent + _escape_html(body))
    return "\n".join(lines)


def description_markdown(text: str) -> str:
    """A served description, with every block whose layout matters kept verbatim.

    Descriptions mix prose with JSON examples, indented action lists and
    aligned columns. As markdown paragraphs those lose their line breaks and
    indentation, so any blank-line-separated block containing such a line is
    fenced exactly as served, first-line indentation included; the rest is
    prose.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    blocks = []
    for block in re.split(r"\n[ \t]*\n", "\n".join(lines)):
        lines = block.splitlines()
        if any(_PREFORMATTED.search(line) for line in lines):
            longest = max((len(run) for run in re.findall(r"~+", block)), default=0)
            fence = "~" * max(3, longest + 1)
            blocks.append("\n".join([f"{fence}text", *lines, fence]))
        else:
            blocks.append(markdown_text("\n".join(lines)))
    return "\n\n".join(blocks)


def seconds(value: float | None) -> str:
    return "none" if value is None else f"{value:g}s"


def _codes(names) -> str:
    return ", ".join(f"`{n}`" for n in names)


def anchor(title: str) -> str:
    """GitHub's heading anchor for ``title``."""
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def _identity_line(tool: ToolEntry) -> str:
    text = f"`{tool.identity}`"
    if tool.pre_onboard_actions:
        text += f"; `pre_onboard` for {_codes(tool.pre_onboard_actions)}"
    return text


def _timeout_line(tool: ToolEntry) -> str:
    if tool.timeout is None:
        return "none of its own; the forwarded call is limited as its target is (see Timeouts above)"
    text = seconds(tool.timeout)
    if tool.routed and any(a.timeout != tool.timeout for a in tool.actions):
        text += " for the router; some actions stop sooner (table below)"
    if tool.rest_unbounded:
        text += "; not applied on REST (see Timeouts above)"
    return text


def _render_tool(tool: ToolEntry) -> list[str]:
    lines = [
        f"### `{tool.name}`",
        "",
        f"- **Tier** {tool.tier} · **operation** {tool.operation} · "
        f"**stability** {tool.stability}",
        f"- **Identity:** {_identity_line(tool)}",
        f"- **Timeout:** {_timeout_line(tool)}",
    ]
    if tool.deprecated:
        replacement = f"; use `{tool.superseded_by}`" if tool.superseded_by else ""
        lines.append(f"- **Deprecated**{replacement}")
    if tool.hidden:
        lines.append("- **Hidden** from discovery")
    for alias_name, action in tool.workflow_aliases:
        target = f" (action `{action}`)" if action else ""
        lines.append(f"- **Workflow alias:** `{alias_name}`{target}")
    if tool.older_names:
        lines.append(f"- **Older names:** {_codes(tool.older_names)}")
    if tool.depends_on:
        lines.append(f"- **Depends on:** {_codes(tool.depends_on)}")
    if tool.related_to:
        lines.append(f"- **Related:** {_codes(tool.related_to)}")
    lines += ["", description_markdown(tool.description), ""]

    if tool.actions:
        lines += [
            "| Action | Identity | Timeout (at most) | Older names |",
            "|---|---|---|---|",
        ]
        for entry in tool.actions:
            label = f"`{entry.action}`" + (" (default)" if entry.is_default else "")
            lines.append(
                f"| {label} | {entry.identity} | {seconds(entry.timeout)} | "
                f"{_codes(entry.older_names) or '—'} |"
            )
        lines.append("")
    return lines


INTRO = """\
# Tool Reference

**What every MCP tool does, as the running server describes it.** Generated by
importing the handler package and reading the live registries: the decorator
registry for timeouts, identity and actions, `src/tool_meta.py` for category,
tier and operation, the alias table for each tool's other names, and the
description `describe_tool` serves. Regenerate with `make docs`.

{counts}

- **Parameters** are not reproduced here. `describe_tool(tool_name=...,
  action=...)` returns a tool's schema, narrowed to one action on a router.
- **Where the code is:** [`TOOL_EDGE_INDEX.md`](TOOL_EDGE_INDEX.md) maps every
  tool, action and alias to the function that runs.
- **Identity** is the class each call is declared with. The identity gates on
  MCP dispatch and on REST exempt `pre_onboard` calls. What an unbound
  `required` call meets depends on the transport and on
  `STRICT_IDENTITY_REQUIRED`, and a handler can add checks of its own; see
  [`identity.md`](../ontology/identity.md).
- **Timeouts** are the limits each tool's `@mcp_tool` wrapper enforces, at the
  shipped defaults. On a running server these variables change some of them:
  {variables}.{rest}
- **Action timeouts are ceilings.** A router action ends by the smaller of the
  router's limit and the one its delegate declares, and sooner when the
  delegate hands the call to a handler with a shorter limit.{hand_routed}
- **Older names** are alias-table entries. Dispatch resolves them to their
  tool, with the action shown, on REST `/v1/tools/call` and in a stdio server
  that dispatches locally. The MCP endpoint (`/mcp`) mounts only the
  registered tools and the workflow aliases, so an older name sent there,
  directly or through a bridge that forwards to it, is an unknown tool;
  `use_tool` refuses older names too. No listing advertises them.
- First-party tools only: entry-point plugins are disabled while this file is
  generated."""


def render(reference: Reference) -> str:
    tools = [tool for section in reference.sections for tool in section.tools]
    visible = sum(1 for tool in tools if not tool.hidden)
    hidden = len(tools) - visible
    counts = f"**{visible} registered tools"
    if hidden:
        counts += f" (+{hidden} hidden)"
    counts += (
        f" · {reference.advertised} advertised tools** in the full catalog: the "
        f"registered tools plus {reference.workflow_alias_count} workflow aliases."
        f"\nThe default progressive `tools/list` starts with {reference.progressive}"
        " of them; `list_tools`,\n`describe_tool` and `use_tool` reach the rest."
    )
    rest = ""
    if reference.rest_unbounded:
        rest = (
            f"\n  REST `/v1/tools/call` answers {_codes(reference.rest_unbounded)}"
            "\n  through direct handlers that skip that wrapper, so there they run"
            "\n  with no server-side limit."
        )
    hand_routed = sorted(t.name for t in tools if t.actions and not t.routed)
    hand_routed_text = ""
    if hand_routed:
        hand_routed_text = (
            f"\n  {_codes(hand_routed)} branch in their own body, so each of their"
            "\n  actions shows the tool's limit; the handler an action reaches may"
            "\n  stop sooner."
        )

    lines = [
        f"<!-- GENERATED by {GENERATOR} — do not edit by hand. Re-run to refresh. -->",
        "",
        INTRO.format(
            counts=counts,
            variables=", ".join(f"`{v}`" for v in edge_index.PINNED_TIMEOUT_VARIABLES),
            rest=rest,
            hand_routed=hand_routed_text,
        ),
        "",
        "## Contents",
        "",
        "| Category | Tools |",
        "|---|---:|",
    ]
    lines += [
        f"| [{section.title}](#{anchor(section.title)}) | {len(section.tools)} |"
        for section in reference.sections
    ]
    if reference.unplaced_aliases:
        lines.append(
            f"| [{UNPLACED_TITLE}](#{anchor(UNPLACED_TITLE)}) | "
            f"{len(reference.unplaced_aliases)} |"
        )
    lines.append("")

    for section in reference.sections:
        lines += [f"## {section.title}", ""]
        if section.blurb:
            lines += [section.blurb, ""]
        for tool in section.tools:
            lines += _render_tool(tool)

    if reference.unplaced_aliases:
        lines += [
            f"## {UNPLACED_TITLE}",
            "",
            "Aliases whose target is not a registered tool, or whose action the",
            "target does not take.",
            "",
            "| Name | Resolves to |",
            "|---|---|",
        ]
        lines += [f"| `{old}` | `{target}` |" for old, target in reference.unplaced_aliases]

    return "\n".join(lines).rstrip("\n") + "\n"


# --- entry point -------------------------------------------------------------

EXIT_REFUSED = 3
DIFF_LINE_LIMIT = 120


def _shown(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def stale_verdict() -> str:
    return f"{_shown(OUT)} is stale — run: python3 {GENERATOR} (or make docs)"


def stale_report(current: str, generated: str, *, limit: int = DIFF_LINE_LIMIT) -> str:
    """The differing lines, then the verdict as the LAST line.

    The doctor tells a verdict from a crash by the last line alone
    (``unitares_doctor._generator_crashed``), so nothing may follow it.
    """
    rel = _shown(OUT)
    diff = list(
        difflib.unified_diff(
            current.splitlines(),
            generated.splitlines(),
            fromfile=f"{rel} (committed)",
            tofile=f"{rel} (generated)",
            lineterm="",
            n=1,
        )
    )
    if len(diff) > limit:
        diff = diff[:limit] + [f"... {len(diff) - limit} more diff lines"]
    return "\n".join(diff + ["", stale_verdict()])


def build() -> tuple[int, str | None]:
    """``(0, markdown)``, or ``(exit status, None)`` once the reason is printed."""
    try:
        return 0, render(collect())
    except edge_index.MissingDependency as exc:
        return edge_index._cannot_look(f"{exc} — {INSTALL_REMEDY}"), None
    except RegistryDidNotImport as exc:
        return edge_index._cannot_look(
            f"the tool registry did not import ({exc}) — this is a defect in "
            "the tree, not a missing dependency; no reference could be built"
        ), None
    except RegistryUnfit as exc:
        print(f"refusing to generate {_shown(OUT)}: {exc}", file=sys.stderr)
        return EXIT_REFUSED, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the reference is stale")
    args = parser.parse_args(argv)

    edge_index.pin_timeout_environment()
    status, content = build()
    if content is None:
        return status

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != content:
            print(stale_report(current, content), file=sys.stderr)
            return 1
        print(f"{_shown(OUT)} is up to date.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {_shown(OUT)} ({content.count(chr(10))} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
