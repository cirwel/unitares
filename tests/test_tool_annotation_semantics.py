"""The annotation VALUES, held against the code that decides them.

``tests/test_tool_annotations.py`` guards the shape of the annotation surface:
that every advertised tool has a record, that the record serializes to the spec
wire keys, and that the hints survive every listing path including the
stdio-over-REST rebuild. It does not guard a single boolean's value beyond one
cross-check against ``ToolMeta.operation``.

That gap was measured, not guessed. A verification pass re-broke each of four
recently repaired values one at a time — the readOnlyHint on an identity-gated
reader, a display title made to collide with another tool's, ``config``'s
destructiveHint, and ``search_shared_memory``'s readOnlyHint — and the whole
eight-suite battery of 329 tests passed with exit 0 every time. The values were
correct and nothing held them.

This file holds them, and holds them the way the server decides them rather
than by restating today's answer:

* the identity gate is read through ``get_call_identity_requirement``, the same
  function the dispatch middleware calls, not through a hand-listed set;
* router dominance is derived from the live ``action_router`` routing tables,
  so a new action is covered on the day it is wired;
* the audit-writing read set is derived by call-graph reachability over
  ``src/mcp_handlers/knowledge/handlers.py``, so a new handler that grows a
  ``_broadcast_knowledge_read`` call is covered without an edit here.

Every test enumerates the live advertised surface. No test hardcodes a name or
a count except as a floor assertion that the derivation found anything at all —
a derivation that silently resolves to the empty set is a passing test that
guards nothing, which is the failure mode this whole file exists to answer.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, Callable, Dict, List, Optional, Tuple

import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator
from src.interface_contract import get_public_tool_definitions
from src.mcp_handlers.decorators import (
    _TOOL_DEFINITIONS,
    get_call_identity_requirement,
    resolve_canonical_action_and_source,
)
from src.tool_annotations import TOOL_ANNOTATIONS
import pytest


pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def advertised_names() -> List[str]:
    """Every advertised tool name, aliases included, as the server builds them."""
    return [tool.name for tool in get_public_tool_definitions("full")]


def _base_function(func: Callable) -> Callable:
    """The undecorated function under a chain of ``functools.wraps`` wrappers.

    Every handler reaches the registry through at least ``@mcp_tool``, and the
    routers add a second wrapper, so identity comparisons have to be made on
    the function underneath rather than on the registered object.
    """
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _closure(func: Callable) -> Dict[str, Any]:
    if func.__closure__ is None:
        return {}
    return dict(
        zip(func.__code__.co_freevars, [cell.cell_contents for cell in func.__closure__])
    )


def _routing_table(tool_name: str) -> Optional[Dict[str, Callable]]:
    """The ``action -> handler`` map of an ``action_router`` tool, or None.

    Read out of the router closure rather than re-declared here: the map in
    ``src/mcp_handlers/consolidated.py`` is the only authority on what an
    action dispatches into, and a copy of it in a test would be one more thing
    that can drift.
    """
    definition = _TOOL_DEFINITIONS.get(tool_name)
    if definition is None:
        return None
    func: Optional[Callable] = definition.handler
    while func is not None:
        cells = _closure(func)
        actions = cells.get("actions")
        if isinstance(actions, dict):
            return actions
        func = getattr(func, "__wrapped__", None)
    return None


def _registered_tool_by_base_function() -> Dict[Callable, str]:
    """Inverse of the registry: undecorated handler -> the tool name it serves."""
    return {
        _base_function(definition.handler): name
        for name, definition in _TOOL_DEFINITIONS.items()
    }


def dispatch_targets(name: str) -> List[Tuple[str, Callable]]:
    """Every handler a call to advertised ``name`` can land in.

    Two indirections, resolved the way the dispatcher resolves them:

    * a workflow alias PINS one action (``search_shared_memory`` is
      ``knowledge(action='search')`` and can be nothing else), so only that
      handler is reachable;
    * a plain router name is reachable at every action in its table. A router
      with a ``default_action`` resolves to that action on a bare call, but the
      caller may pass any action, and one annotation covers all of them — so
      the default must NOT narrow the set. Getting this wrong is how a
      dominance check silently stops covering ``config(action='set')``.
    """
    canonical, action, source = resolve_canonical_action_and_source(name, {})
    table = _routing_table(canonical)
    if table is None:
        definition = _TOOL_DEFINITIONS.get(canonical)
        return [(action or "", definition.handler)] if definition else []
    if source == "alias_injected" and action in table:
        return [(action, table[action])]
    return list(table.items())


def dispatch_children(name: str) -> List[Tuple[str, str]]:
    """The subset of ``dispatch_targets`` that are themselves annotated tools.

    Returns ``(action, child)`` pairs — the only case where two annotation
    records describe one code path and can therefore disagree.
    """
    by_base = _registered_tool_by_base_function()
    annotated = set(TOOL_ANNOTATIONS)
    children: List[Tuple[str, str]] = []
    for action, handler in dispatch_targets(name):
        child = by_base.get(_base_function(handler))
        if child and child != name and child in annotated:
            children.append((action, child))
    return children


def _functions_reaching(module_path: pathlib.Path, target: str) -> set:
    """Module-level functions whose call graph reaches ``target``, transitively.

    Static and deliberately shallow: it reads one module's own call names and
    closes over them, which is enough because the audit-emitting helper and
    every handler that reaches it live in the same file. A same-named method on
    an unrelated object would be a false positive here; that direction only
    ever makes the guard stricter, never permissive.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    called: Dict[str, set] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    if isinstance(func, ast.Name):
                        names.add(func.id)
                    elif isinstance(func, ast.Attribute):
                        names.add(func.attr)
            called[node.name] = names

    reaching = {name for name, names in called.items() if target in names}
    changed = True
    while changed:
        changed = False
        for name, names in called.items():
            if name not in reaching and names & reaching:
                reaching.add(name)
                changed = True
    return reaching


def audit_writing_read_tools() -> Dict[str, str]:
    """Advertised tools whose dispatch path reaches ``_broadcast_knowledge_read``.

    Derived, not listed: every entry point that can land in a handler which
    fire-and-forget persists a ``knowledge_read`` row to ``audit.events``.
    Value is the handler function that carries the write, so a failure names
    the code rather than the conclusion.
    """
    handlers_path = REPO_ROOT / "src" / "mcp_handlers" / "knowledge" / "handlers.py"
    reaching = _functions_reaching(handlers_path, "_broadcast_knowledge_read")

    by_base = _registered_tool_by_base_function()
    found: Dict[str, str] = {}
    for name in advertised_names():
        canonical, action, _source = resolve_canonical_action_and_source(name, {})
        table = _routing_table(canonical)
        if table is None:
            definition = _TOOL_DEFINITIONS.get(canonical)
            candidates = [definition.handler] if definition else []
        elif action and action in table:
            candidates = [table[action]]
        else:
            candidates = list(table.values())
        for handler in candidates:
            base = _base_function(handler)
            if base.__name__ in reaching:
                found[name] = base.__name__
                break
        else:
            # A tool that only reaches the writer through a router it is not
            # itself: covered under the router's own name, not this one.
            child_names = {child for _act, child in dispatch_children(name)}
            for child in child_names:
                child_base = next(
                    (b for b, n in by_base.items() if n == child), None
                )
                if child_base is not None and child_base.__name__ in reaching:
                    found[name] = child_base.__name__
                    break
    return found


# ---------------------------------------------------------------------------
# 1. Identity gating
# ---------------------------------------------------------------------------


def test_no_read_only_hint_on_an_identity_gated_tool():
    """Prevents: readOnlyHint=True on a tool whose bare call MINTS an agent row.

    A tool that resolves to ``requires_identity='required'`` never reaches its
    handler on an unbound call. The dispatch middleware resolves identity
    first, with ``force_new=True``
    (src/mcp_handlers/middleware/identity_step.py:1069), which mints and
    persists an agent row before a single line of the handler runs. Such a tool
    is not read-only however purely it reads, and the first annotation pass got
    exactly this wrong — ``dashboard``, ``get_thresholds``,
    ``get_trajectory_status``, ``get_workspace_health``,
    ``list_process_bindings``, ``outcome_correlation`` and
    ``verify_trajectory_identity`` all read purely and all mint.

    The requirement is read through ``get_call_identity_requirement(name, {})``
    — the same call the middleware makes, alias- and action-aware. The first
    pass instead read ``func._mcp_pre_onboard_actions``, which is None on every
    tool, so every tool looked exempt and the bug was invisible.

    ``test_read_only_hint_never_contradicts_the_recorded_operation`` in
    tests/test_tool_annotations.py cannot catch this: the offending tools are
    recorded ``operation='read'`` in src/tool_meta.py, which is true of the
    handler and says nothing about the middleware in front of it.
    """
    gated_read_only = {}
    for name in advertised_names():
        payload = TOOL_ANNOTATIONS.get(name)
        if not payload or not payload["readOnlyHint"]:
            continue
        requirement = get_call_identity_requirement(name, {})
        if requirement == "required":
            gated_read_only[name] = requirement

    assert not gated_read_only, (
        "readOnlyHint=True on identity-gated tools "
        f"{sorted(gated_read_only)}: a bare call resolves to "
        "requires_identity='required', so the middleware mints and persists an "
        "agent row (src/mcp_handlers/middleware/identity_step.py:1069) before "
        "the handler runs. Either the hint is wrong or the tool needs a "
        "pre-onboard exemption; the middleware decides, not the handler body."
    )


def test_the_identity_gate_probe_still_sees_a_gate():
    """Guards the guard: the probe above must not resolve to 'nothing is gated'.

    If ``get_call_identity_requirement`` ever stops seeing the advertised
    names — a rename, an alias table change, a signature change — the test
    above passes vacuously and the invariant is unheld again. Assert that the
    live surface still splits both ways.
    """
    requirements = {
        name: get_call_identity_requirement(name, {}) for name in advertised_names()
    }
    assert "required" in requirements.values(), (
        "no advertised tool resolves to requires_identity='required'; the "
        "identity-gating guard above is passing vacuously"
    )
    assert "pre_onboard" in requirements.values(), (
        "no advertised tool is pre-onboard exempt; the probe is not reading "
        "the real gate"
    )


# ---------------------------------------------------------------------------
# 2. Title uniqueness
# ---------------------------------------------------------------------------


def test_annotation_titles_are_unique_across_the_advertised_set():
    """Prevents: two advertised tools sharing one display title.

    The title is what a client's tool picker shows instead of the wire name, so
    a collision makes two different tools indistinguishable at the point of
    choosing one. Three collisions were introduced and repaired in the same
    pass — a workflow alias and the raw primitive behind it kept identical
    titles (``sync_state``/``process_agent_update``,
    ``record_result``/``outcome_event``,
    ``check_working_state``/``get_governance_metrics``), which is precisely the
    pair a caller most needs to tell apart. The repair gives the primitive a
    ``(Primitive)`` suffix and leaves the plain workflow name on the alias.

    Nothing else in the suite reads two titles at once, so nothing stopped them
    from converging again.
    """
    titles: Dict[str, List[str]] = {}
    for name in advertised_names():
        payload = TOOL_ANNOTATIONS.get(name)
        if not payload:
            continue  # held by test_every_advertised_tool_has_an_annotation_record
        titles.setdefault(payload["title"], []).append(name)

    collisions = {
        title: sorted(names) for title, names in titles.items() if len(names) > 1
    }
    assert not collisions, (
        f"annotation titles shared by more than one advertised tool: {collisions}. "
        "A client's tool picker shows the title, not the name; give the raw "
        "primitive a '(Primitive)' suffix and leave the plain name on the "
        "workflow alias."
    )


# ---------------------------------------------------------------------------
# 3. Router dominance
# ---------------------------------------------------------------------------

_DOMINANCE_RULES = (
    # (hint, router value that is a violation when the child value is other)
    ("readOnlyHint", "a router must not claim readOnlyHint a dispatch target denies"),
    ("destructiveHint", "a router must carry destructiveHint from any target that has it"),
    ("idempotentHint", "a router must not claim idempotentHint a dispatch target denies"),
    ("openWorldHint", "a router must carry openWorldHint from any target that has it"),
)


def test_a_router_is_never_safer_than_what_it_dispatches_into():
    """Prevents: an action router advertising softer hints than its own targets.

    A router is one wire tool over many handlers, so its annotation has to be
    the union of the risks behind it: not more read-only, not less destructive,
    not more idempotent, and not less open-world than ANY tool its routing
    table reaches. ``config(action='set')`` dispatches straight into
    ``handle_set_thresholds`` (src/mcp_handlers/consolidated.py:270), which
    overwrites the previous override — so ``config`` carrying
    ``destructiveHint=False`` while ``set_thresholds`` carries True would
    advertise the same write as safe under one name and dangerous under
    another. That is the defect this prevents; it was introduced and repaired.

    The pairs are derived from the live routing tables and the alias resolver,
    never hardcoded, so a newly wired action whose handler is separately
    advertised is covered on the day it lands.
    """
    violations: List[str] = []
    covered: List[Tuple[str, str]] = []

    for name in advertised_names():
        parent = TOOL_ANNOTATIONS.get(name)
        if not parent:
            continue
        for action, child_name in dispatch_children(name):
            child = TOOL_ANNOTATIONS[child_name]
            covered.append((name, child_name))
            label = f"{name}(action={action!r}) -> {child_name}"
            if parent["readOnlyHint"] and not child["readOnlyHint"]:
                violations.append(f"{label}: router readOnlyHint=True, target False")
            if child["destructiveHint"] and not parent["destructiveHint"]:
                violations.append(f"{label}: target destructiveHint=True, router False")
            if parent["idempotentHint"] and not child["idempotentHint"]:
                violations.append(f"{label}: router idempotentHint=True, target False")
            if child["openWorldHint"] and not parent["openWorldHint"]:
                violations.append(f"{label}: target openWorldHint=True, router False")

    assert covered, (
        "no router/target pair was derived; the routing-table introspection "
        "stopped seeing action_router closures and this guard is vacuous"
    )
    assert not violations, (
        "an action router advertises softer hints than a handler it dispatches "
        "into:\n  " + "\n  ".join(sorted(violations)) + "\nThe router's "
        "annotation must be the union of the risks behind it."
    )


def test_router_dominance_covers_the_config_set_thresholds_pair():
    """The pair that motivated the rule must actually be in the derived set.

    Not a second assertion of the rule — a check that the derivation still
    finds the case it was built for. If ``config`` stops resolving to
    ``set_thresholds``, the dominance test keeps passing while covering less.
    """
    pairs = {(action, child) for action, child in dispatch_children("config")}
    assert ("set", "set_thresholds") in pairs, (
        f"config no longer resolves action='set' to set_thresholds; got {pairs}"
    )


# ---------------------------------------------------------------------------
# 4. Reads that write an audit row
# ---------------------------------------------------------------------------


def test_a_read_that_persists_an_audit_row_is_neither_read_only_nor_idempotent():
    """Prevents: readOnlyHint/idempotentHint on a search that writes audit.events.

    ``_broadcast_knowledge_read`` (src/mcp_handlers/knowledge/handlers.py:539)
    emits a ``knowledge_read`` event, and ``broadcast_event`` fire-and-forget
    persists every event to ``audit.events`` through a tracked task
    (src/broadcaster.py:110-116). So a knowledge search is a read for the
    caller and a durable write for the server: each call appends a row, which
    makes it neither read-only nor idempotent. Both hints were wrong on
    ``search_shared_memory`` and ``search_knowledge_graph`` before the repair,
    and an agent framework that auto-approves readOnlyHint tools would have
    been silently writing rows on every retry.

    The set is derived by call-graph reachability over the knowledge handlers
    module, so a handler that grows a broadcast call later is covered here
    without anyone editing this test.
    """
    offenders: List[str] = []
    reached = audit_writing_read_tools()
    for name, handler in sorted(reached.items()):
        payload = TOOL_ANNOTATIONS.get(name)
        if not payload:
            continue
        if payload["readOnlyHint"]:
            offenders.append(f"{name} (via {handler}): readOnlyHint=True")
        if payload["idempotentHint"]:
            offenders.append(f"{name} (via {handler}): idempotentHint=True")

    assert not offenders, (
        "tools whose handler path reaches _broadcast_knowledge_read "
        "(src/mcp_handlers/knowledge/handlers.py:539) advertise as a pure "
        "read:\n  " + "\n  ".join(offenders) + "\nEvery call persists a "
        "knowledge_read row to audit.events (src/broadcaster.py:110-116), so "
        "readOnlyHint and idempotentHint must both be False."
    )


def test_the_audit_writing_read_set_still_contains_both_searches():
    """Guards the guard: the reachability derivation must still find the searches.

    ``search_shared_memory`` reaches the writer as the workflow alias for
    ``knowledge(action='search')``; ``search_knowledge_graph`` is the raw tool
    behind the same handler. If either drops out of the derived set, the test
    above passes while guarding less than it did.
    """
    reached = audit_writing_read_tools()
    for name in ("search_shared_memory", "search_knowledge_graph"):
        assert name in reached, (
            f"{name} no longer resolves to a handler that reaches "
            f"_broadcast_knowledge_read; derived set was {sorted(reached)}"
        )
