#!/usr/bin/env python3
"""Fail if a named resident identity or operator domain is hardcoded in shipped source.

``check-repo-scope.sh`` already guards VENDOR neutrality — career artifacts,
per-vendor agent config, operator-local paths. This guards the other axis:
**fleet neutrality**. UNITARES ships to deployments that have their own
residents, or none. Which residents exist is declared per-deployment through
``UNITARES_RESIDENTS`` (see ``src/grounding/class_indicator.py``), and every
shipped code path must read that roster rather than naming anybody.

The leak this exists to catch is quiet by construction. On 2026-08-18 the
``/v1/residents`` tier-3 resolver filtered the declared roster through a
hardcoded ``["Vigil", "Sentinel", "Watcher", "Steward", "Chronicler",
"Lumen"]``. Any deployment whose residents were named anything else resolved
to an EMPTY list — still reported with ``source: "known-residents"``, so the
response read like a successful resolution of a roster that had silently
vanished. Nothing failed. A test even pinned the hardcoded order, so CI
defended the coupling.

WHAT IS AND IS NOT FLAGGED
--------------------------
Only **string literals in executable code** are flagged. Comments are invisible
to the AST and are deliberately fine: a note like "this threshold produced
false high-risk verdicts on Lumen 2026-05-08" is the reason a constant has the
value it has. Stripping that provenance would make the code less honest without
making it more portable. Docstrings are skipped for the same reason.

The exception is Python text the server delivers verbatim. Tool input schemas
are built from Pydantic models, so a ``Field``'s ``description=``, its
``json_schema_extra`` ``"brief"`` and the model's docstring reach every agent
that connects. That text is prose, not code, and gets the served-prose rule: a
resident name anywhere in it, as a word, is a finding. See "Served schema
text" below.

Names live in ``FLEET_IDENTITIES`` below, which is the one place in the repo
they are allowed to appear — the guard has to know what it is looking for, the
same way a secret scanner carries patterns.

The operator's own domain is the same leak on a different axis. Until
2026-09-25 ``src/dashboard_auth.py`` fell back to ``gov.cirwel.org`` as the
passkey relying party, so every other install got passkey setup that no
browser would ever complete, and nothing failed. ``OPERATOR_DOMAINS`` holds
those domains; a string literal CONTAINING one is flagged, because a domain,
unlike "Sentinel", has no innocent homonym in source.

Usage:
    python3 scripts/dev/check_fleet_identity_leak.py [--paths src agents/sdk/src]

Exit codes:
    0 — no hardcoded fleet identities in shipped source
    1 — at least one found (file:line printed)
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories whose contents SHIP or run as the server. Ops scripts, docs,
# tests, dashboards and plist templates are deliberately excluded: a deploy
# script for this operator's fleet is supposed to name this operator's fleet.
#
# This list must track ``[tool.setuptools.packages.find].include`` in
# pyproject.toml, which is ``src``, ``governance_core`` and ``config``;
# tests/test_residentless_install.py fails on a shipped package missing here.
# governance_core was missing from the first version of this guard — half the
# shipped artifact, and the half most obliged to be agnostic, since it is the
# pure-Python core every deployment imports. It passes, but it was not being
# checked. agents/sdk/src ships separately as the unitares-sdk PyPI package.
DEFAULT_PATHS = ("src", "governance_core", "config", "agents/sdk/src")

# The operator fleet identities that must not appear in shipped source. This
# list is the guard's own configuration — it is the single sanctioned mention.
FLEET_IDENTITIES = (
    "Vigil",
    "Sentinel",
    "Watcher",
    "Steward",
    "Chronicler",
    "Lumen",
)

# The operator's domains. Deployment config (plists, docs, ops scripts) names
# them; shipped source must read them from configuration instead.
OPERATOR_DOMAINS = ("cirwel.org",)

# Homonyms and protocol values — NOT agent-label dispatch. Exempt, with the
# reason, because the word is doing a different job in these files.
NOT_IDENTITIES: dict[str, str] = {
    "src/evaluation/resident_validation/model.py":
        '"steward" is a ROLE in VALID_ROLES (dogfood_probe/steward/builder/'
        'reviewer), unrelated to the agent of that name',
    "src/coordination_events.py":
        "Service literal — coordination-protocol service ids, drift-tested by "
        "test_emit_rejects_unknown_service; not agent labels",
    "src/coordination_failure_emit.py":
        "same coordination-protocol service ids as coordination_events.py",
    "src/http_routes/sentinel.py":
        "producer_ref protocol value, not an agent label lookup",
    "src/watcher_state_reader.py":
        "legacy filesystem path component (data/watcher) read only for "
        "migration off the pre-#595 location",
}

# Real couplings that exist today and are NOT yet fixed. These are REPORTED on
# every run and deliberately do NOT fail the build, so this guard can be
# adopted without a six-module refactor first.
#
# ⛔They are not silenced. A guard that printed "clean" over known coupling
# would be the same failure it exists to catch: an instrument reporting health
# it did not establish. Fix an entry and delete its line; never add one to
# quiet a NEW leak.
KNOWN_COUPLINGS: dict[str, str] = {
    "src/http_routes/vigil.py":
        "resident-specific route module that dispatches on label.lower() == "
        '"vigil"; mounted only with the reference-residents route pack '
        "(src/http_routes/packs.py), so an install without it carries no "
        "endpoint, but the name dispatch itself remains",
}

# Match only when the literal IS a name, not when it merely contains one.
#
# This distinction is the whole difference between a usable guard and noise.
# "Sentinel" appears legitimately all over shipped source as a SUBSYSTEM name
# and, in redis_client.py, as an unrelated product (Redis Sentinel). Those are
# route paths ("/v1/sentinel/backlog"), metric ids
# ("governance.sentinel.findings.7d") and prose ("Redis Sentinel connected:
# ..."). None of them dispatch on an agent's label.
#
# A fleet-identity leak looks different: the literal is the label itself,
# compared or keyed against an agent's ``label`` field — ``if label ==
# "Lumen"``, ``canonical_order = ["Vigil", ...]``, ``{"vigil": 2400}``. Those
# are always the bare name. Substring matching flagged 31 places of which 1
# was real; whole-literal matching flags the real ones.
_NAMES_LOWER = frozenset(n.lower() for n in FLEET_IDENTITIES)

# The served-prose rule: a name anywhere in the text, as a whole word, in any
# case. Prose has no comments, so every word reaches a reader, and labels are
# compared case-insensitively, so `lumen` is the same leak as `Lumen`. Word
# boundaries keep ordinary English out: "Sentinels", "vigilant", "watchers"
# and "stewardship" are not names. Used for served schema text (below) and
# for the served non-Python files (further below).
_NAME_WORD = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in FLEET_IDENTITIES) + r")\b", re.I
)


def _identity_literal(value: str) -> str | None:
    """Return the identity if the literal IS one, else None."""
    stripped = value.strip()
    return stripped if stripped.lower() in _NAMES_LOWER else None


def _operator_domain_in(value: str) -> str | None:
    """Return the operator domain a literal contains, else None."""
    lowered = value.lower()
    for domain in OPERATOR_DOMAINS:
        if domain in lowered:
            return domain
    return None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of Constant nodes that are docstrings, which are exempt."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


# ---------------------------------------------------------------------------
# Served schema text
# ---------------------------------------------------------------------------
# Some string literals in shipped Python are not code at all: they are the
# tool input schemas every connecting agent is served. src/tool_schemas.py
# builds each schema from a Pydantic model in these modules. A Field's
# description= reaches the caller whole through describe_tool, and its first
# sentence, or its authored json_schema_extra "brief", through tools/list. The
# model's docstring becomes the schema's own description on both.
# src/alias_schema.py replaces some of those texts for the workflow aliases.
#
# The literal rule only fires on a literal that IS a name, so a name inside a
# longer description passed it. A Codex review on #2489 caught one the guard
# had passed: observe's target_agent_id said some audit writers are "stored
# by name (e.g. system, sentinel)". Those literals are prose, so they get the
# served-prose rule (_NAME_WORD) instead of the literal rule, and, like the
# operator domain, no name exemption defers a hit in them.
#
# The rule is static, because the guard runs where the project's dependencies
# are not installed (the Repo Scope Guard workflow). It reads the text where it
# is written. A bare name is followed to its module-level assignment, so
# `description=_DESC` is read as the text `_DESC` holds; text imported from
# another module is not followed. The rule covers the common authored forms
# and is an early warning, not the complete check:
# tests/test_fleet_identity_leak_served.py reads every string of the schemas
# Pydantic actually builds (enum values, call-built defaults and imported text
# included), so a form this rule does not model is caught there.
SERVED_SCHEMA_GLOB = "src/mcp_handlers/schemas/*.py"
SERVED_SCHEMA_FILES = ("src/alias_schema.py",)
# Schema keywords whose text a caller reads: `description` (describe_tool,
# and tools/list unless a brief replaces it), `brief` (the authored tools/list
# short form, in json_schema_extra and in alias overrides), `examples`
# (advertised as data) and `title` (served when
# UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep preserves property titles) and
# `default` (a non-null default is kept in the advertised input schema).
# Field(...) takes these as keywords (and its default positionally too); the
# rest are dict entries.
SERVED_SCHEMA_KEYS = frozenset({"description", "brief", "examples", "title", "default"})


def is_served_schema_module(rel: str) -> bool:
    """True for a module whose literals include served tool-schema text."""
    path = PurePosixPath(rel)
    return any(path.match(p) for p in (SERVED_SCHEMA_GLOB, *SERVED_SCHEMA_FILES))


def _module_bindings(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """Every ``NAME = value`` and ``NAME: T = value`` at module level or in a
    class body, by name.

    All of them, not just the last: a class body captures the value a name
    holds when the class is defined, so a constant reassigned after a model
    that uses it still serves the earlier text. A class-scoped constant
    (``_DESC = ...`` then ``Field(description=_DESC)``) is served the same
    way. Scopes are merged, so a name bound in two places follows both:
    that over-reads, never under-reads, which is the safe direction for a
    leak guard.
    """
    out: dict[str, list[ast.expr]] = {}
    bodies = [getattr(tree, "body", [])] + [
        node.body for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ]
    for stmt in (stmt for body in bodies for stmt in body):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(stmt.value)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and isinstance(stmt.target, ast.Name)
        ):
            out.setdefault(stmt.target.id, []).append(stmt.value)
    return out


def served_schema_nodes(tree: ast.AST) -> set[int]:
    """ids() of the str Constant nodes whose text a served schema delivers.

    - the value of a keyword in ``SERVED_SCHEMA_KEYS`` on any call: not only
      ``Field(...)`` but the constructor forms that build the same schema
      text, such as ``json_schema_extra=dict(brief=...)`` and
      ``ConfigDict(title=...)``. Any call, rather than a list of known ones,
      over-reads where a list would miss the next wrapper, and the same
      keywords on a class statement, where Pydantic also takes model config;
    - the value of a dict-literal entry keyed by one of them, which covers
      ``json_schema_extra={"brief": ...}`` and the alias overrides;
    - a class docstring, which Pydantic serves as the model's description;
    - a field's default: ``Field``'s first positional argument, and a class
      attribute's plain value (``x: str = "..."``), which the advertised
      schema keeps when it is not null.

    Every string inside such a value counts (implicit concatenation, ``+``,
    f-string parts), and a bare name in it is followed, transitively, to its
    module-level assignment.
    """
    bindings = _module_bindings(tree)
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            roots += [kw.value for kw in node.keywords if kw.arg in SERVED_SCHEMA_KEYS]
            func = node.func
            is_field = (isinstance(func, ast.Name) and func.id == "Field") or (
                isinstance(func, ast.Attribute) and func.attr == "Field"
            )
            if is_field and node.args:
                roots.append(node.args[0])  # Field(default, ...)
        elif isinstance(node, ast.Dict):
            roots += [
                value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value in SERVED_SCHEMA_KEYS
            ]
        elif isinstance(node, ast.ClassDef):
            # Pydantic takes model config as class keywords too:
            # class P(BaseModel, title=...) serves that title.
            roots += [kw.value for kw in node.keywords if kw.arg in SERVED_SCHEMA_KEYS]
            roots += [
                stmt.value for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and stmt.value is not None
                and not isinstance(stmt.value, ast.Call)  # Field(...) is read above
            ]
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                roots.append(first.value)

    served: set[int] = set()
    followed: set[str] = set()
    while roots:
        for sub in ast.walk(roots.pop()):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                served.add(id(sub))
            elif (
                isinstance(sub, ast.Name)
                and sub.id in bindings
                and sub.id not in followed
            ):
                followed.add(sub.id)
                roots.extend(bindings[sub.id])
    return served


def scan_file(path: Path, *, served_schema: bool | None = None) -> list[str]:
    """Findings for one Python file.

    ``served_schema`` says whether the file holds served tool-schema text;
    left unset, it is decided by the file's path (``is_served_schema_module``).
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"  {path}: could not parse ({exc})"]

    exempt = _docstring_nodes(tree)
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    if served_schema is None:
        served_schema = is_served_schema_module(rel)
    served = served_schema_nodes(tree) if served_schema else set()
    findings: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        # A model's docstring is served, so it is not exempt as provenance.
        if id(node) in exempt and id(node) not in served:
            continue
        domain = _operator_domain_in(node.value)
        if domain:
            findings.append(
                f'  {rel}:{node.lineno}: hardcoded operator domain "{domain}" '
                f"in a string literal"
            )
        if id(node) in served:
            # As in served prose, the domain and every name are each reported.
            for name in dict.fromkeys(_NAME_WORD.findall(node.value)):
                findings.append(
                    f'  {rel}:{node.lineno}: fleet identity "{name}" in served '
                    f"schema text"
                )
            continue
        if domain:
            continue
        found = _identity_literal(node.value)
        if not found:
            continue
        findings.append(
            f'  {rel}:{node.lineno}: hardcoded fleet identity "{found}" '
            f"in a string literal"
        )
    return findings


def triage(rel: str, hits: list[str]) -> tuple[list[str], list[str]]:
    """Split one file's hits into (failing, known-but-deferred).

    ``NOT_IDENTITIES`` and ``KNOWN_COUPLINGS`` are exemptions for resident
    NAMES in code — homonyms, and couplings not yet fixed. Neither covers the
    operator's domain, so a domain hit fails the build in every file. Nor do
    they cover served schema text: a homonym argument is about what code does
    with a word, and a reader of prose cannot tell which job it is doing.
    """
    always = [h for h in hits if "operator domain" in h or "served schema text" in h]
    names = [
        h for h in hits if "operator domain" not in h and "served schema text" not in h
    ]
    if rel in NOT_IDENTITIES:
        return always, []
    if rel in KNOWN_COUPLINGS:
        return always, names
    return always + names, []


# ---------------------------------------------------------------------------
# Served surfaces
# ---------------------------------------------------------------------------
# The AST scan above reads Python. A fresh install also receives text that is
# not Python, verbatim: the dashboard served to the operator's browser, and the
# skills and tool descriptions served to every agent that connects. On
# 2026-09-26 a fresh-installer audit found this guard clean while the dashboard
# gated three panels on resident labels and bundled a real capture of one
# deployment's fleet, and the served skills carried one operator's Discord
# bridge. None of it was checked, because none of it was Python.
#
# Two rules, by how the text reaches its reader:
#
# - Dashboard CODE (.js, and <script> in .html) keeps the Python rule: a string
#   literal that IS a name is a finding, a comment naming one is provenance.
#   This half is BEST-EFFORT. It is a small scanner, not a JavaScript parser:
#   it does not see unquoted object keys (`{ Lumen: ... }`) or names inside
#   regex literals (`/^Lumen$/.test(label)`). Closing those means a tokenizer,
#   for files that are mostly being removed: the one keyed dict is in
#   snapshot.js, which is listed below and is being replaced with synthetic
#   data. Review dashboard JS for label dispatch by eye; the guard catches
#   the common form, a label compared or passed as a string.
# - PROSE (served .md, SKILL.md, tool_descriptions.json, .css, and .html
#   markup outside <script>) has no comments: every word is delivered. A
#   resident name anywhere in it, in any case, is a finding; labels are
#   compared case-insensitively, so `lumen` is the same leak as `Lumen`.
#   This half is the strict one, and it covers what every agent is served.
#
# The operator domain fails everywhere, as above.

# Everything under this root with these suffixes is served
# (src/http_routes/dashboard.py, http_dashboard_redesign).
SERVED_DASHBOARD_ROOT = "dashboard/redesign"
SERVED_DASHBOARD_SUFFIXES = (".js", ".html", ".md", ".css")
# The retired classic dashboard still serves these two at /phase.
SERVED_DASHBOARD_FILES = ("dashboard/phase.html", "dashboard/phase.js")
# Agent-facing text. tool_descriptions.json is served through tools/list and
# describe_tool; every skills/<name>/SKILL.md is served by the `skills` tool.
SERVED_PROSE_FILES = ("src/tool_descriptions.json",)
SERVED_SKILLS_GLOB = "skills/*/SKILL.md"

# Served surfaces that name residents today, each pinned to the EXACT
# multiset of matched values (name per occurrence, case as served) it is
# known to hold today — not just a count. Same contract as KNOWN_COUPLINGS:
# reported on every run, never silenced.
#
# A bare count used to be the exemption: "this file may have up to N hits."
# That let a developer remove one known reference and add a DIFFERENT one in
# the same file — the count stayed put, so the guard still passed the
# substitution. Pinning the actual matched values closes that: an approved
# occurrence may be removed for free (a partial fix still defers), but every
# remaining hit must fit inside what is on record for its own name, so a
# swapped-in name — or one more of a name already at its recorded count —
# is a NEW leak and fails. A ratchet that only turns one way: fixing a
# reference means deleting one entry from its file's tuple below, and the
# last deletion removes the entry.
#
# The record is per name, not per location: moving an already-listed name
# within an already-listed file passes. That is deliberate. Every listed file
# is slated to be rewritten or removed, the record can only shrink, and a
# location pin (line or surrounding text) breaks on every unrelated edit to
# files that are still live.
# Empty since 2026-09-27: the last entry, src/tool_descriptions.json, had its
# two example mentions replaced with generic ones.
SERVED_KNOWN_COUPLINGS: dict[str, tuple[tuple[str, ...], str]] = {}

_SCRIPT_BLOCK = re.compile(r"(<script\b[^>]*>)(.*?)(</script\b[^>]*>)", re.S | re.I)
_HIT_VALUE = re.compile(r'"([^"]*)"')


def _hit_value(hit: str) -> str:
    """The quoted matched name/domain out of one finding line."""
    m = _HIT_VALUE.search(hit)
    return m.group(1) if m else hit


def js_string_literals(source: str) -> list[tuple[int, str]]:
    """Return (line, value) for each string literal in JavaScript source.

    A small scanner, not a parser. It knows '...', "...", `...`, // and /* */
    comments, and descends into template interpolation: the dashboard builds
    markup as `<div>${head("Watcher", ...)}</div>`, so the label literal lives
    inside ${...}, and a scanner that took the template as one opaque string
    missed every one of them. The template's own text (outside ${...}) is
    returned as a literal too. Regex literals are skipped as opaque spans
    (heuristically, by what precedes the '/') so a quote INSIDE one — e.g.
    ``/["']/; const label = "Lumen";`` — cannot desynchronize the string
    scanner into missing the literal that follows. Single and double quoted
    strings cannot span a line in JavaScript, so that state is dropped at the
    newline and a desync costs at most one line.
    """
    out: list[tuple[int, str]] = []
    n = len(source)

    def prev_significant(i: int) -> str:
        """The nearest non-whitespace character before i, or "" at start."""
        j = i - 1
        while j >= 0 and source[j] in " \t\r\n":
            j -= 1
        return source[j] if j >= 0 else ""

    def looks_like_regex_start(i: int) -> bool:
        """True unless the char before '/' shows it is division, not a regex.

        A value-ish preceding token (identifier/number char, or a closing
        `)`/`]`/`}`) means '/' divides; anything else (operator, punctuation,
        another '(', start of file/statement) means '/' opens a regex
        literal. Imprecise for `return /re/` and similar keyword-preceded
        cases, but sufficient to stop a quote inside a regex from being read
        as a string opener.
        """
        prev = prev_significant(i)
        if not prev:
            return True
        return not (prev.isalnum() or prev in "_$)]}")

    def regex_literal(i: int, line: int) -> tuple[int, int]:
        """Skip a /pattern/flags literal starting at its opening '/'."""
        j = i + 1
        in_class = False
        while j < n:
            c = source[j]
            if c == "\\" and j + 1 < n:
                j += 2
                continue
            if c == "\n":
                # A real regex literal cannot contain a literal newline, so
                # this was not one; bail without consuming anything.
                return i + 1, line
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                j += 1
                break
            j += 1
        while j < n and source[j].isalpha():
            j += 1
        return j, line

    def scan(i: int, line: int, in_braces: bool) -> tuple[int, int]:
        """Scan code from i; inside ${...} stop after the matching '}'."""
        depth = 0
        while i < n:
            ch = source[i]
            if ch == "\n":
                line += 1
                i += 1
            elif source.startswith("//", i):
                end = source.find("\n", i)
                i = n if end == -1 else end
            elif source.startswith("/*", i):
                end = source.find("*/", i + 2)
                end = n if end == -1 else end + 2
                line += source.count("\n", i, end)
                i = end
            elif ch == "/" and looks_like_regex_start(i):
                i, line = regex_literal(i, line)
            elif ch in "'\"":
                start_line, j, buf = line, i + 1, []
                while j < n and source[j] != ch and source[j] != "\n":
                    if source[j] == "\\" and j + 1 < n:
                        buf.append(source[j + 1])
                        j += 2
                        continue
                    buf.append(source[j])
                    j += 1
                out.append((start_line, "".join(buf)))
                i = j + 1 if j < n and source[j] == ch else j
            elif ch == "`":
                i, line = template(i + 1, line)
            elif ch == "{" and in_braces:
                depth += 1
                i += 1
            elif ch == "}" and in_braces:
                if depth == 0:
                    return i + 1, line
                depth -= 1
                i += 1
            else:
                i += 1
        return i, line

    def template(i: int, line: int) -> tuple[int, int]:
        """Scan a template literal body from i (just past the opening backtick)."""
        start_line, buf = line, []
        while i < n and source[i] != "`":
            if source[i] == "\\" and i + 1 < n:
                buf.append(source[i + 1])
                i += 2
            elif source.startswith("${", i):
                buf.append("${}")
                i, line = scan(i + 2, line, in_braces=True)
            else:
                if source[i] == "\n":
                    line += 1
                buf.append(source[i])
                i += 1
        out.append((start_line, "".join(buf)))
        return i + 1, line

    scan(0, 1, in_braces=False)
    out.sort(key=lambda item: item[0])
    return out


def _code_findings(rel: str, source: str, line_offset: int = 0) -> list[str]:
    """Python's rule applied to JavaScript: a literal that IS a name, or holds the domain."""
    findings: list[str] = []
    for lineno, value in js_string_literals(source):
        at = f"  {rel}:{lineno + line_offset}"
        domain = _operator_domain_in(value)
        if domain:
            findings.append(f'{at}: hardcoded operator domain "{domain}" in a string literal')
        elif _identity_literal(value):
            findings.append(
                f'{at}: hardcoded fleet identity "{_identity_literal(value)}" in a string literal'
            )
    return findings


def _prose_findings(rel: str, text: str, line_offset: int = 0) -> list[str]:
    """Delivered text has no comments: any resident name or the domain is a finding."""
    findings: list[str] = []
    for k, row in enumerate(text.splitlines(), start=1):
        at = f"  {rel}:{k + line_offset}"
        domain = _operator_domain_in(row)
        if domain:
            findings.append(f'{at}: operator domain "{domain}" in served text')
        for name in dict.fromkeys(_NAME_WORD.findall(row)):
            findings.append(f'{at}: fleet identity "{name}" in served text')
    return findings


def scan_served_file(path: Path) -> list[str]:
    """Scan one served, non-Python file by the rule for how it is delivered."""
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".js":
        return _code_findings(rel, text)
    if path.suffix != ".html":
        return _prose_findings(rel, text)
    findings: list[str] = []
    markup, last = [], 0
    for m in _SCRIPT_BLOCK.finditer(text):
        body_start = m.start(2)
        findings += _code_findings(rel, m.group(2), text.count("\n", 0, body_start))
        # Keep the line count of the script body so markup line numbers stay true.
        markup.append(text[last:body_start] + "\n" * m.group(2).count("\n"))
        last = m.end(2)
    markup.append(text[last:])
    return findings + _prose_findings(rel, "".join(markup))


def served_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every non-Python file the server serves verbatim, in a stable order."""
    files: set[Path] = set()
    root = repo_root / SERVED_DASHBOARD_ROOT
    if root.exists():
        files.update(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix in SERVED_DASHBOARD_SUFFIXES
        )
    for rel in SERVED_DASHBOARD_FILES + SERVED_PROSE_FILES:
        if (repo_root / rel).is_file():
            files.add(repo_root / rel)
    files.update(repo_root.glob(SERVED_SKILLS_GLOB))
    return sorted(files)


def triage_served(rel: str, hits: list[str]) -> tuple[list[str], list[str]]:
    """Split one served file's hits into (failing, known-but-deferred).

    A listed file defers only the hits whose matched value fits inside its
    recorded multiset of approved occurrences (fewer of an approved name is a
    partial fix and still defers; an unlisted name, or more of a listed one
    than recorded, is a NEW leak and every name hit in the file fails). The
    guard cannot tell WHICH hit is new once the budget for its name is
    exceeded, so pointing at all of them is better than passing the one that
    is.
    """
    domain = [h for h in hits if "operator domain" in h]
    names = [h for h in hits if "operator domain" not in h]
    approved = SERVED_KNOWN_COUPLINGS.get(rel, ((), ""))[0]
    if names:
        budget = Counter(approved)
        observed = Counter(_hit_value(h) for h in names)
        if all(observed[value] <= budget.get(value, 0) for value in observed):
            return domain, names
    return domain + names, []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="*", default=list(DEFAULT_PATHS))
    parser.add_argument(
        "--no-served", action="store_true",
        help="skip the served dashboard/skills/tool-description scan",
    )
    args = parser.parse_args()

    findings: list[str] = []
    known: list[str] = []
    scanned = 0
    for rel_root in args.paths:
        root = REPO_ROOT / rel_root
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            scanned += 1
            hits = scan_file(path)
            if not hits:
                continue
            failing, deferred = triage(rel, hits)
            findings.extend(failing)
            known.extend(deferred)

    served_known: list[str] = []
    if not args.no_served:
        for path in served_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            scanned += 1
            hits = scan_served_file(path)
            if not hits:
                continue
            failing, deferred = triage_served(rel, hits)
            findings.extend(failing)
            served_known.extend(deferred)

    # Always printed, pass or fail: this repo has known coupling and the guard
    # must not imply otherwise.
    if known:
        print(f"⚠️  Fleet-identity guard: {len(known)} KNOWN coupling(s), not yet fixed")
        for line in known:
            rel = line.strip().split(":")[0]
            print(f"{line}\n      reason deferred: {KNOWN_COUPLINGS.get(rel, '')}")
        print()
    if served_known:
        by_file: dict[str, int] = {}
        for line in served_known:
            rel = line.strip().split(":")[0]
            by_file[rel] = by_file.get(rel, 0) + 1
        print(
            f"⚠️  Fleet-identity guard: {len(served_known)} KNOWN reference(s) in "
            f"{len(by_file)} served file(s), not yet fixed"
        )
        for rel, count in sorted(by_file.items()):
            approved, reason = SERVED_KNOWN_COUPLINGS[rel]
            print(f"  {rel}: {count} of {len(approved)}\n      reason deferred: {reason}")
        print()

    if not findings:
        print(
            f"✅ Fleet-identity guard: {scanned} file(s) scanned, no NEW "
            f"hardcoded identities"
        )
        return 0

    print(f"❌ Fleet-identity guard: {len(findings)} hardcoded identity reference(s)\n")
    for line in findings:
        print(line)
    print(
        "\nShipped source must read the roster from UNITARES_RESIDENTS "
        "(src/grounding/class_indicator.py), never name a resident, and must "
        "read hostnames from configuration, never name the operator's domain.\n"
        "Provenance in a COMMENT is fine and is not flagged — only string "
        "literals in executable code are. Tool-schema text (a Field's "
        "description or brief, a schema model's docstring) is served to every "
        "agent, so a resident name anywhere in it is flagged: describe the "
        "behavior without naming one.\n"
        "See docs/operations/resident-roster.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
