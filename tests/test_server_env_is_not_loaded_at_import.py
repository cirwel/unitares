"""Importing the server must not inject the operator's secrets into the process.

src/mcp_server.py used to call load_dotenv(~/.env.mcp) at module scope, so
`import src.mcp_server` — for any reason, including a test wanting one symbol —
put a live GitHub token and the PRODUCTION governance DSN into os.environ for
the rest of the process. Measured 2026-09-09 in a clean interpreter:
GITHUB_TOKEN absent before the import, present after.

That matters because os.environ is inherited by every subprocess spawned
afterwards. The suite has session-scoped fixtures doing `env = os.environ.copy()`
before spawning a server, so those credentials reached child processes, and
DB_POSTGRES_URL silently pointed local runs at production instead of
governance_test.

The load now happens in the __main__ entrypoint via load_server_env().
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Names ~/.env.mcp is known to define. Kept explicit rather than read from the
# operator's file: a test must not depend on a machine-local secrets file, and
# must never read its values.
DOTENV_KEYS = (
    "GITHUB_TOKEN",
    "DB_POSTGRES_URL",
    "DB_BACKEND",
    "DB_AGE_GRAPH",
    "DB_POSTGRES_MIN_CONN",
    "DB_POSTGRES_MAX_CONN",
)


def _run(snippet: str) -> str:
    """Run in a CLEAN child interpreter.

    In-process assertions cannot prove this: by the time this test runs, some
    earlier test may already have imported the module, so os.environ would
    carry the values regardless of whether the import is what set them.
    """
    env = {k: v for k, v in os.environ.items() if k not in DOTENV_KEYS}
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_importing_the_server_injects_nothing():
    """The regression. Reports only NAMES, never values."""
    out = _run(
        """
        import os, sys
        sys.path.insert(0, ".")
        before = {k for k in os.environ}
        import src.mcp_server  # noqa: F401
        added = sorted(k for k in os.environ if k not in before)
        print("ADDED:" + ",".join(added))
        """
    )
    added = [k for k in out.split("ADDED:")[-1].strip().split(",") if k]
    leaked = sorted(set(added) & set(DOTENV_KEYS))
    assert not leaked, (
        f"importing src.mcp_server injected operator secrets: {leaked}. "
        "The dotenv load belongs in load_server_env(), called from __main__."
    )


def test_the_entrypoint_helper_still_loads_the_file():
    """Deferring the load must not silently disable it, or the server starts
    unconfigured and the fix trades one silent failure for another."""
    out = _run(
        """
        import os, sys
        sys.path.insert(0, ".")
        import src.mcp_server as m
        loaded = m.load_server_env()
        print("LOADED:%s" % loaded)
        print("HAS_DSN:%s" % ("DB_POSTGRES_URL" in os.environ))
        """
    )
    if "LOADED:True" in out:
        assert "HAS_DSN:True" in out, "load_server_env() reported success but set nothing"
    else:
        # No ~/.env.mcp on this machine (CI). The helper must still be callable
        # and must report honestly rather than raising.
        assert "LOADED:False" in out


def test_no_module_level_env_read_reintroduces_the_dependency():
    """load_server_env()'s deferral is only safe while nothing reads these at
    import time. A new module-level os.getenv for one of them would break it
    silently, so fail loudly here instead."""
    import re

    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if re.match(r"^[A-Z_]+\s*=\s*os\.(getenv|environ)", line):
                if any(k in line for k in DOTENV_KEYS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
    assert not offenders, (
        "module-level read of a deferred-load variable: "
        + ", ".join(offenders)
        + " -- read it inside a function, or load_server_env()'s deferral breaks."
    )
