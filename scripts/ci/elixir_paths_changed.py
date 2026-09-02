#!/usr/bin/env python3
"""Decide whether a change reaches anything the Elixir suites validate.

Why this exists (#2040): a path-filtered job that does not run reports NO
status, and branch protection treats a missing required context as pending
forever. So the per-app Elixir jobs can never be required checks directly.
The workflow therefore always runs, this script decides whether the suites
are relevant to the change, and one always-present ``elixir-gate`` job is the
single context worth requiring on master.

The decision is fail-safe: whenever the change set cannot be determined (a
force-push whose ``before`` is unreachable, a branch-creation push, a fetch
that fails), the answer is ``relevant=true`` so the suites run rather than
being skipped on a guess. An unnecessary run costs minutes; a skipped run on
a real Elixir change is exactly the merge-unvalidated hole this closes. The
workflow adds a second layer: if this job fails outright, the app jobs run
anyway and the gate then demands that every one of them succeeded.

Usage (from .github/workflows/elixir-tests.yml):
  python3 scripts/ci/elixir_paths_changed.py --event pull_request \\
      --base <base sha> --head <head sha>

Writes ``relevant=<true|false>`` and ``reason=<text>`` to ``$GITHUB_OUTPUT``
when set, and always echoes them to stdout.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable

# Keep this list in step with the workflow header comment; the unit tests pin
# it against the workflow's `working-directory:` lines and against the
# fixture paths the Elixir tests reference. A change under any of these can
# break one of the suites, so all of them run (the suites share unitares_sdk
# by path: dep, and lease_plane runs against db/postgres).
RELEVANT_PREFIXES: tuple[str, ...] = (
    "elixir/sentinel/",
    "elixir/lease_plane/",
    "elixir/unitares_sdk/",
    "elixir/agent_orchestrator/",
    "elixir/dialectic_live/",
    "elixir/wave3a_handlers/",
    "db/postgres/",
    # Cross-runtime contract fixtures the lease_plane suite reads through
    # relative paths (tests/vectors/*.json, tests/vendored/*.schema.json).
    "tests/vectors/",
    "tests/vendored/",
)

# Single files the suites consume. The docker trio is what the lease_plane
# job's `docker compose up postgres-age` actually builds and runs against
# (service name, image, env defaults, initdb mount all live in
# docker-compose.yml) -- the same set docker-quickstart.yml filters on. The
# gate's own machinery is here so a broken detector cannot silently skip
# everything and still pass.
RELEVANT_FILES: tuple[str, ...] = (
    "docker-compose.yml",
    "Dockerfile",
    ".dockerignore",
    ".github/workflows/elixir-tests.yml",
    "scripts/ci/elixir_paths_changed.py",
    "scripts/ci/elixir_gate.py",
)

ZERO_SHA_PREFIX = "0000000"


def is_relevant(paths: Iterable[str]) -> bool:
    """True when any path lies under a suite directory or is a consumed file."""
    for path in paths:
        path = path.strip()
        if not path:
            continue
        if path in RELEVANT_FILES:
            return True
        if any(path.startswith(prefix) for prefix in RELEVANT_PREFIXES):
            return True
    return False


def _git(args: list[str], cwd: str | None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _commit_present(sha: str, cwd: str | None) -> bool:
    return _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd).returncode == 0


def changed_paths(base: str, head: str, cwd: str | None = None) -> list[str] | None:
    """Paths changed between ``base`` and ``head``, or None when undeterminable.

    Only the two commits are needed (tree-to-tree diff), so a depth-1 checkout
    plus a depth-1 fetch of the base is enough; no full history is required.

    Two flags make the listing machine-safe. Rename detection is disabled: with
    it, a file moved OUT of a suite directory is reported only at its
    destination and the suite's loss would be invisible. And ``-z`` separates
    entries with NUL and turns off git's C-style quoting, so a path with a
    non-ASCII or control character is not wrapped in quotes that would defeat
    the prefix match.
    """
    if not base or base.startswith(ZERO_SHA_PREFIX):
        return None
    if not _commit_present(base, cwd):
        # GitHub serves reachable commits by SHA; a force-pushed-away base is
        # not reachable and this fetch fails, which is the fail-safe path.
        if _git(["fetch", "--quiet", "--depth=1", "origin", base], cwd).returncode != 0:
            return None
        if not _commit_present(base, cwd):
            return None
    result = _git(["diff", "--name-only", "--no-renames", "-z", base, head], cwd)
    if result.returncode != 0:
        return None
    return [
        entry.decode("utf-8", "surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry
    ]


def decide(event: str, base: str, head: str, cwd: str | None = None) -> tuple[bool, str]:
    """Return (relevant, reason). Undeterminable change sets are relevant."""
    paths = changed_paths(base, head, cwd)
    if paths is None:
        return True, f"change set undeterminable for {event} (base={base or 'empty'}); running the suites"
    if is_relevant(paths):
        return True, "a changed path lies under an Elixir suite, db/postgres, the shared fixtures, the docker files, or the gate machinery"
    return False, f"none of {len(paths)} changed path(s) reaches the Elixir suites"


def _emit(key: str, value: str) -> None:
    line = f"{key}={value}"
    print(line)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "unknown"))
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)

    relevant, reason = decide(args.event, args.base, args.head)
    _emit("relevant", "true" if relevant else "false")
    _emit("reason", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
