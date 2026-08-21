"""
Version loading utilities.

Keeps all VERSION file fallback behavior in one module so server entrypoints
cannot drift independently.
"""

import datetime as _dt
import os
import subprocess
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path


DEFAULT_VERSION_FALLBACK = "0.0.0"
DEFAULT_BUILD_DATE_FALLBACK = "unknown"
DEFAULT_BUILD_SHA_FALLBACK = "unknown"


def load_version_from_file(project_root: Path) -> str:
    """Load the checkout version, or installed distribution metadata."""
    version_file = project_root / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()

    # A wheel has no repository-root VERSION file. Only consult distribution
    # metadata when the caller supplied this installed module's actual package
    # root; arbitrary/mocked paths retain the explicit 0.0.0 fallback.
    package_root = Path(__file__).resolve().parent.parent
    try:
        same_root = project_root.resolve() == package_root.resolve()
    except OSError:
        same_root = False
    if same_root:
        try:
            return distribution_version("governance-mcp")
        except PackageNotFoundError:
            pass
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
    version string. When ``.git`` is absent (sdist/wheel install, the Docker
    image) or git is unavailable, falls back to the operator-supplied
    ``UNITARES_BUILD_SHA`` env var (#1792 — forwarded by docker-compose so a
    containerized deployment can still self-report), then ``"unknown"``.
    Git wins when present because it cannot drift; the env var can.
    Never raises.
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

    env_sha = os.environ.get("UNITARES_BUILD_SHA", "").strip()
    if env_sha:
        return env_sha

    return DEFAULT_BUILD_SHA_FALLBACK
