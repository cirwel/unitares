#!/usr/bin/env python3
"""Generate docs/FLAGS.md — a ground-truth catalog of governance env flags.

Why a generator and not a hand-written doc: a static flag list goes stale the
moment a flag is added (the exact discoverability gap this closes). This walks
the source, extracts every ``os.getenv``/``os.environ.get`` read of a
``UNITARES_*`` / ``GOVERNANCE_*`` name via AST, and emits an accurate table of
flag -> default -> purpose -> read sites. Re-run on demand; CI can diff it.

Usage:
    python3 scripts/dev/flag_catalog.py            # write docs/FLAGS.md
    python3 scripts/dev/flag_catalog.py --check    # exit 1 if docs/FLAGS.md is stale
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["config", "src", "agents", "governance_core"]
PREFIXES = ("UNITARES_", "GOVERNANCE_")
GETENV = {"getenv", "get"}  # os.getenv(...) / os.environ.get(...)


@dataclass
class Flag:
    name: str
    default: str | None = None
    purpose: str = ""
    sites: list[str] = field(default_factory=list)


def _is_env_read(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in GETENV:
        # os.getenv(...) or os.environ.get(...)
        base = f.value
        if isinstance(base, ast.Name) and base.id == "os":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "environ":
            return True
    return False


def _first_sentence(text: str | None) -> str:
    if not text:
        return ""
    line = " ".join(text.strip().split())
    for sep in (". ", " (", "—"):
        if sep in line:
            line = line.split(sep)[0]
            break
    return line[:140]


class Collector(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.flags: dict[str, Flag] = {}
        self._func_stack: list[ast.FunctionDef] = []

    def visit_FunctionDef(self, node):
        self._func_stack.append(node)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        if _is_env_read(node) and node.args:
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                    and key.value.startswith(PREFIXES):
                name = key.value
                fl = self.flags.setdefault(name, Flag(name))
                # default = 2nd positional arg, unparsed
                if len(node.args) >= 2:
                    try:
                        d = ast.unparse(node.args[1])
                    except Exception:
                        d = "?"
                    if fl.default is None or fl.default == "(required)":
                        fl.default = d
                elif fl.default is None:
                    fl.default = "(required)"
                # purpose = enclosing function's docstring first sentence
                if self._func_stack and not fl.purpose:
                    doc = ast.get_docstring(self._func_stack[-1])
                    fn = self._func_stack[-1].name
                    fl.purpose = _first_sentence(doc) or f"read by {fn}()"
                fl.sites.append(f"{self.rel}:{node.lineno}")
        self.generic_visit(node)


def collect() -> dict[str, Flag]:
    flags: dict[str, Flag] = {}
    for d in SCAN_DIRS:
        for py in (REPO / d).rglob("*.py"):
            if "/tests/" in str(py) or py.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            c = Collector(str(py.relative_to(REPO)))
            c.visit(tree)
            for name, fl in c.flags.items():
                tgt = flags.setdefault(name, Flag(name))
                if tgt.default in (None, "(required)") and fl.default not in (None, "(required)"):
                    tgt.default = fl.default
                elif tgt.default is None:
                    tgt.default = fl.default
                if not tgt.purpose and fl.purpose:
                    tgt.purpose = fl.purpose
                tgt.sites.extend(fl.sites)
    return flags


def render(flags: dict[str, Flag]) -> str:
    rows = []
    for name in sorted(flags):
        fl = flags[name]
        default = (fl.default or "(required)").replace("|", "\\|")
        if len(default) > 32:
            default = default[:29] + "…"
        purpose = (fl.purpose or "").replace("|", "\\|") or "—"
        sites = ", ".join(dict.fromkeys(fl.sites))  # dedupe, keep order
        if len(fl.sites) > 3:
            first = list(dict.fromkeys(fl.sites))[:2]
            sites = ", ".join(first) + f" (+{len(set(fl.sites)) - len(first)} more)"
        rows.append(f"| `{name}` | `{default}` | {purpose} | {sites} |")

    body = "\n".join(rows)
    return f"""<!-- GENERATED by scripts/dev/flag_catalog.py — do not edit by hand. Re-run to refresh. -->
# Governance Env Flags

Ground-truth catalog of `UNITARES_*` / `GOVERNANCE_*` environment flags, extracted
from the source (every `os.getenv` / `os.environ.get` read). **Generated** by
`scripts/dev/flag_catalog.py` — edit the code's defaults/docstrings, not this file,
then re-run. `Default` is the literal 2nd arg to the read; `Purpose` is the first
sentence of the enclosing accessor's docstring (so giving a flag's accessor a
docstring improves this table).

For *consequential, flag-gated capabilities* and their **wake conditions**, see
`docs/operations/dormant-capability-registry.md` (Theme 6) — this file is the flat
index; that one is the curated decision record.

**{len(flags)} flags.**

| Flag | Default | Purpose | Read at |
|---|---|---|---|
{body}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if docs/FLAGS.md is stale")
    args = ap.parse_args()

    out = REPO / "docs" / "FLAGS.md"
    content = render(collect())
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != content:
            print("docs/FLAGS.md is stale — run: python3 scripts/dev/flag_catalog.py", file=sys.stderr)
            return 1
        print("docs/FLAGS.md is up to date.")
        return 0
    out.write_text(content, encoding="utf-8")
    print(f"Wrote {out.relative_to(REPO)} ({content.count(chr(10))} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
