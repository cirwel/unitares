"""Documented shell commands must resolve against the code that runs them.

Living docs (the ``check_doc_health.collect_md_files`` set: docs, skills,
``commands/*.md``) tell readers to run ``npm run X``, ``make X``,
``scripts/unitares <sub>`` and ``python3 scripts/....py --flag``. The doc lint
is a denylist of known-bad phrasings, so a renamed target or a never-existing
flag passes it; the docs audit found ``npm run format:check`` in a handoff doc
long after ``dashboard/package.json`` had only ``lint``, ``lint:fix`` and
``test``. This test reads only commands inside code (fenced blocks and inline
backtick spans) and holds each one to its source of truth, parsed without
running anything: the ``scripts`` map of ``dashboard/package.json``, the
``Makefile`` rule names, the top-level dispatch ``case`` of ``scripts/unitares``,
and the literal ``add_argument`` option strings of the named script (scripts
whose options are built dynamically, or that are not argparse scripts, are
skipped rather than guessed at). Commands that ``cd`` out of the repo first,
such as the Apache AGE source build, are not about this repo and are ignored.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH_PATH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_PACKAGE_JSON = REPO_ROOT / "dashboard" / "package.json"
_MAKEFILE = REPO_ROOT / "Makefile"
_CLI = REPO_ROOT / "scripts" / "unitares"


# --- Sources of truth -------------------------------------------------------


def _npm_scripts() -> frozenset[str]:
    return frozenset(json.loads(_PACKAGE_JSON.read_text())["scripts"])


_MAKE_RULE = re.compile(r"^([A-Za-z0-9_.][A-Za-z0-9_. -]*?)\s*::?(?!=)")


def _make_targets() -> frozenset[str]:
    targets: set[str] = set()
    for line in _MAKEFILE.read_text().splitlines():
        if line.startswith(("\t", "#", " ")):
            continue
        m = _MAKE_RULE.match(line)
        if m:
            targets.update(t for t in m.group(1).split() if not t.startswith("."))
    return frozenset(targets)


def _cli_subcommands() -> frozenset[str]:
    lines = _CLI.read_text().splitlines()
    start = next(
        i for i, line in enumerate(lines) if re.match(r'^case "\$\{1:-help\}" in', line)
    )
    labels: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip() == "esac":
            break
        m = re.match(r"^\s*([^\s)]+)\)", line)
        if m:
            labels.update(p for p in m.group(1).split("|") if p != "*")
    return frozenset(labels)


_PARSER_FACTORIES = {
    "ArgumentParser",
    "add_parser",
    "add_subparsers",
    "add_argument_group",
    "add_mutually_exclusive_group",
}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


@lru_cache(maxsize=None)
def _argparse_options(script: str) -> frozenset[str] | None:
    return _literal_argparse_options(REPO_ROOT / script)


def _literal_argparse_options(path: Path) -> frozenset[str] | None:
    """Literal ``--option`` strings of a script, or None when not checkable.

    None means: the file is missing, is not an argparse script, or builds any
    option dynamically (non-literal ``add_argument`` names, ``parents=``, or a
    parser object handed to a helper defined in another module).
    """
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return None
    local_defs = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    parser_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _call_name(node.value.func) in _PARSER_FACTORIES:
                parser_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    options: set[str] = set()
    saw_add_argument = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name == "add_argument":
            saw_add_argument = True
            if not node.args or not all(
                isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args
            ):
                return None
            options.update(a.value for a in node.args if a.value.startswith("--"))
        elif name == "ArgumentParser" and any(k.arg == "parents" for k in node.keywords):
            return None
        elif name not in local_defs and name not in _PARSER_FACTORIES and any(
            isinstance(a, ast.Name) and a.id in parser_names for a in node.args
        ):
            return None
    return frozenset(options) if saw_add_argument else None


@dataclass(frozen=True)
class Sources:
    npm_scripts: frozenset[str]
    make_targets: frozenset[str]
    cli_subcommands: frozenset[str]
    argparse_options: Callable[[str], frozenset[str] | None] = field(compare=False)


def _repo_sources() -> Sources:
    return Sources(_npm_scripts(), _make_targets(), _cli_subcommands(), _argparse_options)


# --- Doc command extraction -------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_PYTHON = re.compile(r"^(?:\S*/)?python(?:3(?:\.\d+)?)?$")
_SCRIPT_PY = re.compile(r"^(?:\./)?(scripts/[\w./-]+\.py)$")
_CLI_TOKEN = re.compile(r"^(?:\./)?scripts/unitares$")


def _segments(text: str):
    """Yield (line_no, segment) for each fenced-block command and inline span.

    Fenced commands keep a per-block ``external`` flag that turns on after a
    ``cd`` away from the repo root or ``dashboard/``; inline spans are judged
    on their own.
    """
    lines = text.splitlines()
    in_fence = False
    external = False
    pending: list[str] = []
    pending_line = 0
    for i, raw in enumerate(lines, 1):
        if _FENCE.match(raw):
            in_fence, external, pending = not in_fence, False, []
            continue
        if in_fence:
            line = re.sub(r"^\s*\$\s+", "", raw)
            if not pending:
                pending_line = i
            if line.rstrip().endswith("\\"):
                pending.append(line.rstrip()[:-1])
                continue
            pending.append(line)
            command = " ".join(pending)
            pending = []
            external = yield from _emit(pending_line, command, external)
        else:
            for m in _INLINE.finditer(raw):
                yield from _emit(i, m.group(1), False)


def _emit(line_no: int, command: str, external: bool):
    command = command.split(" #", 1)[0].strip()
    if not command or command.startswith("#"):
        return external
    for part in _SPLIT.split(command):
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        if not tokens:
            continue
        if tokens[:2] == ["git", "clone"]:
            # Commands after a clone build that other tree, not this repo.
            external = True
            continue
        if tokens[0] == "cd":
            target = tokens[1].rstrip("/") if len(tokens) > 1 else "~"
            external = not (
                target in {"-", "dashboard", "..", "unitares"}
                or target.endswith(("/unitares", "/dashboard"))
                or target.startswith("$")
            )
            continue
        if not external:
            yield line_no, tokens
    return external


def check_doc_text(rel: str, text: str, sources: Sources) -> list[str]:
    failures: list[str] = []

    def fail(line_no: int, claim: str, truth: str) -> None:
        failures.append(f"{rel}:{line_no}: documents `{claim}`, but {truth}")

    for line_no, tokens in _segments(text):
        head = tokens[0]
        if head == "npm" and len(tokens) > 1:
            if tokens[1] in {"run", "run-script"} and len(tokens) > 2:
                script = tokens[2]
            elif tokens[1] in {"test", "t", "start"}:
                script = "test" if tokens[1] == "t" else tokens[1]
            else:
                continue
            if script.startswith(("-", "<", "$")):
                continue
            if script not in sources.npm_scripts:
                fail(
                    line_no, " ".join(tokens[:3]),
                    f"dashboard/package.json `scripts` has only {sorted(sources.npm_scripts)}",
                )
        elif head == "make":
            if any(t in {"-C", "-f"} or t.startswith(("-C", "-f", "--directory", "--file"))
                   for t in tokens[1:]):
                continue
            for target in tokens[1:]:
                if target.startswith(("-", "<", "$")) or "=" in target:
                    continue
                if target not in sources.make_targets:
                    fail(line_no, f"make {target}",
                         f"Makefile defines no `{target}` rule")
        elif _CLI_TOKEN.match(head) or (
            head in {"bash", "sh"} and len(tokens) > 1 and _CLI_TOKEN.match(tokens[1])
        ):
            rest = tokens[1:] if _CLI_TOKEN.match(head) else tokens[2:]
            if rest and not rest[0].startswith(("<", "$", "[")):
                if rest[0] not in sources.cli_subcommands:
                    fail(line_no, f"scripts/unitares {rest[0]}",
                         "the scripts/unitares dispatch `case` has no such subcommand")
        else:
            if _PYTHON.match(head) and len(tokens) > 1:
                script_at = 1
            elif _SCRIPT_PY.match(head):
                script_at = 0
            else:
                continue
            m = _SCRIPT_PY.match(tokens[script_at])
            if not m:
                continue
            script = m.group(1)
            options = sources.argparse_options(script)
            if options is None:
                continue
            for token in tokens[script_at + 1 :]:
                if token == "--":
                    break
                if not token.startswith("--") or token == "--help":
                    continue
                flag = token.split("=", 1)[0]
                # argparse accepts any unambiguous prefix by default.
                if flag in options or len([o for o in options if o.startswith(flag)]) == 1:
                    continue
                fail(line_no, f"{script} {flag}",
                     f"{script} declares no `{flag}` add_argument option")
    return failures


# --- Tests ------------------------------------------------------------------

# (doc, claim) pairs that legitimately do not resolve. Each entry must still
# match a real failure, so a fixed or reworded doc forces its removal.
_ALLOWED = {
    # Bare `make install` after the fenced Apache AGE source build (PGXS
    # target in the AGE tree); this repo's Makefile has no install rule.
    ("docs/install/PLAYBOOK.md", "make install"),
    # Quotes the 2026-09-12 CI log verbatim; the flag existed then and was
    # removed with the tracked manifest in #2402.
    (
        "docs/evaluations/accountability-journey/capture-stage-2168-artifacts-arm.md",
        "scripts/dev/skills_manifest.py --check",
    ),
}


def _claim_key(failure: str) -> tuple[str, str]:
    location, rest = failure.split(": documents `", 1)
    return location.rsplit(":", 1)[0], rest.split("`", 1)[0]


@pytest.fixture(scope="module")
def doc_health():
    spec = importlib.util.spec_from_file_location(
        "check_doc_health_commands", _DOC_HEALTH_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sources_of_truth_parse():
    sources = _repo_sources()
    assert {"lint", "test"} <= sources.npm_scripts
    assert {"test", "demo", "docs"} <= sources.make_targets
    assert {"status", "health", "update", "model"} <= sources.cli_subcommands
    assert "--apply" in (_argparse_options("scripts/dev/apply_migrations.py") or ())


def test_documented_commands_resolve(doc_health):
    sources = _repo_sources()
    failures: list[str] = []
    checked = 0
    for path in doc_health.collect_md_files():
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(errors="replace")
        checked += sum(1 for _ in _segments(text))
        failures.extend(check_doc_text(rel, text, sources))
    # Guard against the extractor silently finding nothing to check.
    assert checked > 500, f"only {checked} code commands extracted from docs"
    used = {_claim_key(f) for f in failures} & _ALLOWED
    assert used == _ALLOWED, f"stale _ALLOWED entries, remove them: {sorted(_ALLOWED - used)}"
    failures = [f for f in failures if _claim_key(f) not in _ALLOWED]
    assert not failures, (
        "Documented commands do not resolve against their source of truth:\n  "
        + "\n  ".join(failures)
    )


# Regression: the confirmed stale line from docs/operations/
# kg-lineage-dashboard-handoff.md:172 on origin/master before the audit fix.
_STALE_HANDOFF = """\
- `pytest tests/test_dashboard_redesign_route.py tests/test_dashboard.py`
- `cd dashboard && npm test && npm run lint && npm run format:check`
  (the `format:check` gate bit a prior dashboard PR — prettier the JS).
"""


def test_flags_npm_script_missing_from_package_json():
    failures = check_doc_text(
        "docs/operations/kg-lineage-dashboard-handoff.md", _STALE_HANDOFF, _repo_sources()
    )
    assert len(failures) == 1
    assert failures[0].startswith("docs/operations/kg-lineage-dashboard-handoff.md:2:")
    assert "npm run format:check" in failures[0]
    assert "dashboard/package.json" in failures[0]


def _fake_sources(options: dict[str, frozenset[str]]) -> Sources:
    return Sources(
        npm_scripts=frozenset({"lint", "test"}),
        make_targets=frozenset({"demo", "test"}),
        cli_subcommands=frozenset({"status", "health"}),
        argparse_options=lambda script: options.get(script),
    )


def test_flags_make_cli_and_argparse_drift():
    doc = """\
Run `make demo` then `make up`.

```bash
./scripts/unitares status
./scripts/unitares restart
python3 scripts/dev/tool.py --apply \\
  --dry-rnu
python3 scripts/dev/dynamic.py --anything
```
"""
    failures = check_doc_text(
        "docs/x.md", doc,
        _fake_sources({"scripts/dev/tool.py": frozenset({"--apply", "--dry-run"})}),
    )
    assert [f.split(": documents ")[0] for f in failures] == [
        "docs/x.md:1", "docs/x.md:5", "docs/x.md:6",
    ]
    assert "make up" in failures[0] and "Makefile" in failures[0]
    assert "scripts/unitares restart" in failures[1]
    assert "--dry-rnu" in failures[2]


def test_ignores_commands_run_outside_the_repo_and_prose():
    doc = """\
```bash
cd /tmp/age-build
make PG_CONFIG="$PG_CONFIG"
make install PG_CONFIG="$PG_CONFIG"
cd -
make demo
```

These changes make up most of the load; `npm install` is fine.
"""
    assert check_doc_text("docs/x.md", doc, _fake_sources({})) == []


def test_dynamic_argparse_script_is_skipped(tmp_path):
    script = tmp_path / "dyn.py"
    script.write_text(
        "import argparse\nfrom helpers import add_cross_era_argument\n"
        "p = argparse.ArgumentParser()\np.add_argument('--a')\n"
        "add_cross_era_argument(p)\n"
    )
    assert _literal_argparse_options(script) is None
    script.write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--a', '-a')\nsub = p.add_subparsers()\n"
        "s = sub.add_parser('x')\ns.add_argument('--b')\n"
    )
    assert _literal_argparse_options(script) == frozenset({"--a", "--b"})
