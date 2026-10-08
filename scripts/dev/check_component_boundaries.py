#!/usr/bin/env python3
"""Component boundary ratchet (federation-component-boundaries-v0, Section 6).

The federation boundary proposal draws three rings: members that use public
surfaces only, a core whose components own their tables, and subscribers that
depend on the core. None of that is enforced, so any later change can quietly
reverse it. This guard records where the code stands today and fails a change
that moves further away. It changes no runtime code and imports nothing it
checks: every rule is a static read of the source.

Rules (v0):

- B1, member imports. A non-test file under ``agents/`` that is not a route
  pack must not import ``src.*``. The one exception is the SDK's optional host
  hook: an import inside a ``try`` whose failure path returns, under
  ``agents/sdk/`` only. Residents run inside the checkout, where such an
  import always succeeds, so the exception is not open to them.
- B1p, pack imports. A route pack (a module named in ``PACK_SPECS`` in
  ``src/http_routes/packs.py``) may import only the ``src`` modules in
  ``PACK_ALLOWED_MODULES``. The list is what packs imported when the guard was
  written; shrinking it is the declared extension surface's work.
- B2, member SQL. A non-test file under ``agents/`` must not carry SQL naming a
  ``core.``, ``audit.``, ``knowledge.``, ``lease_plane.`` or ``effects.`` table.
- Subprocess targets. B1 and B2 also apply to a ``scripts/`` file a member file
  names by path, so a breach cannot move into ``scripts/``. Such a violation is
  keyed ``<member> -> <script>``. Naming a script's path is read as launching it.
- B3, private reach. A module in ``src/`` or ``governance_core/``, or a route
  pack, must not import a ``_``-prefixed name from a module in another
  component. Components are ``COMPONENTS``; a module outside them belongs to
  its own directory.
- B4, foreign SQL. A file in ``src/``, ``governance_core/`` or an Elixir
  ``lib/`` tree must not carry SQL against a table in ``TABLE_OWNERS`` unless it
  sits under one of that table's owner or declared co-owner paths.

SQL is matched as a table name after ``FROM``, ``JOIN``, ``INTO``, ``UPDATE`` or
``TABLE``, in Python string literals (docstrings and comments excluded) and in
Elixir source outside ``#`` comment lines.

The ratchet:

- Each violation is counted per rule and per file. A (rule, file) pair not in
  the baseline fails; a count above its baseline fails.
- A count below its baseline is reported; ``--lower`` ratchets the baseline
  down. A baselined pair whose count reached zero must be removed, so an
  emptied rule admits nothing new.
- With ``--base REF`` (CI passes the PR's diff base) the baseline itself is
  checked against its version at REF: an entry may not be added or raised, so
  a change cannot absorb its own violation by editing the baseline.
- ``--lower`` never adds or raises an entry.

Baseline counts are telemetry. They authorize no retirement of anything; the
proposal's Section 8 and the measurement-authority rules in AGENTS.md apply.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REL = Path("scripts") / "dev" / "component_boundaries_baseline.txt"
PACKS_REL = Path("src") / "http_routes" / "packs.py"

MEMBER_ROOT = "agents"
SDK_ROOT = "agents/sdk/"
SCRIPTS_ROOT = "scripts/"
SERVER_ROOTS = ("src", "governance_core")
ELIXIR_ROOT = "elixir"

MEMBER_SQL_SCHEMAS = ("core", "audit", "knowledge", "lease_plane", "effects")

# The src modules route packs imported when this guard was written. A pack
# importing anything else fails B1p. Shrink this list; do not grow it.
PACK_ALLOWED_MODULES = frozenset({
    "src.audit_db",
    "src.broadcaster",
    "src.event_detector",
    "src.http_routes",
    "src.http_routes.access",
    "src.http_routes.findings",
    "src.http_routes.residents",
    "src.logging_utils",
    "src.mcp_handlers.shared",
    "src.watcher_state_reader",
})

# Component -> path prefixes (a prefix ending in "/" is a directory). Used by B3.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "identity": ("src/mcp_handlers/identity/", "src/identity/", "src/db/mixins/identity.py"),
    "eisv": ("governance_core/", "src/db/mixins/state.py", "src/agent_monitor_state.py"),
    "knowledge": (
        "src/mcp_handlers/knowledge/",
        "src/storage/knowledge_graph_age.py",
        "src/storage/knowledge_graph_postgres.py",
        "src/knowledge_graph_lifecycle.py",
        "src/db/mixins/knowledge_graph.py",
    ),
    "dialectic": ("src/mcp_handlers/dialectic/", "src/dialectic_db.py"),
    "audit": ("src/db/mixins/audit.py", "src/audit_db.py"),
    "outcomes": ("src/mcp_handlers/observability/outcome_events.py", "src/db/mixins/tool_usage.py"),
    "lease_plane": ("src/lease_plane/", "elixir/lease_plane/"),
    # src/http_api.py is the declared re-export facade over src/http_routes/.
    "http": ("src/http_api.py", "src/http_routes/"),
}

# Table -> (owner prefixes, declared co-owner prefixes). Used by B4. The tables
# are the ones the proposal's Section 2.4 names.
TABLE_OWNERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "core.identities": (COMPONENTS["identity"], ()),
    "core.agent_state": (COMPONENTS["eisv"], ()),
    "knowledge.discoveries": (COMPONENTS["knowledge"], ()),
    # The BEAM dialectic saga persists sessions when
    # UNITARES_DIALECTIC_BEAM_RESOLUTION is set: a second, declared writer.
    "core.dialectic_sessions": (
        COMPONENTS["dialectic"],
        ("elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex",),
    ),
    "core.dialectic_messages": (
        COMPONENTS["dialectic"],
        ("elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex",),
    ),
    "audit.events": (COMPONENTS["audit"], ()),
    "audit.outcome_events": (COMPONENTS["outcomes"], ()),
    "lease_plane.surface_leases": (COMPONENTS["lease_plane"], ()),
}

_SQL_LEAD = r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+"
_MEMBER_SQL_RE = re.compile(
    _SQL_LEAD + r"((?:" + "|".join(MEMBER_SQL_SCHEMAS) + r")\.[A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_OWNED_SQL_RE = re.compile(
    _SQL_LEAD + r"(" + "|".join(re.escape(t) for t in TABLE_OWNERS) + r")\b",
    re.IGNORECASE,
)
_SCRIPT_PATH_RE = re.compile(r"^[\w./-]+\.py$")

RULES = ("B1", "B1p", "B2", "B3", "B4")


# --- source helpers --------------------------------------------------------


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.exs")
        or name == "conftest.py"
    )


def _py_files(root: Path, top: str) -> list[str]:
    base = root / top
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or _is_test(rel):
            continue
        out.append(rel)
    return out


def _parse(root: Path, rel: str) -> ast.Module | None:
    try:
        return ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _module_name(rel: str) -> str:
    stem = rel[:-3] if rel.endswith(".py") else rel
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _module_path(root: Path, module: str) -> str | None:
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if (root / candidate).is_file():
            return candidate
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _imports(tree: ast.Module, rel: str) -> list[tuple[ast.stmt, str, list[str]]]:
    """(node, absolute module, imported names) for every import, at any depth."""
    package = _module_name(rel)
    if not rel.endswith("/__init__.py"):
        package = package.rpartition(".")[0]
    found: list[tuple[ast.stmt, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node, alias.name, []))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                if node.level > 1:
                    parts = parts[: len(parts) - (node.level - 1)]
                module = ".".join(p for p in [*parts, module] if p)
            found.append((node, module, [a.name for a in node.names]))
    return found


def _is_src(module: str) -> bool:
    return module == "src" or module.startswith("src.")


def _guarded_by_returning_try(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when ``node`` sits in a ``try`` body whose every handler returns."""
    child, current = node, parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try) and child in current.body and current.handlers:
            return all(
                any(isinstance(n, ast.Return) for n in ast.walk(h)) for h in current.handlers
            )
        child, current = current, parents.get(current)
    return False


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _string_literals(tree: ast.Module) -> list[tuple[int, str]]:
    docstrings = _docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append((node.lineno, node.value))
    return out


def _python_sql(tree: ast.Module, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    hits = []
    for lineno, text in _string_literals(tree):
        for match in pattern.finditer(text):
            hits.append((lineno, match.group(1).lower()))
    return hits


def _elixir_sql(root: Path, rel: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    hits = []
    for lineno, line in enumerate((root / rel).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for match in pattern.finditer(line):
            hits.append((lineno, match.group(1).lower()))
    return hits


def _path_references(tree: ast.Module) -> set[str]:
    """Repository-relative ``.py`` paths a file names, as a string or a Path join."""
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "/" in node.value and _SCRIPT_PATH_RE.match(node.value):
                refs.add(node.value.lstrip("./"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts: list[str] = []
            current: ast.AST = node
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                right = current.right
                if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
                    break
                parts.insert(0, right.value)
                current = current.left
            if parts and parts[-1].endswith(".py"):
                refs.add("/".join(parts))
    return refs


def _component(rel: str) -> str:
    best, best_len = None, -1
    for name, prefixes in COMPONENTS.items():
        for prefix in prefixes:
            hit = rel.startswith(prefix) if prefix.endswith("/") else rel == prefix
            if hit and len(prefix) > best_len:
                best, best_len = name, len(prefix)
    return best or "dir:" + rel.rpartition("/")[0]


def _owned(rel: str, prefixes: tuple[str, ...]) -> bool:
    return any(rel.startswith(p) if p.endswith("/") else rel == p for p in prefixes)


def route_packs(root: Path) -> set[str]:
    """Repository paths of the route-pack modules under ``agents/``."""
    tree = _parse(root, PACKS_REL.as_posix())
    packs: set[str] = set()
    if tree is None:
        return packs
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "PACK_SPECS" for t in targets):
            continue
        try:
            specs = ast.literal_eval(node.value)
        except ValueError:
            continue
        for routes in specs.values():
            for _path, ref, _methods in routes:
                target = ref.rsplit(":", 1)[0]
                rel = target if target.endswith(".py") else _module_path(root, target)
                if rel and rel.startswith(MEMBER_ROOT + "/"):
                    packs.add(rel)
    return packs


# --- rules -------------------------------------------------------------------

Violation = tuple[str, str, int, str]  # rule, key, line, detail


def _member_breaches(root: Path, rel: str, tree: ast.Module, *, sdk_exempt: bool) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    parents = _parents(tree)
    for node, module, _names in _imports(tree, rel):
        if not _is_src(module):
            continue
        if sdk_exempt and _guarded_by_returning_try(node, parents):
            continue
        found.append(("B1", node.lineno, f"imports {module}"))
    for lineno, table in _python_sql(tree, _MEMBER_SQL_RE):
        found.append(("B2", lineno, f"SQL against {table}"))
    return found


def check_members(root: Path, packs: set[str]) -> list[Violation]:
    out: list[Violation] = []
    for rel in _py_files(root, MEMBER_ROOT):
        tree = _parse(root, rel)
        if tree is None:
            continue
        if rel in packs:
            for node, module, _names in _imports(tree, rel):
                if _is_src(module) and module not in PACK_ALLOWED_MODULES:
                    out.append(("B1p", rel, node.lineno, f"imports {module}, not on the pack allowlist"))
            for lineno, table in _python_sql(tree, _MEMBER_SQL_RE):
                out.append(("B2", rel, lineno, f"SQL against {table}"))
        else:
            sdk = rel.startswith(SDK_ROOT)
            for rule, lineno, detail in _member_breaches(root, rel, tree, sdk_exempt=sdk):
                out.append((rule, rel, lineno, detail))
        for target in sorted(_path_references(tree)):
            if not target.startswith(SCRIPTS_ROOT) or not (root / target).is_file():
                continue
            target_tree = _parse(root, target)
            if target_tree is None:
                continue
            for rule, lineno, detail in _member_breaches(root, target, target_tree, sdk_exempt=False):
                out.append((rule, f"{rel} -> {target}", lineno, f"{target}:{lineno} {detail}"))
    return out


def check_private_reach(root: Path, packs: set[str]) -> list[Violation]:
    out: list[Violation] = []
    files = [f for top in SERVER_ROOTS for f in _py_files(root, top)] + sorted(packs)
    for rel in files:
        tree = _parse(root, rel)
        if tree is None:
            continue
        here = _component(rel)
        for node, module, names in _imports(tree, rel):
            if not isinstance(node, ast.ImportFrom):
                continue
            private = [n for n in names if n.startswith("_") and not n.endswith("__")]
            if not private:
                continue
            target = _module_path(root, module)
            if target is None or _component(target) == here:
                continue
            for name in private:
                out.append(("B3", rel, node.lineno, f"imports private {module}.{name}"))
    return out


def check_foreign_sql(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for rel in [f for top in SERVER_ROOTS for f in _py_files(root, top)]:
        tree = _parse(root, rel)
        if tree is None:
            continue
        for lineno, table in _python_sql(tree, _OWNED_SQL_RE):
            owners, co_owners = TABLE_OWNERS[table]
            if not _owned(rel, owners + co_owners):
                out.append(("B4", rel, lineno, f"SQL against {table}"))
    base = root / ELIXIR_ROOT
    if base.is_dir():
        for path in sorted(base.rglob("*.ex")):
            rel = path.relative_to(root).as_posix()
            if "/lib/" not in rel or _is_test(rel) or "/deps/" in rel or "/_build/" in rel:
                continue
            for lineno, table in _elixir_sql(root, rel, _OWNED_SQL_RE):
                owners, co_owners = TABLE_OWNERS[table]
                if not _owned(rel, owners + co_owners):
                    out.append(("B4", rel, lineno, f"SQL against {table}"))
    return out


def measure(root: Path) -> list[Violation]:
    packs = route_packs(root)
    return check_members(root, packs) + check_private_reach(root, packs) + check_foreign_sql(root)


def counts(violations: list[Violation]) -> Counter[tuple[str, str]]:
    return Counter((rule, key) for rule, key, _line, _detail in violations)


# --- baseline ----------------------------------------------------------------


def parse_baseline_text(text: str) -> dict[tuple[str, str], int]:
    entries: dict[tuple[str, str], int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count, rule, key = line.split(" ", 2)
        entries[(rule, key)] = int(count)
    return entries


def read_baseline(path: Path) -> tuple[list[str], dict[tuple[str, str], int]]:
    if not path.is_file():
        return [], {}
    text = path.read_text(encoding="utf-8")
    header = []
    for raw in text.splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            break
        header.append(raw)
    return header, parse_baseline_text(text)


def read_base_baseline(root: Path, ref: str) -> dict[tuple[str, str], int] | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{BASELINE_REL.as_posix()}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return parse_baseline_text(result.stdout)


def write_baseline(path: Path, header: list[str], entries: dict[tuple[str, str], int]) -> None:
    body = [f"{n} {rule} {key}" for (rule, key), n in sorted(entries.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *body]) + "\n", encoding="utf-8")


HEADER = [
    "# Component boundary violations that existed when the guard was written, per rule and file.",
    "# Checked by scripts/dev/check_component_boundaries.py (see its docstring for the rules).",
    "# Lower an entry when its count drops (run the script with --lower); never add or raise one.",
    "# The counts are telemetry: they authorize no retirement of anything.",
    "# Format: <count> <rule> <file, or 'member -> script' for a launched script>",
]


def baseline_escalations(base: dict[tuple[str, str], int], head: dict[tuple[str, str], int]) -> list[str]:
    problems = []
    for (rule, key), n in sorted(head.items()):
        before = base.get((rule, key))
        if before is None:
            problems.append(f"baseline entry added: {rule} {key} ({n}). The baseline may only shrink.")
        elif n > before:
            problems.append(f"baseline entry raised: {rule} {key} from {before} to {n}. The baseline may only shrink.")
    return problems


def check(root: Path) -> tuple[list[str], list[str]]:
    """Return (problems, notes)."""
    _, baseline = read_baseline(root / BASELINE_REL)
    violations = measure(root)
    current = counts(violations)
    where: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rule, key, line, detail in violations:
        where[(rule, key)].append(f"{key.split(' -> ')[0]}:{line} {detail}")
    problems: list[str] = []
    notes: list[str] = []
    script = Path(__file__).name
    for pair, n in sorted(current.items()):
        rule, key = pair
        allowed = baseline.get(pair)
        if allowed is None:
            problems.append(f"new {rule} violation in {key}: " + "; ".join(where[pair]))
        elif n > allowed:
            problems.append(f"{rule} {key} grew to {n}, past its baseline of {allowed}: " + "; ".join(where[pair]))
        elif n < allowed:
            notes.append(f"{rule} {key} dropped to {n} (baseline {allowed}); run {script} --lower.")
    for pair in sorted(set(baseline) - set(current)):
        rule, key = pair
        problems.append(f"stale baseline entry: {rule} {key} no longer occurs. Remove it (run {script} --lower).")
    return problems, notes


def lower(root: Path) -> int:
    path = root / BASELINE_REL
    header, baseline = read_baseline(path)
    current = counts(measure(root))
    lowered = {pair: min(allowed, current[pair]) for pair, allowed in baseline.items() if current.get(pair)}
    write_baseline(path, header or HEADER, lowered)
    print(f"Baseline rewritten: {len(baseline)} -> {len(lowered)} entr(ies); none added or raised.")
    return 0


def init(root: Path) -> int:
    path = root / BASELINE_REL
    if path.exists():
        print(f"{BASELINE_REL} already exists; --init only writes the first baseline.", file=sys.stderr)
        return 1
    write_baseline(path, HEADER, dict(counts(measure(root))))
    print(f"Wrote {BASELINE_REL}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--lower", action="store_true", help="lower the baseline to current counts")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--base", metavar="REF", help="also reject baseline entries added or raised relative to REF")
    args = parser.parse_args(argv)

    if args.init:
        return init(args.root)
    if args.lower:
        return lower(args.root)

    problems, notes = check(args.root)
    if args.base:
        base_entries = read_base_baseline(args.root, args.base)
        if base_entries is None:
            notes.append(f"no baseline at {args.base}; skipping the add/raise comparison")
        else:
            _, head_entries = read_baseline(args.root / BASELINE_REL)
            problems.extend(baseline_escalations(base_entries, head_entries))
    print("🔍 Component boundary ratchet...")
    for note in notes:
        print(f"⚠️  {note}")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        print(f"\n❌ Component boundary ratchet: {len(problems)} problem(s)")
        return 1
    print("✅ Component boundary ratchet: no new boundary violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
