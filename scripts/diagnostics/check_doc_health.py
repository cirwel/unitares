#!/usr/bin/env python3
"""Check documentation health: dead refs, ghost tools, hardcoded IPs/counts.

Can also print an advisory **demotion-candidate** list: planning/RFC docs whose
own status says the work shipped but which still live in an active location
(proposals/ outside resolved/, or the ontology/ identity tree). That list is
triage, not a defect, so it is opt-in via ``--demotion-candidates`` and never
affects the exit code; clear it by moving fully-shipped docs to
proposals/resolved/ (or stubbing them out) on a real signal, rather than letting
a subsystem's doc set sprawl.

Usage:
    python3 scripts/diagnostics/check_doc_health.py [--strict] [--demotion-candidates]

Exit codes:
    0 — no warnings (or warnings only in non-strict mode)
    1 — warnings found (strict mode only) — demotion candidates excluded
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


# --- Check 1b: Broken relative .md links ---
#
# check_dead_refs only matches paths that START with a known repo root
# ({src,config,scripts,docs,db,dashboard}/...). Bare or relative markdown
# links to sibling docs — `[x](OPERATOR_RUNBOOK.md)`, `[x](../FOO.md)`,
# `[x](./bar.md)` — never matched those patterns, so a wrong relative path
# (e.g. a doc moved between `docs/` and `docs/operations/` keeping a stale
# link) slipped through silently. This check resolves such links against the
# linking file's own directory. Same skip surface as the dead-ref check:
# proposals/specs/handoffs/plans and plan.md reference paths that don't (yet)
# exist by design, so they stay exempt.

_REL_MD_LINK = re.compile(r'\]\((\.{0,2}/?[^)\s#]+\.md)(?:#[^)]*)?\)')
_REPO_ROOT_PREFIXES = ("src/", "config/", "scripts/", "docs/", "db/", "dashboard/")


def check_relative_links(md_files: list[Path]) -> list[str]:
    warnings = []
    for fpath in md_files:
        rel = fpath.relative_to(REPO_ROOT)
        if rel.as_posix() in _DEAD_REF_SKIP_FILES:
            continue
        if any(d in rel.parts for d in _DEAD_REF_SKIP_DIRS):
            continue
        if rel.name.endswith(_REVIEW_SUFFIXES):
            continue
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for match in _REL_MD_LINK.finditer(line):
                link = match.group(1)
                # Repo-root-prefixed links are handled by check_dead_refs.
                if link.startswith(_REPO_ROOT_PREFIXES):
                    continue
                # Operator-local handoffs are gitignored; cited by name as
                # provenance (same exemption as the dead-ref check).
                if "handoffs/" in link:
                    continue
                if any(c in link for c in "*<>{}"):
                    continue
                target = (fpath.parent / link).resolve()
                if not target.exists():
                    warnings.append(f"  {rel}:{i}: broken relative link `{link}`")
    return warnings


# --- Check 1c: Index orphans ---
#
# A doc under docs/ that no other .md and no code file references by name is
# effectively unreachable — the curated README indexes drift behind new files
# and the doc goes undiscovered (the 2026-06 audit found ~9 such orphans).
# Dated point-in-time records (`*-YYYY-MM-DD.md`) are exempt: they are
# deliberately preserved in place and may legitimately be unlinked. README
# files are index roots, not index targets.

_DATED_RECORD = re.compile(r'-\d{4}-\d{2}-\d{2}\.md$')
_ORPHAN_SKIP_NAMES = {"README.md"}
_REF_SCAN_EXTS = {".md", ".py", ".ex", ".exs", ".sh", ".js", ".ts", ".toml", ".txt"}
_MD_BASENAME = re.compile(r'([\w.\-/]+\.md)')


def _collect_md_basename_refs() -> dict[str, set[str]]:
    """Map every referenced `*.md` basename → the set of files that mention it.

    Scans docs and code alike so a doc cited only from `src/` (e.g.
    `lineage-causal-only-semantics.md` from a lifecycle handler) is not a
    false orphan.
    """
    refs: dict[str, set[str]] = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if Path(fn).suffix not in _REF_SCAN_EXTS:
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            rel = fpath.relative_to(REPO_ROOT).as_posix()
            for m in _MD_BASENAME.finditer(text):
                refs.setdefault(os.path.basename(m.group(1)), set()).add(rel)
    return refs


def check_index_orphans(md_files: list[Path]) -> list[str]:
    refs = _collect_md_basename_refs()
    warnings = []
    for fpath in md_files:
        rel = fpath.relative_to(REPO_ROOT)
        if not rel.parts or rel.parts[0] != "docs":
            continue
        if rel.name in _ORPHAN_SKIP_NAMES or "handoffs" in rel.parts:
            continue
        if _DATED_RECORD.search(rel.name):
            continue
        # "Referenced" = mentioned by some file other than itself.
        mentioning = refs.get(rel.name, set()) - {rel.as_posix()}
        if not mentioning:
            warnings.append(
                f"  {rel}: orphan — not linked from any index/doc or referenced in code"
            )
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
    "load", "retheme", "applyEvent", "notifyNew", "refreshActive",  # dashboard hooks
}

# Files/dirs where ghost tool warnings are noise (plans, specs, internal
# analysis docs that reference implementation details by name as part of
# the analysis — the names are correct internal-function references, not
# MCP-tool claims).
_GHOST_SKIP_DIRS = {"plans", "superpowers", "specs", "handoffs",
                    "ontology", "proposals"}

# Individual files (not whole dirs) where ghost-tool warnings are noise.
# The dormant-capability-registry is, by definition, a catalogue of internal
# functions referenced by name — the same "implementation details named as
# part of analysis" rationale as _GHOST_SKIP_DIRS, but it lives under
# operations/ where most docs ARE runbooks that legitimately name real MCP
# tools (so a ghost there is a real bug). Skip this one file by name rather
# than blinding the check across all of operations/.
_GHOST_SKIP_FILES = {"docs/operations/dormant-capability-registry.md"}


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
        if rel.as_posix() in _GHOST_SKIP_FILES:
            continue
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


# --- Check 5: Demotion candidates (shipped-status docs in an active location) ---
#
# A planning/RFC doc whose own status says the work shipped, but which still
# lives in an "active" location (docs/proposals/ outside resolved/, or
# docs/ontology/ which is the identity tree), is a demotion candidate: its
# forward-looking body keeps reading as a plan after the thing landed, and the
# subsystem's doc set sprawls. This does NOT auto-demote — it surfaces a triage
# list the operator clears on a real signal (the 2026-06 BEAM/lease thread
# reached ~21 docs this way). Advisory only: never affects the exit code,
# because "should this move to resolved/" is a judgment call, not a defect.
#
# Heuristic, deliberately conservative — requires an explicit shipped marker in
# the doc's own header region (first lines / frontmatter), so a doc that merely
# mentions "deployed" deep in its body does not trip it. A doc that is shipped
# AND still names remaining work is flagged as "partially shipped" (split the
# done part out) rather than "fully shipped" (move it wholesale).

# Anchor docs of docs/ontology/ — living canonical/ledger docs, not plans.
_DEMOTION_SKIP_ONTOLOGY = {
    "README.md", "identity.md", "plan.md", "paper-positioning.md", "glossary.md",
}

# A doc that already labels itself historical/archived/superseded is honest
# about its status — it is not a stale plan masquerading as current, so it is
# not a demotion candidate even if it also says "shipped". (e.g. s10-fleet-
# aggregation-plan.md: "archived implementation plan ... retained as design
# provenance" — flagging it would be noise.)
_ALREADY_HISTORICAL = re.compile(
    r'\b(archived|superseded|withdrawn|retained as|design provenance|'
    r'historical record|historical provenance|for provenance|deprecated)\b',
    re.IGNORECASE,
)

_SHIPPED_MARKERS = re.compile(
    r'\b(shipped|deployed|landed|merged|in production|live in prod|'
    r'enforcement shipped|complete[d]?|resolved|done)\b',
    re.IGNORECASE,
)
# Signals the doc still tracks open work — distinguishes "fully shipped" (move
# it) from "partially shipped" (split out the done part, keep the open part).
_ACTIVE_REMAINING = re.compile(
    r'\b(not started|remaining|in progress|in-flight|wip|todo|pending|'
    r'proposed|design-gated|draft|next step|open question|phase [b-z] remains)\b',
    re.IGNORECASE,
)


# A doc cited from this many places is a load-bearing reference (e.g. a
# canonical contract spec like surface-lease-plane-v0.md, 13 inbound) — moving
# it would break references, so it is not a demotion candidate even when its
# status says "shipped." Tuned to keep genuine candidates (beam-coordination-
# kernel.md, 7 inbound) while exempting the few living references above it.
_DEMOTION_LIVING_REF_THRESHOLD = 10


def check_demotion_candidates(md_files: list[Path]) -> list[str]:
    refs = _collect_md_basename_refs()
    warnings = []
    for fpath in md_files:
        rel = fpath.relative_to(REPO_ROOT)
        parts = rel.parts
        if not parts or parts[0] != "docs":
            continue
        # Scope: proposals/ (but not already-demoted resolved/) and ontology/.
        in_proposals = "proposals" in parts and "resolved" not in parts
        in_ontology = "ontology" in parts
        if not (in_proposals or in_ontology):
            continue
        if rel.name == "README.md":
            continue
        # Living reference (cited widely by other docs) — not a stale plan to
        # move. Count doc-to-doc citations only; code/test mentions of a
        # filename don't make a planning doc a load-bearing reference.
        inbound = {
            m for m in refs.get(rel.name, set())
            if m.startswith("docs/") and m.endswith(".md") and m != rel.as_posix()
        }
        if len(inbound) >= _DEMOTION_LIVING_REF_THRESHOLD:
            continue
        if _DATED_RECORD.search(rel.name):
            # Dated point-in-time records are kept in place by design; a shipped
            # status on a record is correct, not a demotion signal.
            continue
        if in_ontology and rel.name in _DEMOTION_SKIP_ONTOLOGY:
            continue
        # Status/header region only: frontmatter + the first prose lines, where
        # docs put **Status:** / **Last Updated:** banners.
        lines = fpath.read_text(errors="replace").splitlines()
        header = "\n".join(lines[:15])
        if not _SHIPPED_MARKERS.search(header):
            continue
        # Already honest about being historical → not a stale-plan candidate.
        if _ALREADY_HISTORICAL.search(header):
            continue
        loc = "ontology/ (identity tree)" if in_ontology else "proposals/"
        if _ACTIVE_REMAINING.search(header):
            warnings.append(
                f"  {rel}: partially shipped in {loc} — split the shipped part "
                f"to resolved/ (or a stub) and keep only the open work forward-looking"
            )
        else:
            warnings.append(
                f"  {rel}: reads fully shipped in {loc} — consider moving to "
                f"proposals/resolved/, or stub + point at the live canonical doc"
            )
    return warnings


# --- Main ---

# --- Check 6: Contested claims (corrected facts that must not reappear) ---
#
# Every finding in the 2026-07-02 README coherence audit was the same failure
# shape: an architecture fact got corrected (e.g. PR #1235's verdict-driver
# inversion), some prose copies were updated, and the stale copies kept
# asserting the old claim. Files exist and links resolve, so the other checks
# stay green — only the *sentence* is wrong. This check pins the known
# corrected claims as deny-patterns so the stale wording becomes a warning the
# moment it reappears anywhere reader-facing.
#
# The canonical wording for each claim lives in the "Contested claims
# registry" section of docs/dev/CANONICAL_SOURCES.md — add a row there when
# adding a pattern here. Patterns are deliberately narrow (exact stale
# phrasings observed in the wild), not topic filters: prose *about* warmup or
# Redis is fine; the specific corrected assertion is not.
#
# Scope: reader-facing surfaces only. Historical/internal research prose
# (docs/proposals/) may legitimately quote superseded claims as provenance;
# CANONICAL_SOURCES.md quotes them by design.

_CONTESTED_CLAIMS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"Redis is optional", re.IGNORECASE),
        'corrected: Redis is the de-facto primary session store (boots degraded '
        'local-only without it) — see UNIFIED_ARCHITECTURE.md and the registry',
    ),
    (
        re.compile(r"falls back to self-reported signals", re.IGNORECASE),
        "corrected (PR #1235 inversion): warmup verdict is the cold-start prior, "
        "≥70% server-derived; self-report capped at ≤30%",
    ),
    (
        re.compile(r"verdict[^.\n]{0,40}self-report(?:ed)?-driven", re.IGNORECASE),
        "corrected (PR #1235 inversion): the verdict is not self-report-driven",
    ),
    (
        re.compile(
            r"(?:live (?:path|verdict)|warmup) uses fixed universal thresholds",
            re.IGNORECASE,
        ),
        "conflation: the behavioral TRACK scores against fixed thresholds during "
        "warmup, but the live VERDICT falls back to the server-derived cold-start "
        "prior — say which one you mean (canonical wording in the registry)",
    ),
]

# Reader-facing scope for the contested-claims check.
_CONTESTED_SKIP_PARTS = ("proposals",)  # internal research/provenance prose
_CONTESTED_SKIP_FILES = {"CANONICAL_SOURCES.md"}  # quotes the patterns by design


def check_contested_claims(md_files: list[Path]) -> list[str]:
    warnings = []
    for fpath in md_files:
        rel = fpath.relative_to(REPO_ROOT)
        if rel.name in _CONTESTED_SKIP_FILES:
            continue
        if any(part in _CONTESTED_SKIP_PARTS for part in rel.parts):
            continue
        for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
            for pattern, reason in _CONTESTED_CLAIMS:
                if pattern.search(line):
                    warnings.append(f"  {rel}:{i}: {reason}")
    return warnings


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
    show_demotion = "--demotion-candidates" in sys.argv
    md_files = collect_md_files()
    tool_names = _load_tool_names()

    all_warnings = []

    dead = check_dead_refs(md_files)
    if dead:
        all_warnings.append(("Dead file references", dead))

    rel_links = check_relative_links(md_files)
    if rel_links:
        all_warnings.append(("Broken relative .md links", rel_links))

    orphans = check_index_orphans(md_files)
    if orphans:
        all_warnings.append(("Index orphans (unreachable docs)", orphans))

    ghosts = check_ghost_tools(md_files, tool_names)
    if ghosts:
        all_warnings.append(("Possible ghost tools", ghosts))

    ips = check_hardcoded_ips(md_files)
    if ips:
        all_warnings.append(("Hardcoded Tailscale IPs", ips))

    counts = check_hardcoded_counts(md_files)
    if counts:
        all_warnings.append(("Hardcoded counts (will go stale)", counts))

    contested = check_contested_claims(md_files)
    if contested:
        all_warnings.append(("Contested claims (corrected facts reappearing)", contested))

    # Advisory: surfaced only when requested, never gates the exit code
    # (demotion is a judgment call, not a defect). Printed separately from the
    # warning total so pre-push stays quiet unless an operator is auditing docs.
    demotion = check_demotion_candidates(md_files) if show_demotion else []

    if not all_warnings and not demotion:
        print("📄 Doc health: all clear")
        return 0

    if all_warnings:
        total = sum(len(w) for _, w in all_warnings)
        print(f"📄 Doc health: {total} warning(s)")
        for category, items in all_warnings:
            print(f"\n  {category}:")
            for item in items:
                print(item)
        print()
    else:
        print("📄 Doc health: all clear")

    if demotion:
        print(f"📦 Demotion candidates ({len(demotion)}) — advisory, does not fail the check:")
        for item in demotion:
            print(item)
        print()

    return 1 if (strict and all_warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
