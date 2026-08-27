#!/usr/bin/env python3
"""Generate docs/FLAGS.md — a Python-runtime catalog of governance env flags.

Why a generator and not a hand-written doc: a static flag list goes stale the
moment a flag is added (the exact discoverability gap this closes). This walks
the Python roots in ``SCAN_DIRS`` and extracts supported direct reads, registered
wrapper calls, module-level string keys, and explicitly declared dynamic keys.
It deliberately does not inventory Elixir or deployment manifests.

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
NO_READER_FALLBACK = "None (no reader fallback)"
# Governance-critical flags that carry no UNITARES_/GOVERNANCE_ prefix and would
# otherwise be invisible in this catalog. Kept as an explicit allowlist rather
# than "index every getenv" so the table stays a governance index and does not
# flood with PATH/HOME/PORT noise. Add a name here when it gates governance
# behavior but cannot be renamed to a prefixed form.
EXTRA_FLAGS = frozenset({"STRICT_IDENTITY_REQUIRED"})
GETENV = {"getenv", "get"}  # os.getenv(...) / os.environ.get(...)


@dataclass(frozen=True)
class EnvHelper:
    """A bounded call-through env reader understood by the catalog.

    ``default_arg`` / ``default_keyword`` identify an explicit fallback at the
    call site. ``implicit_fallback`` is used only when the wrapper supplies its
    own fallback. Paths prevent an unrelated same-named function from becoming
    a false env read elsewhere in the repository.
    """

    paths: frozenset[str]
    implicit_fallback: str | None = None
    default_arg: int | None = 1
    default_keyword: str | None = "default"
    purpose: str = ""


# Env reads that go through a wrapper instead of os.getenv directly. Keep this
# registry deliberately explicit: it is auditable, path-scoped, and says how an
# unset value is represented without pretending to do interprocedural analysis.
ENV_HELPERS = {
    "env_truthy": EnvHelper(
        frozenset({"src/mcp_listen_config.py"}),
        implicit_fallback="False",
    ),
    "split_csv_env": EnvHelper(
        frozenset({"src/mcp_listen_config.py"}),
        implicit_fallback="[]",
        default_arg=None,
        default_keyword=None,
    ),
    "_flag_enabled": EnvHelper(
        frozenset({"src/retrieval.py", "src/reranker.py"}),
        implicit_fallback="False",
    ),
    "_env_float": EnvHelper(
        frozenset({"agents/dialectic_reviewer/reviewer.py"}),
    ),
    "_server_marker_path": EnvHelper(
        frozenset({"src/process_management.py"}),
        default_keyword="default_name",
        purpose="Resolve the server PID/lock path or use the repo-local data path",
    ),
}


@dataclass
class Flag:
    name: str
    fallback_sites: dict[str, list[str]] = field(default_factory=dict)
    purpose: str = ""
    purpose_priority: int = -1
    sites: list[str] = field(default_factory=list)

    def add_read(self, fallback: str, site: str) -> None:
        """Record one reader contract without collapsing conflicting fallbacks."""
        self.fallback_sites.setdefault(fallback, []).append(site)
        self.sites.append(site)

    def consider_purpose(self, purpose: str, priority: int) -> None:
        """Prefer behavioral consumers over transport-only inferred reads."""
        if purpose and priority > self.purpose_priority:
            self.purpose = purpose
            self.purpose_priority = priority


@dataclass(frozen=True)
class IndirectFlag:
    """An env key selected through a named module-level string constant."""

    path: str
    constant_name: str
    fallback: str
    purpose: str


# Dynamic key selection is real runtime behavior, not a reason to widen
# SCAN_DIRS into deployment files. Resolve the declared constant from its source
# module so a rename cannot leave both the declaration and generated doc stale.
INDIRECT_FLAGS = (
    IndirectFlag(
        "agents/dialectic_reviewer/host_backends.py",
        "_DEFAULT_KEY_ENV",
        "''",
        "Default variable holding the external reviewer's API key",
    ),
)

BINDING_READER_PATH = "src/http_routes/effects.py"
BINDING_FLAG_CONSTANT = "_BINDING_FLAG"
EFFECT_TYPE_PRODUCER_PATHS = frozenset(
    {
        "agents/sdk/src/unitares_sdk/lease_plane/client.py",
        "src/mcp_handlers/dialectic/governed_spawn.py",
    }
)


def _contains_os_environ(node: ast.expr) -> bool:
    """Whether an expression selects os.environ, possibly via an injected map."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    ):
        return True
    if isinstance(node, ast.IfExp):
        return _contains_os_environ(node.body) or _contains_os_environ(node.orelse)
    return False


def _is_env_read(node: ast.Call, rel: str) -> str | None:
    """Return the reader's name if this call reads an env var, else None.

    "os" for a direct os.getenv / os.environ.get; otherwise the wrapper's name
    (see ENV_HELPERS), which the caller uses to render the reader fallback.
    """
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in GETENV:
        # os.getenv(...) or os.environ.get(...)
        base = f.value
        if isinstance(base, ast.Name) and base.id == "os":
            return "os"
        if _contains_os_environ(base):
            return "os"
    if isinstance(f, ast.Name):
        helper = ENV_HELPERS.get(f.id)
        if helper is not None and rel in helper.paths:
            return f.id
    return None


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Collect simple top-level NAME = 'string' and annotated equivalents."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.target.id] = node.value.value
    return consts


def _literal_dict_values(tree: ast.Module, field: str) -> list[tuple[str, int]]:
    """Find literal string values assigned to one literal dictionary field."""
    values: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == field
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                values.append((value.value, value.lineno))
    return values


def _explicit_default(node: ast.Call, reader: str) -> ast.expr | None:
    """Return an explicit reader fallback, including keyword ``default=``."""
    if reader == "os":
        default_arg = 1
        default_keyword = "default"
    else:
        helper = ENV_HELPERS[reader]
        default_arg = helper.default_arg
        default_keyword = helper.default_keyword

    if default_arg is not None and len(node.args) > default_arg:
        return node.args[default_arg]
    if default_keyword is not None:
        for keyword in node.keywords:
            if keyword.arg == default_keyword:
                return keyword.value
    return None


def _reader_fallback(node: ast.Call, reader: str) -> str:
    """Render the fallback contract for one concrete reader call."""
    default_node = _explicit_default(node, reader)
    if default_node is not None:
        try:
            return ast.unparse(default_node)
        except Exception:
            return "?"
    if reader != "os":
        fallback = ENV_HELPERS[reader].implicit_fallback
        if fallback is not None:
            return f"{fallback} (via {reader})"
    return NO_READER_FALLBACK


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
    def __init__(self, rel: str, consts: dict[str, str] | None = None):
        self.rel = rel
        self.flags: dict[str, Flag] = {}
        self._func_stack: list[ast.FunctionDef] = []
        self._static_loop_depth = 0
        # Module-level NAME = "UNITARES_..." bindings. Without these, a read
        # written as
        #     _OPERATOR_TOKENS_ENV = "UNITARES_OPERATOR_TOKENS"
        #     os.environ.get(_OPERATOR_TOKENS_ENV, "")
        # is a Name, not a Constant, and drops out of the catalog -- which is
        # how the operator-token allowlist came to be undocumented.
        self._value_scopes: list[dict[str, str | tuple[str, ...] | None]] = [
            dict(consts or {})
        ]

    def _lookup_value(self, name: str) -> str | tuple[str, ...] | None:
        for scope in reversed(self._value_scopes):
            if name in scope:
                return scope[name]
        return None

    def _static_value(self, node: ast.expr) -> str | tuple[str, ...] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self._lookup_value(node.id)
        if isinstance(node, (ast.Tuple, ast.List)):
            values: list[str] = []
            for elt in node.elts:
                value = self._static_value(elt)
                if not isinstance(value, str):
                    return None
                values.append(value)
            return tuple(values)
        return None

    def _key_name(self, key: ast.expr) -> str | None:
        value = self._static_value(key)
        return value if isinstance(value, str) else None

    def visit_FunctionDef(self, node):
        self._func_stack.append(node)
        arguments = (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        # Parameters and dynamic assignments shadow an outer static binding. A
        # stored None means "known unknown" and stops lookup from falling through
        # to a same-named module constant.
        self._value_scopes.append({argument.arg: None for argument in arguments})
        self.generic_visit(node)
        self._value_scopes.pop()
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._value_scopes.append({})
        self.generic_visit(node)
        self._value_scopes.pop()

    def visit_Assign(self, node):
        value = self._static_value(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._value_scopes[-1][target.id] = value
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None and isinstance(node.target, ast.Name):
            value = self._static_value(node.value)
            self._value_scopes[-1][node.target.id] = value
        self.generic_visit(node)

    def visit_For(self, node):
        values = self._static_value(node.iter)
        if isinstance(node.target, ast.Name) and isinstance(values, tuple):
            # Visit a statically bounded loop once per concrete string binding.
            # This covers forwarding allowlists such as:
            #     names = ("UNITARES_A", "UNITARES_B")
            #     for name in names: os.environ.get(name)
            self.visit(node.iter)
            scope = self._value_scopes[-1]
            had_previous = node.target.id in scope
            previous = scope.get(node.target.id)
            self._static_loop_depth += 1
            try:
                for value in values:
                    scope[node.target.id] = value
                    for statement in node.body:
                        self.visit(statement)
            finally:
                self._static_loop_depth -= 1
            if had_previous:
                scope[node.target.id] = previous
            else:
                scope.pop(node.target.id, None)
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_Call(self, node):
        reader = _is_env_read(node, self.rel)
        if reader and node.args:
            name = self._key_name(node.args[0])
            if name and (name.startswith(PREFIXES) or name in EXTRA_FLAGS):
                fl = self.flags.setdefault(name, Flag(name))
                # purpose = enclosing function's docstring first sentence
                if self._func_stack and not self._static_loop_depth:
                    doc = ast.get_docstring(self._func_stack[-1])
                    fn = self._func_stack[-1].name
                    fl.consider_purpose(
                        _first_sentence(doc) or f"read by {fn}()",
                        priority=1,
                    )
                elif reader != "os" and not self._static_loop_depth:
                    fl.consider_purpose(ENV_HELPERS[reader].purpose, priority=1)
                fl.add_read(
                    _reader_fallback(node, reader),
                    f"{self.rel}:{node.lineno}",
                )
        self.generic_visit(node)


def _merge_flag(target: Flag, source: Flag) -> None:
    """Merge a file-local result while preserving every fallback/site pair."""
    for fallback, sites in source.fallback_sites.items():
        target.fallback_sites.setdefault(fallback, []).extend(sites)
    target.consider_purpose(source.purpose, source.purpose_priority)
    target.sites.extend(source.sites)


def collect() -> dict[str, Flag]:
    flags: dict[str, Flag] = {}
    module_constants: dict[str, dict[str, str]] = {}
    concrete_effect_types: dict[str, list[str]] = {}
    for d in SCAN_DIRS:
        # sorted(): rglob yields in filesystem order, which differs between
        # APFS and ext4. Two fields below are order-dependent — `purpose` takes
        # the first non-empty docstring found, and `sites` preserves insertion
        # order — so an unsorted walk makes this generated file platform-
        # specific. It then passes `--check` only on whichever OS last ran the
        # generator: regenerating on macOS reliably reddens CI on Linux, and
        # vice versa, with a diff that points at flags the author never touched.
        # Measured 2026-08-06: 4 flags (UNITARES_AGENT_LOCK_BACKEND,
        # UNITARES_FIRST_RUN, UNITARES_LLM_MODEL, UNITARES_UDS_SOCKET) rendered
        # differently under the two walk orders.
        for py in sorted((REPO / d).rglob("*.py")):
            relative_path = py.relative_to(REPO)
            if "tests" in relative_path.parts or py.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = relative_path.as_posix()
            consts = _module_string_constants(tree)
            module_constants[rel] = consts
            if rel in EFFECT_TYPE_PRODUCER_PATHS:
                for effect_type, lineno in _literal_dict_values(tree, "effect_type"):
                    concrete_effect_types.setdefault(effect_type, []).append(
                        f"{rel}:{lineno}"
                    )
            c = Collector(rel, consts)
            c.visit(tree)
            for name, fl in c.flags.items():
                tgt = flags.setdefault(name, Flag(name))
                _merge_flag(tgt, fl)

    for indirect in INDIRECT_FLAGS:
        name = module_constants.get(indirect.path, {}).get(indirect.constant_name)
        if name is None:
            raise RuntimeError(
                f"indirect env key {indirect.path}:{indirect.constant_name} is missing"
            )
        tgt = flags.setdefault(name, Flag(name))
        tgt.add_read(indirect.fallback, f"{indirect.path}:0")
        tgt.consider_purpose(indirect.purpose, priority=1)

    # _binding_enforced() constructs a flag suffix from the forwarded effect
    # type. Tie the catalog to both halves of that runtime contract: the actual
    # base constant in effects.py and concrete literal effect payloads emitted by
    # production code. A new producer therefore updates the catalog without a
    # second hand-maintained list.
    binding_base = module_constants.get(BINDING_READER_PATH, {}).get(
        BINDING_FLAG_CONSTANT
    )
    if binding_base is None:
        raise RuntimeError(
            f"binding env base {BINDING_READER_PATH}:{BINDING_FLAG_CONSTANT} is missing"
        )
    for effect_type in sorted(concrete_effect_types):
        suffix = "".join(
            character if character.isalnum() else "_" for character in effect_type
        ).upper()
        name = f"{binding_base}_{suffix}"
        if not name.startswith(PREFIXES):
            continue
        tgt = flags.setdefault(name, Flag(name))
        tgt.add_read(NO_READER_FALLBACK, f"{BINDING_READER_PATH}:0")
        tgt.consider_purpose(
            f"Default-off per-effect binding gate for {effect_type}",
            priority=1,
        )
    return flags


def render(flags: dict[str, Flag]) -> str:
    rows = []
    for name in sorted(flags):
        fl = flags[name]
        fallback_items = list(fl.fallback_sites.items()) or [
            (NO_READER_FALLBACK, [])
        ]
        if len(fallback_items) == 1:
            fallback = f"`{fallback_items[0][0].replace('|', '\\|')}`"
        else:
            file_to_fallbacks: dict[str, set[str]] = {}
            for value, read_sites in fallback_items:
                for site in read_sites:
                    filename = site.rsplit(":", 1)[0]
                    file_to_fallbacks.setdefault(filename, set()).add(value)
            rendered_fallbacks = []
            for value, read_sites in fallback_items:
                locations = []
                for site in read_sites:
                    filename = site.rsplit(":", 1)[0]
                    location = site if len(file_to_fallbacks[filename]) > 1 else filename
                    if location not in locations:
                        locations.append(location)
                rendered_fallbacks.append(
                    f"`{value.replace('|', '\\|')}` ({', '.join(locations)})"
                )
            fallback = "varies: " + "; ".join(rendered_fallbacks)
        purpose = (fl.purpose or "").replace("|", "\\|") or "—"
        # The general Read-at column cites files so unrelated line movement does
        # not stale the catalog. The fallback column uses file:line only when one
        # file contains conflicting contracts and the line is needed to map them.
        files = list(dict.fromkeys(s.rsplit(":", 1)[0] for s in fl.sites))
        if len(files) > 3:
            sites = ", ".join(files[:2]) + f" (+{len(files) - 2} more)"
        else:
            sites = ", ".join(files)
        rows.append(f"| `{name}` | {fallback} | {purpose} | {sites} |")

    body = "\n".join(rows)
    return f"""<!-- GENERATED by scripts/dev/flag_catalog.py — do not edit by hand. Re-run to refresh. -->
# Python Governance Env Reads

Catalog of statically resolvable `UNITARES_*` / `GOVERNANCE_*` Python-runtime
environment reads under `config/`, `src/`, `agents/`, and `governance_core/`
(plus a curated allowlist of governance-critical unprefixed flags and bounded
inference for indirect/dynamic keys). It covers direct `os.getenv` /
`os.environ.get` reads, registered wrappers, module-level string keys, and
literal tuple-loop forwarding allowlists. It does **not** claim to inventory
Elixir or deployment manifests. **Generated** by
`scripts/dev/flag_catalog.py` — edit the reader defaults/docstrings or the bounded
reader declarations, not this file, then re-run. `Reader fallback(s)` lists every
distinct explicit fallback expression passed to a reader, registered wrapper
fallback, or `None (no reader fallback)`; `varies` maps conflicting contracts to
their read sites. Downstream accessors may further transform those values.
`Purpose` is the first sentence of the enclosing accessor's docstring.

For *consequential, flag-gated capabilities* and their **wake conditions**, see
`docs/operations/dormant-capability-registry.md` (Theme 6) — this file is the flat
index; that one is the curated decision record.

**{len(flags)} flags.**

| Flag | Reader fallback(s) | Purpose | Read at |
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
