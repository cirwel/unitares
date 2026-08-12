"""A pinned checkout may lag by commits; it may not lag by a version silently.

On 2026-08-12 the deploy worktree sat 28 commits and a full minor behind master
— `pyproject.toml` said 2.18.0, the live server answered 2.17.0 — while this
doctor printed "all surfaces running merged code" every hour. It was not
malfunctioning: `check_behind=False` on the pinned worktree is correct, because
commit-lag there is a staging decision. But nothing watched the one gap that
carried meaning, so the operator reasonably believed the fleet was synced.

`version_lag` is the narrow signal that closes it. The tests below pin both
directions: it must fire on a real version gap, and it must stay quiet on
ordinary commit lag — a check that cries wolf hourly is one nobody reads, which
is how the original blind spot was affordable in the first place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Same loader the sibling doctor test uses — the script lives outside any
# importable package, so a path insert would depend on collection order.
MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "deploy_drift_doctor.py"
_spec = importlib.util.spec_from_file_location("deploy_drift_doctor", MODULE_PATH)
ddd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ddd)

DEFAULT_SURFACES = ddd.DEFAULT_SURFACES
Surface = ddd.Surface
_pyproject_version = ddd._pyproject_version
diagnose = ddd.diagnose


def _io(*, local_version: str, origin_version: str, behind: int = 0):
    """Minimal IO seam: a checkout on-branch, N commits behind, two pyprojects.

    Surfaces use "/tmp" because `diagnose` guards on `os.path.isdir` before any
    IO — a nonexistent path returns no diagnoses at all, which reads as "the
    check did not fire" rather than "the path was fake".
    """
    def _git(path, *args):
        if args[:1] == ("show",):
            ref = args[1]
            v = origin_version if ref.startswith("origin/") else local_version
            return f'[project]\nname = "unitares"\nversion = "{v}"\n'
        if args[:1] == ("rev-list",):
            return f"0\t{behind}"
        if args[:3] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "master"
        if args[:1] == ("log",):
            return "abc1234 some merged commit"
        return ""
    return {
        "git": _git,
        "fetch": lambda path: None,
        # Key name matches the module's IO map; a wrong one raises KeyError
        # inside the restart-pending branch rather than being ignored.
        "process_start_epoch": lambda label: None,
    }


def _conditions(diagnoses):
    return {d.condition for d in diagnoses}


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_reads_the_project_version_not_a_tool_version():
    """A `[tool.*]` section's own version must not be mistaken for the project's."""
    text = '[project]\nversion = "2.17.0"\n[tool.poetry]\nversion = "9.9.9"\n'
    assert _pyproject_version(text) == "2.17.0"


@pytest.mark.parametrize("bad", [None, "", "no version here", "[tool.x]\nversion = '1'"])
def test_unparseable_degrades_to_silence_not_a_false_alarm(bad):
    """Returning None means the check stays quiet rather than inventing drift."""
    assert _pyproject_version(bad) is None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_version_gap_on_a_pinned_surface_is_reported():
    surface = Surface("unitares-deploy", "/tmp", "master", "com.unitares.governance-mcp",
                      check_behind=False, check_version=True)
    found = diagnose(surface, _io(local_version="2.17.0", origin_version="2.18.0",
                                          behind=28))
    assert "version_lag" in _conditions(found)
    detail = next(d for d in found if d.condition == "version_lag").detail
    assert "2.17.0" in detail and "2.18.0" in detail


def test_commit_lag_alone_stays_silent():
    """The whole point of the pin — 28 commits behind at the same version is fine."""
    surface = Surface("unitares-deploy", "/tmp", "master", "com.unitares.governance-mcp",
                      check_behind=False, check_version=True)
    found = diagnose(surface, _io(local_version="2.18.0", origin_version="2.18.0",
                                          behind=28))
    assert "version_lag" not in _conditions(found)
    assert "behind_origin" not in _conditions(found), (
        "check_behind=False must still suppress commit-lag; version_lag is an "
        "addition to it, not a way around it"
    )


def test_surfaces_without_the_opt_in_are_unaffected():
    surface = Surface("unitares-governance-plugin", "/tmp", "master", None)
    found = diagnose(surface, _io(local_version="0.4.12", origin_version="0.4.13"))
    assert "version_lag" not in _conditions(found)


def test_the_deploy_worktree_is_the_surface_that_opts_in():
    """Pin the configuration, since the blind spot was a config decision."""
    deploy = next(s for s in DEFAULT_SURFACES if s.name == "unitares-deploy")
    assert deploy.check_behind is False, "commit-lag on the pin should stay suppressed"
    assert deploy.check_version is True, (
        "the pinned deploy worktree is exactly the surface whose version gap "
        "nothing else would report"
    )
