"""P006 fires only on a handler whose body is effectively silent.

Triage of 93 unresolved Watcher findings, 2026-09-24: P006 was 50 of them, and
about 40 flagged handlers that already log at warning or above, re-raise, or
return an error. Every true positive was a handler whose body is only ``pass``
or only a ``logger.debug`` call. ``p006_actually_fires`` is the deterministic
AST post-filter that encodes that line; ``parse_findings`` applies it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.watcher.agent import p006_actually_fires, parse_findings


def _write(tmp_path: Path, source: str, name: str = "mod.py") -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def _handler(body: str) -> str:
    """A module whose `except` clause is line 6 and body starts at line 7."""
    return (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as exc:\n"
        + "".join(f"        {line}\n" for line in body.splitlines())
        + "    return 1\n"
    )


SILENT_BODIES = [
    "pass",
    "...",
    "logger.debug(f'skipped: {exc}')",
    "logging.debug('skipped')",
    "self.log.debug('skipped')",
    "logger.log(logging.DEBUG, 'skipped')",
    "'''Deliberately ignored.'''\npass",
    "logger.debug('a')\npass",
]

LOUD_BODIES = [
    "logger.info(f'skipped: {exc}')",
    "logger.warning(f'skipped: {exc}')",
    "logger.error('failed')",
    "logger.exception('failed')",
    "logging.warning('failed')",
    "logger.log(logging.WARNING, 'failed')",
    "raise",
    "raise RuntimeError('wrapped') from exc",
    "return None",
    "return {'success': False, 'error': str(exc)}",
    "result = None",
    "self.error = exc",
    "logger.debug('x')\nreturn None",
    "logger.debug('x')\nlogger.warning('y')",
    "errors.append(exc)",
]


@pytest.mark.parametrize("body", SILENT_BODIES)
@pytest.mark.parametrize("flagged", [6, 7])
def test_silent_body_is_kept(tmp_path, body, flagged):
    # The model cites either the clause (6) or the first body line (7).
    path = _write(tmp_path, _handler(body))
    assert p006_actually_fires(str(path), flagged) is True


@pytest.mark.parametrize("body", LOUD_BODIES)
@pytest.mark.parametrize("flagged", [6, 7])
def test_reacting_body_is_dropped(tmp_path, body, flagged):
    path = _write(tmp_path, _handler(body))
    assert p006_actually_fires(str(path), flagged) is False


def test_continue_in_loop_is_silent(tmp_path):
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        try:\n"
        "            use(item)\n"
        "        except ValueError:\n"
        "            continue\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 5) is True


def test_flag_on_try_body_uses_its_handlers(tmp_path):
    silent = _write(tmp_path, _handler("pass"), "silent.py")
    loud = _write(tmp_path, _handler("logger.warning('x')"), "loud.py")
    # Line 5 is `work()` inside the try body.
    assert p006_actually_fires(str(silent), 5) is True
    assert p006_actually_fires(str(loud), 5) is False


def test_any_silent_handler_of_the_try_keeps_it(tmp_path):
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except KeyError:\n"
        "        logger.warning('missing')\n"
        "    except Exception:\n"
        "        pass\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 3) is True
    assert p006_actually_fires(str(path), 4) is False
    assert p006_actually_fires(str(path), 6) is True


def test_innermost_handler_governs(tmp_path):
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as exc:\n"
        "        logger.warning(f'outer: {exc}')\n"
        "        try:\n"
        "            cleanup()\n"
        "        except Exception:\n"
        "            pass\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 4) is False
    assert p006_actually_fires(str(path), 8) is True
    assert p006_actually_fires(str(path), 9) is True


def test_try_finally_defers_to_the_enclosing_handlers(tmp_path):
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            work()\n"
        "        finally:\n"
        "            cleanup()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 4) is True
    assert p006_actually_fires(str(path), 6) is True


def test_line_outside_any_try_is_dropped(tmp_path):
    path = _write(tmp_path, "def f():\n    return work()\n")
    assert p006_actually_fires(str(path), 2) is False


@pytest.mark.parametrize(
    "name, source",
    [
        ("broken.py", "def f(:\n    pass\n"),
        ("script.sh", "set -e\n"),
    ],
)
def test_unverifiable_files_keep_the_finding(tmp_path, name, source):
    path = _write(tmp_path, source, name)
    assert p006_actually_fires(str(path), 1) is True


def test_missing_file_keeps_the_finding(tmp_path):
    assert p006_actually_fires(str(tmp_path / "gone.py"), 3) is True


def _model_reply(line: int) -> str:
    return json.dumps(
        {"findings": [{"pattern": "P006", "line": line, "hint": "silent swallow", "evidence": ""}]}
    )


def test_parse_findings_drops_p006_on_a_logging_handler(tmp_path):
    path = _write(tmp_path, _handler("logger.warning(f'failed: {exc}')"))
    assert parse_findings(_model_reply(6), str(path), "test", 1) == []


def test_parse_findings_keeps_p006_on_a_pass_handler(tmp_path):
    path = _write(tmp_path, _handler("pass"))
    parsed = parse_findings(_model_reply(6), str(path), "test", 1)
    assert [(f.pattern, f.line) for f, _ in parsed] == [("P006", 6)]
