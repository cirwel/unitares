"""A malformed class-calibration overlay must not take the import down.

``config/governance_config.py`` applies the ``UNITARES_CLASS_CALIBRATION``
overlay at module import, and the module sits on the server's eager import path
(``src.mcp_server`` -> ``governance_monitor`` -> ``governance_config``), so a
raise out of the overlay is a governance server that does not start. Three
shapes escaped: a section that was not an object, because each section was read
as ``(data.get(section) or {}).items()`` outside the per-entry guard; an entry
raising OverflowError, which the per-entry guards did not catch; and a file
nested too deeply for the JSON decoder, whose RecursionError the file-level
catch missed. Each now skips the offending file, section or entry with a
WARNING naming it, and whatever remains applies.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV = "UNITARES_CLASS_CALIBRATION"
LOGGER = "config.governance_config"

# One well-formed entry per section, all for the same generic label.
_GOOD = {
    "healthy_operating_point": {"resident-a": [0.31, 0.78, 0.21]},
    "delta_norm_max": {"resident-a": 0.16},
    "void_threshold": {"resident-a": 0.3},
    "label_intervals": {"resident-a": 1800},
}
SECTIONS = list(_GOOD)

# What resident-a receives from each well-formed section above.
_EXPECTED = {
    "healthy_operating_point": [0.31, 0.78, 0.21],
    "delta_norm_max": 0.16,
    "void_threshold": 0.3,
    "label_intervals": 1800,
}

# Truthy on purpose: `(data.get(section) or {})` read an empty list, a zero or
# an empty string as no section, so only these reached `.items()` and raised.
_NOT_AN_OBJECT = {
    "list": ["resident-a", 1800],
    "number": 0.5,
    "string": "resident-a",
}

# One entry per section that raised OverflowError past the per-entry guard,
# which caught TypeError and ValueError but not this. float() of an integer
# beyond the float range raises it; json.dumps writes inf as Infinity, and
# int() of that raises it too.
_OVERFLOWING = {
    "healthy_operating_point": [10**400, 0.5, 0.2],
    "delta_norm_max": 10**400,
    "void_threshold": 10**400,
    "label_intervals": float("inf"),
}


def _received(g) -> dict:
    """What each section left for resident-a; None where nothing landed."""
    hop = g.HEALTHY_OPERATING_POINT_BY_CLASS.get("resident-a")
    dnm = g.DELTA_NORM_MAX_BY_CLASS.get("resident-a")
    return {
        "healthy_operating_point": None if hop is None else list(hop),
        "delta_norm_max": None if dnm is None else dnm.value,
        "void_threshold": g.GovernanceConfig.VOID_THRESHOLD_BY_CLASS.get("resident-a"),
        "label_intervals": g.LABEL_CHECKIN_INTERVALS.get("resident-a"),
    }


# The child imports the module the way a server start does, then reports
# through the same _received the in-process tests use. It puts this checkout
# first on sys.path itself rather than relying on `-c` to put the cwd there:
# PYTHONSAFEPATH turns that off, and an inherited PYTHONPATH could then import
# another tree, so the test would pass against the wrong code.
_CHILD = (
    f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n"
    + inspect.getsource(_received)
    + "\nimport json\nimport config.governance_config as g\n"
    + "print(json.dumps(_received(g)))\n"
)


def _import_fresh(tmp_path: Path, doc: dict | str) -> subprocess.CompletedProcess:
    """Import the module in a new interpreter with the overlay set to ``doc``
    (a dict is written as JSON, a str verbatim)."""
    path = tmp_path / "class-calibration.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=REPO_ROOT,
        env={**os.environ, ENV: str(path)},
        capture_output=True, text=True, timeout=120,
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.fixture
def apply_overlay(monkeypatch, tmp_path, set_governance_config, caplog):
    """Apply an overlay in-process, against copies of the dicts it writes.

    The overlay mutates module-level dicts that other modules hold by
    reference (``background_tasks`` imports ``LABEL_CHECKIN_INTERVALS`` by
    name), so each is swapped for a copy that monkeypatch restores.
    """
    import config.governance_config as gc

    for name in ("HEALTHY_OPERATING_POINT_BY_CLASS", "DELTA_NORM_MAX_BY_CLASS",
                 "LABEL_CHECKIN_INTERVALS"):
        monkeypatch.setattr(gc, name, dict(getattr(gc, name)))
    set_governance_config("VOID_THRESHOLD_BY_CLASS",
                          dict(gc.GovernanceConfig.VOID_THRESHOLD_BY_CLASS))
    caplog.set_level(logging.WARNING, logger=LOGGER)
    path = tmp_path / "class-calibration.json"

    def apply(content) -> dict:
        """Point the variable at ``content`` (a dict is written as JSON, a str
        verbatim, None leaves the file absent) and apply it."""
        if content is not None:
            path.write_text(
                content if isinstance(content, str) else json.dumps(content))
        monkeypatch.setenv(ENV, str(path))
        gc._apply_class_calibration_overlay()
        return _received(gc)

    return apply


@pytest.mark.parametrize("kind", list(_NOT_AN_OBJECT))
@pytest.mark.parametrize("section", SECTIONS)
def test_non_object_section_imports_in_a_fresh_interpreter(section, kind, tmp_path):
    """The failure this guards is a server that does not start, so import the
    module in a new interpreter, as a server start does. Every other section is
    well formed and must still land."""
    proc = _import_fresh(tmp_path, {**_GOOD, section: _NOT_AN_OBJECT[kind]})

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {**_EXPECTED, section: None}
    # With no logging configured the skip still reaches stderr, through
    # logging's last-resort handler.
    assert f"section {section!r}" in proc.stderr


@pytest.mark.parametrize("kind", list(_NOT_AN_OBJECT))
@pytest.mark.parametrize("section", SECTIONS)
def test_non_object_section_is_skipped_with_a_warning_naming_it(
    section, kind, apply_overlay, caplog,
):
    received = apply_overlay({**_GOOD, section: _NOT_AN_OBJECT[kind]})

    assert received == {**_EXPECTED, section: None}
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    # The section is what the operator has to fix; the type says what is wrong.
    assert f"section {section!r}" in warnings[0]
    assert type(_NOT_AN_OBJECT[kind]).__name__ in warnings[0]


def test_empty_non_object_section_is_named_too(apply_overlay, caplog):
    """`or {}` read an empty list as no section at all, so the wrong shape went
    unremarked until someone filled it in and the import crashed."""
    received = apply_overlay({**_GOOD, "label_intervals": []})

    assert received == {**_EXPECTED, "label_intervals": None}
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "section 'label_intervals' is a list" in warnings[0]


@pytest.mark.parametrize("empty", [None, {}], ids=["null", "empty-object"])
def test_null_or_empty_section_is_silent(empty, apply_overlay, caplog):
    """A null or empty section says "none here"; neither is a skip."""
    received = apply_overlay({**_GOOD, "void_threshold": empty})

    assert received == {**_EXPECTED, "void_threshold": None}
    assert _warnings(caplog) == []


@pytest.mark.parametrize("section", SECTIONS)
def test_overflowing_entry_imports_in_a_fresh_interpreter(section, tmp_path):
    """The per-entry guard is the other escape. The bad entry comes first in
    its section, so the good entry after it shows the section kept applying."""
    doc = {**_GOOD, section: {"resident-bad": _OVERFLOWING[section], **_GOOD[section]}}
    proc = _import_fresh(tmp_path, doc)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == _EXPECTED


@pytest.mark.parametrize("section", SECTIONS)
def test_overflowing_entry_is_skipped_with_a_warning_naming_it(
    section, apply_overlay, caplog,
):
    received = apply_overlay(
        {**_GOOD, section: {"resident-bad": _OVERFLOWING[section], **_GOOD[section]}})

    assert received == _EXPECTED
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert f"{section} entry 'resident-bad'" in warnings[0]
    assert "OverflowError" in warnings[0]


# One malformed entry for each other error class the entry guard catches. All
# four sections share that one guard, so narrowing it would bring the import
# crash back for the commonest typos while every test above stayed green.
_MALFORMED_ENTRIES = {
    "null-interval": ("label_intervals", None, "TypeError"),
    "unit-suffixed-interval": ("label_intervals", "30m", "ValueError"),
    "list-threshold": ("void_threshold", [0.3], "TypeError"),
    "zero-radius": ("delta_norm_max", 0, "ValueError"),
    "short-operating-point": ("healthy_operating_point", [0.3, 0.7], "IndexError"),
    "keyed-operating-point": ("healthy_operating_point", {"0": 0.3}, "KeyError"),
}


@pytest.mark.parametrize("case", list(_MALFORMED_ENTRIES))
def test_each_entry_error_class_is_skipped_with_a_warning_naming_it(
    case, apply_overlay, caplog,
):
    section, value, error = _MALFORMED_ENTRIES[case]
    received = apply_overlay(
        {**_GOOD, section: {"resident-bad": value, **_GOOD[section]}})

    assert received == _EXPECTED
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert f"{section} entry 'resident-bad'" in warnings[0]
    assert error in warnings[0]


def _leave_unloadable(path: Path, kind: str) -> None:
    """Leave an overlay at ``path`` that cannot be loaded, the way ``kind`` says."""
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_text(json.dumps(_GOOD))
        path.chmod(0)
    elif kind == "truncated-json":
        path.write_text(json.dumps(_GOOD)[:-1])
    elif kind == "not-utf8":
        # 0xFF never starts a UTF-8 sequence, and under a single-byte locale it
        # decodes to a character no JSON document can start with, so the file
        # fails to load whatever the locale's encoding.
        path.write_bytes(b"\xff" + json.dumps(_GOOD).encode())
    # "missing" leaves the path absent.


# The error each kind is named with; None where it depends on the locale.
_UNLOADABLE = {
    "missing": "FileNotFoundError",
    "directory": "IsADirectoryError",
    "unreadable": "PermissionError",
    "truncated-json": "JSONDecodeError",
    "not-utf8": None,
}


@pytest.mark.parametrize("kind", list(_UNLOADABLE))
def test_unloadable_file_is_named(kind, apply_overlay, caplog, tmp_path):
    """The variable was set, so an overlay was wanted. Applying none of it
    leaves the same defaults as never setting it; the warning is the difference.
    The kinds span the file-level catch: OSErrors, and ValueErrors other than
    JSONDecodeError."""
    if kind == "unreadable" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file")
    path = tmp_path / "class-calibration.json"
    _leave_unloadable(path, kind)
    try:
        received = apply_overlay(None)
    finally:
        if kind == "unreadable":
            path.chmod(0o600)

    assert received == dict.fromkeys(SECTIONS)
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "class-calibration.json could not be loaded" in warnings[0]
    if _UNLOADABLE[kind]:
        assert _UNLOADABLE[kind] in warnings[0]


def test_too_deeply_nested_file_imports_in_a_fresh_interpreter(tmp_path):
    """Valid JSON nested past the decoder's recursion limit makes json.load
    raise RecursionError, which is not a ValueError. It runs in a child so the
    depth cannot disturb this interpreter."""
    depth = 200_000
    proc = _import_fresh(tmp_path, "[" * depth + "]" * depth)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == dict.fromkeys(SECTIONS)
    assert "could not be loaded (RecursionError" in proc.stderr


@pytest.mark.parametrize("payload", ['["resident-a", 1800]', '"resident-a"', "3"],
                         ids=["list", "string", "number"])
def test_non_object_file_is_named(payload, apply_overlay, caplog):
    received = apply_overlay(payload)

    assert received == dict.fromkeys(SECTIONS)
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "not an object; applying none of it" in warnings[0]


def test_well_formed_overlay_applies_silently(apply_overlay, caplog):
    """The warnings must not fire on a clean overlay. A top-level ``_comment``
    is outside the schema and is ignored, not reported."""
    received = apply_overlay({"_comment": "deployment-local anchors", **_GOOD})

    assert received == _EXPECTED
    assert _warnings(caplog) == []


def test_unset_variable_is_silent(monkeypatch, caplog):
    """No overlay was asked for, so there is nothing to report."""
    import config.governance_config as gc

    monkeypatch.delenv(ENV, raising=False)
    caplog.set_level(logging.WARNING, logger=LOGGER)
    gc._apply_class_calibration_overlay()

    assert _warnings(caplog) == []
