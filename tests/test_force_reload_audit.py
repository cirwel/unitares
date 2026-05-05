"""Wave 2 source-level regression: pin the force=True audit.

PR #350 dropped force=True from 6 observe sub-handlers; Wave 2 audited
the remaining 19 sites and dropped force=True from 18 of them (per the
PR #350 read-only-fleet precedent). One site is intentionally kept with
explicit-comment justification: admin/handlers.py's TTL-gated cache
refresh, where load_metadata_async returns early without force=True
(the function checks `_metadata_loaded and not force` and short-circuits),
so force=True is structurally required for the admin handler to ever
refresh against external writes.

This test scans the codebase and asserts the only remaining force=True
call site is the admin one. New force=True calls require either:

  1. Updating ALLOWED_FORCE_TRUE_SITES below (with operator review of
     the post-write-consistency or TTL-refresh case), OR
  2. Choosing one of the three PR #350 treatments instead:
     - drop force=True (read-only-fleet pattern; cache is fresh enough)
     - replace with single-agent fetch (load_monitor_state in executor)
     - keep with explicit-comment justification (this allowlist)

The pin is structural rather than per-handler because per-site mocking
across 18 handlers would be heavier than the value it adds. This test
catches regressions globally with one assertion.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

# Sites where force=True is intentionally retained, with the rationale.
# Each entry: (relative path, line number, rationale-tag).
# Update this list ONLY with operator review.
ALLOWED_FORCE_TRUE_SITES: list[tuple[str, str]] = [
    # admin TTL-gated cache refresh: load_metadata_async returns early
    # without force=True (function checks _metadata_loaded), so force is
    # structurally required for the admin handler to ever refresh against
    # external writes. Cost is bounded by the EXPLORATION_CACHE_TTL gate.
    ("src/mcp_handlers/admin/handlers.py", "TTL-gated cache refresh"),
]


def _grep_force_true_sites() -> list[tuple[str, int, str]]:
    """Return [(file, line, line_text)] for every load_metadata_async(force=True)
    in src/ and agents/ — code only, comments excluded."""
    cmd = [
        "grep",
        "-rn",
        "load_metadata_async(force=True)",
        str(PROJECT_ROOT / "src"),
        str(PROJECT_ROOT / "agents"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        # 0 = matches found, 1 = no matches found, anything else = error
        raise RuntimeError(f"grep failed: {result.stderr}")

    sites: list[tuple[str, int, str]] = []
    line_pattern = re.compile(r"^([^:]+):(\d+):(.*)$")
    for raw in result.stdout.splitlines():
        m = line_pattern.match(raw)
        if not m:
            continue
        path, lineno, content = m.group(1), int(m.group(2)), m.group(3)
        # Skip comment-only references (the call must appear in a code
        # context, not in a docstring or # comment that mentions the call).
        # Heuristic: lines whose stripped form starts with # or "..." are
        # comments / docstrings.
        stripped = content.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith('"') and not stripped.startswith('"""'):
            continue
        # The actual code form is `await ... load_metadata_async(force=True)`
        if "await" not in content:
            continue
        rel = str(Path(path).relative_to(PROJECT_ROOT))
        sites.append((rel, lineno, content))
    return sites


def test_only_allowlisted_force_true_sites_remain():
    """Wave 2 source-level regression: every force=True call site MUST be in
    ALLOWED_FORCE_TRUE_SITES. New force=True calls are not introduced
    silently; they are surfaced as explicit allowlist additions."""
    sites = _grep_force_true_sites()

    allowed_paths = {entry[0] for entry in ALLOWED_FORCE_TRUE_SITES}
    unauthorized = [
        f"{path}:{lineno}: {content.strip()}"
        for path, lineno, content in sites
        if path not in allowed_paths
    ]

    assert not unauthorized, (
        "Unauthorized load_metadata_async(force=True) call site(s) found.\n\n"
        "Each force=True site triggers a 3221-await per-agent cache.set "
        "loop on every call (~16s blocking). The PR #350 audit dropped "
        "force=True from 6 observe handlers; Wave 2 dropped it from 18 more. "
        "If you are adding a new force=True call:\n\n"
        "  1. Verify the use case is genuinely TTL-gated cache refresh OR "
        "post-write-consistency where the in-memory dict isn't kept current "
        "by the regular write paths.\n"
        "  2. If yes, add (path, rationale-tag) to ALLOWED_FORCE_TRUE_SITES "
        "in tests/test_force_reload_audit.py with operator review.\n"
        "  3. If no, choose one of the three PR #350 treatments instead.\n\n"
        f"Unauthorized site(s):\n  " + "\n  ".join(unauthorized)
    )


def test_at_least_one_dropped_site_per_module():
    """Sanity check: confirm Wave 2 actually dropped force=True from each
    target module (not just from the easy ones). If this test passes but
    the unauthorized-sites test also passes, we know force=True is gone
    from each module's call sites."""
    expected_dropped_modules = [
        "src/mcp_handlers/dialectic/handlers.py",
        "src/mcp_handlers/dialectic/resolution.py",
        "src/mcp_handlers/dialectic/auto_resolve.py",
        "src/mcp_handlers/lifecycle/operations.py",
        "src/mcp_handlers/lifecycle/mutation.py",
        "src/mcp_handlers/lifecycle/resume.py",
        "src/mcp_handlers/identity/handlers.py",
        "src/mcp_handlers/support/condition_parser.py",
        "src/agent_loop_detection.py",
    ]

    sites = _grep_force_true_sites()
    site_paths = {path for path, _, _ in sites}

    still_present = [m for m in expected_dropped_modules if m in site_paths]

    assert not still_present, (
        "Wave 2 was supposed to drop force=True from these modules, but "
        "force=True is still present in code:\n  " + "\n  ".join(still_present)
    )


def test_admin_force_true_has_audit_comment():
    """The one allowlisted site (admin/handlers.py) must have the audit
    comment justifying its retention. Without the comment, future readers
    will think the audit missed it."""
    admin_path = PROJECT_ROOT / "src" / "mcp_handlers" / "admin" / "handlers.py"
    text = admin_path.read_text()
    # The audit comment MUST appear above the force=True call. Use a
    # tolerant pattern so cosmetic edits to the comment don't break the test.
    assert "Wave 2 audit: force=True KEPT" in text, (
        "Expected 'Wave 2 audit: force=True KEPT' comment in "
        f"{admin_path.relative_to(PROJECT_ROOT)} above the allowlisted "
        "force=True call. If the call was removed, also remove the entry "
        "from ALLOWED_FORCE_TRUE_SITES in this test."
    )


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
