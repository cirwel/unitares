"""Guard: every param a knowledge handler READS must be one an agent can SEND.

Why this exists (2026-06-21): supersession looked wired but wasn't — the
handlers read `superseded_by` / `supersedes` / `supersedes_id`, but those were
not declared on `KnowledgeParams`, so the unified `knowledge` tool's advertised
inputSchema (built from the Pydantic model, src/tool_schemas.py) never exposed
them and the params were dropped before the handler ever ran. Status could be
set, the link never recorded → 18 superseded rows, 0 edges. This is the
recurring "Pydantic schema is authoritative about what handlers see" bug class
(KG 2026-04-19): a handler reading a param the schema doesn't expose is a silent
dead read.

This lint catches that class statically: it AST-scans the knowledge handlers for
every `arguments.get("X")` / `arguments["X"]`, and asserts each key is either
declared on `KnowledgeParams`, an internal injected key, or an explicitly
recorded known-gap. A NEW undeclared, unclassified read fails the suite — so the
next `superseded_by` can't slip in silently.

NOT inert: test_lint_fires_on_synthetic_undeclared_read proves the check rejects
an undeclared read (a lint that never fires would be the very inertia it guards).
"""
from __future__ import annotations

import ast
from pathlib import Path

from src.mcp_handlers.schemas.knowledge import KnowledgeParams

HANDLERS = Path(__file__).resolve().parent.parent / "src" / "mcp_handlers" / "knowledge" / "handlers.py"


def _read_arg_keys(source: str) -> set[str]:
    """Every string-literal key read from `arguments` in the source."""
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        # arguments.get("X" [, default])
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "arguments"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        # arguments["X"]
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "arguments"
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
    return keys


# Server-injected / plumbing keys an agent never sends — legitimately absent
# from the schema. Underscore-prefixed keys are auto-allowed below.
INTERNAL_KEYS = {
    "discoveries",   # pre-resolved result list passed between internal steps
    "text",          # internal alias-resolution scratch
}

# Read by a handler but NOT exposed on KnowledgeParams as of 2026-06-21. Each is
# either a genuinely useful param the unified tool should expose (search/get
# controls like semantic, search_mode, min_similarity, include_archived,
# include_cold, include_provenance, response_to, offset/length/...) or legacy
# plumbing. This is a VISIBLE backlog, not a hidden one: the goal is to shrink
# it by declaring the real params on KnowledgeParams (then deleting them here),
# not to grow it. Adding a NEW entry must be a deliberate, reviewed act.
KNOWN_UNEXPOSED = {
    "auto_link_related", "exclude_agent_labels",
    "related_files", "resolve_question",
    "synthesize", "use_model",
}
# include_archived / include_cold were exposed on KnowledgeParams (2026-06-22) as
# recall-recovery levers. include_provenance / search_mode / semantic /
# min_similarity / operator followed on 2026-06-26 as handler-documented search
# controls. offset / length / include_response_chain / max_chain_depth /
# response_to followed as details/threading controls. confidence followed as a
# store-path writer-authored quality signal. epoch_scope / including_cold
# followed as list/stats scope controls. audit scope / top_n followed as public
# read-only audit controls. Shrinking this set is the goal; growing it is the
# smell.


def _classify_undeclared() -> set[str]:
    """Read keys that are neither declared, internal, nor a recorded known-gap."""
    read = _read_arg_keys(HANDLERS.read_text())
    declared = set(KnowledgeParams.model_fields.keys())
    allowed = declared | INTERNAL_KEYS | KNOWN_UNEXPOSED
    return {k for k in read if not k.startswith("_") and k not in allowed}


def test_no_new_unexposed_handler_param():
    """A knowledge handler must not read an agent param the schema can't send.

    If this fails, you read `arguments.get("X")` for an X the agent can't pass
    via the unified `knowledge` tool. Fix by declaring X on KnowledgeParams
    (preferred — exposes it), or, if X is server-injected, add it to
    INTERNAL_KEYS with a note. Do NOT add it to KNOWN_UNEXPOSED to silence this
    unless it is a deliberate, reviewed deferral.
    """
    offenders = _classify_undeclared()
    assert not offenders, (
        "knowledge handlers read params the unified schema does not expose "
        f"(silent-strip class): {sorted(offenders)}. Declare them on "
        "KnowledgeParams or classify them — see this file's docstring."
    )


def test_known_unexposed_has_no_declared_params():
    """Hygiene: a param that's been declared on the schema must not linger in the
    KNOWN_UNEXPOSED backlog — otherwise the backlog overstates what's missing."""
    declared = set(KnowledgeParams.model_fields.keys())
    stale = declared & KNOWN_UNEXPOSED
    assert not stale, f"declared params still listed as unexposed: {sorted(stale)}"


def test_archived_cold_recall_levers_exposed():
    """include_archived / include_cold must be agent-sendable (recall recovery)."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert {"include_archived", "include_cold"} <= declared


def test_search_routing_controls_exposed():
    """Handler-documented search knobs must be agent-sendable on knowledge()."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert {
        "include_provenance",
        "search_mode",
        "semantic",
        "min_similarity",
        "operator",
    } <= declared


def test_details_and_threading_controls_exposed():
    """Handler-documented details/threading knobs must be agent-sendable."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert {
        "offset",
        "length",
        "include_response_chain",
        "max_chain_depth",
        "response_to",
    } <= declared


def test_store_confidence_signal_exposed():
    """Store-path confidence is a writer-authored signal, not internal plumbing."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert "confidence" in declared
    assert KnowledgeParams(action="store", summary="x", confidence="0.8").confidence == 0.8
    assert KnowledgeParams(action="store", summary="x", confidence="not-a-number").confidence is None


def test_list_scope_controls_exposed():
    """List/stats scope controls must be sendable through knowledge(action=list)."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert {"epoch_scope", "including_cold"} <= declared
    model = KnowledgeParams(action="list", epoch_scope="all", including_cold="true")
    assert model.epoch_scope == "all"
    assert model.including_cold is True


def test_audit_scope_controls_exposed():
    """Audit controls must be sendable through knowledge(action=audit)."""
    declared = set(KnowledgeParams.model_fields.keys())
    assert {"scope", "top_n"} <= declared
    model = KnowledgeParams(action="audit", scope="by_agent", top_n="3")
    assert model.scope == "by_agent"
    assert model.top_n == 3


def test_audit_scope_controls_advertised_in_tool_schema():
    """Declaring fields is not enough; list_tools must advertise them."""
    from src.tool_schemas import get_tool_definitions

    knowledge = next(t for t in get_tool_definitions(verbosity="full") if t.name == "knowledge")
    props = knowledge.inputSchema["properties"]
    assert {"scope", "top_n"} <= set(props)


def test_supersession_link_params_stay_declared():
    """Regression: the params whose absence caused the supersession bug must
    remain on the schema (the live fix this lint generalizes)."""
    declared = set(KnowledgeParams.model_fields.keys())
    for field in ("superseded_by", "supersedes", "supersedes_id"):
        assert field in declared, f"{field} must stay declared on KnowledgeParams"


def test_lint_fires_on_synthetic_undeclared_read():
    """Not inert: the scanner+classifier must reject an undeclared read."""
    snippet = "def h(arguments):\n    return arguments.get('totally_made_up_param')\n"
    read = _read_arg_keys(snippet)
    assert "totally_made_up_param" in read
    declared = set(KnowledgeParams.model_fields.keys())
    allowed = declared | INTERNAL_KEYS | KNOWN_UNEXPOSED
    assert "totally_made_up_param" not in allowed  # would be flagged as an offender
