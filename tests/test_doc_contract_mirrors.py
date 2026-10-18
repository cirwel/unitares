"""Docs that copy a code registry must match that registry.

Some docs restate a list that lives in code: the governance-sensitivity table
(the manifest TSV), the fleet-identity guard's exemption dicts, the hint
scanner's accepted dead ends and the dispatch middleware chain. Each of these
has drifted before (a manifest symbol and three entries missing from the
table, "and three more" exemptions that were one, a coupling ledger described
as populated after it emptied, "four entries" in an empty ledger, a middleware
step missing from the diagram). The doc-health lint is a denylist of known-bad
phrasings and cannot see this class.

A doc declares a copy with ``<!-- mirrors: path::SYMBOL -->`` (several symbols
comma-separated, or no symbol for a whole-file source such as the TSV),
followed immediately by what it compares:

* a bullet list or table whose first backticked span per item is a key, for a
  dict/list/set constant (compared as sets; an empty constant instead needs
  "none" or "empty" right after the marker and no items);
* a fenced block of ``a → b → c`` lines, one per symbol in marker order, for an
  ordered list of step functions (``handler()`` is diagram-only and ignored);
* a table of backticked paths (``·``-separated) and symbols, for the manifest,
  compared as (path, symbol) pairs, ``(whole file)`` standing for ``-``.

Sources are read by parsing (``ast``, TSV split), never by import. Every
marker in a living doc is checked, and REQUIRED_MIRRORS pins the markers that
must exist so deleting one is a failure rather than a silent opt-out.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_HISTORICAL_PREFIXES = ("docs/proposals/", "docs/releases/", "papers/", "docs/evaluations/")
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}(?:-\d{2})?(?!\d)")

_MANIFEST = "scripts/dev/governance_sensitivity_manifest.tsv"
_LEAK_GUARD = "scripts/dev/check_fleet_identity_leak.py"
_HINT_SCAN = "scripts/diagnostics/hint_target_advertisement.py"
_MIDDLEWARE = "src/mcp_handlers/middleware/__init__.py"
_MIDDLEWARE_SYMBOLS = ("PRE_DISPATCH_STEPS", "POST_VALIDATION_STEPS", "POST_EXECUTION_STEPS")

# (doc, source path, symbols) that must carry a marker.
REQUIRED_MIRRORS = {
    ("docs/dev/GOVERNANCE_SENSITIVITY.md", _MANIFEST, ()),
    ("docs/operations/resident-roster.md", _LEAK_GUARD, ("NOT_IDENTITIES",)),
    ("docs/operations/resident-roster.md", _LEAK_GUARD, ("KNOWN_COUPLINGS",)),
    ("docs/operations/resident-roster.md", _LEAK_GUARD, ("SERVED_KNOWN_COUPLINGS",)),
    ("docs/dev/TOOL_REGISTRATION.md", _HINT_SCAN, ("KNOWN_DEAD_ENDS",)),
    ("docs/dev/TOOL_REGISTRATION.md", _MIDDLEWARE, _MIDDLEWARE_SYMBOLS),
}

_MARKER = re.compile(
    r"<!--\s*mirrors:\s*(?P<path>[\w./-]+?)(?:::(?P<symbols>\w+(?:,\w+)*))?\s*-->"
)
_FENCE = re.compile(r"^\s*(```|~~~)")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+`([^`]+)`")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_EMPTY_WORDS = re.compile(r"\b(?:none|empty)\b", re.I)
_EMPTY_WINDOW = 120
_IDENT = re.compile(r"^[A-Za-z_]\w*$")


@dataclass(frozen=True)
class Source:
    """What a marker's source of truth holds.

    kind is "set" (unordered keys/elements), "sequence" (ordered step names,
    one tuple per symbol) or "pairs" (manifest (path, symbol) pairs).
    """

    kind: str
    value: object
    shown: str  # value rendered for failure messages


# --- sources of truth -------------------------------------------------------


def _render_key(key: object) -> str:
    return key if isinstance(key, str) else repr(key)


def _module_assignment(path: str, symbol: str) -> ast.AST:
    tree = ast.parse((REPO_ROOT / path).read_text(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == symbol for t in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == symbol
            and node.value is not None
        ):
            return node.value
    raise LookupError(f"no module-level assignment to {symbol} in {path}")


def _python_source(path: str, symbols: tuple[str, ...]) -> Source:
    values = [_module_assignment(path, s) for s in symbols]
    if all(
        isinstance(v, (ast.List, ast.Tuple)) and v.elts and all(isinstance(e, ast.Name) for e in v.elts)
        for v in values
    ):
        seqs = tuple(tuple(e.id for e in v.elts) for v in values)
        shown = "; ".join(f"{s} = {' → '.join(seq)}" for s, seq in zip(symbols, seqs))
        return Source("sequence", seqs, shown)
    if len(symbols) != 1:
        raise LookupError(
            f"{path}::{','.join(symbols)}: several symbols are only supported for "
            "ordered lists of names"
        )
    literal = ast.literal_eval(values[0])
    keys = literal.keys() if isinstance(literal, dict) else literal
    rendered = frozenset(_render_key(k) for k in keys)
    shown = ", ".join(sorted(rendered))
    return Source("set", rendered, f"{symbols[0]} = {{{shown}}}")


def _manifest_source(path: str) -> Source:
    pairs = set()
    for lineno, raw in enumerate((REPO_ROOT / path).read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cols = raw.split("\t")
        assert len(cols) >= 2, f"{path}:{lineno}: expected path<TAB>symbol_regex<TAB>why"
        file_path, regex = cols[0].strip(), cols[1].strip()
        symbols = ["-"] if regex == "-" else regex.split("|")
        for symbol in symbols:
            assert symbol == "-" or _IDENT.match(symbol), (
                f"{path}:{lineno}: symbol_regex alternative {symbol!r} is not a plain "
                "identifier; the doc table cannot mirror a real regex, extend this test"
            )
            pairs.add((file_path, symbol))
    shown = "; ".join(f"{p} {s}" for p, s in sorted(pairs))
    return Source("pairs", frozenset(pairs), shown)


def load_source(path: str, symbols: tuple[str, ...]) -> Source:
    if not (REPO_ROOT / path).is_file():
        raise LookupError(f"source {path} does not exist")
    if path.endswith(".tsv"):
        if symbols:
            raise LookupError(f"{path}: a TSV source takes no ::SYMBOL")
        return _manifest_source(path)
    if path.endswith(".py"):
        if not symbols:
            raise LookupError(f"{path}: a Python source needs ::SYMBOL")
        return _python_source(path, symbols)
    raise LookupError(f"{path}: unsupported source type")


# --- doc side ---------------------------------------------------------------


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _block_after(text: str, end: int) -> tuple[str, int]:
    """The text a marker governs, and its starting offset.

    A fenced block when the marker's line is followed by a fence; otherwise
    the rest of the paragraph (to the next blank line).
    """
    lines_after = text[end:].split("\n")
    if not lines_after[0].strip() and len(lines_after) > 1:
        fence = _FENCE.match(lines_after[1])
        if fence:
            body_start = end + len(lines_after[0]) + 1 + len(lines_after[1]) + 1
            close = re.compile(r"^\s*" + re.escape(fence.group(1)), re.M)
            m = close.search(text, body_start)
            return text[body_start : m.start() if m else len(text)], body_start
    m = re.compile(r"\n[ \t]*\n").search(text, end)
    return text[end : m.start() if m else len(text)], end


def _items(block: str, start: int, text: str) -> list[tuple[str, int]]:
    out = []
    offset = start
    for line in block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and not _TABLE_SEPARATOR.match(line):
            first_cell = stripped.strip("|").split("|")[0]
            spans = re.findall(r"`([^`]+)`", first_cell)
            if spans:
                out.append((spans[0], _line_of(text, offset)))
        else:
            m = _ITEM.match(line)
            if m:
                out.append((m.group(1), _line_of(text, offset)))
        offset += len(line) + 1
    return out


def _check_set(rel, line, label, src: Source, block, start, text) -> list[str]:
    items = _items(block, start, text)
    if not src.value:
        problems = []
        if items:
            listed = ", ".join(f"{k} (line {n})" for k, n in items)
            problems.append(f"lists {listed}")
        if not _EMPTY_WORDS.search(block[:_EMPTY_WINDOW]):
            problems.append(
                f"does not say 'none' or 'empty' within {_EMPTY_WINDOW} characters of the marker"
            )
        return [
            f"{rel}:{line}: the copy of {label} {p}, but the source of truth is empty "
            f"({src.shown})"
            for p in problems
        ]
    if not items:
        return [
            f"{rel}:{line}: the copy of {label} has no bullet or table items after the "
            f"marker; the source of truth holds {src.shown}"
        ]
    listed = {k for k, _ in items}
    out = []
    for key in sorted(src.value - listed):
        out.append(f"{rel}:{line}: the copy of {label} is missing `{key}` ({src.shown})")
    for key, n in items:
        if key not in src.value:
            out.append(f"{rel}:{n}: the copy of {label} lists `{key}`, which is not in {src.shown}")
    return out


def _check_sequence(rel, line, label, symbols, src: Source, block, start, text) -> list[str]:
    chains = []
    offset = start
    for raw in block.split("\n"):
        if "→" in raw:
            steps = tuple(s.strip() for s in raw.split("→") if s.strip() and s.strip() != "handler()")
            chains.append((steps, _line_of(text, offset)))
        offset += len(raw) + 1
    if len(chains) != len(symbols):
        return [
            f"{rel}:{line}: the copy of {label} has {len(chains)} arrow chains in its fenced "
            f"block, expected one per symbol ({len(symbols)}); source of truth: {src.shown}"
        ]
    out = []
    for symbol, expected, (got, n) in zip(symbols, src.value, chains):
        if got != expected:
            out.append(
                f"{rel}:{n}: the diagram shows {symbol} as {' → '.join(got)}, but "
                f"{label.split('::')[0]}::{symbol} is {' → '.join(expected)}"
            )
    return out


def _parse_manifest_table(block, start, text) -> tuple[set, list[str]]:
    pairs, problems = set(), []
    offset = start
    for raw in block.split("\n"):
        stripped = raw.strip()
        n = _line_of(text, offset)
        offset += len(raw) + 1
        if not stripped.startswith("|") or _TABLE_SEPARATOR.match(raw):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2 or not cells[0].startswith("`"):
            continue  # header row
        paths = re.findall(r"`([^`]+)`", cells[0])
        if cells[1] == "(whole file)":
            symbols = ["-"]
        else:
            symbols = re.findall(r"`([^`]+)`", cells[1])
        if not symbols:
            problems.append(f"line {n}: no backticked symbols or '(whole file)' in {cells[1]!r}")
        pairs.update((p, s, n) for p in paths for s in symbols)
    return pairs, problems


def _check_pairs(rel, line, label, src: Source, block, start, text) -> list[str]:
    rows, problems = _parse_manifest_table(block, start, text)
    out = [f"{rel}:{line}: the copy of {label}: {p}" for p in problems]
    if not rows:
        return out + [f"{rel}:{line}: the copy of {label} has no table rows after the marker"]
    documented = {(p, s) for p, s, _ in rows}
    for p, s in sorted(src.value - documented):
        what = "(whole file)" if s == "-" else f"`{s}`"
        out.append(
            f"{rel}:{line}: the table is missing `{p}` {what}, which {label} lists"
        )
    for p, s, n in sorted(rows, key=lambda r: r[2]):
        if (p, s) not in src.value:
            what = "(whole file)" if s == "-" else f"`{s}`"
            out.append(f"{rel}:{n}: the table lists `{p}` {what}, which is not in {label}")
    return out


def find_markers(text: str) -> list[tuple[str, tuple[str, ...], re.Match]]:
    out = []
    for m in _MARKER.finditer(text):
        symbols = tuple(m.group("symbols").split(",")) if m.group("symbols") else ()
        out.append((m.group("path"), symbols, m))
    return out


# Anything that looks like a mirrors marker; one _MARKER does not match is a
# typo that would otherwise be skipped silently.
_LOOSE_MARKER = re.compile(r"<!--\s*mirrors?\s*:[^>]*-->")


def check_doc(rel: str, text: str, load=load_source) -> list[str]:
    failures = []
    strict_starts = {m.start() for m in _MARKER.finditer(text)}
    for loose in _LOOSE_MARKER.finditer(text):
        if loose.start() not in strict_starts:
            failures.append(
                f"{rel}:{_line_of(text, loose.start())}: malformed mirrors marker "
                f"{loose.group(0)!r}; expected <!-- mirrors: path[::SYM[,SYM]] -->"
            )
    for path, symbols, m in find_markers(text):
        line = _line_of(text, m.start())
        label = path + ("::" + ",".join(symbols) if symbols else "")
        try:
            src = load(path, symbols)
        except (LookupError, ValueError, SyntaxError) as exc:
            failures.append(f"{rel}:{line}: marker {label} does not resolve: {exc}")
            continue
        block, start = _block_after(text, m.end())
        if src.kind == "set":
            failures += _check_set(rel, line, label, src, block, start, text)
        elif src.kind == "sequence":
            failures += _check_sequence(rel, line, label, symbols, src, block, start, text)
        else:
            failures += _check_pairs(rel, line, label, src, block, start, text)
    return failures


# --- living docs ------------------------------------------------------------


@pytest.fixture(scope="module")
def living_docs() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location("check_doc_health_mirrors", _DOC_HEALTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", "*.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    in_git = set(filter(None, listed.split("\0")))
    docs = []
    for path in module.collect_md_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(_HISTORICAL_PREFIXES) or rel not in in_git:
            continue
        if _DATED_SNAPSHOT.search(path.name):
            continue
        docs.append((rel, path.read_text(errors="replace")))
    return docs


def test_sources_of_truth_parse():
    manifest = load_source(_MANIFEST, ())
    assert ("config/governance_config.py", "GAP_RECOVERY_ARM_SECONDS") in manifest.value
    assert load_source(_LEAK_GUARD, ("NOT_IDENTITIES",)).kind == "set"
    steps = load_source(_MIDDLEWARE, _MIDDLEWARE_SYMBOLS)
    assert steps.kind == "sequence" and steps.value[0][0] == "unwrap_kwargs"


def test_required_mirror_markers_present(living_docs):
    by_rel = dict(living_docs)
    missing = []
    for rel, path, symbols in sorted(REQUIRED_MIRRORS):
        assert rel in by_rel, f"{rel} is not in the living-doc set; the filter or the doc moved"
        declared = {(p, s) for p, s, _ in find_markers(by_rel[rel])}
        if (path, symbols) not in declared:
            label = path + ("::" + ",".join(symbols) if symbols else "")
            missing.append(f"{rel}: no `<!-- mirrors: {label} -->` marker")
    assert not missing, "\n".join(missing)


def test_doc_mirrors_of_code_registries(living_docs):
    failures = []
    for rel, text in living_docs:
        failures += check_doc(rel, text)
    assert not failures, "Doc copies of code registries drifted:\n" + "\n".join(failures)


# --- regressions: stale wording these checks exist to catch ------------------


def test_catches_table_missing_a_manifest_symbol_and_rows():
    # The table before the arming-threshold split was documented, and before
    # the advisory's own files joined the manifest.
    stale = (
        "<!-- mirrors: scripts/dev/governance_sensitivity_manifest.tsv -->\n"
        "| Surface | Symbols | Leverage |\n"
        "|---|---|---|\n"
        "| `config/governance_config.py` | `GAP_RECOVERY_CYCLES`, `DT_MAX` | window |\n"
    )
    failures = check_doc("docs/x.md", stale)
    joined = "\n".join(failures)
    assert "missing `config/governance_config.py` `GAP_RECOVERY_ARM_SECONDS`" in joined
    assert "missing `scripts/dev/check_governance_sensitivity.sh` (whole file)" in joined


def test_catches_middleware_diagram_missing_a_step():
    stale = (
        "<!-- mirrors: src/mcp_handlers/middleware/__init__.py::"
        "PRE_DISPATCH_STEPS,POST_VALIDATION_STEPS,POST_EXECUTION_STEPS -->\n"
        "```\n"
        "unwrap_kwargs → resolve_identity → verify_trajectory → resolve_alias → "
        "inject_identity → validate_params\n"
        "    ↓ (handler lookup)\n"
        "check_rate_limit → track_patterns → handler()\n"
        "    ↓ (result)\n"
        "apply_experience_envelope → apply_identity_warnings\n"
        "```\n"
    )
    failures = check_doc("docs/x.md", stale)
    assert len(failures) == 1, failures
    assert failures[0].startswith("docs/x.md:3: the diagram shows PRE_DISPATCH_STEPS")
    assert "strip_untrusted_dispatch_metadata" in failures[0]


def test_catches_empty_ledger_described_as_populated():
    stale = (
        "The four reviewed entries from #2119 remain in `KNOWN_DEAD_ENDS` <!-- mirrors: "
        "scripts/diagnostics/hint_target_advertisement.py::KNOWN_DEAD_ENDS --> with "
        "reasons and keys that include the action.\n"
    )
    failures = check_doc("docs/x.md", stale)
    assert len(failures) == 1 and "source of truth is empty" in failures[0], failures


def test_catches_served_couplings_described_as_listed():
    stale = (
        "The served files that already named residents when the rule arrived are "
        "listed in `SERVED_KNOWN_COUPLINGS` <!-- mirrors: "
        "scripts/dev/check_fleet_identity_leak.py::SERVED_KNOWN_COUPLINGS -->, each "
        "with the exact names it holds: they are reported on every run.\n"
    )
    failures = check_doc("docs/x.md", stale)
    assert len(failures) == 1 and "source of truth is empty" in failures[0], failures


def test_catches_exemption_list_with_extra_and_missing_entries():
    stale = (
        "<!-- mirrors: scripts/dev/check_fleet_identity_leak.py::NOT_IDENTITIES -->\n"
        "- `src/evaluation/resident_validation/model.py`: a role.\n"
        "- `src/coordination_events.py`: service ids.\n"
    )
    failures = check_doc("docs/x.md", stale)
    joined = "\n".join(failures)
    assert "missing `src/watcher_state_reader.py`" in joined
    assert "docs/x.md:3: the copy of" in joined and "`src/coordination_events.py`" in joined


def test_unresolvable_marker_fails():
    failures = check_doc("docs/x.md", "<!-- mirrors: scripts/dev/check_fleet_identity_leak.py::NO_SUCH -->\n- `a`\n")
    assert failures and "does not resolve" in failures[0]


def test_accepts_current_shapes():
    ok = (
        "Ledger `KNOWN_DEAD_ENDS` <!-- mirrors: "
        "scripts/diagnostics/hint_target_advertisement.py::KNOWN_DEAD_ENDS --> is empty.\n"
        "\n"
        "<!-- mirrors: scripts/dev/check_fleet_identity_leak.py::NOT_IDENTITIES -->\n"
        "- `src/evaluation/resident_validation/model.py`: a role.\n"
        "- `src/watcher_state_reader.py`: a legacy path.\n"
    )
    assert check_doc("docs/x.md", ok) == []


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- mirrors: src/mcp_handlers/middleware/__init__.py::PRE_DISPATCH_STEPS, POST_VALIDATION_STEPS -->",
        "<!-- mirrors: scripts/dev/check_fleet_identity_leak.py :: NOT_IDENTITIES -->",
        "<!-- mirror: scripts/dev/check_fleet_identity_leak.py::NOT_IDENTITIES -->",
    ],
)
def test_malformed_marker_is_reported_not_skipped(marker):
    failures = check_doc("d.md", marker + "\n- `bogus`\n")
    assert any("malformed mirrors marker" in f for f in failures), failures
