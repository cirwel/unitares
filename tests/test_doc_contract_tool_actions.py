"""Every action a living doc names for a routed tool is one that routes.

The doc lint (scripts/diagnostics/check_doc_health.py) is a denylist of known
bad phrasings, so it cannot notice a doc that tells an agent to call an action
the router never had or has since dropped: the call fails at dispatch with an
unknown-action error, after the reader trusted the doc. This test reads the
action vocabulary from the code instead -- the ``actions={...}`` routing table
of each ``action_router(...)`` call in src/mcp_handlers/consolidated.py, the
``action: Literal[...]`` of a non-router ``<Tool>Params`` model in
src/mcp_handlers/schemas/, and the ``_TOOL_ALIASES`` table, whose aliases pass
an explicit action through to their router -- all by parsing, never importing.
It then holds three call shapes to that vocabulary: ``tool(..., action=...)``
(also ``mcp__<server>__tool(...)``, ``'a'|'b'`` chains, and the router's
``op=`` alias when no ``action=`` is given),
``describe_tool(tool_name='t', action='a')`` and
``use_tool(tool_name='t', arguments={'action': 'a'})``. The scan covers the
git-visible living docs, the skills and the served src/tool_descriptions.json;
dated captures, proposals and point-in-time snapshots are records of their
moment and are left out. It is a subset check only: a doc may name some of a
router's actions; it may not name one that does not route. Mentions of tools
with no parsed vocabulary (plugin examples, ``cirs_protocol``'s two-level
selector) and placeholder values (``action=action``, ``action=Action.X``) are
skipped rather than guessed at.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_ROUTERS = REPO_ROOT / "src" / "mcp_handlers" / "consolidated.py"
_SCHEMAS = REPO_ROOT / "src" / "mcp_handlers" / "schemas"
_ALIASES = REPO_ROOT / "src" / "mcp_handlers" / "tool_stability.py"
_DESCRIPTIONS = REPO_ROOT / "src" / "tool_descriptions.json"

# Known-stale mentions awaiting a doc fix, keyed (doc, tool, action) so a line
# move does not break the key. Each entry must still fire: when the doc is
# fixed, test_known_stale_entries_still_fire fails until the entry is removed.
KNOWN_STALE: dict[tuple[str, str, str], str] = {}

# Records of their moment, not instructions: dated run captures and dogfood
# reads (docs/evaluations/), proposals, which name an action before it routes
# and keep the wording they were decided on (docs/proposals/), and any doc whose
# name carries its snapshot date (tool-surface-audit-2026-09-12.md).
HISTORICAL_PREFIXES = ("docs/evaluations/", "docs/proposals/")
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}(?:-\d{2})?\.md$")

# A call: `tool(` (optionally `mcp__<server>__tool(`) and its arguments, up to
# the closing paren, allowing one level of nested parentheses. The arguments
# never cross a backtick or a blank line, so an unclosed `tool(` in prose ends
# at its inline-code span or paragraph; the lookahead lets a call written
# inside another one's span still be matched on its own.
_ARG_CHAR = r"(?:[^()`\n]|\n(?![ \t]*\n))"
_CALL = re.compile(
    r"(?<![\w.])(?:mcp__[\w-]+?__)?(?P<tool>[a-z][a-z0-9_]*)\("
    rf"(?=(?P<args>(?:{_ARG_CHAR}|\((?:{_ARG_CHAR})*\))*))"
)
# One action value: quoted, or bare. A bare value followed by `.`, `(` or `[`
# is an expression (`Action.SEARCH`), which _CHAIN does not match as a value.
_VALUE = r"""(?:"[A-Za-z_]\w*"|'[A-Za-z_]\w*'|[A-Za-z_]\w*(?![\w.(\[]))"""
_CHAIN = rf"(?P<chain>{_VALUE}(?:\s*\|\s*{_VALUE})*)"
# A top-level keyword argument `action=` / `op=` inside a call's arguments.
_KWARG = re.compile(rf"(?:^|,)\s*(?P<kw>action|op)\s*=\s*{_CHAIN}")
# The wrappers' target tool and, for use_tool, the action inside `arguments`.
_TOOL_NAME = re.compile(r"""(?:^|,)\s*tool_name\s*=\s*["'](?P<target>[a-z][a-z0-9_]*)["']""")
_ARG_ACTION = re.compile(
    rf"""(?:^|,)\s*arguments\s*=\s*\{{[^{{}}]*?["'](?P<kw>action|op)["']\s*:\s*"""
    r"""(?P<chain>"[A-Za-z_]\w*"|'[A-Za-z_]\w*')"""
)
# Bare names that are the variable, not a value: `knowledge(action=action)`.
_PLACEHOLDERS = {"action", "op"}


# ---------------------------------------------------------------------------
# Code sources of truth (parsed, never imported)
# ---------------------------------------------------------------------------


def _router_actions() -> dict[str, tuple[frozenset[str], str]]:
    """``action_router("name", actions={...})`` -> (actions, "file:line")."""
    tree = ast.parse(_ROUTERS.read_text())
    out: dict[str, tuple[frozenset[str], str]] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "action_router"
        ):
            continue
        name = ast.literal_eval(node.args[0])
        actions = next(k.value for k in node.keywords if k.arg == "actions")
        assert isinstance(actions, ast.Dict), f"{name}: actions= is not a literal dict"
        keys = frozenset(ast.literal_eval(k) for k in actions.keys)
        out[name] = (keys, f"src/mcp_handlers/consolidated.py:{node.lineno} actions={{...}}")
    return out


def _tool_name(params_class: str) -> str:
    # Same derivation as src/tool_schemas.py::_load_pydantic_schemas.
    name = re.sub(r"([A-Z])", r"_\1", params_class).lower().lstrip("_")
    return name.replace("_params", "")


def _literal_values(annotation: ast.expr) -> frozenset[str] | None:
    """Values of ``Literal[...]`` or ``Optional[Literal[...]]``; else None."""
    if not isinstance(annotation, ast.Subscript):
        return None
    head = annotation.value
    head_name = head.id if isinstance(head, ast.Name) else getattr(head, "attr", "")
    if head_name == "Optional":
        return _literal_values(annotation.slice)
    if head_name != "Literal":
        return None
    elts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    return frozenset(ast.literal_eval(e) for e in elts)


def _schema_actions() -> dict[str, tuple[frozenset[str], str]]:
    """``<Tool>Params.action: Literal[...]`` -> (values, "file:line")."""
    out: dict[str, tuple[frozenset[str], str]] = {}
    for path in sorted(_SCHEMAS.glob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for cls in ast.parse(path.read_text()).body:
            if not (isinstance(cls, ast.ClassDef) and cls.name.endswith("Params")):
                continue
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "action"
                ):
                    values = _literal_values(stmt.annotation)
                    if values:
                        out[_tool_name(cls.name)] = (
                            values,
                            f"{rel}:{stmt.lineno} {cls.name}.action Literal",
                        )
    return out


def _alias_targets() -> dict[str, str]:
    """``_TOOL_ALIASES`` key -> ``new_name``."""
    tree = ast.parse(_ALIASES.read_text())
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) else None
        )
        if isinstance(target, ast.Name) and target.id == "_TOOL_ALIASES":
            out = {}
            for key, value in zip(node.value.keys, node.value.values):
                new_name = next(k.value for k in value.keywords if k.arg == "new_name")
                out[ast.literal_eval(key)] = ast.literal_eval(new_name)
            return out
    raise AssertionError("_TOOL_ALIASES not found in src/mcp_handlers/tool_stability.py")


def build_vocabulary() -> dict[str, tuple[frozenset[str], str]]:
    """tool name -> (valid actions, source-of-truth label)."""
    vocab = dict(_schema_actions())
    # The routing table is what dispatch consults, so it wins over a Literal.
    vocab.update(_router_actions())
    for alias, target in _alias_targets().items():
        if target in vocab and alias not in vocab:
            actions, source = vocab[target]
            vocab[alias] = (actions, f"alias of {target}: {source}")
    return vocab


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _selector(args: str, pattern: re.Pattern, allow_op: bool) -> re.Match | None:
    """The action the router reads: `action` if given, else `op` (routers only)."""
    found = {m.group("kw"): m for m in reversed(list(pattern.finditer(args)))}
    if "action" in found:
        return found["action"]
    return found.get("op") if allow_op else None


def find_invalid_action_mentions(
    text: str,
    where: str,
    vocab: dict[str, tuple[frozenset[str], str]],
    routers: frozenset[str] | None = None,
) -> tuple[list[tuple[str, str, str, str]], int]:
    """Return ((where:line, tool, action, message)..., checked-mention count).

    ``routers`` names the tools dispatched by ``action_router``, the only ones
    that read the ``op`` alias; it defaults to the parsed routing tables.
    """
    if routers is None:
        routers = frozenset(_router_actions())
    problems = []
    checked = 0
    for call in _CALL.finditer(text):
        tool, args = call.group("tool"), call.group("args")
        if tool in ("describe_tool", "use_tool"):
            target = _TOOL_NAME.search(args)
            if not target:
                continue
            tool = target.group("target")
            pattern = _KWARG if call.group("tool") == "describe_tool" else _ARG_ACTION
        else:
            pattern = _KWARG
        if tool not in vocab:
            continue
        selector = _selector(args, pattern, allow_op=tool in routers)
        if selector is None:
            continue
        values = []
        for raw in selector.group("chain").split("|"):
            raw = raw.strip()
            value = raw.strip("'\"")
            if raw == value and (value in _PLACEHOLDERS or value != value.lower()):
                values = []  # a bare variable or constant, not a literal action
                break
            values.append(value.lower())
        if not values:
            continue
        checked += 1
        valid, source = vocab[tool]
        offset = call.start("args") + selector.start("chain")
        line = text.count("\n", 0, offset) + 1
        shown = text[call.start():call.end("args") + 1].strip()
        if len(shown) > 160:
            shown = shown[:157] + "..."
        for action in values:
            if action not in valid:
                problems.append((
                    f"{where}:{line}",
                    tool,
                    action,
                    f"{where}:{line}: `{shown}` names action {action!r}, which "
                    f"{tool} does not route. Valid: {sorted(valid)} (source of "
                    f"truth: {source})",
                ))
    return problems, checked


def _git_visible(paths: list[Path]) -> list[Path]:
    """Drop gitignored files: what a checkout holds locally, CI never sees.

    Tracked files plus untracked files git does not ignore (a new doc not yet
    added still counts). Without git, the walk is returned unfiltered.
    """
    try:
        listed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", "*.md"],
            capture_output=True, check=True, timeout=30,
        ).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return paths
    visible = {REPO_ROOT / rel for rel in listed.split("\0") if rel}
    return [p for p in paths if p in visible]


def _is_historical(rel: str) -> bool:
    return rel.startswith(HISTORICAL_PREFIXES) or bool(_DATED_SNAPSHOT.search(rel))


def _collect_md_files() -> list[Path]:
    spec = importlib.util.spec_from_file_location("check_doc_health_tool_actions", _DOC_HEALTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _git_visible(module.collect_md_files())


def _living_docs() -> list[tuple[str, str]]:
    docs = []
    for path in _collect_md_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not _is_historical(rel):
            docs.append((rel, path.read_text(errors="replace")))
    for tool, text in json.loads(_DESCRIPTIONS.read_text()).items():
        docs.append((f"src/tool_descriptions.json[{tool}]", text))
    return docs


@pytest.fixture(scope="module")
def vocab():
    return build_vocabulary()


@pytest.fixture(scope="module")
def scan(vocab):
    routers = frozenset(_router_actions())
    problems, checked = [], 0
    for where, text in _living_docs():
        found, n = find_invalid_action_mentions(text, where, vocab, routers)
        problems.extend(found)
        checked += n
    return problems, checked


def test_vocabulary_parses_every_router(vocab):
    routers = _router_actions()
    # A parse that silently found nothing would make every mention "unknown"
    # and the scan vacuous.
    assert {"knowledge", "dialectic", "agent", "admin", "observe"} <= set(routers)
    assert vocab["self_recovery"][0] == {"check", "quick", "review"}
    assert vocab["search_shared_memory"][0] == routers["knowledge"][0]


def test_scan_checks_real_mentions(scan):
    _, checked = scan
    assert checked >= 100, (
        f"only {checked} tool(action=...) mentions matched a known tool; the "
        "mention pattern or the doc set has stopped matching"
    )


def test_every_doc_action_mention_names_a_routed_action(scan):
    problems, _ = scan
    unexpected = [
        msg for loc, tool, action, msg in problems
        if (loc.rsplit(":", 1)[0], tool, action) not in KNOWN_STALE
    ]
    assert not unexpected, "\n".join(unexpected)


def test_doc_set_is_git_visible_and_scoped():
    # A gitignored scratch doc (.unitares/ reports, docs/handoffs/) exists only
    # in one checkout; CI never sees it, so neither may this test.
    walked = _collect_md_files()
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in walked}
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--stdin", "-z"],
        input="\0".join(sorted(rels)).encode(), capture_output=True,
    ).stdout.decode().split("\0")
    assert not [r for r in ignored if r], f"gitignored docs in the scan: {ignored}"
    # Each historical exclusion must still name something, or it is dead scope.
    for prefix in HISTORICAL_PREFIXES:
        assert any(r.startswith(prefix) for r in rels), (
            f"HISTORICAL_PREFIXES entry {prefix!r} matches no doc; remove it"
        )
    assert any(_DATED_SNAPSHOT.search(r) for r in rels)
    assert not _is_historical("docs/dev/TOOL_REFERENCE.md")
    assert _is_historical("docs/operations/tool-surface-audit-2026-09-12.md")


def test_known_stale_entries_still_fire(scan):
    problems, _ = scan
    firing = {(loc.rsplit(":", 1)[0], tool, action) for loc, tool, action, _ in problems}
    fixed = sorted(set(KNOWN_STALE) - firing)
    assert not fixed, f"KNOWN_STALE entries no longer needed, remove them: {fixed}"


# ---------------------------------------------------------------------------
# Regression: the check flags the stale shapes it exists for
# ---------------------------------------------------------------------------


def test_flags_an_action_that_does_not_route(vocab):
    # docs/dev/TOOL_REGISTRATION.md's consolidated-tools table, as on master.
    snippet = "| `admin` | 9 | `admin(action='health')` |\n"
    problems, checked = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert checked == 1
    assert [(loc, tool, action) for loc, tool, action, _ in problems] == [
        ("doc.md:1", "admin", "health")
    ]
    assert "src/mcp_handlers/consolidated.py:" in problems[0][3]


def test_flags_a_stale_member_of_a_pipe_chain(vocab):
    snippet = (
        "intro\n"
        "- `dialectic(action='get'|'list'|'vote'|'synthesis')`\n"
    )
    problems, _ = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert [(loc, action) for loc, _, action, _ in problems] == [("doc.md:2", "vote")]


def test_flags_action_after_other_arguments_and_through_an_alias(vocab):
    snippet = (
        "knowledge(query='x', action=\"browse\")\n"
        "update_finding(action='rewrite', discovery_id=...)\n"
    )
    problems, _ = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert [(tool, action) for _, tool, action, _ in problems] == [
        ("knowledge", "browse"),
        ("update_finding", "rewrite"),
    ]


def test_allows_real_actions_and_skips_unknown_tools(vocab):
    snippet = (
        "dialectic(action='get'|'list'|'consult'|'reconcile_effect')\n"
        "observe(action=aggregate|anomalies)\n"
        "self_recovery(action='quick', reason='...')\n"
        "my_group(action='action1')\n"
        "cirs_protocol(protocol=\"void_alert\", action=\"emit\")\n"
        "client.call(action='anything')\n"
    )
    problems, checked = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert problems == []
    assert checked == 3


def test_flags_the_wrapper_and_prefixed_call_forms(vocab):
    snippet = (
        'describe_tool(tool_name="knowledge", action="browse")\n'
        "`use_tool(tool_name='agent', arguments={'action': 'kill',\n"
        "  'lite': true})`\n"
        'mcp__unitares-governance__dialectic(action="vote")\n'
        'knowledge(tags=list(x), action="browse")\n'
    )
    problems, checked = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert checked == 4
    assert [(loc, tool, action) for loc, tool, action, _ in problems] == [
        ("doc.md:1", "knowledge", "browse"),
        ("doc.md:2", "agent", "kill"),
        ("doc.md:4", "dialectic", "vote"),
        ("doc.md:5", "knowledge", "browse"),
    ]


def test_action_wins_over_op_and_op_is_router_only(vocab):
    # The router reads `action` before `op` (decorators.py action_router);
    # only routers read `op` at all.
    snippet = (
        'observe(action="agent", agent_id=x, op="bogus")\n'
        'observe(op="bogus", action="agent")\n'
        'self_recovery(op="bogus")\n'
        'agent(op="kill")\n'
    )
    problems, checked = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert checked == 3
    assert [(loc, tool, action) for loc, tool, action, _ in problems] == [
        ("doc.md:4", "agent", "kill"),
    ]


def test_skips_placeholders_and_unclosed_prose_calls(vocab):
    snippet = (
        "knowledge(action=action, query=q)\n"
        "agent(action=ACTION)\n"
        "knowledge(action=Action.SEARCH)\n"
        # An unclosed `knowledge(` ends at its backtick; the call after it is
        # still read on its own.
        "`knowledge(` reads the same; `dialectic(action='vote')`\n"
    )
    problems, checked = find_invalid_action_mentions(snippet, "doc.md", vocab)
    assert checked == 1
    assert [(loc, action) for loc, _, action, _ in problems] == [("doc.md:4", "vote")]
