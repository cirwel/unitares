#!/usr/bin/env python3
"""Check documentation health: dead refs, ghost tools, hardcoded IPs/counts.

Usage:
    python3 scripts/diagnostics/check_doc_health.py [--strict]

Exit codes:
    0 — no warnings (or warnings only in non-strict mode)
    1 — warnings found (strict mode only)
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories to skip when scanning .md files.
# `.worktrees` lives inside the parent checkout but belongs to parallel branches;
# auditing it double-counts every other worktree's in-flight docs as "dead refs"
# against the current branch's tree.
SKIP_DIRS = {".git", ".venv", "venv", ".pytest_cache", "node_modules",
             ".claude", "archive", "__pycache__", ".hypothesis",
             ".agent-guides", "plans", "superpowers", ".worktrees",
             "deps", "_build"}

# Files to skip (historical records — dead refs are expected)
SKIP_FILES = {"docs/CHANGELOG.md", "CHANGELOG.md"}

# --- Check 1: Dead file references ---

# Patterns that look like file paths in docs
_PATH_PATTERNS = [
    # Backtick-quoted paths: `src/foo.py`, `config/bar.py`
    re.compile(r'`((?:src|config|scripts|docs|db|dashboard)/[^`\s]+)`'),
    # Markdown links: [text](path/to/file)
    re.compile(r'\]\(((?:src|config|scripts|docs|db|dashboard)/[^\)#\s]+)\)'),
]

# Individual files where dead refs are expected. Keep these narrow: most
# ontology docs are current contract docs, but plan.md is an intentionally
# preserved internal ledger after public master removed some private drafts.
_DEAD_REF_SKIP_FILES = {"docs/ontology/plan.md"}

# Directories where dead refs are expected: historical planning records, not
# current documentation. Specs/handoffs/plans describe intent at a moment in
# time; implementation often lands under different names, so refs drift by
# design. Keep these files auditable for hardcoded IPs/counts (which DO need
# to stay current) but skip the dead-ref check.
# `proposals` is added because design proposals reference paths the proposal
# would create. When a proposal lands the refs become real; when it stalls or
# changes scope they look "dead" but the doc is correctly capturing what was
# planned at proposal time. Same reasoning as `specs`/`handoffs`/`plans`.
_DEAD_REF_SKIP_DIRS = {"specs", "handoffs", "plans", "proposals"}


_REVIEW_SUFFIXES = (
    ".code-review.md",
    ".dialectic-review.md",
    ".adversary-review.md",
)


def _strip_ref_locator(ref: str) -> str:
    """Return the bare filesystem path from a doc reference.

    Doc refs commonly carry locators after the path:
        src/foo.py:73                  — line number
        src/foo.py:73-99               — ASCII range
        src/foo.py:73–99               — en-dash range (markdown editors auto-correct)
        src/foo.py:73,99               — comma-separated line list
        src/foo.py:func_name           — symbol reference
        src/foo.py::js_func(           — JS double-colon function
        src/foo.py::PROPERTY           — same, capitalized

    Filesystem paths cannot contain `:` on POSIX (Windows drive letters
    don't apply here — the path-pattern regex requires the path to start
    with one of {src,config,scripts,docs,db,dashboard}/, which excludes
    Windows-style refs). So splitting on the first `:` is sufficient.
    """
    return ref.split(":", 1)[0]


def check_dead_refs(md_files: list[Path]) -> list[str]:
    warnings = []
    seen = set()
    for fpath in md_files:
        rel = fpath.relative_to(REPO_ROOT)
        if rel.as_posix() in _DEAD_REF_SKIP_FILES:
            continue
        if any(d in rel.parts for d in _DEAD_REF_SKIP_DIRS):
            continue
        # Review artifacts (code-review, dialectic-review, adversary-review)
        # capture point-in-time references during a review pass; the code
        # they cite legitimately moves on after the review lands. Treat them
        # the same as plans/specs/handoffs — auditable for hardcoded IPs/
        # counts (which DO need to stay current) but skip the dead-ref check.
        if rel.name.endswith(_REVIEW_SUFFIXES):
            continue
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for pat in _PATH_PATTERNS:
                for match in pat.finditer(line):
                    ref = match.group(1).rstrip(".,;:)")
                    ref_path = _strip_ref_locator(ref)
                    if ref_path in seen:
                        continue
                    seen.add(ref_path)
                    # Refs INTO docs/handoffs/ are operator-local by
                    # convention: the directory is gitignored and holds
                    # private handoffs; public docs cite them by filename
                    # as provenance (see e.g. AGENTS.md strict-identity
                    # stage-1 burn-in, wave-3 §5.2 audit summary). They
                    # are expected to be unresolvable in the public tree.
                    if ref_path.startswith("docs/handoffs/"):
                        continue
                    # Skip wildcards, placeholders, and example paths
                    if any(c in ref_path for c in "*<>{}"):
                        continue
                    if "/foo" in ref_path or "YYYY" in ref_path:
                        continue
                    # Check if file or directory exists
                    candidate = REPO_ROOT / ref_path
                    if not candidate.exists():
                        warnings.append(f"  {rel}:{i}: dead ref `{ref}`")
    return warnings


# --- Check 2: Ghost tools ---

def _load_tool_names() -> set[str]:
    """Load canonical tool names from this repo's registry.

    Discovers names from three sources:
    - TOOL_ORDER in src/tool_schemas.py (canonical list)
    - Aliases in src/mcp_handlers/tool_stability.py
    - @mcp_tool("name") decorator usages anywhere under src/ (catches tools
      that are registered via decorator but not yet hand-listed in TOOL_ORDER —
      missing them caused real ghost-tool false positives, e.g.
      list_process_bindings flagged in commands/diagnose.md though it ships
      registered at src/mcp_handlers/identity/process_binding_handler.py).
    - HANDLERS dict in src/anima_mcp/tool_registry.py (for anima-mcp checkout)
    """
    names = set()

    # TOOL_ORDER in src/tool_schemas.py
    schemas = REPO_ROOT / "src" / "tool_schemas.py"
    if schemas.exists():
        for m in re.finditer(r'"(\w+)"', schemas.read_text()):
            names.add(m.group(1))

    # Aliases from tool_stability.py
    stability = REPO_ROOT / "src" / "mcp_handlers" / "tool_stability.py"
    if stability.exists():
        text = stability.read_text()
        for m in re.finditer(r'"(\w+)":\s*ToolAlias', text):
            names.add(m.group(1))

    # @mcp_tool("name", ...) decorator usages under src/
    src_dir = REPO_ROOT / "src"
    if src_dir.exists():
        decorator_pat = re.compile(r'@mcp_tool\(\s*"(\w+)"')
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                try:
                    text = (Path(root) / f).read_text(errors="replace")
                except Exception:
                    continue
                for m in decorator_pat.finditer(text):
                    names.add(m.group(1))

    # anima-mcp: HANDLERS dict in tool_registry.py
    registry = REPO_ROOT / "src" / "anima_mcp" / "tool_registry.py"
    if registry.exists():
        text = registry.read_text()
        for m in re.finditer(r'"(\w+)":\s*handle_', text):
            names.add(m.group(1))

    return names


# Common words and internal functions that appear in backticks but aren't MCP tools
_TOOL_ALLOWLIST = {
    "master", "main", "true", "false", "null", "none", "ok",
    "proceed", "guide", "pause", "reject",  # verdict names
    "open", "resolved", "archived",  # status names
    "convergent", "divergent", "mixed",  # task types
    "note", "insight", "bug_found", "improvement", "analysis", "pattern",  # discovery types
    "comfortable", "tight", "critical",  # margin levels
    "high", "low", "boundary",  # basin names
    "postgres", "redis", "age", "docker",  # infra
    "smoke", "pytest",  # test
    "export",  # consolidated tool (registered as action, not standalone)
    "get_db",  # internal DB helper, not an MCP tool
    "create_task",  # asyncio.create_task(), not an MCP tool
    "str", "float", "int", "bool", "dict", "list", "set", "tuple",  # Python casts
    "acquire", "release",  # asyncio.Lock / Semaphore methods
    "accept",  # socket.accept(), TLS accept
    "getAuthToken",  # JS dashboard helper
}

# Files/dirs where ghost tool warnings are noise (plans, specs, internal
# analysis docs that reference implementation details by name as part of
# the analysis — the names are correct internal-function references, not
# MCP-tool claims).
_GHOST_SKIP_DIRS = {"plans", "superpowers", "specs", "handoffs",
                    "ontology", "proposals"}


def check_ghost_tools(md_files: list[Path], tool_names: set[str]) -> list[str]:
    if not tool_names:
        return []  # Can't validate without a registry

    warnings = []
    # Match backtick-quoted identifiers that look like MCP tool calls: `foo()`
    # Only check function-call patterns, not arbitrary backtick-quoted words
    pat = re.compile(r'`(\w+)\(\)`')
    seen = set()

    for fpath in md_files:
        # Skip plans/specs — they reference hypothetical code
        rel = fpath.relative_to(REPO_ROOT)
        if any(d in rel.parts for d in _GHOST_SKIP_DIRS):
            continue
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for match in pat.finditer(line):
                name = match.group(1)
                if not name or name in seen:
                    continue
                if name in tool_names or name in _TOOL_ALLOWLIST:
                    continue
                if name.startswith(("pi_", "mcp_", "_")):
                    # pi_ = proxy tools, _private = internal functions
                    continue
                seen.add(name)
                warnings.append(f"  {rel}:{i}: possible ghost tool `{name}`")
    return warnings


# --- Check 3: Hardcoded Tailscale IPs ---

_IP_PATTERN = re.compile(r'100\.\d{1,3}\.\d{1,3}\.\d{1,3}')
# CIDR suffix marks a network spec (e.g. `100.64.0.0/10` is Tailscale CGNAT
# block per RFC 6598), not a specific operator's IP. The linter's job is to
# catch live operator defaults baked into shipping code/docs, not constants
# describing IP-space topology.
_CIDR_AFTER_IP = re.compile(r'100\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d+')


def check_hardcoded_ips(md_files: list[Path]) -> list[str]:
    warnings = []
    seen = set()
    for fpath in md_files:
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for match in _IP_PATTERN.finditer(line):
                ip = match.group(0)
                key = (fpath, ip)
                if key in seen:
                    continue
                seen.add(key)
                # Skip placeholder instructions
                if "tailscale status" in line.lower() or "<tailscale-ip>" in line:
                    continue
                # Skip CIDR network-spec constants
                if _CIDR_AFTER_IP.search(line):
                    continue
                # Skip historical-record rows in audit/install docs — table
                # cells marked ✅ resolved or ⏸ deferred describe prior state,
                # not live config. The fix has already been applied; the IP
                # value is preserved for traceability.
                if ("✅ resolved" in line or "⏸ deferred" in line or
                        "historical" in line.lower()):
                    continue
                rel = fpath.relative_to(REPO_ROOT)
                warnings.append(f"  {rel}:{i}: hardcoded Tailscale IP {ip}")
    return warnings


# --- Check 4: Hardcoded counts ---

_COUNT_PATTERN = re.compile(
    r'\d{1,2},\d{3}\+?\s*(?:tests|agents|discoveries|identities|awakenings|check-ins|entries|edges)',
    re.IGNORECASE,
)


def check_hardcoded_counts(md_files: list[Path]) -> list[str]:
    warnings = []
    for fpath in md_files:
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for match in _COUNT_PATTERN.finditer(line):
                rel = fpath.relative_to(REPO_ROOT)
                warnings.append(f"  {rel}:{i}: hardcoded count \"{match.group(0)}\"")
    return warnings


# --- Main ---

def collect_md_files() -> list[Path]:
    md_files = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".md") and f not in SKIP_FILES:
                md_files.append(Path(root) / f)
    return sorted(md_files)


def main():
    strict = "--strict" in sys.argv
    md_files = collect_md_files()
    tool_names = _load_tool_names()

    all_warnings = []

    dead = check_dead_refs(md_files)
    if dead:
        all_warnings.append(("Dead file references", dead))

    ghosts = check_ghost_tools(md_files, tool_names)
    if ghosts:
        all_warnings.append(("Possible ghost tools", ghosts))

    ips = check_hardcoded_ips(md_files)
    if ips:
        all_warnings.append(("Hardcoded Tailscale IPs", ips))

    counts = check_hardcoded_counts(md_files)
    if counts:
        all_warnings.append(("Hardcoded counts (will go stale)", counts))

    if not all_warnings:
        print("📄 Doc health: all clear")
        return 0

    total = sum(len(w) for _, w in all_warnings)
    print(f"📄 Doc health: {total} warning(s)")
    for category, items in all_warnings:
        print(f"\n  {category}:")
        for item in items:
            print(item)
    print()

    return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
