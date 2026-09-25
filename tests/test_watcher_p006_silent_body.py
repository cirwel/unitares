"""P006 is dropped only on positive evidence that the handler reacts.

A manual triage of the unresolved queue on 2026-09-24 found P006 on 50 of 93
rows (rows, not distinct sites: the same code appeared once per worktree and
per line shift). About 40 flagged handlers that already log at warning or
above, re-raise, or return an error. Those are triage calls, not recorded
verdicts (the lifetime record has 0 confirmed P006, see
test_watcher_noise_narrowing.py).

``p006_actually_fires`` drops a finding only when every handler on the path
from the flagged line outward reacts: its body, nested blocks included, holds
a ``raise``, a logging call at info level or above on a logger, or a
``return`` with a non-None value. Inside the body of a nested ``try`` that has
a handler, only the first statement can count, and only a ``return`` of a
literal or variable or a log on a plain logger name with literal or variable
arguments; that ``try``'s ``else`` is skipped, since it does not run when the
body raised. Evidence in a nested ``try``'s own handler never counts. Every
other case keeps it; ``parse_findings`` applies the rule.
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
    # A raise in the body of a nested try that has a handler may be caught by
    # it, and then the failure is swallowed (review round 3, finding 1).
    "try:\n    raise RuntimeError('wrapped') from exc\nexcept Exception:\n    pass",
    "try:\n    if bad:\n        raise\nexcept ValueError:\n    pass",
    # contextlib.suppress swallows a raise the same way (independent review).
    "with contextlib.suppress(Exception):\n    raise RuntimeError('x')",
    "with suppress(ValueError):\n    raise",
    # A return whose value calls something can raise into the nested handler
    # (#2424 final review, P3 2).
    "try:\n    return compute()\nexcept Exception:\n    pass",
    "try:\n    return {'error': str(exc)}\nexcept Exception:\n    pass",
    "try:\n    return await fetch()\nexcept Exception:\n    pass",
    # Not only calls raise (#2442 review, P2).
    "try:\n    return cache[key]\nexcept KeyError:\n    pass",
    "try:\n    return self.result\nexcept AttributeError:\n    pass",
    "try:\n    return total / count\nexcept Exception:\n    pass",
    "try:\n    return f'{exc}'\nexcept Exception:\n    pass",
    "try:\n    return {**extra}\nexcept Exception:\n    pass",
    # Something before the reaction can raise into the nested handler, or
    # the log call's own arguments can (#2442 review, round 3).
    "try:\n    cleanup()\n    return False\nexcept Exception:\n    pass",
    "try:\n    cleanup()\n    logger.warning('x')\nexcept Exception:\n    pass",
    "try:\n    logger.warning('x %s', compute())\nexcept Exception:\n    pass",
    "with contextlib.suppress(Exception):\n    cleanup()\n    return False",
    # A nested else is skipped when the body raised into its handler, and a
    # log receiver that is not a plain name can raise (round 4).
    "try:\n    cleanup()\nexcept OSError:\n    pass\nelse:\n    raise",
    "try:\n    cleanup()\nexcept Exception:\n    pass\nelse:\n    return False",
    "try:\n    self.logger.warning('x')\nexcept AttributeError:\n    pass",
    "try:\n    logging.getLogger(name).warning('x')\nexcept Exception:\n    pass",
    # **mapping can raise; a context manager entered after suppress() can
    # raise into it before the body runs (round 6).
    "try:\n    logger.warning('x', **extra)\nexcept Exception:\n    pass",
    "with suppress(Exception), open(p) as fh:\n    return False",
    "with contextlib.suppress(Exception):\n    return compute()",
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
    # A log call counts in the nested body: it runs before anything can be
    # caught. A raise there does not (see SILENT_BODIES); one in `finally`
    # escapes the nested handlers and does.
    path = _write(tmp_path, _handler(body))
    assert p006_actually_fires(str(path), 6) is False
    assert p006_actually_fires(str(path), 7) is True


@pytest.mark.parametrize(
    "body",
    [
        # try/finally has no handler to catch the raise.
        "try:\n    raise RuntimeError('wrapped') from exc\nfinally:\n    cleanup()",
        # A non-None return whose value calls nothing cannot raise into the
        # nested handler.
        "try:\n    return {'error': 'failed', 'exc': exc}\nexcept Exception:\n    pass",
        "try:\n    return False\nexcept Exception:\n    pass",
        # A signed number is a literal too (#2442 review, round 3).
        "try:\n    return -1\nexcept Exception:\n    pass",
        # A first-statement log call with inert arguments runs before
        # anything can be caught.
        "try:\n    logger.warning('x %s', exc)\n    cleanup()\nexcept Exception:\n    pass",
        # Outside a caught body a call in the value is fine.
        "try:\n    cleanup()\nexcept OSError:\n    pass\nreturn {'error': str(exc)}",
        # A log call under suppress() still runs; only a raise is swallowed.
        "with contextlib.suppress(Exception):\n    logger.error('x')",
        # A with block that is not suppress() lets a raise escape.
        "with lock:\n    raise",
    ],
)
def test_escaping_reaction_in_nested_try_counts(tmp_path, body):
    path = _write(tmp_path, _handler(body))
    assert p006_actually_fires(str(path), 6) is False


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


def _outer_with_nested(inner_body: str) -> str:
    """Line 2 is the outer `try:`, 3 `y = 1`, 4 the nested `try:`, 6 the
    nested clause, 7 its body; the outer handler (8-9) re-raises."""
    return (
        "def f():\n"
        "    try:\n"
        "        y = 1\n"
        "        try:\n"
        "            x()\n"
        "        except Exception:\n"
        f"            {inner_body}\n"
        "    except ValueError:\n"
        "        raise\n"
    )


@pytest.mark.parametrize("flagged", [2, 3, 4, 6, 7])
def test_cite_above_a_nested_silent_try_is_kept(tmp_path, flagged):
    # #2424 final review, P3 1: citing the outer `try:` (2) or an early body
    # line (3) once examined only the outer re-raising handler and dropped
    # the finding, though the nested `except Exception: pass` swallows.
    path = _write(tmp_path, _outer_with_nested("pass"))
    assert p006_actually_fires(str(path), flagged) is True


@pytest.mark.parametrize("flagged", [2, 3, 4, 6, 7])
def test_cite_above_a_nested_reacting_try_is_dropped(tmp_path, flagged):
    path = _write(tmp_path, _outer_with_nested("logger.warning('x')"))
    assert p006_actually_fires(str(path), flagged) is False


def test_nested_try_above_the_cited_line_is_not_added(tmp_path):
    # Only tries starting below the cited line are brought in.
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            x()\n"
        "        except Exception:\n"
        "            pass\n"
        "        y = 1\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 7) is False
    assert p006_actually_fires(str(path), 2) is True


def test_cite_in_an_earlier_nested_handler_adds_no_later_tries(tmp_path):
    # #2442 review, P3: a line in a nested try's handler is not above a later
    # swallow. Master dropped this (both handlers on the path react).
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            a()\n"
        "        except OSError:\n"
        "            logger.warning('x')\n"
        "        try:\n"
        "            b()\n"
        "        except Exception:\n"
        "            pass\n"
        "    except Exception:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 5) is False
    assert p006_actually_fires(str(path), 6) is False
    # A cite above both nested tries still reaches the silent one.
    assert p006_actually_fires(str(path), 2) is True


@pytest.mark.parametrize(
    "later",
    [
        # A try inside a nested def does not run as part of the block.
        "        def later():\n"
        "            try:\n"
        "                x()\n"
        "            except Exception:\n"
        "                pass\n",
        # The author acknowledged this clause.
        "        try:\n"
        "            x()\n"
        "        except Exception:  # noqa: BLE001\n"
        "            pass\n",
    ],
)
def test_nested_step_skips_scopes_and_acknowledged_clauses(tmp_path, later):
    source = (
        "def f():\n"
        "    try:\n"
        "        y = 1\n"
        + later
        + "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 2) is False
    assert p006_actually_fires(str(path), 3) is False


_LATER_SILENT_TRY = "        try:\n            b()\n        except Exception:\n            pass\n"


@pytest.mark.parametrize(
    "region, cites",
    [
        # A cite in a nested def runs in another function (round 5, P2).
        (
            "        def cb():\n"
            "            try:\n"
            "                a()\n"
            "            except Exception:\n"
            "                logger.warning('x')\n",
            (6, 7),
        ),
        # A nested else header with a comment before its first statement
        # (round 5, P3).
        (
            "        try:\n"
            "            a()\n"
            "        except OSError:\n"
            "            raise\n"
            "        else:\n"
            "            # note\n"
            "            logger.info('ok')\n",
            (7, 8, 9),
        ),
    ],
)
def test_cite_in_a_region_of_its_own_adds_no_later_tries(tmp_path, region, cites):
    source = (
        "def f():\n"
        "    try:\n"
        + region
        + _LATER_SILENT_TRY
        + "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    for cite in cites:
        assert p006_actually_fires(str(path), cite) is False, cite
    # A cite above everything still reaches the later silent try.
    assert p006_actually_fires(str(path), 2) is True


def test_a_lambda_on_the_cited_line_is_not_a_barrier(tmp_path):
    # Round 6: the rest of the line runs in the block, above the later try.
    source = (
        "def f():\n"
        "    try:\n"
        "        items.sort(key=lambda v: v)\n"
        + _LATER_SILENT_TRY
        + "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 3) is True


def test_a_class_body_runs_in_the_block(tmp_path):
    # #2442 round 8, P3 1: a class body runs at definition time, so a try in
    # it is below the cite and a cite in it is not in a region of its own.
    source = (
        "def f():\n"
        "    try:\n"
        "        y = 1\n"
        "        class K:\n"
        "            z = 2\n"
        "            try:\n"
        "                b()\n"
        "            except Exception:\n"
        "                pass\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    for cite in (2, 3, 5):
        assert p006_actually_fires(str(path), cite) is True, cite


def test_a_class_body_inside_a_handler_runs_in_the_handler(tmp_path):
    # #2447 review, P3: a try in a class body in a handler runs as part of
    # that handler, so like any block there it takes no nested handlers.
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        class K:\n"
        "            try:\n"
        "                y = 1\n"
        "                try:\n"
        "                    b()\n"
        "                except OSError:\n"
        "                    pass\n"
        "            except ValueError:\n"
        "                raise\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 7) is False


def test_a_later_try_in_the_same_else_is_below_the_cite(tmp_path):
    # #2442 round 8, P3 2: a nested else is a region of its own, but a try
    # further down that same else still runs after the cited line.
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            a()\n"
        "        except OSError:\n"
        "            raise\n"
        "        else:\n"
        "            y = 1\n"
        "            try:\n"
        "                b()\n"
        "            except Exception:\n"
        "                pass\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 8) is True
    # A try later in the block but outside the else is still not taken.
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            a()\n"
        "        except OSError:\n"
        "            raise\n"
        "        else:\n"
        "            y = 1\n"
        + _LATER_SILENT_TRY
        + "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 8) is False


def test_a_cite_in_an_inner_try_block_reaches_later_tries_outside_it(tmp_path):
    # #2442 round 8, P3 3: the cite's innermost try block is the inner
    # try/finally, but the silent try further down the enclosing block
    # still runs after it.
    source = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            y = 1\n"
        "        finally:\n"
        "            pass\n"
        + _LATER_SILENT_TRY
        + "    except ValueError:\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 4) is True
    assert p006_actually_fires(str(path), 2) is True
    # The inner finally is a region of its own.
    assert p006_actually_fires(str(path), 6) is False


def test_a_def_inside_a_handler_is_its_own_scope(tmp_path):
    # Round 7: a try in a function defined inside a handler does not run as
    # part of that handler, so the nested step applies there as anywhere.
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        def cb():\n"
        "            try:\n"
        "                y = 1\n"
        "                try:\n"
        "                    b()\n"
        "                except Exception:\n"
        "                    pass\n"
        "            except ValueError:\n"
        "                raise\n"
        "        raise\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 6) is True
    assert p006_actually_fires(str(path), 7) is True


def test_try_finally_inside_a_handler_takes_no_nested_handlers(tmp_path):
    # Round 5, P3: citing the handler's log line and citing its clause must
    # agree; nested handlers are never on a handler's path.
    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        try:\n"
        "            logger.warning('x')\n"
        "            try:\n"
        "                cleanup()\n"
        "            except OSError:\n"
        "                pass\n"
        "        finally:\n"
        "            pass\n"
    )
    path = _write(tmp_path, source)
    assert p006_actually_fires(str(path), 4) is False
    assert p006_actually_fires(str(path), 6) is False


@pytest.mark.parametrize(
    "above",
    [
        "",
        "        try:\n            w()\n        except Exception:\n            pass\n",
    ],
)
def test_nested_reacting_try_never_drops_a_line_with_no_handler(tmp_path, above):
    # PR review: in a try/finally the cited line has no handler of its own,
    # which keeps the finding; the nested step must not turn that path into
    # one whose handlers all react and so drop it.
    source = (
        "def f():\n"
        "    try:\n"
        + above
        + "        y = 1\n"
        "        try:\n"
        "            z()\n"
        "        except Exception:\n"
        "            logger.warning('x')\n"
        "    finally:\n"
        "        cleanup()\n"
    )
    path = _write(tmp_path, source)
    cited = 3 + above.count("\n")
    assert p006_actually_fires(str(path), cited) is True


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
    # Ends in "logger" without being one (#2424 final review, P3 3).
    "blogger",
    "self.blogger",
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
    "APP_LOGGER",
    "self._logger",
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
    """scan_file runs parse_findings and _verify_finding_against_source for real,
    with the indented lines the P006 checks read. The line-based re-raise check
    would see the nested `raise` and drop these; for a .py file that parses it
    must not run."""
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


def test_line_based_reraise_check_still_runs_for_unparseable_files(tmp_path, monkeypatch):
    """For a .py file the AST filter cannot parse, the line-based re-raise
    check is the only one, and it must work on scan_file's own snippet lines
    (review round 3, finding 2: it once read the indentation-stripped map and
    never fired)."""
    from agents.watcher.agent import scan_file

    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        raise\n"
        "def broken(:\n"
    )
    path = _write(tmp_path, source)
    for flagged in (4, 5):
        _stub_model(monkeypatch, tmp_path, _model_reply(flagged))
        assert scan_file(str(path), persist=False) == []


def test_unparseable_silent_handler_is_kept(tmp_path, monkeypatch):
    from agents.watcher.agent import scan_file

    source = (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n"
        "    raise ValueError('after the handler')\n"
        "def broken(:\n"
    )
    path = _write(tmp_path, source)
    _stub_model(monkeypatch, tmp_path, _model_reply(4))
    found = scan_file(str(path), persist=False)
    assert [(f.pattern, f.line) for f in found] == [("P006", 4)]


def test_scan_file_keeps_p006_without_a_cited_line(tmp_path, monkeypatch):
    """No cited line: neither re-raise check judges the region_start fallback,
    so the finding is kept even where region_start is a re-raising clause."""
    from agents.watcher.agent import scan_file

    source = (
        "try:\n"
        "    work()\n"
        "except Exception:\n"
        "    raise\n"
    )
    path = _write(tmp_path, source)
    reply = json.dumps({"findings": [{"pattern": "P006", "hint": "silent swallow"}]})
    _stub_model(monkeypatch, tmp_path, reply)
    found = scan_file(str(path), region="3-4", persist=False)
    assert [(f.pattern, f.line) for f in found] == [("P006", 3)]


def test_noqa_on_an_earlier_try_does_not_cover_a_later_one():
    """A body line of one try must not reach back to the noqa-acknowledged
    except of an earlier try (independent review, P2). The walk-back stops at
    the later try's own header."""
    from agents.watcher.agent import _p006_governing_except

    lines = {
        1: "def f():",
        2: "    try:",
        3: "        a()",
        4: "    except Exception:  # noqa: BLE001",
        5: "        logger.warning('x')",
        6: "    try:",
        7: "        b()",
        8: "    except Exception:",
        9: "        pass",
    }
    assert _p006_governing_except(7, lines) is None
    assert _p006_governing_except(9, lines) == 8
    assert _p006_governing_except(5, lines) == 4
    # A body line nested deeper inside a handler still reaches its clause.
    nested = {
        1: "    except Exception:  # noqa: BLE001",
        2: "        if retry:",
        3: "            again()",
    }
    assert _p006_governing_except(3, nested) == 1


@pytest.mark.parametrize("owner", ["if retry:", "for item in items:", "while pending:"])
def test_non_try_else_inside_a_handler_reaches_its_clause(owner):
    """#2424 final review, P3 4: the `else:` of an if/for/while inside a
    handler once stopped the walk-back, so `# noqa: BLE001` on the handler's
    clause was missed and the finding kept."""
    from agents.watcher.agent import _p006_governing_except

    lines = {
        1: "    except Exception:  # noqa: BLE001",
        2: f"        {owner}",
        3: "            again()",
        4: "        else:",
        5: "            give_up()",
    }
    assert _p006_governing_except(5, lines) == 1


def test_else_after_a_black_split_if_reaches_its_clause():
    """Round 5, P3: black splits a long condition, so the nearest same-indent
    line before the `else:` is the closing `):`, not the `if (`."""
    from agents.watcher.agent import _p006_governing_except

    lines = {
        1: "    except Exception:  # noqa: BLE001",
        2: "        if (",
        3: "            retry and budget",
        4: "        ):",
        5: "            again()",
        6: "        else:",
        7: "            give_up()",
    }
    assert _p006_governing_except(7, lines) == 1


def test_try_else_still_stops_the_walk():
    from agents.watcher.agent import _p006_governing_except

    lines = {
        1: "    except Exception:  # noqa: BLE001",
        2: "        try:",
        3: "            again()",
        4: "        except OSError:",
        5: "            logger.warning('x')",
        6: "        else:",
        7: "            give_up()",
    }
    assert _p006_governing_except(7, lines) is None
    # An else whose owner is not visible is taken to be a try's, so the walk
    # stops there instead of reaching the acknowledged clause above it.
    hidden_owner = {
        1: "    except Exception:  # noqa: BLE001",
        2: "        else:",
        3: "            work()",
    }
    assert _p006_governing_except(3, hidden_owner) is None
