"""Modules must import on their own, without a particular import order first.

`src.services.runtime_queries` used to be un-importable as the first import in a
process. It imports `src.mcp_handlers.shared` at module scope, which executes
`src/mcp_handlers/__init__.py`, which pulls the whole handler tree, which
reached `observability/outcome_events.py` — and that imported
`_build_eisv_semantics` back out of the still-executing `runtime_queries`.

The failure was invisible in normal operation, because the server imports
`src.mcp_handlers` long before anything touches `runtime_queries`. It only bit
code that reached for `runtime_queries` first, and the test suite worked around
it with explicit "import-order anchor" lines rather than fixing the cycle.

Measured 2026-08-13 by importing all 350 `src` modules in cold interpreters:
this was the *only* circular-import failure in the tree, so the guard is narrow
by evidence rather than by taste. A subprocess is the point — importing inside
the pytest process proves nothing, since by then the handler chain is loaded and
the cycle cannot reproduce.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Modules that a cycle would strand, and that nothing guarantees is imported
# late. Add to this list when a module is found to need an import-order anchor —
# the anchor is the bug, not the fix.
FIRST_IMPORT_SAFE = [
    "src.services.runtime_queries",
    "src.mcp_handlers.observability.outcome_events",
]


@pytest.mark.parametrize("module", FIRST_IMPORT_SAFE)
def test_module_imports_as_the_first_import(module):
    proc = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '.'); import {module}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        hint = ""
        if "partially initialized" in stderr or "circular import" in stderr:
            hint = (
                "\n\nThis is a circular import. Break it by deferring the "
                "offending import into the function that uses it — do not add an "
                "import-order anchor to the caller, which hides the cycle instead "
                "of removing it."
            )
        pytest.fail(f"`import {module}` failed as a first import:\n{stderr}{hint}")
