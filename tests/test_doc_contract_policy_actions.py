"""Docs that name the policy `action` field must list only values it carries.

The check-in envelope emits `action` as `proceed` or `pause`. A guided
check-in is `action: proceed` with `sub_action: guide`, and a reject-severity
decision is `action: pause` with `sub_action: reject`. A walkthrough once told
agents that `action` is "`proceed`, `guide` or `pause`", so a caller comparing
`action == "guide"` would never match. The doc-health lint is a denylist of
known-bad phrasings and cannot see this class, so this test reads the source of
truth by parsing it (`_ACTION_ALIASES` in
`src/mcp_handlers/middleware/envelope_step.py`, and the decision dicts each
`make_decision` returns) and holds field-level doc claims to it.

Scope is deliberately narrow: a sentence must name the backticked field
(`` `action` ``, `` `action_summary.action` ``, or an `` `action: <v>` `` span)
and list `guide` or `reject` as a quoted or backticked value. Such a sentence
must also mention `sub_action`. Loose verdict prose such as "policy action
(proceed, guide, or pause)" is out of scope, because the server's own MCP
instructions use that wording.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_ENVELOPE = "src/mcp_handlers/middleware/envelope_step.py"
_DECISION_SOURCES = ("config/governance_config.py", "src/monitor_decision.py")

# Values that docs have listed as `action` values but the envelope carries as a
# `sub_action`. The source test below fails if the code ever emits either one
# as an action, so this list cannot silently go wrong.
CHECKED_VALUES = ("guide", "reject")

HISTORICAL_PREFIXES = (
    "docs/proposals/",
    "docs/releases/",
    "papers/",
    "docs/evaluations/",
)
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}(?:-\d{2})?\.md$")


# --- source of truth ---------------------------------------------------------


def _module_assign(path: str, name: str) -> ast.expr:
    tree = ast.parse((REPO_ROOT / path).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"{path}: no module-level assignment to {name}")


def action_aliases() -> dict[str, tuple[str, str | None]]:
    """`_ACTION_ALIASES`: raw lifecycle word -> (emitted action, sub_action)."""
    return ast.literal_eval(_module_assign(_ENVELOPE, "_ACTION_ALIASES"))


def make_decision_vocabulary(path: str) -> tuple[set[str], set[str]]:
    """(actions, sub_actions) in the literal dicts `make_decision` returns."""
    tree = ast.parse((REPO_ROOT / path).read_text())
    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "make_decision"
    ]
    assert funcs, f"{path}: no make_decision function"
    actions: set[str] = set()
    subs: set[str] = set()
    for func in funcs:
        for node in ast.walk(func):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and isinstance(value, ast.Constant)):
                    continue
                if key.value == "action":
                    actions.add(value.value)
                elif key.value == "sub_action":
                    subs.add(value.value)
    return actions, subs


@pytest.fixture(scope="module")
def aliases() -> dict[str, tuple[str, str | None]]:
    return action_aliases()


@pytest.fixture(scope="module")
def emitted_actions(aliases) -> frozenset[str]:
    return frozenset(action for action, _ in aliases.values())


def test_source_of_truth_emits_only_proceed_and_pause(aliases, emitted_actions):
    assert emitted_actions == {"proceed", "pause"}, (
        f"{_ENVELOPE}::_ACTION_ALIASES now emits actions {sorted(emitted_actions)}; "
        "update this test and the docs it guards."
    )
    for value in CHECKED_VALUES:
        assert value not in emitted_actions, (
            f"{_ENVELOPE}::_ACTION_ALIASES emits '{value}' as an action; "
            "drop it from CHECKED_VALUES."
        )
        assert value in aliases and aliases[value][1] == value, (
            f"{_ENVELOPE}::_ACTION_ALIASES no longer maps '{value}' to "
            f"sub_action '{value}' (got {aliases.get(value)!r})"
        )
    for path in _DECISION_SOURCES:
        actions, subs = make_decision_vocabulary(path)
        assert actions <= emitted_actions, (
            f"{path}::make_decision returns action(s) "
            f"{sorted(actions - emitted_actions)} that {_ENVELOPE}::_ACTION_ALIASES "
            f"does not emit ({sorted(emitted_actions)})"
        )
        assert "guide" in subs, f"{path}::make_decision no longer returns sub_action 'guide'"


# --- doc scan ----------------------------------------------------------------

# A backticked field name: `action`, `action_summary.action`, or the start of
# an assignment span such as `action: guide` / `action="guide"`.
_FIELD = re.compile(r"`(?:action_summary\.)?action(?:`|\s*[:=])")
_QUOTED = re.compile(r"`([^`\n]+)`|\"([A-Za-z_-]+)\"|'([A-Za-z_-]+)'")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*\[(])")
_BLOCK_START = re.compile(r"^(?:[-*+]|\d+[.)]|#+)\s")


def _units(text: str) -> list[tuple[int, str]]:
    """(first line, text) for each prose paragraph, list item, heading or table
    row outside fenced code. Paragraph lines are joined with newlines so a
    sentence's own line can be recovered."""
    units: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    in_fence = False

    def flush() -> None:
        nonlocal buf
        if buf:
            units.append((start, "\n".join(buf)))
        buf = []

    for lineno, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped or stripped.startswith("|") or _BLOCK_START.match(stripped):
            flush()
            if stripped:
                start = lineno
                buf = [stripped]
            continue
        if not buf:
            start = lineno
        buf.append(stripped)
    flush()
    return units


def _sentences(text: str) -> list[tuple[int, str]]:
    out = []
    for start, unit in _units(text):
        pos = 0
        for brk in list(_SENTENCE_BREAK.finditer(unit)) + [None]:
            end = brk.start() if brk else len(unit)
            out.append((start + unit.count("\n", 0, pos), unit[pos:end]))
            if brk:
                pos = brk.end()
    return out


def _quoted_values(sentence: str) -> set[str]:
    values = set()
    for match in _QUOTED.finditer(sentence):
        body = next(g for g in match.groups() if g is not None)
        for token in re.split(r"[\s/|,=:]+", body):
            values.add(token.strip("'\".;()"))
    return values


def check_doc(rel: str, text: str, aliases, emitted) -> tuple[list[str], int]:
    """Return (failures, number of sentences naming the field)."""
    failures = []
    named = 0
    for lineno, sentence in _sentences(text):
        if not _FIELD.search(sentence):
            continue
        named += 1
        listed = sorted(_quoted_values(sentence) & set(CHECKED_VALUES))
        if not listed or "sub_action" in sentence:
            continue
        mapping = "; ".join(
            f"'{v}' -> action '{aliases[v][0]}', sub_action '{aliases[v][1]}'"
            for v in listed
        )
        claim = " ".join(sentence.split())
        failures.append(
            f"{rel}:{lineno}: lists {listed} as a value of the `action` field "
            f"without naming `sub_action`: \"{claim}\". Source of truth: "
            f"{_ENVELOPE}::_ACTION_ALIASES emits action in {sorted(emitted)}; {mapping}."
        )
    return failures, named


def _git_visible(paths: list[Path]) -> list[Path]:
    """Drop gitignored files. Without git, the walk is returned unfiltered."""
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


def _living_docs() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location(
        "check_doc_health_policy_actions", _DOC_HEALTH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    docs = []
    for path in _git_visible(module.collect_md_files()):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(HISTORICAL_PREFIXES) or _DATED_SNAPSHOT.search(rel):
            continue
        docs.append((rel, path.read_text(errors="replace")))
    return docs


def test_doc_policy_action_field_vocabulary(aliases, emitted_actions):
    failures: list[str] = []
    named = 0
    for rel, text in _living_docs():
        doc_failures, doc_named = check_doc(rel, text, aliases, emitted_actions)
        failures.extend(doc_failures)
        named += doc_named
    # Guard against a vacuous pass: the walkthrough and glossary name the field.
    assert named >= 2, f"only {named} doc sentences name the `action` field; the scan is broken"
    assert not failures, "\n".join(failures)


# --- regression cases --------------------------------------------------------


def _check(text: str, aliases, emitted) -> list[str]:
    return check_doc("docs/guide.md", text, aliases, emitted)[0]


def test_flags_the_stale_walkthrough_claim(aliases, emitted_actions):
    stale = (
        "Intro paragraph.\n"
        "\n"
        "`action` is the policy action: `proceed`, `guide` or `pause`. On a fresh\n"
        "identity it is provisional.\n"
    )
    failures = _check(stale, aliases, emitted_actions)
    assert len(failures) == 1
    assert failures[0].startswith("docs/guide.md:3: lists ['guide']")
    assert "'guide' -> action 'proceed', sub_action 'guide'" in failures[0]
    assert "_ACTION_ALIASES emits action in ['pause', 'proceed']" in failures[0]


def test_reports_the_line_of_a_wrapped_sentence(aliases, emitted_actions):
    text = (
        "First sentence here. The response field\n"
        "`action_summary.action` is `proceed`, `pause`\n"
        "or `reject`.\n"
    )
    failures = _check(text, aliases, emitted_actions)
    assert len(failures) == 1 and failures[0].startswith("docs/guide.md:1: lists ['reject']")


@pytest.mark.parametrize(
    "text",
    [
        "Check-ins return `action: guide` when drift is mild.\n",
        "The summary carries `action=\"reject\"` for severe risk.\n",
    ],
)
def test_flags_assignment_spans(text, aliases, emitted_actions):
    assert len(_check(text, aliases, emitted_actions)) == 1


@pytest.mark.parametrize(
    "text",
    [
        # The corrected walkthrough line.
        "`action` is the policy action: `proceed` or `pause`. A guided check-in\n"
        "returns `action: proceed` with `sub_action: guide`.\n",
        # Glossary-style sentence that names sub_action.
        "(`action` is `proceed` or `pause`; `guide` is a `sub_action` of `proceed`)\n",
        # Loose verdict prose without the backticked field is out of scope.
        "sync_state returns a policy action (proceed, guide, or pause).\n",
        "One policy action: `proceed` / `guide` / `pause`.\n",
        # A fenced example is not a prose claim.
        "```\n`action` is `guide`\n```\n",
        # The field named in a sentence that lists no checked value.
        "Read `action` before anything else.\n",
    ],
)
def test_allows_correct_or_out_of_scope_text(text, aliases, emitted_actions):
    assert _check(text, aliases, emitted_actions) == []
