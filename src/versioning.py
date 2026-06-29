"""
Version loading utilities.

Keeps all VERSION file fallback behavior in one module so server entrypoints
cannot drift independently.
"""

import datetime as _dt
import subprocess
from pathlib import Path


DEFAULT_VERSION_FALLBACK = "0.0.0"
DEFAULT_BUILD_DATE_FALLBACK = "unknown"
DEFAULT_BUILD_SHA_FALLBACK = "unknown"


def load_version_from_file(project_root: Path) -> str:
    """Load version from project VERSION file, with centralized fallback."""
    version_file = project_root / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return DEFAULT_VERSION_FALLBACK


def load_build_date_from_repo(project_root: Path) -> str:
    """Best-effort build/deploy date (ISO ``YYYY-MM-DD``).

    Derived, not hand-maintained — a hardcoded constant silently froze at its
    first value because nothing ever bumped it. Resolution order:

    1. HEAD commit date (``git log -1 --format=%cs``) — answers "what code is
       this build running"; works wherever the server runs from a checkout
       (the live deploy does).
    2. VERSION file mtime — for a git-cloned deploy this is ~checkout/deploy
       time; covers installs where ``.git`` is absent (sdist/wheel).
    3. ``"unknown"`` — never raise from a metadata read.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "log", "-1", "--format=%cs"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        date = out.stdout.strip()
        if out.returncode == 0 and date:
            return date
    except Exception:
        pass

    try:
        version_file = project_root / "VERSION"
        if version_file.exists():
            mtime = version_file.stat().st_mtime
            return _dt.date.fromtimestamp(mtime).isoformat()
    except Exception:
        pass

    return DEFAULT_BUILD_DATE_FALLBACK


def load_build_sha_from_repo(project_root: Path) -> str:
    """Best-effort short commit SHA of the running build (``git rev-parse``).

    This is the precise answer to "what code is live" — unlike the hand-typed
    semver, it can't drift, so observability can key on it instead of the
    version string. Returns ``"unknown"`` when ``.git`` is absent (sdist/wheel
    install) or git is unavailable; never raises.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except Exception:
        pass

    return DEFAULT_BUILD_SHA_FALLBACK
