"""P006 is dropped only on positive evidence that the handler reacts.

A manual triage of the unresolved queue on 2026-09-24 found P006 on 50 of 93
rows (rows, not distinct sites: the same code appeared once per worktree and
per line shift). About 40 flagged handlers that already log at warning or
above, re-raise, or return an error. Those are triage calls, not recorded
verdicts (the lifetime record has 0 confirmed P006, see
test_watcher_noise_narrowing.py).

``p006_actually_fires`` drops a finding only when every handler on the path
from the flagged line outward contains, anywhere in its body, a ``raise``, a
logging call at info level or above, or a ``return`` with a non-None value.
Every other case keeps it; ``parse_findings`` applies the rule.
"""

from __future__ import annotations

import json
import sys
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


# No positive evidence: the finding is kept.
SILENT_BODIES = [
    "pass",
    "...",
    "logger.debug(f'skipped: {exc}')",
    "logging.debug('skipped')",
    "self.log.debug('skipped')",
    "logger.log(logging.DEBUG, 'skipped')",
    "logger.log(10, 'skipped')",
    "logger.log(level, 'unknown level')",
    "'''Deliberately ignored.'''\npass",
    "logger.debug('a')\npass",
    "return",
    "return None",
    "logger.debug('x')\nreturn None",
    "result = None",
    "self.error = exc",
    "errors.append(exc)",
    "print(exc)",
    # Compound bodies are not evidence by themselves (open review finding 2).
    "if VERBOSE:\n    logger.debug('x')",
    "with lock:\n    pass",
    "for item in items:\n    logger.debug(item)",
    "try:\n    cleanup()\nexcept OSError:\n    pass",
    # A nested try's own handler reacts to a different exception: when
    # cleanup() succeeds, the caught exception is still swallowed.
    "try:\n    cleanup()\nexcept OSError:\n    raise",
    "try:\n    cleanup()\nexcept OSError:\n    logger.error('cleanup failed')",
    # Methods named like log levels on something that is not a logger.
    "task.exception()",
    "parser.error('bad input')",
    "self.status.info('x')",
    # Code in a nested scope does not run when the handler does.
    "def later():\n    raise RuntimeError('x')\npass",
    "callback = lambda: logger.error('x')",
    "class Err:\n    def f(self):\n        return 1",
]

# Positive evidence somewhere in the body: the finding is dropped.
LOUD_BODIES = [
    "logger.info(f'skipped: {exc}')",
    "logger.warning(f'skipped: {exc}')",
    "logger.warn('deprecated alias')",
    "logger.error('failed')",
    "logger.exception('failed')",
    "logger.critical('failed')",
    "logging.warning('failed')",
    "logger.log(logging.WARNING, 'failed')",
    "logger.log(logging.INFO, 'failed')",
    "logger.log(40, 'failed')",
    "raise",
    "raise RuntimeError('wrapped') from exc",
    "return {'success': False, 'error': str(exc)}",
    "return False",
    "logger.debug('x')\nlogger.warning('y')",
    "result = None\nlogger.warning('fallback')",
    # Evidence inside a nested block counts.
    "if VERBOSE:\n    logger.warning('x')",
    "if retryable(exc):\n    return retry()\nraise",
    "with lock:\n    raise",
    # Any non-None return counts, a fallback included (the chosen standard).
    "return []",
    "return default",
    # Logger receivers in other shapes.
    "self._logger.warning('x')",
    "LOG.error('x')",
    "structlog.get_logger().error('x')",
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


@pytest.mark.parametrize(
    "body",
    [
        "try:\n    logger.error('failed')\n    cleanup()\nexcept OSError:\n    pass",
        "try:\n    cleanup()\nexcept OSError:\n    pass\nfinally:\n    raise",
    ],
)
def test_nested_try_body_and_finally_count_for_the_handler(tmp_path, body):
    # The nested try's body and finally run on the handler's own path, so
    # evidence there counts when the outer clause (6) is cited. A cite inside
    # the nested try (7) also reaches its silent `except OSError`, so it stays.
    path = _write(tmp_path, _handler(body))
    assert p006_actually_fires(str(path), 6) is False
    assert p006_actually_fires(str(path), 7) is True


@pytest.mark.parametrize("body", ["continue", "break"])
def test_loop_control_is_silent(tmp_path, body):
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        try:\n"
        "            use(item)\n"
        "        except ValueError:\n"
        f"            {body}\n"
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


def test_silent_handler_nested_in_a_reacting_one_is_kept(tmp_path):
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


def _nested(outer_body: str) -> str:
    """Inner ``except KeyError: logger.warning`` inside an outer handler.

    Line 4 is the inner try body, 5 the inner clause, 6 its body, 7 the outer
    clause, 8 the outer body.
    """
    return (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            work()\n"
        "        except KeyError:\n"
        "            logger.warning('missing')\n"
        "    except Exception:\n"
        f"        {outer_body}\n"
    )


@pytest.mark.parametrize("flagged", [3, 4, 5, 6])
def test_outer_silent_handler_is_not_masked_by_an_inner_logging_one(tmp_path, flagged):
    # Open review finding 1: a non-KeyError from `work()` reaches the outer
    # `except Exception: pass` and is swallowed, whichever inner line is cited.
    path = _write(tmp_path, _nested("pass"))
    assert p006_actually_fires(str(path), flagged) is True
    assert p006_actually_fires(str(path), 7) is True


@pytest.mark.parametrize("flagged", [3, 4, 5, 6, 7])
def test_nested_handlers_that_all_react_are_dropped(tmp_path, flagged):
    path = _write(tmp_path, _nested("logger.error('failed')"))
    assert p006_actually_fires(str(path), flagged) is False


def test_outer_silent_handler_with_nested_try_in_its_body_is_kept(tmp_path):
    # The outer handler's nested try has only a silent handler, so nothing on
    # the outer handler's path reacts.
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        try:\n"
        "            cleanup()\n"
        "        except OSError:\n"
        "            pass\n"
    )
    path = _write(tmp_path, source)
    for flagged in (3, 4, 5, 6, 7, 8):
        assert p006_actually_fires(str(path), flagged) is True


def test_flag_on_the_try_line_uses_its_handlers(tmp_path):
    silent = _write(tmp_path, _handler("pass"), "silent.py")
    loud = _write(tmp_path, _handler("raise"), "loud.py")
    # Line 4 is `try:`.
    assert p006_actually_fires(str(silent), 4) is True
    assert p006_actually_fires(str(loud), 4) is False


@pytest.mark.parametrize("block", ["else", "finally"])
def test_else_and_finally_lines_are_not_governed_by_the_try(tmp_path, block):
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        raise\n"
        f"    {block}:\n"
        "        tidy()\n"
    )
    path = _write(tmp_path, source)
    # No handler catches an exception from `tidy()`: nothing shows a reaction.
    assert p006_actually_fires(str(path), 7) is True
    assert p006_actually_fires(str(path), 3) is False


@pytest.mark.skipif(sys.version_info < (3, 11), reason="except* needs 3.11")
@pytest.mark.parametrize("body, expected", [("pass", True), ("raise", False)])
def test_except_star_handlers(tmp_path, body, expected):
    source = (
        f"def f():\n    try:\n        work()\n    except* ValueError:\n        {body}\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 3) is expected
    assert p006_actually_fires(str(path), 5) is expected


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


def test_line_outside_any_try_keeps_the_finding(tmp_path):
    # Unverifiable (e.g. a trailing comment after `pass`): fail open.
    path = _write(tmp_path, "def f():\n    return work()\n")
    assert p006_actually_fires(str(path), 2) is True


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
        {
            "findings": [
                {
                    "pattern": "P006",
                    "line": line,
                    "hint": "silent swallow",
                    "evidence": "",
                }
            ]
        }
    )


def test_parse_findings_drops_p006_on_a_logging_handler(tmp_path):
    path = _write(tmp_path, _handler("logger.warning(f'failed: {exc}')"))
    assert parse_findings(_model_reply(6), str(path), "test", 1) == []


def test_parse_findings_keeps_p006_on_a_pass_handler(tmp_path):
    path = _write(tmp_path, _handler("pass"))
    parsed = parse_findings(_model_reply(6), str(path), "test", 1)
    assert [(f.pattern, f.line) for f, _ in parsed] == [("P006", 6)]


def test_parse_findings_keeps_p006_without_a_cited_line(tmp_path):
    # region_start lands on a logging handler, which the filter would drop if
    # it treated the fallback line as one the model cited.
    path = _write(tmp_path, _handler("logger.warning('x')"))
    assert p006_actually_fires(str(path), 6) is False
    reply = json.dumps({"findings": [{"pattern": "P006", "hint": "silent swallow"}]})
    assert [f.pattern for f, _ in parse_findings(reply, str(path), "test", 6)] == [
        "P006"
    ]


def test_parse_findings_keeps_p006_on_an_outer_silent_handler(tmp_path):
    path = _write(tmp_path, _nested("pass"))
    parsed = parse_findings(_model_reply(4), str(path), "test", 1)
    assert [(f.pattern, f.line) for f, _ in parsed] == [("P006", 4)]


# The receiver's last name must be a logger name, not merely contain "log".
NOT_LOGGER_RECEIVERS = [
    "dialog",
    "self.catalog",
    "blog",
    "login",
    "self.backlog",
]
LOGGER_RECEIVERS = [
    "logger",
    "log",
    "_logger",
    "_log",
    "LOGGER",
    "LOG",
    "logging",
    "self.log",
    "self._log",
    "self.app_logger",
    "structlog.get_logger()",
    "logging.getLogger(__name__)",
]


@pytest.mark.parametrize("receiver", NOT_LOGGER_RECEIVERS)
@pytest.mark.parametrize("flagged", [6, 7])
def test_receiver_that_only_contains_log_is_not_a_logger(tmp_path, receiver, flagged):
    path = _write(tmp_path, _handler(f"{receiver}.warning('x')"))
    assert p006_actually_fires(str(path), flagged) is True


@pytest.mark.parametrize("receiver", LOGGER_RECEIVERS)
def test_logger_named_receiver_counts(tmp_path, receiver):
    path = _write(tmp_path, _handler(f"{receiver}.warning('x')"))
    assert p006_actually_fires(str(path), 6) is False


# End to end: the AST filter is the single authority for a .py file that
# parses. The older line-based re-raise check in _verify_finding_against_source
# must not drop what p006_actually_fires kept (review round 2, finding 1).
_NESTED_HANDLER_RERAISES = (
    "def f():\n"
    "    try:\n"
    "        work()\n"
    "    except Exception:\n"
    "        try:\n"
    "            cleanup()\n"
    "        except OSError:\n"
    "            raise\n"
)
_SILENT_OUTER_INNER_RERAISES = (
    "def f():\n"
    "    try:\n"
    "        try:\n"
    "            work()\n"
    "        except KeyError:\n"
    "            raise\n"
    "    except Exception:\n"
    "        pass\n"
)
_RAISE_IN_NESTED_DEF = (
    "def f():\n"
    "    try:\n"
    "        work()\n"
    "    except Exception:\n"
    "        def later():\n"
    "            raise RuntimeError('x')\n"
    "        pass\n"
)
END_TO_END_CASES = [
    *[(_NESTED_HANDLER_RERAISES, line) for line in (4, 5, 6, 7, 8)],
    *[(_SILENT_OUTER_INNER_RERAISES, line) for line in (5, 6)],
    *[(_RAISE_IN_NESTED_DEF, line) for line in (4, 5)],
]


def _stub_model(monkeypatch, tmp_path: Path, reply: str) -> None:
    from agents.watcher import agent as watcher_agent

    # Only the model call is stubbed; the pattern library is the real one, so
    # parse_findings sees P006 as a known pattern.
    monkeypatch.setattr(watcher_agent, "build_prompt", lambda *a, **k: "stub")
    monkeypatch.setattr(
        watcher_agent,
        "call_model",
        lambda _p: {"text": reply, "tokens_used": 0, "model_used": "stub"},
    )
    # Keep the scan away from the live model-failure state file.
    state_dir = tmp_path / "watcher-state"
    monkeypatch.setattr(watcher_agent, "watcher_state_dir", lambda: state_dir)
    monkeypatch.setattr(watcher_agent, "_clear_model_failures", lambda *a, **k: None)

    def _no_failure(exc, *a, **k):
        raise AssertionError(f"scan recorded a model failure: {exc}")

    monkeypatch.setattr(watcher_agent, "_record_model_failure", _no_failure)


@pytest.mark.parametrize("source,flagged", END_TO_END_CASES)
def test_scan_file_keeps_p006_the_ast_filter_keeps(tmp_path, monkeypatch, source, flagged):
    """scan_file runs parse_findings and _verify_finding_against_source for real.

    scan_file's snippet map strips each line's indentation, so the line-based
    re-raise check cannot see a nested body here and these pass with or
    without the fix. The next test keeps the indentation and is the one that
    fails if that check overrides the AST filter again.
    """
    from agents.watcher.agent import scan_file

    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), flagged) is True
    _stub_model(monkeypatch, tmp_path, _model_reply(flagged))
    found = scan_file(str(path), persist=False)
    assert [(f.pattern, f.line) for f in found] == [("P006", flagged)]


@pytest.mark.parametrize("source,flagged", END_TO_END_CASES)
def test_verification_does_not_override_the_ast_filter(tmp_path, source, flagged):
    """With indentation kept in the snippet, the line-based re-raise check would
    see the nested `raise` and drop these. For a .py file that parses it must
    not run."""
    from agents.watcher.agent import _verify_finding_against_source

    path = _write(tmp_path, source)
    snippet = {i: text for i, text in enumerate(source.splitlines(), start=1)}
    [(finding, evidence)] = parse_findings(_model_reply(flagged), str(path), "test", 1)
    assert _verify_finding_against_source(finding, evidence, snippet) is True


def test_line_based_reraise_check_still_runs_for_unparseable_files(tmp_path):
    from agents.watcher.agent import Finding, _verify_finding_against_source

    source = _NESTED_HANDLER_RERAISES + "def broken(:\n"
    path = _write(tmp_path, source)
    snippet = {i: text for i, text in enumerate(source.splitlines(), start=1)}
    finding = Finding(
        pattern="P006",
        file=str(path),
        line=4,
        hint="silent swallow",
        severity="medium",
        detected_at="2026-09-24T00:00:00Z",
        model_used="test",
    )
    assert _verify_finding_against_source(finding, "", snippet) is False
