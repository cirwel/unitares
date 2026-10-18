"""Doc claims about an env flag's default match the flag catalog's reader fallback.

`scripts/dev/flag_catalog.py` already extracts, by AST, the fallback each
Python reader passes for every `UNITARES_*` / `GOVERNANCE_*` flag. A living doc
that says "`FLAG` (default off)" or "default `FLAG=1`" restates that value, and
nothing tied the two together, so a flipped default left the prose behind
("`UNITARES_PHI_TELEMETRY_ONLY` (default off)" while the reader falls back to
`'1'`). This test reuses the catalog's `collect()` (import-light and read-only)
and checks only narrow, structured claims: a backticked flag followed closely
by `default ...` or `on/off by default`, and a backticked `FLAG=value` marked
as the default. Flags the catalog does not see (shell, plugin, Elixir) and
flags whose readers disagree are skipped. An accessor that inverts the raw
value needs an `_ACCESSOR_DEFAULTS` entry, which fails once it stops being
needed.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG = REPO_ROOT / "scripts" / "dev" / "flag_catalog.py"
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"

# Point-in-time records: true when written, not restatements of current code.
_HISTORICAL_PREFIXES = ("docs/proposals/", "docs/releases/", "papers/", "docs/evaluations/")
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}(?:-\d{2})?(?:[-.]|$)")
# The generated catalog itself is the source, not a claim about it.
_GENERATED = {"docs/FLAGS.md"}

_ON = {"1", "true", "on", "yes", "enabled"}
_OFF = {"0", "false", "off", "no", "", "disabled"}


@dataclass(frozen=True)
class AccessorDefault:
    """The effective default when the accessor transforms the raw fallback."""

    polarity: str
    path: str
    evidence: str  # regex that must still match the accessor source
    reason: str


# Readers whose accessor does not read the raw fallback literally. Each entry
# must still disagree with the catalog's raw polarity and its evidence must
# still be in the source, or `test_accessor_defaults_still_needed` fails.
_ACCESSOR_DEFAULTS = {
    "STRICT_IDENTITY_REQUIRED": AccessorDefault(
        "on",
        "src/mcp_handlers/identity_bootstrap.py",
        r'os\.getenv\("STRICT_IDENTITY_REQUIRED", ""\)[^\n]*\n\s*return raw not in _FALSY',
        "unset or empty keeps strict identity on; only a falsy word turns it off",
    ),
}

# Stale claims already corrected in an open PR that is not on this branch.
# Each entry must still suppress a real failure; once the fix lands the entry
# is unused and the test fails, asking for its removal.
_PENDING_FIXES: dict[tuple[str, str], str] = {}


# --- claim extraction --------------------------------------------------------


def _flag_names(prefixes: tuple[str, ...], extra: frozenset[str]) -> str:
    """Alternation of the names the catalog indexes (its prefixes and extras)."""
    return "|".join([f"{re.escape(p)}[A-Z0-9_]+" for p in prefixes] + [re.escape(e) for e in sorted(extra)])


# Up to 40 characters inside one paragraph and one table cell, crossing no
# other code span (so the claim cannot belong to a different backticked name).
_GAP = r"(?P<gap>(?:[^`|\n]|\n(?!\s*\n)){0,40}?)"
_VALUE = r"(?:`(?P<bt>(?!UNITARES_|GOVERNANCE_)[\w.:+-]{0,40})`|(?P<word>on|off|true|false|enabled|disabled)\b)"
_NEGATED_OR_PAST = re.compile(
    r"(?:\b(?:not|never|no\s+longer|was|were|previously|formerly|used\s+to)\b|n't)[^.;]*$", re.I
)
_QUALIFIED_AFTER = re.compile(r"[*_]*\s+(?:until|before|prior\s+to)\b", re.I)


def _patterns(names: str) -> list[tuple[str, re.Pattern]]:
    flag_token = rf"`(?P<flag>{names})(?:=(?P<set>[^`\s]*))?`"
    flag_setting = rf"`(?P<flag>{names})=(?P<set>[^`\s]*)`"
    return [
        # `FLAG` (default off) / `FLAG` defaults to `0` / `FLAG` ... default-ON
        ("after", re.compile(
            flag_token + _GAP + r"\bdefault(?:s|ed)?(?:\s+(?:to|is|=))?[\s:*_-]*" + _VALUE, re.I)),
        # `FLAG` is off by default / `FLAG` is off (the default)
        ("by-default", re.compile(
            flag_token + _GAP + r"\b(?P<word>on|off|enabled|disabled)[*_]*\s+"
            r"(?:by\s+default\b|\((?:the\s+)?default\))", re.I)),
        # `FLAG=1` (the default) / `FLAG=auto`, the default, / `FLAG=1` is the default
        ("set-is-default", re.compile(
            flag_setting + r"(?:\s*\((?:the\s+)?default\)|,\s+the\s+default(?=[,;.)])|\s+is\s+the\s+default\b)", re.I)),
        # (default `FLAG=1`) / under the default `FLAG=strict`
        ("default-set", re.compile(
            r"\bdefault(?:s|ed)?(?:\s+(?:to|is|of))?[\s:*_-]*"
            + flag_setting, re.I)),
    ]


@dataclass(frozen=True)
class Claim:
    line: int
    flag: str
    value: str
    phrase: str


def default_claims(text: str, names: str) -> list[Claim]:
    """Every structured default claim in `text`, one per flag mention."""
    claims: dict[tuple[int, str], Claim] = {}
    for kind, pattern in _patterns(names):
        for match in pattern.finditer(text):
            groups = match.groupdict()
            if kind in ("after", "by-default"):
                if _NEGATED_OR_PAST.search(groups["gap"] or ""):
                    continue
                if _QUALIFIED_AFTER.match(text, match.end()):
                    continue
                value = groups["word"] if groups.get("word") is not None else groups["bt"]
            else:
                value = groups["set"]
            line = text.count("\n", 0, match.start("flag")) + 1
            phrase = " ".join(match.group(0).split())
            claims.setdefault((match.start("flag"), match.group("flag")), Claim(line, match.group("flag"), value, phrase))
    return list(claims.values())


# --- source of truth ---------------------------------------------------------


def _polarity(value: str) -> str | None:
    folded = value.strip().lower()
    return "on" if folded in _ON else "off" if folded in _OFF else None


def _literal_fallback(raw: str) -> str | None:
    """A catalog fallback cell as the string an unset env yields, else None."""
    raw = re.sub(r"\s+\(via \w+\)$", "", raw)
    if raw.startswith("None (no reader fallback)"):
        return ""
    if raw in ("True", "False"):
        return raw.lower()
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def judge(claim: Claim, fallbacks: dict[str, list[str]] | None, rel: str) -> str | None:
    """A failure message when the claim contradicts the reader fallback(s)."""
    if not fallbacks:
        return None
    sources = "; ".join(f"{raw} at {', '.join(sites)}" for raw, sites in fallbacks.items())
    override = _ACCESSOR_DEFAULTS.get(claim.flag)
    claimed = _polarity(claim.value)
    if override is not None:
        expected, source = override.polarity, f"{override.path} accessor ({override.reason}; reader fallback {sources})"
    else:
        literals = {_literal_fallback(raw) for raw in fallbacks}
        if None in literals:
            return None
        source = f"scripts/dev/flag_catalog.py collect(): reader fallback {sources}"
        if claimed is not None:
            polarities = {_polarity(v) for v in literals}
            if len(polarities) != 1 or None in polarities:
                return None
            expected = polarities.pop()
        else:
            if len(literals) != 1 or _polarity(next(iter(literals))) is not None:
                return None
            expected = next(iter(literals))
            if claim.value.lower() == expected.lower():
                return None
            return f"{rel}:{claim.line}: claims `{claim.flag}` defaults to {claim.value!r} ({claim.phrase!r}), but {source} gives {expected!r}"
    if claimed is None or claimed == expected:
        return None
    return f"{rel}:{claim.line}: claims `{claim.flag}` defaults {claimed} ({claim.phrase!r}), but {source} gives {expected}"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def catalog_module():
    return _load("_flag_catalog_doc_contract", _CATALOG)


@pytest.fixture(scope="module")
def catalog(catalog_module) -> dict[str, dict[str, list[str]]]:
    return {name: dict(flag.fallback_sites) for name, flag in catalog_module.collect().items()}


@pytest.fixture(scope="module")
def flag_names(catalog_module) -> str:
    return _flag_names(catalog_module.PREFIXES, catalog_module.EXTRA_FLAGS)


@pytest.fixture(scope="module")
def living_docs() -> list[tuple[str, str]]:
    module = _load("_check_doc_health_flag_defaults", _DOC_HEALTH)
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    in_git = set(filter(None, listed.split("\0")))
    docs = []
    for path in module.collect_md_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (
            rel not in in_git
            or rel in _GENERATED
            or rel.startswith(_HISTORICAL_PREFIXES)
            or _DATED_SNAPSHOT.search(Path(rel).stem + ".")
        ):
            continue
        docs.append((rel, path.read_text(errors="replace")))
    return docs


# --- tests -------------------------------------------------------------------


def test_sources_of_truth_parse(catalog):
    assert len(catalog) > 100, "flag catalog collected almost nothing; its walk moved"
    assert catalog["UNITARES_PHI_TELEMETRY_ONLY"] and catalog["STRICT_IDENTITY_REQUIRED"]


def test_accessor_defaults_still_needed(catalog):
    for flag, override in _ACCESSOR_DEFAULTS.items():
        assert flag in catalog, f"_ACCESSOR_DEFAULTS names {flag}, which the catalog no longer reads"
        source = (REPO_ROOT / override.path).read_text()
        assert re.search(override.evidence, source), (
            f"_ACCESSOR_DEFAULTS[{flag!r}]: {override.path} no longer matches {override.evidence!r}; "
            "re-derive the accessor's default"
        )
        raw = {_polarity(v) for v in map(_literal_fallback, catalog[flag]) if v is not None}
        assert raw != {override.polarity}, (
            f"_ACCESSOR_DEFAULTS[{flag!r}] is unnecessary: the reader fallback already reads {override.polarity}"
        )


def test_doc_flag_default_claims(living_docs, catalog, flag_names):
    rels = {rel for rel, _ in living_docs}
    assert "docs/UNIFIED_ARCHITECTURE.md" in rels, "living-doc set is empty or wrong"
    failures, checked, pending_used = [], 0, set()
    for rel, text in living_docs:
        for claim in default_claims(text, flag_names):
            if claim.flag not in catalog:
                continue
            checked += 1
            failure = judge(claim, catalog[claim.flag], rel)
            if failure is None:
                continue
            if (rel, claim.flag) in _PENDING_FIXES:
                pending_used.add((rel, claim.flag))
                continue
            failures.append(failure)
    assert checked >= 5, f"only {checked} flag default claims parsed; the patterns went blind"
    unused = set(_PENDING_FIXES) - pending_used
    assert not unused, f"_PENDING_FIXES entries no longer suppress a stale claim, remove them: {sorted(unused)}"
    assert not failures, "Docs state a flag default the reader fallback contradicts:\n" + "\n".join(failures)


# --- regressions -------------------------------------------------------------

_PHI = {"UNITARES_PHI_TELEMETRY_ONLY": {"'1'": ["config/governance_config.py:1547"]}}
_STRICT = {"STRICT_IDENTITY_REQUIRED": {"''": ["src/mcp_handlers/identity_bootstrap.py:28"]}}
_NAMES = _flag_names(("UNITARES_", "GOVERNANCE_"), frozenset({"STRICT_IDENTITY_REQUIRED"}))


def _failures(snippet: str, fallbacks: dict[str, dict[str, list[str]]]) -> list[str]:
    claims = default_claims(snippet, _NAMES)
    return [f for c in claims if (f := judge(c, fallbacks.get(c.flag), "docs/x.md"))]


@pytest.mark.parametrize(
    "snippet, fallbacks, claim, truth",
    [
        # The pre-fix dormant-capability-registry row.
        (
            "| **Φ → telemetry-only** — behavioral verdict authoritative; Φ stops flooring risk "
            "| `UNITARES_PHI_TELEMETRY_ONLY` (default off) | DECIDE | A values call |\n",
            _PHI, "defaults off", "'1' at config/governance_config.py:1547",
        ),
        ("Post-warmup (default `UNITARES_PHI_TELEMETRY_ONLY=0`) the verdict\n", _PHI, "defaults off", "gives on"),
        ("`UNITARES_PHI_TELEMETRY_ONLY` is off by default.\n", _PHI, "defaults off", "gives on"),
        ("`STRICT_IDENTITY_REQUIRED` defaults to `0`.\n", _STRICT, "defaults off", "identity_bootstrap.py accessor"),
        (
            "`UNITARES_KNOWLEDGE_BACKEND=age`, the default, resolves",
            {"UNITARES_KNOWLEDGE_BACKEND": {"'auto'": ["src/knowledge_graph.py:361"]}},
            "defaults to 'age'", "gives 'auto'",
        ),
    ],
)
def test_flags_stale_default_claim(snippet, fallbacks, claim, truth):
    failures = _failures(snippet, fallbacks)
    assert len(failures) == 1, failures
    assert failures[0].startswith("docs/x.md:1: ") and claim in failures[0] and truth in failures[0]


@pytest.mark.parametrize(
    "snippet, fallbacks",
    [
        # The corrected registry row.
        ("| `UNITARES_PHI_TELEMETRY_ONLY` (default on since `#1133`) | **LIVE (default-on)** |\n", _PHI),
        ("Post-warmup (default `UNITARES_PHI_TELEMETRY_ONLY=1`) the verdict\n", _PHI),
        ("`UNITARES_PHI_TELEMETRY_ONLY=1` (the\ndefault), so behavioral\n", _PHI),
        ("`STRICT_IDENTITY_REQUIRED` now defaults on, and\n", _STRICT),
        # Negated, historical and qualified claims are not current-default claims.
        ("`UNITARES_PHI_TELEMETRY_ONLY` no longer defaults off.\n", _PHI),
        ("`UNITARES_PHI_TELEMETRY_ONLY` was default off until #1133.\n", _PHI),
        ("`UNITARES_PHI_TELEMETRY_ONLY` (default off until #1133)\n", _PHI),
        # A default stated for a different flag in between.
        ("`UNITARES_PHI_TELEMETRY_ONLY` and `UNITARES_OTHER` (default off)\n", _PHI),
        # Across a table cell or a paragraph break.
        ("| `UNITARES_PHI_TELEMETRY_ONLY` | default off |\n", _PHI),
        ("`UNITARES_PHI_TELEMETRY_ONLY`\n\nDefault off for the rest.\n", _PHI),
        # A flag the catalog does not read, or whose readers disagree.
        ("`UNITARES_WATCHER_ENABLED` defaults to `1`.\n", {}),
        ("`UNITARES_X` (default off)\n", {"UNITARES_X": {"'1'": ["a.py:1"], "'0'": ["b.py:2"]}}),
        # A non-boolean value against an empty raw fallback: the accessor supplies it.
        ("`UNITARES_MODEL_ID` (default `gemma4:latest`)\n", {"UNITARES_MODEL_ID": {"''": ["a.py:1"]}}),
    ],
)
def test_allows_current_or_out_of_scope_claims(snippet, fallbacks):
    assert _failures(snippet, fallbacks) == []
